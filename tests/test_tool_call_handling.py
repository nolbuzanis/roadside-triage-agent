"""Tests for tool-call handling: update (persistence) and confirm (completion) handlers."""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.twilio import (
    _background_tasks,
    handle_confirm_assistance_request,
    handle_update_assistance_request,
)
from app.realtime.tools import (
    CONFIRM_ASSISTANCE_REQUEST_TOOL,
    REALTIME_TOOLS,
    UPDATE_ASSISTANCE_REQUEST_TOOL,
    AssistanceRequestArgs,
    AssistanceRequestToolResult,
)


@pytest.fixture(autouse=True)
def _mock_complete_intake() -> Generator[MagicMock]:
    """Keep completion-path tests off the real database."""
    with patch("app.api.twilio.complete_intake") as mock_complete:
        yield mock_complete

# ---------------------------------------------------------------------------
# UPDATE_ASSISTANCE_REQUEST_TOOL schema tests
# ---------------------------------------------------------------------------


class TestUpdateAssistanceRequestToolSchema:
    """Tests for the model-facing tool schema (progressive persistence)."""

    def test_all_intake_fields_are_optional(self) -> None:
        params = UPDATE_ASSISTANCE_REQUEST_TOOL["parameters"]
        assert params["required"] == []

    def test_all_intake_fields_are_exposed(self) -> None:
        props = UPDATE_ASSISTANCE_REQUEST_TOOL["parameters"]["properties"]
        assert set(props) == {"location", "vehicle", "issue"}

    def test_description_encourages_progressive_saving(self) -> None:
        description = UPDATE_ASSISTANCE_REQUEST_TOOL["description"].lower()
        assert "as soon as" in description
        assert "call again" in description

    def test_description_requires_confirmation_before_close(self) -> None:
        description = UPDATE_ASSISTANCE_REQUEST_TOOL["description"].lower()
        assert "all three fields" in description
        assert "ready_for_confirmation" in UPDATE_ASSISTANCE_REQUEST_TOOL["description"]
        assert "confirm_assistance_request" in UPDATE_ASSISTANCE_REQUEST_TOOL["description"]

    def test_description_does_not_claim_saving_completes_the_call(self) -> None:
        description = UPDATE_ASSISTANCE_REQUEST_TOOL["description"]
        assert "'created'" not in description
        assert "the call can close" not in description

    def test_tool_name_unchanged(self) -> None:
        assert UPDATE_ASSISTANCE_REQUEST_TOOL["name"] == "update_assistance_request"


# ---------------------------------------------------------------------------
# CONFIRM_ASSISTANCE_REQUEST_TOOL schema tests
# ---------------------------------------------------------------------------


class TestConfirmAssistanceRequestToolSchema:
    """Tests for the model-facing confirmation tool (explicit completion)."""

    def test_tool_name(self) -> None:
        assert CONFIRM_ASSISTANCE_REQUEST_TOOL["name"] == "confirm_assistance_request"

    def test_tool_requires_no_arguments(self) -> None:
        params = CONFIRM_ASSISTANCE_REQUEST_TOOL["parameters"]
        assert params["required"] == []
        assert params["properties"] == {}

    def test_description_requires_prior_caller_confirmation(self) -> None:
        description = CONFIRM_ASSISTANCE_REQUEST_TOOL["description"].lower()
        assert "verbally confirmed" in description
        assert "never before" in description

    def test_description_rejects_declined_or_unanswered_summary(self) -> None:
        description = CONFIRM_ASSISTANCE_REQUEST_TOOL["description"].lower()
        assert "declined, ambiguous, hedged, or unanswered summary" in description
        assert "do not call this tool" in description
        assert "ask again instead" in description

    def test_registered_alongside_update_and_emergency_tools(self) -> None:
        names = [tool["name"] for tool in REALTIME_TOOLS]
        assert names == [
            "update_assistance_request",
            "confirm_assistance_request",
            "transfer_to_emergency",
        ]

# ---------------------------------------------------------------------------
# AssistanceRequestArgs model tests
# ---------------------------------------------------------------------------


class TestAssistanceRequestArgs:
    """Tests for the AssistanceRequestArgs Pydantic model."""

    def test_valid_args(self) -> None:
        args = AssistanceRequestArgs.model_validate_json(
            '{"location": "Main St", "vehicle": "Toyota Camry", "issue": "Flat tire"}'
        )
        assert args.location == "Main St"
        assert args.vehicle == "Toyota Camry"
        assert args.issue == "Flat tire"

    def test_partial_args_are_valid(self) -> None:
        args = AssistanceRequestArgs.model_validate_json('{"location": "Main St"}')
        assert args.location == "Main St"
        assert args.vehicle is None
        assert args.issue is None

    def test_all_fields_missing_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            AssistanceRequestArgs.model_validate_json("{}")

    def test_all_empty_fields_raise_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            AssistanceRequestArgs.model_validate_json('{"location": "", "vehicle": "", "issue": ""}')

    def test_empty_and_nonempty_mix_is_valid(self) -> None:
        args = AssistanceRequestArgs.model_validate_json(
            '{"location": "", "vehicle": "Toyota Camry", "issue": ""}'
        )
        assert args.location == ""
        assert args.vehicle == "Toyota Camry"
        assert args.issue == ""

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValidationError):
            AssistanceRequestArgs.model_validate_json("not json")

    def test_extra_fields_are_ignored(self) -> None:
        args = AssistanceRequestArgs.model_validate_json(
            '{"location": "A", "vehicle": "B", "issue": "C", "extra": "ignored"}'
        )
        assert args.location == "A"


# ---------------------------------------------------------------------------
# AssistanceRequestToolResult model tests
# ---------------------------------------------------------------------------


class TestAssistanceRequestToolResult:
    """Tests for the AssistanceRequestToolResult Pydantic model."""

    def test_success_result(self) -> None:
        result = AssistanceRequestToolResult(
            status="created",
            assistance_request_id="abc-123",
            message="Assistance request saved successfully.",
        )
        data = result.model_dump()
        assert data["status"] == "created"
        assert data["assistance_request_id"] == "abc-123"
        assert data["message"] == "Assistance request saved successfully."
        assert data["error"] is None

    def test_error_result(self) -> None:
        result = AssistanceRequestToolResult(status="error", error="Unable to save the assistance request")
        data = result.model_dump()
        assert data["status"] == "error"
        assert data["assistance_request_id"] is None
        assert data["message"] is None
        assert data["error"] == "Unable to save the assistance request"

    def test_model_dump_json_is_valid_json(self) -> None:
        result = AssistanceRequestToolResult(status="created", assistance_request_id="x")
        parsed = json.loads(result.model_dump_json())
        assert parsed["status"] == "created"
        assert parsed["assistance_request_id"] == "x"


# ---------------------------------------------------------------------------
# handle_update_assistance_request tests
# ---------------------------------------------------------------------------


class TestHandleUpdateAssistanceRequest:
    """Tests for the async tool-specific handler."""

    _COMPLETE_TICKET: ClassVar[dict] = {
        "id": "ticket-uuid-123",
        "call_id": "CA_test",
        "location": "Main St",
        "vehicle": "Honda Civic",
        "issue": "Won't start",
        "notification_status": "pending",
    }

    @pytest.mark.asyncio
    async def test_valid_args_saves_request_ready_for_confirmation(self) -> None:
        with patch(
            "app.api.twilio.create_ticket", return_value=dict(self._COMPLETE_TICKET)
        ) as mock_create:
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_update_assistance_request(
                    call_sid="CA_test",
                    caller_phone="+15551234567",
                    arguments='{"location": "Main St", "vehicle": "Honda Civic", "issue": "Won\'t start"}',
                )

        assert result.status == "ready_for_confirmation"
        assert result.assistance_request_id == "ticket-uuid-123"
        assert result.message is not None
        assert "confirmation" in result.message.lower()
        assert result.error is None
        mock_create.assert_called_once_with(
            call_id="CA_test",
            caller_phone="+15551234567",
            location="Main St",
            vehicle="Honda Civic",
            issue="Won't start",
        )
        # Saving all three fields never notifies the dispatcher.
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_arguments_returns_error(self) -> None:
        result = await handle_update_assistance_request(
            call_sid="CA_test",
            caller_phone="+15551234567",
            arguments="not valid json",
        )

        assert result.status == "error"
        assert result.error == "Invalid arguments"
        assert result.assistance_request_id is None

    @pytest.mark.asyncio
    async def test_no_fields_returns_error(self) -> None:
        result = await handle_update_assistance_request(
            call_sid="CA_test",
            caller_phone="+15551234567",
            arguments="{}",
        )

        assert result.status == "error"
        assert result.error == "Invalid arguments"

    @pytest.mark.asyncio
    async def test_partial_save_returns_updated_without_sms(self) -> None:
        partial_ticket = {
            "id": "ticket-uuid-2",
            "call_id": "CA_partial",
            "location": "Main St",
            "vehicle": None,
            "issue": None,
            "notification_status": "pending",
        }
        with patch("app.api.twilio.create_ticket", return_value=partial_ticket):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_update_assistance_request(
                    call_sid="CA_partial",
                    caller_phone="+15551234567",
                    arguments='{"location": "Main St"}',
                )

        assert result.status == "updated"
        assert result.assistance_request_id == "ticket-uuid-2"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_save_does_not_log_completion_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        partial_ticket = {
            "id": "ticket-uuid-3",
            "call_id": "CA_partial2",
            "location": None,
            "vehicle": "Honda Civic",
            "issue": None,
            "notification_status": "pending",
        }
        with patch("app.api.twilio.create_ticket", return_value=partial_ticket):
            with patch("app.api.twilio.notify_dispatcher"):
                with caplog.at_level("INFO"):
                    result = await handle_update_assistance_request(
                        call_sid="CA_partial2",
                        caller_phone="+15551234567",
                        arguments='{"vehicle": "Honda Civic"}',
                    )

        assert result.status == "updated"
        events = [
            json.loads(r.message)["event"]
            for r in caplog.records
            if r.message.startswith("{")
        ]
        assert "assistance_request_confirmed" not in events
        assert "assistance_request_ready_for_confirmation" not in events
        assert "assistance_request_updated" in events

    @pytest.mark.asyncio
    async def test_full_save_logs_ready_for_confirmation_not_completion(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch(
            "app.api.twilio.create_ticket", return_value=dict(self._COMPLETE_TICKET)
        ):
            with patch("app.api.twilio.notify_dispatcher"):
                with caplog.at_level("INFO"):
                    await handle_update_assistance_request(
                        call_sid="CA_test",
                        caller_phone="+15551234567",
                        arguments='{"location": "Main St", "vehicle": "Honda Civic", "issue": "Won\'t start"}',
                    )

        events = [
            json.loads(r.message)["event"]
            for r in caplog.records
            if r.message.startswith("{")
        ]
        assert "assistance_request_ready_for_confirmation" in events
        assert "assistance_request_confirmed" not in events

    @pytest.mark.asyncio
    async def test_full_save_never_notifies_dispatcher(self) -> None:
        """Even a fully populated row stays pending until the caller confirms."""
        notified_ticket = {
            "id": "ticket-uuid-4",
            "call_id": "CA_retry",
            "location": "A",
            "vehicle": "B",
            "issue": "C",
            "notification_status": "sent",
        }
        with patch("app.api.twilio.create_ticket", return_value=notified_ticket):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_update_assistance_request(
                    call_sid="CA_retry",
                    caller_phone="+15551234567",
                    arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                )

        assert result.status == "ready_for_confirmation"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ticket_failure_returns_safe_error(self) -> None:
        with patch("app.api.twilio.create_ticket", side_effect=RuntimeError("DB connection failed")):
            result = await handle_update_assistance_request(
                call_sid="CA_test",
                caller_phone="+15551234567",
                arguments='{"location": "Main St", "vehicle": "Honda Civic", "issue": "Flat tire"}',
            )

        assert result.status == "error"
        assert result.error == "Unable to save the assistance request"
        assert result.assistance_request_id is None

    @pytest.mark.asyncio
    async def test_create_ticket_failure_does_not_expose_exception_details(self) -> None:
        with patch("app.api.twilio.create_ticket", side_effect=RuntimeError("sensitive internal detail")):
            result = await handle_update_assistance_request(
                call_sid="CA_test",
                caller_phone="+15551234567",
                arguments='{"location": "X", "vehicle": "Y", "issue": "Z"}',
            )

        serialized = result.model_dump_json()
        assert "sensitive internal detail" not in serialized

    @pytest.mark.asyncio
    async def test_call_sid_falls_back_to_unknown(self) -> None:
        mock_ticket = {
            "id": "t1",
            "call_id": "unknown",
            "location": "A",
            "vehicle": "B",
            "issue": "C",
            "notification_status": "pending",
        }
        with patch("app.api.twilio.create_ticket", return_value=mock_ticket) as mock_create:
            with patch("app.api.twilio.notify_dispatcher"):
                result = await handle_update_assistance_request(
                    call_sid="",
                    caller_phone="",
                    arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                )

        assert result.status == "ready_for_confirmation"
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["call_id"] == "unknown"
        assert call_kwargs["caller_phone"] == "unknown"

    @pytest.mark.asyncio
    async def test_handler_uses_to_thread_for_blocking_io(self) -> None:
        """Verify that create_ticket is called via asyncio.to_thread (non-blocking)."""
        mock_ticket = {
            "id": "t1",
            "location": "A",
            "vehicle": "B",
            "issue": "C",
            "notification_status": "pending",
        }
        with patch("app.api.twilio.asyncio.to_thread", wraps=__import__("asyncio").to_thread) as mock_to_thread:
            with patch("app.api.twilio.create_ticket", return_value=mock_ticket):
                with patch("app.api.twilio.notify_dispatcher"):
                    await handle_update_assistance_request(
                        call_sid="CA_test",
                        caller_phone="+15551234567",
                        arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                    )

        mock_to_thread.assert_called()

    @pytest.mark.asyncio
    async def test_full_save_does_not_finalize_intake_status(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        """Saving all three fields must not flip the row out of the open status."""
        with patch(
            "app.api.twilio.create_ticket", return_value=dict(self._COMPLETE_TICKET)
        ):
            with patch("app.api.twilio.notify_dispatcher"):
                result = await handle_update_assistance_request(
                    call_sid="CA_test",
                    caller_phone="+15551234567",
                    arguments='{"location": "Main St", "vehicle": "Honda Civic", "issue": "Won\'t start"}',
                )

        assert result.status == "ready_for_confirmation"
        _mock_complete_intake.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_save_does_not_finalize_intake_status(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        """A partial save must not flip the row out of the open status."""
        partial_ticket = {
            "id": "ticket-uuid-9",
            "call_id": "CA_partial",
            "location": "Main St",
            "vehicle": None,
            "issue": None,
            "notification_status": "pending",
        }
        with patch("app.api.twilio.create_ticket", return_value=partial_ticket):
            with patch("app.api.twilio.notify_dispatcher"):
                result = await handle_update_assistance_request(
                    call_sid="CA_partial",
                    caller_phone="+15551234567",
                    arguments='{"location": "Main St"}',
                )

        assert result.status == "updated"
        _mock_complete_intake.assert_not_called()


# ---------------------------------------------------------------------------
# handle_confirm_assistance_request tests
# ---------------------------------------------------------------------------


class TestHandleConfirmAssistanceRequest:
    """Tests for the explicit completion tool (caller-confirmed intake)."""

    _COMPLETE_ROW: ClassVar[dict] = {
        "id": "ticket-uuid-c1",
        "call_id": "CA_confirm",
        "location": "Main St",
        "vehicle": "Honda Civic",
        "issue": "Won't start",
        "intake_status": "in_progress",
        "notification_status": "pending",
    }

    @staticmethod
    async def _drain_background_tasks() -> None:
        import asyncio as _asyncio

        await _asyncio.sleep(0)
        pending = [t for t in _background_tasks if not t.done()]
        if pending:
            await _asyncio.gather(*pending, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_confirmation_completes_intake_and_notifies_once(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        with patch(
            "app.api.twilio.get_assistance_request", return_value=dict(self._COMPLETE_ROW)
        ):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid="CA_confirm",
                    caller_phone="+15551234567",
                )
                await self._drain_background_tasks()

        assert result.status == "confirmed"
        assert result.assistance_request_id == "ticket-uuid-c1"
        assert result.error is None
        _mock_complete_intake.assert_called_once_with(call_id="CA_confirm")
        mock_notify.assert_called_once_with(
            call_id="CA_confirm",
            caller_phone="+15551234567",
            location="Main St",
            vehicle="Honda Civic",
            issue="Won't start",
        )

    @pytest.mark.asyncio
    async def test_confirmation_requires_all_three_fields(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        partial_row = {**self._COMPLETE_ROW, "issue": None}
        with patch("app.api.twilio.get_assistance_request", return_value=partial_row):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid="CA_confirm",
                    caller_phone="+15551234567",
                )

        assert result.status == "incomplete"
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmation_without_a_row_is_incomplete(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        with patch("app.api.twilio.get_assistance_request", return_value=None):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid="CA_missing",
                    caller_phone="+15551234567",
                )

        assert result.status == "incomplete"
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_escalated_request_is_never_completed(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        escalated_row = {
            **self._COMPLETE_ROW,
            "intake_status": "escalated",
            "status": "escalated",
        }
        with patch("app.api.twilio.get_assistance_request", return_value=escalated_row):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid="CA_confirm",
                    caller_phone="+15551234567",
                )

        assert result.status == "escalated"
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_confirmation_does_not_resend_sms(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        already_notified = {**self._COMPLETE_ROW, "notification_status": "sent"}
        with patch(
            "app.api.twilio.get_assistance_request", return_value=already_notified
        ):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid="CA_confirm",
                    caller_phone="+15551234567",
                )
                await self._drain_background_tasks()

        # Idempotent success: the guarded finalizer re-runs, the SMS does not.
        assert result.status == "confirmed"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmation_fetch_failure_returns_safe_error(self) -> None:
        with patch(
            "app.api.twilio.get_assistance_request",
            side_effect=RuntimeError("sensitive internal detail"),
        ):
            result = await handle_confirm_assistance_request(
                call_sid="CA_confirm",
                caller_phone="+15551234567",
            )

        assert result.status == "error"
        assert "sensitive internal detail" not in result.model_dump_json()

    @pytest.mark.asyncio
    async def test_missing_call_sid_falls_back_to_unknown(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        mock_row = {**self._COMPLETE_ROW, "call_id": "unknown"}
        with patch("app.api.twilio.get_assistance_request", return_value=mock_row) as mock_get:
            with patch("app.api.twilio.notify_dispatcher"):
                result = await handle_confirm_assistance_request(
                    call_sid="",
                    caller_phone="",
                )
                await self._drain_background_tasks()

        assert result.status == "confirmed"
        mock_get.assert_called_once_with(call_id="unknown")
        _mock_complete_intake.assert_called_once_with(call_id="unknown")
