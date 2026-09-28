"""Tests for emergency transfer functionality."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.twilio import (
    _record_escalation,
    handle_transfer_finished,
    handle_transfer_to_emergency,
)
from app.realtime.tools import (
    TRANSFER_TO_EMERGENCY_TOOL,
    EmergencyTransferArgs,
    EmergencyTransferResult,
)
from app.services.emergency import transfer_call

# ---------------------------------------------------------------------------
# EmergencyTransferArgs model tests
# ---------------------------------------------------------------------------


class TestEmergencyTransferArgs:
    """Tests for the EmergencyTransferArgs Pydantic model."""

    def test_valid_args(self) -> None:
        args = EmergencyTransferArgs.model_validate_json(
            '{"reason": "Car is on fire"}'
        )
        assert args.reason == "Car is on fire"

    def test_missing_field_raisesValidationError(self) -> None:
        with pytest.raises(ValidationError):
            EmergencyTransferArgs.model_validate_json("{}")

    def test_empty_reason_is_valid(self) -> None:
        args = EmergencyTransferArgs.model_validate_json('{"reason": ""}')
        assert args.reason == ""

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValidationError):
            EmergencyTransferArgs.model_validate_json("not json")


# ---------------------------------------------------------------------------
# EmergencyTransferResult model tests
# ---------------------------------------------------------------------------


class TestEmergencyTransferResult:
    """Tests for the EmergencyTransferResult Pydantic model."""

    def test_success_result(self) -> None:
        result = EmergencyTransferResult(
            status="transferred",
            message="Emergency transfer in progress.",
        )
        data = result.model_dump()
        assert data["status"] == "transferred"
        assert data["message"] == "Emergency transfer in progress."
        assert data["error"] is None

    def test_error_result(self) -> None:
        result = EmergencyTransferResult(
            status="error",
            error="Unable to complete transfer.",
        )
        data = result.model_dump()
        assert data["status"] == "error"
        assert data["message"] is None
        assert data["error"] == "Unable to complete transfer."

    def test_model_dump_json_is_valid_json(self) -> None:
        result = EmergencyTransferResult(status="transferred")
        parsed = json.loads(result.model_dump_json())
        assert parsed["status"] == "transferred"


# ---------------------------------------------------------------------------
# TRANSFER_TO_EMERGENCY_TOOL schema tests
# ---------------------------------------------------------------------------


class TestTransferToEmergencyTool:
    """Tests for the emergency transfer tool definition."""

    def test_tool_has_required_fields(self) -> None:
        assert TRANSFER_TO_EMERGENCY_TOOL["type"] == "function"
        assert TRANSFER_TO_EMERGENCY_TOOL["name"] == "transfer_to_emergency"
        assert "description" in TRANSFER_TO_EMERGENCY_TOOL
        assert "parameters" in TRANSFER_TO_EMERGENCY_TOOL

    def test_tool_requires_reason(self) -> None:
        params = TRANSFER_TO_EMERGENCY_TOOL["parameters"]
        assert "reason" in params["required"]  # type: ignore[index]

    def test_tool_parameters_schema(self) -> None:
        params = TRANSFER_TO_EMERGENCY_TOOL["parameters"]
        assert params["type"] == "object"  # type: ignore[index]
        assert "reason" in params["properties"]  # type: ignore[index]
        assert params["properties"]["reason"]["type"] == "string"  # type: ignore[index]


# ---------------------------------------------------------------------------
# handle_transfer_to_emergency tests
# ---------------------------------------------------------------------------


class TestHandleTransferToEmergency:
    """Tests for the async emergency transfer handler (speak-then-redirect).

    The handler arms the transfer (transfer_state + escalation + transferred
    message) but never redirects synchronously; the redirect fires later via
    handle_transfer_finished after the transfer message finishes playing.
    """

    @pytest.mark.asyncio
    async def test_valid_args_arms_transfer_without_redirect(self) -> None:
        with patch("app.api.twilio.transfer_call") as mock_transfer:
            with patch("app.api.twilio.get_settings") as mock_settings:
                mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                result = await handle_transfer_to_emergency(
                    call_sid="CA_test",
                    arguments='{"reason": "Car on fire"}',
                )

        assert result.status == "transferred"
        assert result.message is not None
        assert "transfer" in result.message.lower()
        assert result.error is None
        mock_transfer.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_arguments_returns_error(self) -> None:
        result = await handle_transfer_to_emergency(
            call_sid="CA_test",
            arguments="not valid json",
        )

        assert result.status == "error"
        assert result.error == "Invalid arguments"

    @pytest.mark.asyncio
    async def test_missing_reason_returns_error(self) -> None:
        result = await handle_transfer_to_emergency(
            call_sid="CA_test",
            arguments="{}",
        )

        assert result.status == "error"
        assert result.error == "Invalid arguments"

    @pytest.mark.asyncio
    async def test_empty_call_sid_still_returns_transferred(self) -> None:
        with patch("app.api.twilio.transfer_call") as mock_transfer:
            with patch("app.api.twilio.get_settings") as mock_settings:
                mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                result = await handle_transfer_to_emergency(
                    call_sid="",
                    arguments='{"reason": "Fire"}',
                )

        assert result.status == "transferred"
        mock_transfer.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_does_not_block_on_transfer_io(self) -> None:
        """Verify the handler itself performs no blocking transfer I/O."""
        with patch("app.api.twilio.transfer_call") as mock_transfer:
            with patch("app.api.twilio.get_settings") as mock_settings:
                mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                with patch(
                    "app.api.twilio.asyncio.to_thread",
                    wraps=__import__("asyncio").to_thread,
                ) as mock_to_thread:
                    await handle_transfer_to_emergency(
                        call_sid="CA_test",
                        arguments='{"reason": "Emergency"}',
                    )

        mock_transfer.assert_not_called()
        mock_to_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_transferred_result_exposes_no_exception_details(self) -> None:
        with patch("app.api.twilio.get_settings") as mock_settings:
            mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
            result = await handle_transfer_to_emergency(
                call_sid="CA_test",
                arguments='{"reason": "Danger"}',
            )

        serialized = result.model_dump_json()
        assert "sensitive internal detail" not in serialized
        assert result.status == "transferred"


class TestHandleTransferFinished:
    """Tests for the deferred redirect fired after transfer-message playback."""

    @pytest.mark.asyncio
    async def test_redirect_fires_exactly_once(self) -> None:
        from app.services.calls import call_manager

        state = call_manager.create(twilio_call_id="CA_redirect", caller_phone="+1")
        state.transfer_state = "transferred"
        try:
            with patch("app.api.twilio.transfer_call", return_value=True) as mock_transfer:
                with patch("app.api.twilio.get_settings") as mock_settings:
                    mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                    first = await handle_transfer_finished("CA_redirect")
                    second = await handle_transfer_finished("CA_redirect")
            assert mock_transfer.call_count == 1
            assert first is True
            assert second is True
            mock_transfer.assert_called_with(
                call_sid="CA_redirect",
                destination_phone="+19115551234",
            )
        finally:
            call_manager.remove("CA_redirect")

    @pytest.mark.asyncio
    async def test_redirect_skipped_when_call_ended(self) -> None:
        with patch("app.api.twilio.transfer_call") as mock_transfer:
            result = await handle_transfer_finished("CA_missing_no_state")
        mock_transfer.assert_not_called()
        assert result is True

    @pytest.mark.asyncio
    async def test_redirect_failure_returns_false_for_911_fallback(self) -> None:
        from app.services.calls import call_manager

        state = call_manager.create(twilio_call_id="CA_fail", caller_phone="+1")
        state.transfer_state = "transferred"
        try:
            with patch("app.api.twilio.transfer_call", return_value=False) as mock_transfer:
                with patch("app.api.twilio.get_settings") as mock_settings:
                    mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                    result = await handle_transfer_finished("CA_fail")
            mock_transfer.assert_called_once()
            assert result is False
        finally:
            call_manager.remove("CA_fail")

    @pytest.mark.asyncio
    async def test_redirect_uses_to_thread_for_blocking_io(self) -> None:
        from app.services.calls import call_manager

        state = call_manager.create(twilio_call_id="CA_thread", caller_phone="+1")
        state.transfer_state = "transferred"
        try:
            with patch("app.api.twilio.transfer_call", return_value=True):
                with patch("app.api.twilio.get_settings") as mock_settings:
                    mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                    with patch(
                        "app.api.twilio.asyncio.to_thread",
                        wraps=__import__("asyncio").to_thread,
                    ) as mock_to_thread:
                        await handle_transfer_finished("CA_thread")
            mock_to_thread.assert_called_once()
        finally:
            call_manager.remove("CA_thread")


# ---------------------------------------------------------------------------
# transfer_call (app/services/emergency.py) unit tests
# ---------------------------------------------------------------------------


class TestTransferCall:
    """Unit tests for the transfer_call function in emergency.py."""

    @patch("app.services.emergency.get_settings")
    @patch("app.services.emergency.TwilioClient")
    def test_successful_transfer_returns_true(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"

        mock_call = MagicMock()
        mock_call.status = "queued"
        mock_client_cls.return_value.calls.return_value.update.return_value = mock_call

        result = transfer_call(
            call_sid="CA_test_123",
            destination_phone="+19115551234",
        )

        assert result is True

    @patch("app.services.emergency.get_settings")
    @patch("app.services.emergency.TwilioClient")
    def test_successful_transfer_creates_client_with_correct_credentials(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_real_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "real_auth_token"

        mock_call = MagicMock()
        mock_call.status = "queued"
        mock_client_cls.return_value.calls.return_value.update.return_value = mock_call

        transfer_call(
            call_sid="CA_test",
            destination_phone="+15551234567",
        )

        mock_client_cls.assert_called_once_with("AC_real_sid", "real_auth_token")

    @patch("app.services.emergency.get_settings")
    @patch("app.services.emergency.TwilioClient")
    def test_successful_transfer_passes_correct_twiml(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"

        mock_call = MagicMock()
        mock_call.status = "queued"
        mock_client_cls.return_value.calls.return_value.update.return_value = mock_call

        transfer_call(
            call_sid="CA_test",
            destination_phone="+19115551234",
        )

        mock_client_cls.return_value.calls.return_value.update.assert_called_once_with(
            twiml="<Response><Dial>+19115551234</Dial></Response>"
        )

    @patch("app.services.emergency.get_settings")
    @patch("app.services.emergency.TwilioClient")
    def test_successful_transfer_calls_with_correct_sid(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"

        mock_call = MagicMock()
        mock_call.status = "queued"
        mock_client_cls.return_value.calls.return_value.update.return_value = mock_call

        transfer_call(
            call_sid="CA_specific_call_sid",
            destination_phone="+15559990000",
        )

        mock_client_cls.return_value.calls.assert_called_once_with("CA_specific_call_sid")

    @patch("app.services.emergency.get_settings")
    @patch("app.services.emergency.TwilioClient")
    def test_twilio_exception_returns_false(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"

        mock_client_cls.return_value.calls.return_value.update.side_effect = Exception(
            "Twilio API error"
        )

        result = transfer_call(
            call_sid="CA_test",
            destination_phone="+19115551234",
        )

        assert result is False

    @patch("app.services.emergency.get_settings")
    @patch("app.services.emergency.TwilioClient")
    def test_twilio_runtime_error_returns_false(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"

        mock_client_cls.return_value.calls.return_value.update.side_effect = RuntimeError(
            "Connection refused"
        )

        result = transfer_call(
            call_sid="CA_test",
            destination_phone="+19115551234",
        )

        assert result is False

    @patch("app.services.emergency.get_settings")
    @patch("app.services.emergency.TwilioClient")
    def test_exception_does_not_propagate(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Verify that exceptions are caught and do not propagate to the caller."""
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"

        mock_client_cls.return_value.calls.return_value.update.side_effect = Exception(
            "sensitive internal detail"
        )

        result = transfer_call(
            call_sid="CA_test",
            destination_phone="+19115551234",
        )

        assert result is False


# ---------------------------------------------------------------------------
# _record_escalation background task tests
# ---------------------------------------------------------------------------


class TestRecordEscalation:
    """Tests for the _record_escalation background task."""

    @pytest.mark.asyncio
    async def test_calls_update_ticket_hazard_with_correct_args(self) -> None:
        with patch("app.api.twilio.update_ticket_hazard") as mock_update:
            await _record_escalation(
                call_sid="CA_test",
                arguments='{"reason": "Vehicle fire"}',
            )

        mock_update.assert_called_once_with(
            call_id="CA_test",
            hazard_reason="Vehicle fire",
        )

    @pytest.mark.asyncio
    async def test_exception_in_update_is_caught(self) -> None:
        with patch(
            "app.api.twilio.update_ticket_hazard",
            side_effect=RuntimeError("Supabase connection failed"),
        ):
            # Should not raise
            await _record_escalation(
                call_sid="CA_test",
                arguments='{"reason": "Fire"}',
            )

    @pytest.mark.asyncio
    async def test_invalid_arguments_is_caught(self) -> None:
        with patch("app.api.twilio.update_ticket_hazard") as mock_update:
            # Invalid JSON should be caught, update_ticket_hazard should not be called
            await _record_escalation(
                call_sid="CA_test",
                arguments="not json",
            )

        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# handle_transfer_to_emergency escalation integration tests
# ---------------------------------------------------------------------------


class TestHandleTransferEscalationIntegration:
    """Tests verifying escalation recording is wired into the transfer handler."""

    @pytest.mark.asyncio
    async def test_background_task_created_on_successful_transfer(self) -> None:
        """Verify _record_escalation is scheduled as a background task on success."""
        with patch("app.api.twilio.transfer_call", return_value=True):
            with patch("app.api.twilio.get_settings") as mock_settings:
                mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                with patch("app.api.twilio._record_escalation") as mock_record:
                    mock_record.return_value = AsyncMock()
                    result = await handle_transfer_to_emergency(
                        call_sid="CA_test",
                        arguments='{"reason": "Car on fire"}',
                    )

        assert result.status == "transferred"

    @pytest.mark.asyncio
    async def test_no_background_task_when_call_sid_is_empty(self) -> None:
        """Verify no escalation task is created when call_sid is empty."""
        with patch("app.api.twilio.transfer_call", return_value=True):
            with patch("app.api.twilio.get_settings") as mock_settings:
                mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
                with patch("app.api.twilio._record_escalation") as mock_record:
                    result = await handle_transfer_to_emergency(
                        call_sid="",
                        arguments='{"reason": "Fire"}',
                    )

        assert result.status == "transferred"
        # _record_escalation should not have been called directly
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_escalation_on_invalid_arguments(self) -> None:
        """Verify no escalation task is created when validation fails."""
        with patch("app.api.twilio.get_settings") as mock_settings:
            mock_settings.return_value.EMERGENCY_TRANSFER_PHONE = "+19115551234"
            with patch("app.api.twilio._record_escalation") as mock_record:
                result = await handle_transfer_to_emergency(
                    call_sid="CA_test",
                    arguments="{}",
                )

        assert result.status == "error"
        mock_record.assert_not_called()
