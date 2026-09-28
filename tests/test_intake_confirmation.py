"""Behavior tests for the confirmation-gated intake lifecycle.

Saving location, vehicle, and issue only makes a request ready for
confirmation. Only confirm_assistance_request completes the intake, fires the
dispatcher SMS, and starts the fixed closing flow.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.twilio import (
    _background_tasks,
    handle_confirm_assistance_request,
    handle_update_assistance_request,
)
from app.realtime.instructions import CLOSING_MESSAGE, ROADSIDE_ASSISTANT_INSTRUCTIONS
from app.realtime.session import RealtimeSession

CALL_SID = "CA_confirm_flow"
CALLER_PHONE = "+15551234567"
THREE_FIELD_ARGS = (
    '{"location": "12th and Main", "vehicle": "blue 2021 Toyota Tacoma", '
    '"issue": "flat front tire"}'
)


@pytest.fixture(autouse=True)
def _mock_complete_intake() -> Generator[MagicMock]:
    """Capture the guarded lifecycle finalizer instead of hitting the database."""
    with patch("app.api.twilio.complete_intake") as mock_complete:
        yield mock_complete


@pytest.fixture(autouse=True)
def _mock_claim_notification_status() -> Generator[MagicMock]:
    """Claim the notification by default; tests modeling a lost claim patch it themselves."""
    with patch("app.api.twilio.claim_notification_status", return_value=True) as mock_claim:
        yield mock_claim


def _row(**overrides: Any) -> dict[str, Any]:
    """Build a fully populated, still-open assistance-request row."""
    row: dict[str, Any] = {
        "id": "req_1",
        "call_id": CALL_SID,
        "caller_phone": CALLER_PHONE,
        "location": "12th and Main",
        "vehicle": "blue 2021 Toyota Tacoma",
        "issue": "flat front tire",
        "status": "in_progress",
        "intake_status": "in_progress",
        "notification_status": "pending",
    }
    row.update(overrides)
    return row


async def _drain_background_tasks() -> None:
    await asyncio.sleep(0)
    pending = [t for t in _background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
def no_grace() -> Generator[None]:
    """Collapse the hangup grace period to zero for deterministic tests."""
    with patch("app.realtime.session.CLOSING_HANGUP_GRACE_SECONDS", 0):
        yield


def _events_named(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{") and json.loads(record.message).get("event") == event_name
    ]


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _make_session(**kwargs: Any) -> RealtimeSession:
    defaults: dict[str, Any] = {
        "call_sid": CALL_SID,
        "caller_phone": CALLER_PHONE,
        "stream_sid": "MZ_confirm_flow",
    }
    defaults.update(kwargs)
    session = RealtimeSession(**defaults)
    if "on_closing_mark_requested" not in kwargs:

        async def _auto_ack_mark(mark_name: str) -> None:
            session.acknowledge_closing_mark(mark_name)

        session.on_closing_mark_requested = _auto_ack_mark
    return session


async def _connect_session(session: RealtimeSession) -> AsyncMock:
    settings = MagicMock()
    settings.OPENAI_API_KEY = "sk-test-key"
    settings.OPENAI_REALTIME_MODEL = "gpt-4o-realtime-preview"

    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()

    async def _empty_aiter() -> object:
        return iter([])

    ws.__aiter__ = MagicMock(side_effect=_empty_aiter)

    with patch(
        "app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws
    ):
        with patch("app.realtime.session.get_settings", return_value=settings):
            await session.connect()
    ws.reset_mock()
    return ws


def _response_creates(ws: AsyncMock) -> list[dict]:
    return [
        json.loads(call[0][0])
        for call in ws.send.call_args_list
        if json.loads(call[0][0]).get("type") == "response.create"
    ]


async def _save_three_fields(session: RealtimeSession) -> None:
    """Drive the progressive save that makes the request ready to confirm."""
    session.on_tool_call = AsyncMock(
        return_value='{"status": "ready_for_confirmation", "assistance_request_id": "req_1"}'
    )
    await session._handle_function_call({
        "call_id": "call_save",
        "type": "function_call",
        "name": "update_assistance_request",
        "arguments": THREE_FIELD_ARGS,
    })


async def _confirm(session: RealtimeSession, *, result: str | None = None) -> None:
    session.on_tool_call = AsyncMock(
        return_value=result or '{"status": "confirmed", "assistance_request_id": "req_1"}'
    )
    await session._handle_function_call({
        "call_id": "call_confirm",
        "type": "function_call",
        "name": "confirm_assistance_request",
        "arguments": "{}",
    })


async def _finish_closing_response(session: RealtimeSession) -> None:
    await session._handle_event({
        "type": "response.created",
        "response": {"id": "resp_closing", "status": "in_progress"},
    })
    await session._handle_event({
        "type": "response.done",
        "response": {"id": "resp_closing", "status": "completed", "output": []},
    })


# ---------------------------------------------------------------------------
# 1. Persistence does not complete intake
# ---------------------------------------------------------------------------


class TestPersistenceDoesNotCompleteIntake:
    async def test_three_saved_fields_stay_open_and_silent(
        self, _mock_complete_intake: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch("app.api.twilio.create_ticket", return_value=_row()) as mock_save:
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                with caplog.at_level("INFO"):
                    result = await handle_update_assistance_request(
                        call_sid=CALL_SID,
                        caller_phone=CALLER_PHONE,
                        arguments=THREE_FIELD_ARGS,
                    )

        assert result.status == "ready_for_confirmation"
        saved = mock_save.call_args.kwargs
        assert saved["location"] == "12th and Main"
        assert saved["vehicle"] == "blue 2021 Toyota Tacoma"
        assert saved["issue"] == "flat front tire"

        # Nothing completes the intake, notifies, or closes on a save.
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()
        assert _events_named(caplog, "assistance_request_ready_for_confirmation")
        assert _events_named(caplog, "assistance_request_confirmed") == []

    async def test_saving_never_starts_closing_or_hangup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _save_three_fields(session)

        creates = _response_creates(ws)
        assert len(creates) == 1
        # The model gets a plain turn to summarize; the closing line never plays.
        assert "instructions" not in creates[0].get("response", {})
        assert CLOSING_MESSAGE not in str(creates[0])

        assert session.intake_completed is False
        assert session.closing_response_started is False
        assert session.hangup_started is False
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Successful confirmation
# ---------------------------------------------------------------------------


class TestSuccessfulConfirmation:
    async def test_confirmation_completes_intake_and_notifies_once(
        self, _mock_complete_intake: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch("app.api.twilio.get_assistance_request", return_value=_row()):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                with caplog.at_level("INFO"):
                    result = await handle_confirm_assistance_request(
                        call_sid=CALL_SID,
                        caller_phone=CALLER_PHONE,
                    )
                    await _drain_background_tasks()

        assert result.status == "confirmed"
        _mock_complete_intake.assert_called_once_with(call_id=CALL_SID)
        mock_notify.assert_called_once_with(
            call_id=CALL_SID,
            caller_phone=CALLER_PHONE,
            location="12th and Main",
            vehicle="blue 2021 Toyota Tacoma",
            issue="flat front tire",
        )
        assert _events_named(caplog, "assistance_request_confirmed")
        assert _events_named(caplog, "assistance_request_confirmation_requested")

    async def test_confirmation_starts_closing_exactly_once(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _confirm(session)

        creates = _response_creates(ws)
        assert len(creates) == 1
        assert CLOSING_MESSAGE in creates[0]["response"]["instructions"]

        assert session.intake_completed is True
        assert session.closing_response_started is True
        assert session.hangup_started is False
        assert len(_events_named(caplog, "closing_response_started")) == 1

        await _finish_closing_response(session)
        assert session.closing_response_completed is True
        assert session._hangup_grace_task is not None
        await session._hangup_grace_task
        on_closing_finished.assert_called_once_with(CALL_SID)

    async def test_unconfirmed_request_stays_in_progress(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        """A fully populated row still needs the caller's confirmation."""
        with patch("app.api.twilio.create_ticket", return_value=_row()):
            with patch("app.api.twilio.notify_dispatcher"):
                await handle_update_assistance_request(
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                    arguments=THREE_FIELD_ARGS,
                )

        # intake_status only changes through the guarded finalizer.
        _mock_complete_intake.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Missing fields reject confirmation
# ---------------------------------------------------------------------------


class TestConfirmationRequiresAllFields:
    @pytest.mark.parametrize(
        "missing_field",
        ["location", "vehicle", "issue"],
    )
    async def test_confirmation_rejected_when_a_field_is_missing(
        self,
        missing_field: str,
        _mock_complete_intake: MagicMock,
    ) -> None:
        row = _row(**{missing_field: None})
        with patch("app.api.twilio.get_assistance_request", return_value=row):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                )

        assert result.status == "incomplete"
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()

    async def test_incomplete_confirmation_result_starts_no_closing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _confirm(
                session,
                result='{"status": "incomplete", "message": "All three fields are required."}',
            )

        creates = _response_creates(ws)
        assert len(creates) == 1
        assert "instructions" not in creates[0].get("response", {})
        assert session.intake_completed is False
        assert session.closing_response_started is False
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()

    @pytest.mark.parametrize(
        "result",
        [
            '{"status": "escalated", "message": "An emergency transfer owns this call."}',
            "not-json",
        ],
        ids=["escalated", "unparseable"],
    )
    async def test_other_non_confirmed_results_start_no_closing(
        self, result: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Escalated and unparseable confirm results keep the generic turn."""
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _confirm(session, result=result)

        creates = _response_creates(ws)
        assert len(creates) == 1
        assert "instructions" not in creates[0].get("response", {})
        assert CLOSING_MESSAGE not in str(creates[0])
        assert session.intake_completed is False
        assert session.closing_response_started is False
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Corrections re-open confirmation, not completion
# ---------------------------------------------------------------------------


class TestCorrectionFlow:
    async def test_corrected_location_does_not_complete_or_notify(
        self, _mock_complete_intake: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        corrected = _row(location="Main and 13th")
        with patch("app.api.twilio.create_ticket", return_value=corrected) as mock_save:
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                with caplog.at_level("INFO"):
                    result = await handle_update_assistance_request(
                        call_sid=CALL_SID,
                        caller_phone=CALLER_PHONE,
                        arguments='{"location": "Main and 13th"}',
                    )

        assert result.status == "ready_for_confirmation"
        assert mock_save.call_args.kwargs["location"] == "Main and 13th"
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()
        assert _events_named(caplog, "assistance_request_confirmed") == []

    async def test_confirmation_uses_corrected_row(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        corrected = _row(location="Main and 13th")
        with patch("app.api.twilio.get_assistance_request", return_value=corrected):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                )
                await _drain_background_tasks()

        assert result.status == "confirmed"
        mock_notify.assert_called_once_with(
            call_id=CALL_SID,
            caller_phone=CALLER_PHONE,
            location="Main and 13th",
            vehicle="blue 2021 Toyota Tacoma",
            issue="flat front tire",
        )

    async def test_late_save_after_confirmation_adds_no_turn(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        await _save_three_fields(session)
        await _confirm(session)
        creates_before = len(_response_creates(ws))

        # A late correction after confirmation must not produce another turn.
        session.on_tool_call = AsyncMock(
            return_value='{"status": "ready_for_confirmation", "assistance_request_id": "req_1"}'
        )
        await session._handle_function_call({
            "call_id": "call_late_correction",
            "type": "function_call",
            "name": "update_assistance_request",
            "arguments": '{"location": "Main and 13th"}',
        })

        # save turn + closing turn; the late save adds nothing.
        assert creates_before == 2
        assert len(_response_creates(ws)) == 2
        assert session.intake_completed is True


# ---------------------------------------------------------------------------
# 5. Duplicate confirmation is idempotent
# ---------------------------------------------------------------------------


class TestDuplicateConfirmation:
    async def test_second_confirmation_does_not_resend_sms(
        self, _mock_complete_intake: MagicMock
    ) -> None:
        rows = [_row(), _row(notification_status="sent")]
        with patch("app.api.twilio.get_assistance_request", side_effect=rows):
            with patch(
                "app.api.twilio.claim_notification_status",
                side_effect=[True, False],
            ):
                with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                    first = await handle_confirm_assistance_request(
                        call_sid=CALL_SID, caller_phone=CALLER_PHONE
                    )
                    await _drain_background_tasks()
                    second = await handle_confirm_assistance_request(
                        call_sid=CALL_SID, caller_phone=CALLER_PHONE
                    )
                    await _drain_background_tasks()

        assert first.status == "confirmed"
        assert second.status == "confirmed"
        mock_notify.assert_called_once()

    async def test_second_confirmation_starts_no_second_closing(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _confirm(session)
            await _confirm(
                session,
                result='{"status": "confirmed", "assistance_request_id": "req_1"}',
            )

        assert len(_response_creates(ws)) == 1
        assert len(_events_named(caplog, "closing_response_started")) == 1
        assert _events_named(caplog, "duplicate_assistance_request_confirmation_ignored")

        await _finish_closing_response(session)
        assert session._hangup_grace_task is not None
        await session._hangup_grace_task
        on_closing_finished.assert_called_once_with(CALL_SID)


# ---------------------------------------------------------------------------
# 6. Emergency handling keeps priority over confirmation
# ---------------------------------------------------------------------------


class TestEmergencyBeatsConfirmation:
    async def test_escalated_request_is_never_completed_or_notified(
        self, _mock_complete_intake: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        escalated = _row(status="escalated", intake_status="escalated")
        with patch("app.api.twilio.get_assistance_request", return_value=escalated):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                with caplog.at_level("INFO"):
                    result = await handle_confirm_assistance_request(
                        call_sid=CALL_SID,
                        caller_phone=CALLER_PHONE,
                    )

        assert result.status == "escalated"
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()
        assert _events_named(caplog, "assistance_request_confirmation_escalated")

    async def test_confirmation_after_transfer_starts_no_closing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            session.on_tool_call = AsyncMock(return_value='{"status": "transferred"}')
            await session._handle_function_call({
                "call_id": "call_transfer",
                "type": "function_call",
                "name": "transfer_to_emergency",
                "arguments": '{"reason": "Car on fire"}',
            })
            await _confirm(session)

        assert session.closing_response_started is False
        assert session.hangup_started is False
        assert _events_named(caplog, "confirmation_suppressed_transfer")
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()

        # Only the emergency tool_result turn was created — no closing directive.
        creates = _response_creates(ws)
        assert len(creates) == 1
        assert CLOSING_MESSAGE not in str(creates[0])

    async def test_confirmation_after_transfer_never_reaches_backend(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The completion handler must not run for a transfer-owned call."""
        session = _make_session()
        ws = await _connect_session(session)

        transfer_handler = AsyncMock(return_value='{"status": "transferred"}')
        session.on_tool_call = transfer_handler
        with caplog.at_level("INFO"):
            await session._handle_function_call({
                "call_id": "call_transfer",
                "type": "function_call",
                "name": "transfer_to_emergency",
                "arguments": '{"reason": "Car on fire"}',
            })

            confirm_handler = AsyncMock(
                return_value='{"status": "confirmed", "assistance_request_id": "req_1"}'
            )
            session.on_tool_call = confirm_handler
            await session._handle_function_call({
                "call_id": "call_confirm_after_transfer",
                "type": "function_call",
                "name": "confirm_assistance_request",
                "arguments": "{}",
            })

        # Only the transfer tool ran; the confirm handler was never invoked.
        transfer_handler.assert_awaited_once()
        confirm_handler.assert_not_awaited()

        # The model still receives a result, synthesized as the handler's
        # own escalated response.
        outputs = [
            json.loads(call[0][0])["item"]["output"]
            for call in ws.send.call_args_list
            if json.loads(call[0][0]).get("type") == "conversation.item.create"
            and json.loads(call[0][0])["item"].get("type") == "function_call_output"
        ]
        assert len(outputs) == 2
        assert json.loads(outputs[1])["status"] == "escalated"

        # Suppression is logged exactly once, before dispatch.
        assert len(_events_named(caplog, "confirmation_suppressed_transfer")) == 1
        assert session.intake_completed is False
        assert session.closing_response_started is False


# ---------------------------------------------------------------------------
# 7. Caller answer scenarios for the final confirmation summary
#
# The caller's answer is classified by the prompt: this codebase has no
# transcript gate, so each scenario pins both the prompt rule for that
# answer and the runtime invariant that keeps a mis-classified answer from
# completing the intake.
# ---------------------------------------------------------------------------


def _instruction_text() -> str:
    return ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()


def _install_tool_handler(session: RealtimeSession) -> AsyncMock:
    """Attach one shared handler so every dispatch in a test is recorded."""

    def _respond(call_id: str, func_name: str, arguments: str) -> str:
        if func_name == "confirm_assistance_request":
            return '{"status": "confirmed", "assistance_request_id": "req_1"}'
        return '{"status": "ready_for_confirmation", "assistance_request_id": "req_1"}'

    handler = AsyncMock(side_effect=_respond)
    session.on_tool_call = handler
    return handler


async def _call_tool(session: RealtimeSession, name: str, arguments: str = "{}") -> None:
    await session._handle_function_call({
        "call_id": f"call_{name}",
        "type": "function_call",
        "name": name,
        "arguments": arguments,
    })


def _dispatched(handler: AsyncMock) -> list[str]:
    """Names of the tool handlers actually dispatched, in order."""
    return [call.args[1] for call in handler.await_args_list]


def _closing_turns(creates: list[dict]) -> list[dict]:
    return [c for c in creates if "instructions" in c.get("response", {})]


class TestConfirmationAnswerScenarios:
    async def test_clear_affirmative_completes_intake_exactly_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A clear "yes, that's right" confirms once and closes once."""
        lower = _instruction_text()
        assert "clear affirmative" in lower
        assert "call confirm_assistance_request, exactly once" in lower

        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)
        handler = _install_tool_handler(session)

        with caplog.at_level("INFO"):
            await _call_tool(session, "update_assistance_request", THREE_FIELD_ARGS)
            # The caller clearly confirms the summary...
            await _call_tool(session, "confirm_assistance_request")
            # ...and the model's confirm call is retried once.
            await _call_tool(session, "confirm_assistance_request")

        assert _dispatched(handler) == [
            "update_assistance_request",
            "confirm_assistance_request",
            "confirm_assistance_request",
        ]
        assert session.intake_completed is True
        closing_turns = _closing_turns(_response_creates(ws))
        assert len(closing_turns) == 1
        assert CLOSING_MESSAGE in closing_turns[0]["response"]["instructions"]
        assert len(_events_named(caplog, "closing_response_started")) == 1
        on_closing_finished.assert_not_called()

    async def test_clear_rejection_saves_only_the_changed_field_and_reasks(
        self, _mock_complete_intake: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A "no, the location is ..." re-saves only that field and re-asks."""
        lower = _instruction_text()
        assert "clear rejection" in lower
        assert "do not call confirm_assistance_request" in lower
        assert "ask briefly what needs correcting" in lower

        session = _make_session()
        ws = await _connect_session(session)
        handler = _install_tool_handler(session)

        with caplog.at_level("INFO"):
            await _call_tool(session, "update_assistance_request", THREE_FIELD_ARGS)
            # The rejection turn persists the correction instead of confirming.
            await _call_tool(
                session, "update_assistance_request", '{"location": "Main and 13th"}'
            )

        assert session.intake_completed is False
        assert session.closing_response_started is False
        assert _dispatched(handler) == [
            "update_assistance_request",
            "update_assistance_request",
        ]
        # Summary turn + correction turn: the model gets a turn to re-ask.
        assert len(_response_creates(ws)) == 2
        assert _events_named(caplog, "closing_response_started") == []

        # Persistence side: only the corrected field is written.
        corrected = _row(location="Main and 13th")
        with patch("app.api.twilio.create_ticket", return_value=corrected) as mock_save:
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_update_assistance_request(
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                    arguments='{"location": "Main and 13th"}',
                )

        assert result.status == "ready_for_confirmation"
        saved = mock_save.call_args.kwargs
        assert saved["location"] == "Main and 13th"
        # The untouched fields are omitted, so the merge cannot overwrite them.
        assert saved["vehicle"] is None
        assert saved["issue"] is None
        _mock_complete_intake.assert_not_called()
        mock_notify.assert_not_called()
        assert _events_named(caplog, "assistance_request_confirmed") == []

    async def test_ambiguous_response_never_completes_intake(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An "I think so" gets an explicit re-ask; no confirm tool runs."""
        lower = _instruction_text()
        assert "ambiguous or hedged" in lower
        assert "i think so" in lower
        assert "ask the caller to answer with a clear yes or no" in lower

        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)
        handler = _install_tool_handler(session)

        with caplog.at_level("INFO"):
            await _call_tool(session, "update_assistance_request", THREE_FIELD_ARGS)
            # The summary response finishes, opening the gate for caller turns.
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_summary", "status": "completed", "output": []},
            })
            # The caller hedges; per the prompt the model asks for an explicit
            # yes/no and calls no tool at all.
            await session._handle_event({
                "type": "input_audio_buffer.committed",
                "item_id": "item_ambiguous",
            })
            await session._handle_event({
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item_ambiguous",
                "transcript": "I think so maybe",
            })

        assert session.intake_completed is False
        assert session.closing_response_started is False
        # Only the save ran — no confirm was dispatched for the hedge.
        assert _dispatched(handler) == ["update_assistance_request"]
        # Save/summary turn + the clarifying re-ask turn, and nothing else.
        assert len(_response_creates(ws)) == 2
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()

    async def test_unanswered_confirmation_never_completes_intake(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dead air after the summary completes nothing: no confirm, no closing."""
        lower = _instruction_text()
        assert "silence or no answer" in lower
        assert "dead air is not a confirmation" in lower
        assert "do not assume silence means confirmation" in lower

        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)
        handler = _install_tool_handler(session)

        with caplog.at_level("INFO"):
            await _call_tool(session, "update_assistance_request", THREE_FIELD_ARGS)
            # The confirmation question is out and the caller never answers,
            # so there is no affirmative for the model to act on.

        assert session.intake_completed is False
        assert session.closing_response_started is False
        assert session.hangup_started is False
        # Silence produced no confirm dispatch and no extra turn.
        assert _dispatched(handler) == ["update_assistance_request"]
        assert len(_response_creates(ws)) == 1
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()

    async def test_correction_is_persisted_then_reconfirmed_before_completion(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Save → correction → full re-summary → clear "yes" completes once."""
        lower = _instruction_text()
        assert "call update_assistance_request with only the corrected field or fields" in lower
        assert "summarize the complete current location, vehicle, and issue again" in lower
        assert "ask for confirmation again" in lower

        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)
        handler = _install_tool_handler(session)

        with caplog.at_level("INFO"):
            await _call_tool(session, "update_assistance_request", THREE_FIELD_ARGS)

            # The caller rejects the location: only the changed field is saved.
            await _call_tool(
                session, "update_assistance_request", '{"location": "Main and 13th"}'
            )

            assert session.intake_completed is False
            assert session.closing_response_started is False

            # The caller now clearly confirms the re-summarized three fields.
            await _call_tool(session, "confirm_assistance_request")

        assert _dispatched(handler) == [
            "update_assistance_request",
            "update_assistance_request",
            "confirm_assistance_request",
        ]
        # The correction carried only the changed field.
        correction_args = json.loads(handler.await_args_list[1].args[2])
        assert correction_args == {"location": "Main and 13th"}

        assert session.intake_completed is True
        creates = _response_creates(ws)
        closing_turns = _closing_turns(creates)
        assert len(closing_turns) == 1
        assert CLOSING_MESSAGE in closing_turns[0]["response"]["instructions"]
        # save turn + correction turn + closing turn, in that order.
        assert len(creates) == 3
        assert len(_events_named(caplog, "closing_response_started")) == 1


# ---------------------------------------------------------------------------
# 8. Completion persistence failure fails safe
# ---------------------------------------------------------------------------


class TestCompletionPersistenceFailure:
    """A completion write that never lands must fail the whole confirmation."""

    async def test_failed_completion_write_returns_error_and_sends_no_sms(
        self,
        _mock_complete_intake: MagicMock,
        _mock_claim_notification_status: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _mock_complete_intake.return_value = False
        with patch("app.api.twilio.get_assistance_request", return_value=_row()):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                with caplog.at_level("INFO"):
                    result = await handle_confirm_assistance_request(
                        call_sid=CALL_SID,
                        caller_phone=CALLER_PHONE,
                    )
                    await _drain_background_tasks()

        assert result.status == "error"
        assert result.error
        assert "could not be saved" in result.error
        _mock_complete_intake.assert_called_once_with(call_id=CALL_SID)
        _mock_claim_notification_status.assert_not_called()
        mock_notify.assert_not_called()
        assert _events_named(caplog, "assistance_request_completion_not_persisted")
        assert _events_named(caplog, "assistance_request_confirmed") == []

    async def test_forced_completion_write_failure_returns_error_via_real_finalizer(
        self,
        _mock_claim_notification_status: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Drive the real finalizer against a database that refuses the write."""
        from app.services import tickets

        with patch("app.api.twilio.complete_intake", tickets.complete_intake):
            with patch("app.services.tickets._get_supabase") as mock_supabase:
                mock_supabase.side_effect = RuntimeError("DB down")
                with patch("app.api.twilio.get_assistance_request", return_value=_row()):
                    with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                        with caplog.at_level("INFO"):
                            result = await handle_confirm_assistance_request(
                                call_sid=CALL_SID,
                                caller_phone=CALLER_PHONE,
                            )
                            await _drain_background_tasks()

        assert result.status == "error"
        assert result.assistance_request_id == "req_1"
        assert mock_supabase.called
        _mock_claim_notification_status.assert_not_called()
        mock_notify.assert_not_called()
        assert _events_named(caplog, "assistance_request_completion_not_persisted")
        assert _events_named(caplog, "assistance_request_confirmed") == []

    async def test_completion_write_exception_returns_error_result(
        self,
        _mock_complete_intake: MagicMock,
        _mock_claim_notification_status: MagicMock,
    ) -> None:
        """An unexpected raise out of the finalizer is still a failure result."""
        _mock_complete_intake.side_effect = RuntimeError("sensitive internal detail")
        with patch("app.api.twilio.get_assistance_request", return_value=_row()):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                result = await handle_confirm_assistance_request(
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                )

        assert result.status == "error"
        assert "sensitive internal detail" not in result.model_dump_json()
        _mock_claim_notification_status.assert_not_called()
        mock_notify.assert_not_called()

    async def test_failure_then_retry_completes_the_same_request_once(
        self,
        _mock_complete_intake: MagicMock,
        _mock_claim_notification_status: MagicMock,
    ) -> None:
        """The retry after a transient failure completes and notifies exactly once."""
        _mock_complete_intake.side_effect = [False, True]
        with patch("app.api.twilio.get_assistance_request", return_value=_row()):
            with patch("app.api.twilio.notify_dispatcher") as mock_notify:
                first = await handle_confirm_assistance_request(
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                )
                await _drain_background_tasks()
                second = await handle_confirm_assistance_request(
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                )
                await _drain_background_tasks()

        assert first.status == "error"
        assert second.status == "confirmed"
        assert _mock_complete_intake.call_count == 2
        mock_notify.assert_called_once()

    async def test_failed_completion_starts_no_closing_and_retry_closes_once(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No closing or hangup arms on a failed completion; a retry still closes."""
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        failure = (
            '{"status": "error", "assistance_request_id": "req_1", "error": '
            '"Unable to complete the assistance request: the completion could not be '
            'saved, so the request is still open."}'
        )
        with caplog.at_level("INFO"):
            await _confirm(session, result=failure)

        creates = _response_creates(ws)
        assert len(creates) == 1
        # The model gets a plain turn to tell the caller; no closing directive.
        assert "instructions" not in creates[0].get("response", {})
        assert CLOSING_MESSAGE not in str(creates[0])
        assert session.intake_completed is False
        assert session.closing_response_started is False
        assert session.hangup_started is False
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()

        # The model's failure message plays out as an ordinary assistant turn.
        await session._handle_event({
            "type": "response.created",
            "response": {"id": "resp_failure_turn", "status": "in_progress"},
        })
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_failure_turn", "status": "completed", "output": []},
        })

        # The same request is retried and now persists: one closing, one hangup.
        with caplog.at_level("INFO"):
            await _confirm(session)

        creates = _response_creates(ws)
        assert len(creates) == 2
        assert CLOSING_MESSAGE in creates[1]["response"]["instructions"]
        assert session.intake_completed is True
        assert session.closing_response_started is True
        assert len(_events_named(caplog, "closing_response_started")) == 1

        await _finish_closing_response(session)
        assert session._hangup_grace_task is not None
        await session._hangup_grace_task
        on_closing_finished.assert_called_once_with(CALL_SID)
