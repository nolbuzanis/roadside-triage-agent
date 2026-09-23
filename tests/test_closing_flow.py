"""Tests for the end-of-call closing flow after successful ticket creation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.twilio import handle_closing_finished, handle_create_breakdown_ticket
from app.realtime.instructions import CLOSING_MESSAGE
from app.realtime.session import CLOSING_HANGUP_GRACE_SECONDS, RealtimeSession
from app.services.calls import call_manager
from app.services.hangup import hangup_call

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOL_ITEM = {
    "call_id": "call_close_1",
    "name": "create_breakdown_ticket",
    "arguments": '{"location": "Main St", "vehicle": "Honda", "issue": "Flat tire"}',
}
SUCCESS_RESULT = '{"status": "created", "ticket_id": "tkt_abc"}'
ERROR_RESULT = '{"status": "error", "error": "Unable to create the ticket"}'
TRANSFER_ITEM = {
    "type": "function_call",
    "call_id": "call_em_1",
    "name": "transfer_to_emergency",
    "arguments": '{"reason": "Car on fire"}',
}
TRANSFER_RESULT = '{"status": "transferred"}'


def _make_session(**kwargs: object) -> RealtimeSession:
    defaults: dict[str, object] = {
        "call_sid": "CA_closing_test",
        "caller_phone": "+15551234567",
        "stream_sid": "MZ_closing_test",
    }
    defaults.update(kwargs)
    return RealtimeSession(**defaults)  # type: ignore[arg-type]


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.OPENAI_API_KEY = "sk-test-key"
    settings.OPENAI_REALTIME_MODEL = "gpt-4o-realtime-preview"
    return settings


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()

    async def _empty_aiter() -> object:
        return iter([])

    ws.__aiter__ = MagicMock(side_effect=_empty_aiter)
    return ws


async def _connect_session(session: RealtimeSession) -> AsyncMock:
    ws = _make_ws()
    with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
        with patch("app.realtime.session.get_settings", return_value=_make_settings()):
            await session.connect()
    ws.reset_mock()
    return ws


def _sent_events(ws: AsyncMock) -> list[dict]:
    return [json.loads(c[0][0]) for c in ws.send.call_args_list]


def _response_creates(ws: AsyncMock) -> list[dict]:
    return [e for e in _sent_events(ws) if e.get("type") == "response.create"]


def _events_named(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict]:
    return [
        json.loads(r.message)
        for r in caplog.records
        if json.loads(r.message).get("event") == event_name
    ]


@pytest.fixture
def no_grace() -> object:
    """Collapse the hangup grace period to zero for deterministic tests."""
    with patch("app.realtime.session.CLOSING_HANGUP_GRACE_SECONDS", 0):
        yield


async def _run_successful_ticket(session: RealtimeSession) -> None:
    """Drive a successful create_breakdown_ticket tool call through the session."""
    on_tool_call = AsyncMock(return_value=SUCCESS_RESULT)
    session.on_tool_call = on_tool_call
    await session._handle_function_call(dict(TOOL_ITEM))


async def _finish_closing_response(
    session: RealtimeSession,
    *,
    response_id: str = "resp_closing",
    status: str = "completed",
) -> None:
    """Deliver the response.created/response.done pair for the closing response."""
    await session._handle_event({
        "type": "response.created",
        "response": {"id": response_id, "status": "in_progress"},
    })
    await session._handle_event({
        "type": "response.done",
        "response": {"id": response_id, "status": status, "output": []},
    })


# ---------------------------------------------------------------------------
# Closing message and grace period constants
# ---------------------------------------------------------------------------


class TestClosingConstants:
    def test_closing_message_is_exact(self) -> None:
        assert CLOSING_MESSAGE == (
            "You're all set. I've logged your roadside assistance request, and a "
            "dispatcher will follow up with you shortly. Please stay somewhere "
            "safe. Goodbye."
        )

    def test_closing_message_forbids_extra_conversation(self) -> None:
        assert "anything else" not in CLOSING_MESSAGE
        assert "?" not in CLOSING_MESSAGE
        assert "ETA" not in CLOSING_MESSAGE.upper()

    def test_grace_period_is_500_to_1000ms(self) -> None:
        assert 0.5 <= CLOSING_HANGUP_GRACE_SECONDS <= 1.0


# ---------------------------------------------------------------------------
# 1. Successful ticket creation triggers the exact closing message
# ---------------------------------------------------------------------------


class TestClosingResponseTriggered:
    async def test_successful_ticket_sends_exact_closing_message(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        await _run_successful_ticket(session)

        creates = _response_creates(ws)
        assert len(creates) == 1
        instructions = creates[0]["response"]["instructions"]
        assert CLOSING_MESSAGE in instructions
        # The directive must not permit extra conversation after the closing line.
        assert "anything else" not in instructions
        assert "?" not in instructions
        assert "say nothing after" in instructions or "do not say anything after" in instructions

    async def test_successful_ticket_sends_post_ticket_closing_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_session(call_sid="CA_reason_log")
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_ticket(session)

        entries = [
            e
            for e in _events_named(caplog, "response_create_sent")
            if e.get("reason") == "post_ticket_closing"
        ]
        assert len(entries) == 1
        assert entries[0]["call_id"] == "CA_reason_log"
        assert entries[0]["response_source"] == "app.realtime.session._start_closing_response"

    async def test_closing_state_flags_set_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_session(call_sid="CA_flags")
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_ticket(session)

        assert session.ticket_created is True
        assert session.closing_response_started is True
        assert session.closing_response_completed is False
        assert session.hangup_started is False

        started = _events_named(caplog, "closing_response_started")
        assert len(started) == 1
        assert started[0]["call_sid"] == "CA_flags"
        assert started[0]["reason"] == "post_ticket_closing"
        assert started[0]["ticket_id"] == "tkt_abc"

    async def test_ticket_created_logged_by_handler(self, caplog: pytest.LogCaptureFixture) -> None:
        mock_ticket = {"id": "ticket-log-1", "call_id": "CA_log"}
        with patch("app.api.twilio.create_ticket", return_value=mock_ticket):
            with patch("app.api.twilio.notify_dispatcher"):
                with caplog.at_level("INFO"):
                    result = await handle_create_breakdown_ticket(
                        call_sid="CA_log",
                        caller_phone="+15551234567",
                        arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                    )

        assert result.status == "created"
        entries = _events_named(caplog, "ticket_created")
        assert len(entries) == 1
        assert entries[0]["call_sid"] == "CA_log"
        assert entries[0]["ticket_id"] == "ticket-log-1"


# ---------------------------------------------------------------------------
# 2. Hangup does not occur before the closing response finishes
# ---------------------------------------------------------------------------


class TestHangupWaitsForClosing:
    async def test_no_hangup_immediately_after_ticket_tool(
        self, no_grace: None
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        await _run_successful_ticket(session)

        assert session.closing_response_started is True
        assert session.closing_response_completed is False
        assert session.hangup_started is False
        on_closing_finished.assert_not_called()

    async def test_unrelated_response_done_does_not_complete_closing(
        self, no_grace: None
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        await _run_successful_ticket(session)
        await session._handle_event({
            "type": "response.created",
            "response": {"id": "resp_closing", "status": "in_progress"},
        })
        assert session.closing_response_id == "resp_closing"

        # An unrelated assistant response finishing must not complete the flow.
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_unrelated", "status": "completed", "output": []},
        })

        assert session.closing_response_completed is False
        assert session.hangup_started is False
        on_closing_finished.assert_not_called()

        # Only the matching response completes the closing flow.
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "completed", "output": []},
        })
        assert session.closing_response_completed is True

        assert session._hangup_grace_task is not None
        # Hangup has not fired yet — it waits for the grace task to run.
        on_closing_finished.assert_not_called()
        await session._hangup_grace_task
        on_closing_finished.assert_called_once_with("CA_closing_test")

    async def test_closing_completed_logged_with_response_id(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_session(call_sid="CA_completed_log")
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_ticket(session)
            await _finish_closing_response(session, response_id="resp_done_1")
            assert session._hangup_grace_task is not None
            await session._hangup_grace_task

        entries = _events_named(caplog, "closing_response_completed")
        assert len(entries) == 1
        assert entries[0]["call_sid"] == "CA_completed_log"
        assert entries[0]["response_id"] == "resp_done_1"
        assert entries[0]["ticket_id"] == "tkt_abc"

    async def test_unobserved_closing_id_fails_safe(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Without the closing response.created, no unrelated done may hang up."""
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_ticket(session)
            assert session.closing_response_id is None

            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_any", "status": "completed", "output": []},
            })

        assert session.closing_response_completed is False
        assert session._hangup_grace_task is None
        on_closing_finished.assert_not_called()
        assert _events_named(caplog, "closing_response_id_unobserved")

    async def test_failed_send_does_not_enqueue_closing_reason(self) -> None:
        session = _make_session()
        # Not connected: _send fails, so the reason must not be queued.
        await session._send_response_create(
            reason="post_ticket_closing",
            response_source="test",
        )
        assert len(session._response_create_reasons) == 0


# ---------------------------------------------------------------------------
# 3 + 5. Hangup happens once; duplicates do not duplicate closing/hangup
# ---------------------------------------------------------------------------


class TestHangupExactlyOnce:
    async def test_hangup_fires_once_after_success(self, no_grace: None) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        await _run_successful_ticket(session)
        await _finish_closing_response(session)
        assert session._hangup_grace_task is not None
        await session._hangup_grace_task

        assert session.hangup_started is True
        on_closing_finished.assert_called_once_with("CA_closing_test")

        # A repeated response.done and a repeated arm attempt do not re-fire.
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "completed", "output": []},
        })
        session._maybe_arm_hangup()
        if session._hangup_grace_task is not None:
            await session._hangup_grace_task
        on_closing_finished.assert_called_once()

    async def test_duplicate_tool_call_closes_only_once(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_ticket(session)
            # Duplicate/retried tool call with the same successful result.
            session.on_tool_call = AsyncMock(return_value=SUCCESS_RESULT)
            await session._handle_function_call({**TOOL_ITEM, "call_id": "call_close_2"})

        creates = _response_creates(ws)
        assert len(creates) == 1
        assert CLOSING_MESSAGE in creates[0]["response"]["instructions"]
        assert _events_named(caplog, "duplicate_ticket_tool_call_ignored")
        assert len(_events_named(caplog, "closing_response_started")) == 1

        await _finish_closing_response(session)
        assert session._hangup_grace_task is not None
        await session._hangup_grace_task
        on_closing_finished.assert_called_once()

    async def test_second_duplicate_after_close_still_no_extra_response(self) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        await _run_successful_ticket(session)
        session.on_tool_call = AsyncMock(return_value=SUCCESS_RESULT)
        await session._handle_function_call({**TOOL_ITEM, "call_id": "call_close_3"})
        session.on_tool_call = AsyncMock(return_value=SUCCESS_RESULT)
        await session._handle_function_call({**TOOL_ITEM, "call_id": "call_close_4"})

        assert len(_response_creates(ws)) == 1


# ---------------------------------------------------------------------------
# 4. Failed ticket creation does not hang up
# ---------------------------------------------------------------------------


class TestFailedTicketNoHangup:
    async def test_failed_ticket_keeps_tool_result_path(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            session.on_tool_call = AsyncMock(return_value=ERROR_RESULT)
            await session._handle_function_call(dict(TOOL_ITEM))

        creates = _response_creates(ws)
        assert len(creates) == 1
        # Failure keeps the generic tool_result path — no closing directive.
        assert "instructions" not in creates[0].get("response", {})
        assert CLOSING_MESSAGE not in str(creates[0])

        assert session.ticket_created is False
        assert session.closing_response_started is False
        assert _events_named(caplog, "closing_response_started") == []

        # Even later response activity never arms a hangup.
        await _finish_closing_response(session)
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_other", "status": "completed", "output": []},
        })
        if session._hangup_grace_task is not None:
            await session._hangup_grace_task
        on_closing_finished.assert_not_called()
        assert session.hangup_started is False

    async def test_handler_failure_does_not_log_ticket_created(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch("app.api.twilio.create_ticket", side_effect=RuntimeError("db down")):
            with caplog.at_level("INFO"):
                result = await handle_create_breakdown_ticket(
                    call_sid="CA_fail",
                    caller_phone="+15551234567",
                    arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                )

        assert result.status == "error"
        assert _events_named(caplog, "ticket_created") == []


# ---------------------------------------------------------------------------
# 6. Caller interruption during closing is handled safely
# ---------------------------------------------------------------------------


class TestCallerInterruption:
    async def test_barge_in_defers_hangup_until_followup_completes(
        self, no_grace: None
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        # Open the greeting gate so later caller commits take the normal path.
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_greeting", "status": "completed", "output": []},
        })
        ws.reset_mock()

        await _run_successful_ticket(session)
        await session._handle_event({
            "type": "response.created",
            "response": {"id": "resp_closing", "status": "in_progress"},
        })

        # Caller barges in while the closing message is playing.
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "cancelled", "output": []},
        })
        await session._handle_event({"type": "input_audio_buffer.speech_stopped"})

        assert session.closing_response_completed is False
        assert session.hangup_started is False
        on_closing_finished.assert_not_called()

        # Normal barge-in turn-taking still creates a response for the caller.
        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_barge",
        })
        creates = _response_creates(ws)
        assert len(creates) == 2  # closing + caller turn
        # The caller-turn response is a plain response.create, not a closing directive.
        assert "instructions" not in creates[-1].get("response", {})

        # Once the caller's follow-up turn finishes, the flow completes safely.
        await _finish_closing_response(session, response_id="resp_followup")
        assert session.closing_response_completed is True
        assert session._hangup_grace_task is not None
        await session._hangup_grace_task
        on_closing_finished.assert_called_once_with("CA_closing_test")

    async def test_hangup_deferred_while_caller_is_speaking(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_ticket(session)
            # Caller starts speaking right as the closing response completes.
            await session._handle_event({"type": "input_audio_buffer.speech_started"})
            await session._handle_event({
                "type": "response.created",
                "response": {"id": "resp_closing", "status": "in_progress"},
            })
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_closing", "status": "completed", "output": []},
            })

            # Closing completed but the caller is mid-speech: no grace task is
            # even armed, and no hangup fires while they talk.
            assert session.closing_response_completed is True
            assert session._hangup_grace_task is None
            assert session.hangup_started is False
            on_closing_finished.assert_not_called()
            assert _events_named(caplog, "hangup_deferred_caller_speaking")

            # Speech ends -> hangup is armed and completes after the grace period.
            await session._handle_event({"type": "input_audio_buffer.speech_stopped"})
            assert session._hangup_grace_task is not None
            await session._hangup_grace_task

        assert session.hangup_started is True
        on_closing_finished.assert_called_once()

    async def test_speech_started_does_not_cancel_closing_state(self) -> None:
        session = _make_session()
        await _connect_session(session)

        await _run_successful_ticket(session)
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        assert session.closing_response_started is True
        assert session.closing_response_completed is False
        assert session.hangup_started is False


# ---------------------------------------------------------------------------
# 7. Concurrent calls maintain independent closing state
# ---------------------------------------------------------------------------


class TestConcurrentClosingState:
    async def test_sessions_have_isolated_closing_state(self, no_grace: None) -> None:
        callback_a = AsyncMock()
        callback_b = AsyncMock()
        session_a = _make_session(
            call_sid="CA_concurrent_a",
            on_closing_finished=callback_a,
        )
        session_b = _make_session(
            call_sid="CA_concurrent_b",
            on_closing_finished=callback_b,
        )
        await _connect_session(session_a)
        await _connect_session(session_b)

        # Call A completes its full closing flow.
        await _run_successful_ticket(session_a)
        await _finish_closing_response(session_a)
        assert session_a._hangup_grace_task is not None
        await session_a._hangup_grace_task

        # Call B only reaches the closing response — no completion, no hangup.
        await _run_successful_ticket(session_b)

        assert session_a.hangup_started is True
        callback_a.assert_called_once_with("CA_concurrent_a")

        assert session_b.ticket_created is True
        assert session_b.closing_response_started is True
        assert session_b.closing_response_completed is False
        assert session_b.hangup_started is False
        callback_b.assert_not_called()

        # B finishes independently, targeting only B's call_sid.
        await _finish_closing_response(session_b)
        assert session_b._hangup_grace_task is not None
        await session_b._hangup_grace_task
        callback_b.assert_called_once_with("CA_concurrent_b")
        callback_a.assert_called_once()


# ---------------------------------------------------------------------------
# 8. Emergency behavior is unchanged
# ---------------------------------------------------------------------------


class TestEmergencyUnaffected:
    async def test_emergency_tool_does_not_start_closing(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
            await session._handle_function_call(dict(TRANSFER_ITEM))

        creates = _response_creates(ws)
        assert len(creates) == 1
        # Emergency keeps the generic tool_result path with no closing directive.
        assert "instructions" not in creates[0].get("response", {})
        assert CLOSING_MESSAGE not in str(creates[0])

        assert session.ticket_created is False
        assert session.closing_response_started is False
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()

    async def test_emergency_then_response_never_hangs_up(
        self, no_grace: None
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
        await session._handle_function_call(dict(TRANSFER_ITEM))
        await _finish_closing_response(session)
        if session._hangup_grace_task is not None:
            await session._hangup_grace_task

        on_closing_finished.assert_not_called()
        assert session.hangup_started is False

    async def test_transfer_suppresses_hangup_after_closing_completes(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Once the transfer tool is observed, the closing hangup never fires."""
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_ticket(session)
            session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
            await session._handle_function_call(dict(TRANSFER_ITEM))

            await _finish_closing_response(session)
            # Completion tries to arm, but the transfer flag suppresses it.
            assert session.closing_response_completed is True
            assert session._hangup_grace_task is None
            assert session.hangup_started is False
            on_closing_finished.assert_not_called()
            assert _events_named(caplog, "hangup_suppressed_transfer")

    async def test_transfer_in_same_response_as_closing_completion_never_hangs_up(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The race: closing completes and the transfer tool runs in one response.done."""
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        # Open the greeting gate.
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_greeting", "status": "completed", "output": []},
        })
        await _run_successful_ticket(session)
        await session._handle_event({
            "type": "response.created",
            "response": {"id": "resp_closing", "status": "in_progress"},
        })

        # Caller barges in with an emergency: closing is cancelled.
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "cancelled", "output": []},
        })
        await session._handle_event({"type": "input_audio_buffer.speech_stopped"})

        # The follow-up response both completes the closing flow AND carries the
        # emergency transfer function call. The closing completion arms the grace
        # task first; the transfer flag must still suppress the eventual hangup.
        session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
        with caplog.at_level("INFO"):
            await session._handle_event({
                "type": "response.done",
                "response": {
                    "id": "resp_follow",
                    "status": "completed",
                    "output": [dict(TRANSFER_ITEM)],
                },
            })

        assert session.closing_response_completed is True
        session.on_tool_call.assert_called_once()
        assert _events_named(caplog, "transfer_requested_hangup_suppressed")

        assert session._hangup_grace_task is not None
        await session._hangup_grace_task
        on_closing_finished.assert_not_called()
        assert session.hangup_started is False


# ---------------------------------------------------------------------------
# Twilio hangup handler (app.api.twilio.handle_closing_finished)
# ---------------------------------------------------------------------------


class TestHandleClosingFinished:
    @pytest.fixture(autouse=True)
    def _clean_call_manager(self) -> object:
        yield
        for sid in ("CA_active", "CA_gone", "CA_xfer"):
            call_manager.remove(sid)

    async def test_transferred_call_is_not_hung_up(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        call_manager.create(twilio_call_id="CA_xfer", caller_phone="+15550000002")
        state = call_manager.get("CA_xfer")
        assert state is not None
        state.transfer_state = "transferred"

        with patch("app.api.twilio.hangup_call", return_value=True) as mock_hangup:
            with caplog.at_level("INFO"):
                await handle_closing_finished("CA_xfer")

        mock_hangup.assert_not_called()
        assert _events_named(caplog, "call_hangup_skipped_transferred")
        assert _events_named(caplog, "call_hangup_started") == []

    async def test_active_call_is_hung_up(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        call_manager.create(twilio_call_id="CA_active", caller_phone="+15550000001")

        with patch("app.api.twilio.hangup_call", return_value=True) as mock_hangup:
            with caplog.at_level("INFO"):
                await handle_closing_finished("CA_active")

        mock_hangup.assert_called_once_with(call_sid="CA_active")
        started = _events_named(caplog, "call_hangup_started")
        completed = _events_named(caplog, "call_hangup_completed")
        assert len(started) == 1 and started[0]["call_sid"] == "CA_active"
        assert len(completed) == 1 and completed[0]["call_sid"] == "CA_active"

    async def test_natural_disconnect_skips_hangup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Call state already cleaned up -> the call ended naturally.
        with patch("app.api.twilio.hangup_call", return_value=True) as mock_hangup:
            with caplog.at_level("INFO"):
                await handle_closing_finished("CA_gone")

        mock_hangup.assert_not_called()
        assert _events_named(caplog, "call_hangup_skipped_call_ended")
        assert _events_named(caplog, "call_hangup_started") == []
        assert _events_named(caplog, "call_hangup_failed") == []

    async def test_hangup_failure_logs_warning_not_completed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        call_manager.create(twilio_call_id="CA_active", caller_phone="+15550000001")

        with patch("app.api.twilio.hangup_call", return_value=False):
            with caplog.at_level("INFO"):
                await handle_closing_finished("CA_active")

        assert _events_named(caplog, "call_hangup_started")
        assert _events_named(caplog, "call_hangup_completed") == []
        assert _events_named(caplog, "call_hangup_failed")

    async def test_empty_call_sid_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("app.api.twilio.hangup_call", return_value=True) as mock_hangup:
            with caplog.at_level("INFO"):
                await handle_closing_finished("")

        mock_hangup.assert_not_called()
        assert _events_named(caplog, "call_hangup_skipped_missing_call_sid")


# ---------------------------------------------------------------------------
# Twilio hangup service (app.services.hangup)
# ---------------------------------------------------------------------------


class TestHangupService:
    @patch("app.services.hangup.get_settings")
    @patch("app.services.hangup.TwilioClient")
    def test_hangup_sets_status_completed(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"
        mock_call = MagicMock()
        mock_call.status = "completed"
        mock_client_cls.return_value.calls.return_value.update.return_value = mock_call

        assert hangup_call(call_sid="CA_specific") is True

        mock_client_cls.assert_called_once_with("AC_test_sid", "test_auth_token")
        mock_client_cls.return_value.calls.assert_called_once_with("CA_specific")
        mock_client_cls.return_value.calls.return_value.update.assert_called_once_with(
            status="completed"
        )

    @patch("app.services.hangup.get_settings")
    @patch("app.services.hangup.TwilioClient")
    def test_hangup_exception_returns_false(
        self, mock_client_cls: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.TWILIO_ACCOUNT_SID = "AC_test_sid"
        mock_settings.return_value.TWILIO_AUTH_TOKEN = "test_auth_token"
        mock_client_cls.return_value.calls.return_value.update.side_effect = Exception(
            "Twilio API error"
        )

        assert hangup_call(call_sid="CA_failing") is False
