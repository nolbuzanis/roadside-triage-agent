"""Tests for tool-call handling: handle_create_breakdown_ticket and handle_tool_call."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.api.twilio import handle_create_breakdown_ticket
from app.realtime.tools import (
    TicketArgs,
    TicketToolResult,
)

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

    def test_missing_field_raisesValidationError(self) -> None:
        with pytest.raises(ValidationError):
            TicketArgs.model_validate_json('{"location": "Main St", "vehicle": "Toyota Camry"}')

    def test_empty_string_fields_are_valid(self) -> None:
        args = TicketArgs.model_validate_json(
            '{"location": "", "vehicle": "", "issue": ""}'
        )
        assert args.location == ""
        assert args.vehicle == ""
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

    async def test_valid_args_creates_ticket(self) -> None:
        mock_ticket = {"id": "ticket-uuid-123", "call_id": "CA_test"}
        with patch("app.api.twilio.create_ticket", return_value=mock_ticket) as mock_create:
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

    async def test_invalid_json_arguments_returns_error(self) -> None:
        result = await handle_create_breakdown_ticket(
            call_sid="CA_test",
            caller_phone="+15551234567",
            arguments="not valid json",
        )

        assert result.status == "error"
        assert result.error == "Invalid arguments"
        assert result.ticket_id is None

    async def test_missing_fields_returns_error(self) -> None:
        result = await handle_create_breakdown_ticket(
            call_sid="CA_test",
            caller_phone="+15551234567",
            arguments='{"location": "Main St"}',
        )

        assert result.status == "error"
        assert result.error == "Invalid arguments"

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

    async def test_create_ticket_failure_does_not_expose_exception_details(self) -> None:
        with patch("app.api.twilio.create_ticket", side_effect=RuntimeError("sensitive internal detail")):
            result = await handle_create_breakdown_ticket(
                call_sid="CA_test",
                caller_phone="+15551234567",
                arguments='{"location": "X", "vehicle": "Y", "issue": "Z"}',
            )

        serialized = result.model_dump_json()
        assert "sensitive internal detail" not in serialized

    async def test_call_sid_falls_back_to_unknown(self) -> None:
        mock_ticket = {"id": "t1", "call_id": "unknown"}
        with patch("app.api.twilio.create_ticket", return_value=mock_ticket) as mock_create:
            result = await handle_create_breakdown_ticket(
                call_sid="",
                caller_phone="",
                arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
            )

        assert result.status == "created"
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["call_id"] == "unknown"
        assert call_kwargs["caller_phone"] == "unknown"

    async def test_handler_uses_to_thread_for_blocking_io(self) -> None:
        """Verify that create_ticket is called via asyncio.to_thread (non-blocking)."""
        mock_ticket = {"id": "t1"}
        with patch("app.api.twilio.asyncio.to_thread", wraps=__import__("asyncio").to_thread) as mock_to_thread:
            with patch("app.api.twilio.create_ticket", return_value=mock_ticket):
                await handle_create_breakdown_ticket(
                    call_sid="CA_test",
                    caller_phone="+15551234567",
                    arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                )

        mock_to_thread.assert_called_once()
