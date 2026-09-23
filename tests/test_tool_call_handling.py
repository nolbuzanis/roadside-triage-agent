"""Tests for tool-call handling: handle_create_breakdown_ticket and handle_tool_call."""

from __future__ import annotations

import json
from typing import ClassVar
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.api.twilio import _background_tasks, handle_create_breakdown_ticket
from app.realtime.tools import (
    CREATE_BREAKDOWN_TICKET_TOOL,
    TicketArgs,
    TicketToolResult,
)

# ---------------------------------------------------------------------------
# CREATE_BREAKDOWN_TICKET_TOOL schema tests
# ---------------------------------------------------------------------------


class TestCreateBreakdownTicketToolSchema:
    """Tests for the model-facing tool schema (progressive persistence)."""

    def test_all_intake_fields_are_optional(self) -> None:
        params = CREATE_BREAKDOWN_TICKET_TOOL["parameters"]
        assert params["required"] == []

    def test_all_intake_fields_are_exposed(self) -> None:
        props = CREATE_BREAKDOWN_TICKET_TOOL["parameters"]["properties"]
        assert set(props) == {"location", "vehicle", "issue"}

    def test_description_encourages_progressive_saving(self) -> None:
        description = CREATE_BREAKDOWN_TICKET_TOOL["description"].lower()
        assert "as soon as" in description
        assert "call again" in description

    def test_description_requires_all_three_before_close(self) -> None:
        description = CREATE_BREAKDOWN_TICKET_TOOL["description"].lower()
        assert "all three" in description
        assert "'created'" in CREATE_BREAKDOWN_TICKET_TOOL["description"]

    def test_tool_name_unchanged(self) -> None:
        assert CREATE_BREAKDOWN_TICKET_TOOL["name"] == "create_breakdown_ticket"

# ---------------------------------------------------------------------------
# TicketArgs model tests
# ---------------------------------------------------------------------------


class TestTicketArgs:
    """Tests for the TicketArgs Pydantic model."""

    def test_valid_args(self) -> None:
        args = TicketArgs.model_validate_json(
            '{"location": "Main St", "vehicle": "Toyota Camry", "issue": "Flat tire"}'
        )
        assert args.location == "Main St"
        assert args.vehicle == "Toyota Camry"
        assert args.issue == "Flat tire"

    def test_partial_args_are_valid(self) -> None:
        args = TicketArgs.model_validate_json('{"location": "Main St"}')
        assert args.location == "Main St"
        assert args.vehicle is None
        assert args.issue is None

    def test_all_fields_missing_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            TicketArgs.model_validate_json("{}")

    def test_all_empty_fields_raise_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            TicketArgs.model_validate_json('{"location": "", "vehicle": "", "issue": ""}')

    def test_empty_and_nonempty_mix_is_valid(self) -> None:
        args = TicketArgs.model_validate_json(
            '{"location": "", "vehicle": "Toyota Camry", "issue": ""}'
        )
        assert args.location == ""
        assert args.vehicle == "Toyota Camry"
        assert args.issue == ""

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValidationError):
            TicketArgs.model_validate_json("not json")

    def test_extra_fields_are_ignored(self) -> None:
        args = TicketArgs.model_validate_json(
            '{"location": "A", "vehicle": "B", "issue": "C", "extra": "ignored"}'
        )
        assert args.location == "A"


# ---------------------------------------------------------------------------
# TicketToolResult model tests
# ---------------------------------------------------------------------------


class TestTicketToolResult:
    """Tests for the TicketToolResult Pydantic model."""

    def test_success_result(self) -> None:
        result = TicketToolResult(
            status="created",
            ticket_id="abc-123",
            message="Ticket created successfully.",
        )
        data = result.model_dump()
        assert data["status"] == "created"
        assert data["ticket_id"] == "abc-123"
        assert data["message"] == "Ticket created successfully."
        assert data["error"] is None

    def test_error_result(self) -> None:
        result = TicketToolResult(status="error", error="Unable to create the ticket")
        data = result.model_dump()
        assert data["status"] == "error"
        assert data["ticket_id"] is None
        assert data["message"] is None
        assert data["error"] == "Unable to create the ticket"

    def test_model_dump_json_is_valid_json(self) -> None:
        result = TicketToolResult(status="created", ticket_id="x")
        parsed = json.loads(result.model_dump_json())
        assert parsed["status"] == "created"
        assert parsed["ticket_id"] == "x"


# ---------------------------------------------------------------------------
# handle_create_breakdown_ticket tests
# ---------------------------------------------------------------------------


class TestHandleCreateBreakdownTicket:
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
    async def test_valid_args_creates_ticket(self) -> None:
        with patch(
            "app.api.twilio.create_ticket", return_value=dict(self._COMPLETE_TICKET)
        ) as mock_create:
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_create_breakdown_ticket(
                    call_sid="CA_test",
                    caller_phone="+15551234567",
                    arguments='{"location": "Main St", "vehicle": "Honda Civic", "issue": "Won\'t start"}',
                )

        assert result.status == "created"
        assert result.ticket_id == "ticket-uuid-123"
        assert result.message is not None
        assert "close the call" in result.message
        assert result.error is None
        mock_create.assert_called_once_with(
            call_id="CA_test",
            caller_phone="+15551234567",
            location="Main St",
            vehicle="Honda Civic",
            issue="Won't start",
        )
        # The SMS is fired as a fire-and-forget task; let it run.
        import asyncio as _asyncio

        await _asyncio.sleep(0)
        pending = [t for t in _background_tasks if not t.done()]
        if pending:
            await _asyncio.gather(*pending, return_exceptions=True)
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_json_arguments_returns_error(self) -> None:
        result = await handle_create_breakdown_ticket(
            call_sid="CA_test",
            caller_phone="+15551234567",
            arguments="not valid json",
        )

        assert result.status == "error"
        assert result.error == "Invalid arguments"
        assert result.ticket_id is None

    @pytest.mark.asyncio
    async def test_no_fields_returns_error(self) -> None:
        result = await handle_create_breakdown_ticket(
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
                result = await handle_create_breakdown_ticket(
                    call_sid="CA_partial",
                    caller_phone="+15551234567",
                    arguments='{"location": "Main St"}',
                )

        assert result.status == "updated"
        assert result.ticket_id == "ticket-uuid-2"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_save_does_not_log_ticket_created(
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
                    result = await handle_create_breakdown_ticket(
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
        assert "ticket_created" not in events
        assert "ticket_updated" in events

    @pytest.mark.asyncio
    async def test_retried_completion_does_not_resend_sms(self) -> None:
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
                result = await handle_create_breakdown_ticket(
                    call_sid="CA_retry",
                    caller_phone="+15551234567",
                    arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                )

        assert result.status == "created"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ticket_failure_returns_safe_error(self) -> None:
        with patch("app.api.twilio.create_ticket", side_effect=RuntimeError("DB connection failed")):
            result = await handle_create_breakdown_ticket(
                call_sid="CA_test",
                caller_phone="+15551234567",
                arguments='{"location": "Main St", "vehicle": "Honda Civic", "issue": "Flat tire"}',
            )

        assert result.status == "error"
        assert result.error == "Unable to create the ticket"
        assert result.ticket_id is None

    @pytest.mark.asyncio
    async def test_create_ticket_failure_does_not_expose_exception_details(self) -> None:
        with patch("app.api.twilio.create_ticket", side_effect=RuntimeError("sensitive internal detail")):
            result = await handle_create_breakdown_ticket(
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
                result = await handle_create_breakdown_ticket(
                    call_sid="",
                    caller_phone="",
                    arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                )

        assert result.status == "created"
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
                    await handle_create_breakdown_ticket(
                        call_sid="CA_test",
                        caller_phone="+15551234567",
                        arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                    )

        mock_to_thread.assert_called()
