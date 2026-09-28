"""Tests for the end-of-call closing flow after successful intake completion."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.twilio import (
    EarlyConnection,
    _pending_connections,
    _send_media_stream_mark,
    handle_closing_finished,
    handle_confirm_assistance_request,
    handle_update_assistance_request,
)
from app.main import app
from app.realtime.instructions import CLOSING_MESSAGE
from app.realtime.session import (
    CLOSING_HANGUP_GRACE_SECONDS,
    CLOSING_MARK_TIMEOUT_SECONDS,
    RealtimeSession,
)
from app.services.calls import call_manager
from app.services.hangup import hangup_call


@pytest.fixture(autouse=True)
def _mock_complete_intake() -> Generator[None]:
    """Keep handler-driven tests off the real database."""
    with patch("app.api.twilio.complete_intake"):
        yield

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOL_ITEM = {
    "call_id": "call_close_1",
    "name": "update_assistance_request",
    "arguments": '{"location": "Main St", "vehicle": "Honda", "issue": "Flat tire"}',
}
CONFIRM_ITEM = {
    "call_id": "call_close_1",
    "name": "confirm_assistance_request",
    "arguments": "{}",
}
READY_RESULT = '{"status": "ready_for_confirmation", "assistance_request_id": "tkt_abc"}'
CONFIRM_RESULT = '{"status": "confirmed", "assistance_request_id": "tkt_abc"}'
ERROR_RESULT = '{"status": "error", "error": "Unable to save the assistance request"}'
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
    session = RealtimeSession(**defaults)  # type: ignore[arg-type]
    # Default to immediately acknowledging the Twilio playback mark so tests
    # that are not specifically about mark timing stay deterministic. Tests
    # about mark behavior pass their own on_closing_mark_requested.
    if "on_closing_mark_requested" not in kwargs:

        async def _auto_ack_mark(mark_name: str) -> None:
            session.acknowledge_closing_mark(mark_name)

        session.on_closing_mark_requested = _auto_ack_mark
    return session


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


async def _pump_hangup_task() -> None:
    """Advance the event loop enough for a runnable hangup task to finish.

    A correct implementation parks in the bounded mark wait during these
    yields; an implementation that skips the wait would complete the hangup
    (and any suppressed/deferred early returns) before this returns, so
    subsequent not-called assertions actually prove the gating.
    """
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


async def _run_ready_intake(session: RealtimeSession) -> None:
    """Drive an update_assistance_request save that makes the request ready."""
    session.on_tool_call = AsyncMock(return_value=READY_RESULT)
    await session._handle_function_call(dict(TOOL_ITEM))


async def _run_successful_intake(session: RealtimeSession) -> None:
    """Drive the confirm_assistance_request call that completes the intake."""
    session.on_tool_call = AsyncMock(return_value=CONFIRM_RESULT)
    await session._handle_function_call(dict(CONFIRM_ITEM))


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

    def test_mark_timeout_is_bounded(self) -> None:
        # A missing mark must fall back safely, never leaving the call open
        # indefinitely — but the bound must also cover the full closing playback.
        assert 5.0 <= CLOSING_MARK_TIMEOUT_SECONDS <= 60.0


# ---------------------------------------------------------------------------
# 1. Successful confirmation triggers the exact closing message
# ---------------------------------------------------------------------------


class TestClosingResponseTriggered:
    async def test_confirmed_intake_sends_exact_closing_message(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        await _run_successful_intake(session)

        creates = _response_creates(ws)
        assert len(creates) == 1
        instructions = creates[0]["response"]["instructions"]
        assert CLOSING_MESSAGE in instructions
        # The directive must not permit extra conversation after the closing line.
        assert "anything else" not in instructions
        assert "?" not in instructions
        assert "say nothing after" in instructions or "do not say anything after" in instructions

    async def test_confirmed_intake_sends_post_intake_closing_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_session(call_sid="CA_reason_log")
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_intake(session)

        entries = [
            e
            for e in _events_named(caplog, "response_create_sent")
            if e.get("reason") == "post_intake_closing"
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
            await _run_successful_intake(session)

        assert session.intake_completed is True
        assert session.closing_response_started is True
        assert session.closing_response_completed is False
        assert session.hangup_started is False

        started = _events_named(caplog, "closing_response_started")
        assert len(started) == 1
        assert started[0]["call_sid"] == "CA_flags"
        assert started[0]["reason"] == "post_intake_closing"
        assert started[0]["assistance_request_id"] == "tkt_abc"

    async def test_confirmation_logged_by_handler(self, caplog: pytest.LogCaptureFixture) -> None:
        mock_row = {
            "id": "ticket-log-1",
            "call_id": "CA_log",
            "location": "A",
            "vehicle": "B",
            "issue": "C",
            "intake_status": "in_progress",
            "notification_status": "pending",
        }
        with patch("app.api.twilio.get_assistance_request", return_value=mock_row):
            with patch("app.api.twilio.claim_notification_status", return_value=True):
                with patch("app.api.twilio.notify_dispatcher"):
                    with caplog.at_level("INFO"):
                        result = await handle_confirm_assistance_request(
                            call_sid="CA_log",
                            caller_phone="+15551234567",
                        )

        assert result.status == "confirmed"
        entries = _events_named(caplog, "assistance_request_confirmed")
        assert len(entries) == 1
        assert entries[0]["call_sid"] == "CA_log"
        assert entries[0]["assistance_request_id"] == "ticket-log-1"

    async def test_saving_three_fields_does_not_start_closing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Saving all three fields reports readiness; only confirmation closes."""
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_ready_intake(session)

        creates = _response_creates(ws)
        assert len(creates) == 1
        # A normal tool_result turn lets the model summarize — no closing directive.
        assert "instructions" not in creates[0].get("response", {})
        assert CLOSING_MESSAGE not in str(creates[0])

        assert session.intake_completed is False
        assert session.closing_response_started is False
        assert _events_named(caplog, "closing_response_started") == []
        on_closing_finished.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Hangup does not occur before the closing response finishes
# ---------------------------------------------------------------------------


class TestHangupWaitsForClosing:
    async def test_no_hangup_immediately_after_confirmation(
        self, no_grace: None
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        await _run_successful_intake(session)

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

        await _run_successful_intake(session)
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
            await _run_successful_intake(session)
            await _finish_closing_response(session, response_id="resp_done_1")
            assert session._hangup_grace_task is not None
            await session._hangup_grace_task

        entries = _events_named(caplog, "closing_response_completed")
        assert len(entries) == 1
        assert entries[0]["call_sid"] == "CA_completed_log"
        assert entries[0]["response_id"] == "resp_done_1"
        assert entries[0]["assistance_request_id"] == "tkt_abc"

    async def test_unobserved_closing_id_fails_safe(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Without the closing response.created, no unrelated done may hang up."""
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_intake(session)
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
            reason="post_intake_closing",
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

        await _run_successful_intake(session)
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

    async def test_duplicate_confirmation_closes_only_once(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(on_closing_finished=on_closing_finished)
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_intake(session)
            # Duplicate/retried confirmation with the same successful result.
            session.on_tool_call = AsyncMock(return_value=CONFIRM_RESULT)
            await session._handle_function_call({**CONFIRM_ITEM, "call_id": "call_close_2"})

        creates = _response_creates(ws)
        assert len(creates) == 1
        assert CLOSING_MESSAGE in creates[0]["response"]["instructions"]
        assert _events_named(caplog, "duplicate_assistance_request_confirmation_ignored")
        assert len(_events_named(caplog, "closing_response_started")) == 1

        await _finish_closing_response(session)
        assert session._hangup_grace_task is not None
        await session._hangup_grace_task
        on_closing_finished.assert_called_once()

    async def test_update_after_confirmation_starts_no_extra_response(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A post-completion save is ignored too — the closing owns the turn."""
        session = _make_session()
        ws = await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_intake(session)
            session.on_tool_call = AsyncMock(return_value=CONFIRM_RESULT)
            await session._handle_function_call({**CONFIRM_ITEM, "call_id": "call_close_3"})
            session.on_tool_call = AsyncMock(return_value=READY_RESULT)
            await session._handle_function_call({**TOOL_ITEM, "call_id": "call_close_4"})

        assert len(_response_creates(ws)) == 1
        assert _events_named(caplog, "duplicate_assistance_request_tool_call_ignored")


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

        assert session.intake_completed is False
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

    async def test_handler_failure_logs_no_completion_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch("app.api.twilio.create_ticket", side_effect=RuntimeError("db down")):
            with caplog.at_level("INFO"):
                result = await handle_update_assistance_request(
                    call_sid="CA_fail",
                    caller_phone="+15551234567",
                    arguments='{"location": "A", "vehicle": "B", "issue": "C"}',
                )

        assert result.status == "error"
        assert _events_named(caplog, "assistance_request_ready_for_confirmation") == []
        assert _events_named(caplog, "assistance_request_confirmed") == []


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

        await _run_successful_intake(session)
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
        await session._handle_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_barge",
            "transcript": "Wait, one more thing",
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
            await _run_successful_intake(session)
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

        await _run_successful_intake(session)
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        assert session.closing_response_started is True
        assert session.closing_response_completed is False
        assert session.hangup_started is False


# ---------------------------------------------------------------------------
# 6b. Hangup waits for the Twilio closing playback mark
# ---------------------------------------------------------------------------


class TestClosingPlaybackMark:
    async def test_no_mark_requested_before_closing_completes(self, no_grace: None) -> None:
        mark_names: list[str] = []

        async def _capture(mark_name: str) -> None:
            mark_names.append(mark_name)

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_capture,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        await _run_successful_intake(session)
        await session._handle_event({
            "type": "response.created",
            "response": {"id": "resp_closing", "status": "in_progress"},
        })

        assert mark_names == []
        assert session._hangup_grace_task is None
        on_closing_finished.assert_not_called()

    async def test_hangup_waits_for_mark_acknowledgment(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        mark_names: list[str] = []

        async def _capture(mark_name: str) -> None:
            mark_names.append(mark_name)

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_capture,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_intake(session)
            await _finish_closing_response(session)
            assert session._hangup_grace_task is not None

            # Let the hangup task run: it must stay parked at the mark wait
            # instead of hanging up on response.done alone.
            await _pump_hangup_task()
            assert len(mark_names) == 1
            assert mark_names[0].startswith(f"closing-{session.call_sid}-")
            on_closing_finished.assert_not_called()
            assert session.hangup_started is False
            assert _events_named(caplog, "closing_playback_mark_acknowledged") == []
            assert _events_named(caplog, "closing_playback_mark_timeout") == []

            # The acknowledgment unblocks the wait (a broken ack path would
            # hang until the bounded timeout instead, failing the log asserts).
            session.acknowledge_closing_mark(mark_names[0])
            await asyncio.wait_for(session._hangup_grace_task, timeout=5.0)

        on_closing_finished.assert_called_once_with("CA_closing_test")
        assert session.hangup_started is True
        assert _events_named(caplog, "closing_playback_mark_acknowledged")
        assert _events_named(caplog, "closing_playback_mark_timeout") == []

    async def test_missing_mark_falls_back_after_bounded_timeout(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _never_ack(mark_name: str) -> None:
            """Mark is 'sent' but Twilio never echoes it back."""

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_never_ack,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        with patch("app.realtime.session.CLOSING_MARK_TIMEOUT_SECONDS", 0.05):
            with caplog.at_level("INFO"):
                await _run_successful_intake(session)
                await _finish_closing_response(session)
                assert session._hangup_grace_task is not None
                await session._hangup_grace_task

        assert session.hangup_started is True
        on_closing_finished.assert_called_once_with("CA_closing_test")
        assert _events_named(caplog, "closing_playback_mark_timeout")
        assert _events_named(caplog, "closing_playback_mark_acknowledged") == []

    async def test_missing_mark_callback_falls_back_via_timeout(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=None,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        with patch("app.realtime.session.CLOSING_MARK_TIMEOUT_SECONDS", 0.05):
            with caplog.at_level("WARNING"):
                await _run_successful_intake(session)
                await _finish_closing_response(session)
                assert session._hangup_grace_task is not None
                await session._hangup_grace_task

        assert _events_named(caplog, "closing_playback_mark_callback_missing")
        assert _events_named(caplog, "closing_playback_mark_timeout")
        on_closing_finished.assert_called_once()

    async def test_mark_callback_error_logged_and_falls_back(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _boom(mark_name: str) -> None:
            raise RuntimeError("websocket write failed")

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_boom,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        with patch("app.realtime.session.CLOSING_MARK_TIMEOUT_SECONDS", 0.05):
            with caplog.at_level("WARNING"):
                await _run_successful_intake(session)
                await _finish_closing_response(session)
                assert session._hangup_grace_task is not None
                await asyncio.wait_for(session._hangup_grace_task, timeout=5.0)

        assert _events_named(caplog, "closing_playback_mark_callback_errored")
        assert _events_named(caplog, "closing_playback_mark_timeout")
        assert _events_named(caplog, "closing_playback_mark_send_failed") == []
        on_closing_finished.assert_called_once()

    async def test_stale_mark_acknowledgment_is_ignored(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        mark_names: list[str] = []

        async def _capture(mark_name: str) -> None:
            mark_names.append(mark_name)

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_capture,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        await _run_successful_intake(session)
        await _finish_closing_response(session)
        await _pump_hangup_task()
        assert len(mark_names) == 1

        # A foreign/stale mark name must not unblock the wait: even if it did,
        # the pump below would let a broken implementation reach hangup.
        with caplog.at_level("DEBUG"):
            session.acknowledge_closing_mark("closing-someone-else-99")
            await _pump_hangup_task()
        on_closing_finished.assert_not_called()
        assert session.hangup_started is False
        assert _events_named(caplog, "closing_playback_mark_unexpected")

        assert session._hangup_grace_task is not None
        session.acknowledge_closing_mark(mark_names[0])
        await asyncio.wait_for(session._hangup_grace_task, timeout=5.0)
        on_closing_finished.assert_called_once()

    async def test_caller_speaking_during_mark_wait_defers_hangup(
        self, no_grace: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        mark_names: list[str] = []

        async def _capture(mark_name: str) -> None:
            mark_names.append(mark_name)

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_capture,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await _run_successful_intake(session)
            await _finish_closing_response(session)
            await _pump_hangup_task()
            assert len(mark_names) == 1

            # Caller starts speaking while the mark wait is in flight. The pump
            # above proves a non-waiting implementation would already have hung
            # up before the speech_started flag was set.
            await session._handle_event({"type": "input_audio_buffer.speech_started"})

            # Mark acks, but the re-check must defer the hangup mid-speech.
            assert session._hangup_grace_task is not None
            session.acknowledge_closing_mark(mark_names[0])
            await session._hangup_grace_task
            on_closing_finished.assert_not_called()
            assert session.hangup_started is False
            assert _events_named(caplog, "hangup_deferred_caller_speaking")

            # Speech ends -> a fresh mark is requested and the hangup completes.
            await session._handle_event({"type": "input_audio_buffer.speech_stopped"})
            assert session._hangup_grace_task is not None
            await asyncio.sleep(0)
            assert len(mark_names) == 2
            assert mark_names[1] != mark_names[0]

            session.acknowledge_closing_mark(mark_names[1])
            await session._hangup_grace_task

        assert session.hangup_started is True
        on_closing_finished.assert_called_once_with("CA_closing_test")

    async def test_late_mark_ack_after_hangup_is_harmless(self, no_grace: None) -> None:
        mark_names: list[str] = []

        async def _capture(mark_name: str) -> None:
            mark_names.append(mark_name)

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_capture,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        await _run_successful_intake(session)
        await _finish_closing_response(session)
        await asyncio.sleep(0)
        assert session._hangup_grace_task is not None
        session.acknowledge_closing_mark(mark_names[0])
        await session._hangup_grace_task
        on_closing_finished.assert_called_once()

        # Repeated acks and re-arm attempts must not produce a second hangup.
        session.acknowledge_closing_mark(mark_names[0])
        session._maybe_arm_hangup()
        if session._hangup_grace_task is not None:
            await session._hangup_grace_task
        on_closing_finished.assert_called_once()

    async def test_close_cancels_pending_mark_wait(self) -> None:
        mark_names: list[str] = []

        async def _capture(mark_name: str) -> None:
            mark_names.append(mark_name)

        on_closing_finished = AsyncMock()
        session = _make_session(
            on_closing_mark_requested=_capture,
            on_closing_finished=on_closing_finished,
        )
        await _connect_session(session)

        await _run_successful_intake(session)
        await _finish_closing_response(session)
        await _pump_hangup_task()
        assert len(mark_names) == 1

        task = session._hangup_grace_task
        assert task is not None
        assert not task.done()

        # Teardown while parked at the mark wait must cancel the hangup task
        # so it can never fire after the call has ended.
        await session.close()

        assert session._hangup_grace_task is None
        assert session._closing_mark_event is None
        assert session._closing_mark_name is None
        with pytest.raises(asyncio.CancelledError):
            await task
        on_closing_finished.assert_not_called()
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
        await _run_successful_intake(session_a)
        await _finish_closing_response(session_a)
        assert session_a._hangup_grace_task is not None
        await session_a._hangup_grace_task

        # Call B only reaches the closing response — no completion, no hangup.
        await _run_successful_intake(session_b)

        assert session_a.hangup_started is True
        callback_a.assert_called_once_with("CA_concurrent_a")

        assert session_b.intake_completed is True
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

        assert session.intake_completed is False
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
            await _run_successful_intake(session)
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
        await _run_successful_intake(session)
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
# Twilio Media Streams mark bridge (send + receive)
# ---------------------------------------------------------------------------


def _make_session_mock() -> MagicMock:
    session = MagicMock()
    session.connect = AsyncMock()
    session.close = AsyncMock()
    session.send_audio = AsyncMock()
    session.process_events = AsyncMock()
    session.set_stream_sid_and_greet = AsyncMock()
    session.is_connected = True
    session.latency_tracker = MagicMock()
    session.acknowledge_closing_mark = MagicMock()
    return session


def _start_event(*, call_sid: str, stream_sid: str = "MS_mark") -> dict:
    return {
        "event": "start",
        "start": {
            "streamSid": stream_sid,
            "callSid": call_sid,
            "customParameters": {
                "call_sid": call_sid,
                "caller_phone": "+15551234567",
            },
        },
    }


class TestMediaStreamMarkBridge:
    async def test_mark_message_shape(self) -> None:
        ws = AsyncMock()
        await _send_media_stream_mark(
            ws, stream_sid="MZ_1", mark_name="closing-CA_1-1", call_sid="CA_1"
        )
        ws.send_json.assert_awaited_once_with({
            "event": "mark",
            "streamSid": "MZ_1",
            "mark": {"name": "closing-CA_1-1"},
        })

    async def test_mark_skipped_without_stream(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = AsyncMock()
        with caplog.at_level("WARNING"):
            await _send_media_stream_mark(
                ws, stream_sid=None, mark_name="closing-CA_1-1", call_sid="CA_1"
            )
        ws.send_json.assert_not_awaited()
        assert _events_named(caplog, "closing_playback_mark_skipped_no_stream")

    async def test_mark_send_failure_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = AsyncMock()
        ws.send_json.side_effect = RuntimeError("socket closed")
        with caplog.at_level("WARNING"):
            await _send_media_stream_mark(
                ws, stream_sid="MZ_1", mark_name="closing-CA_1-1", call_sid="CA_1"
            )
        assert _events_named(caplog, "closing_playback_mark_send_failed")

    @patch("app.api.twilio.abandon_if_open")
    @patch("app.api.twilio.RealtimeSession")
    def test_fallback_path_attaches_mark_callback_and_routes_inbound_mark(
        self,
        mock_session_cls: MagicMock,
        mock_abandon: MagicMock,
    ) -> None:
        client = TestClient(app)
        session = _make_session_mock()
        mock_session_cls.return_value = session

        with client.websocket_connect("/api/v1/twilio/media-stream") as ws:
            ws.send_json({"event": "connected"})
            ws.send_json(_start_event(call_sid="CA_mark_fb"))
            # Events are processed in order, so the mark is handled only after
            # the session was constructed with its mark callback.
            ws.send_json({"event": "mark", "mark": {"name": "closing-CA_mark_fb-1"}})
            ws.send_json({"event": "stop"})

        # Assert only after the handler has finished processing the queue.
        callback = mock_session_cls.call_args.kwargs.get("on_closing_mark_requested")
        assert callable(callback)
        session.acknowledge_closing_mark.assert_called_once_with("closing-CA_mark_fb-1")

    @patch("app.api.twilio.abandon_if_open")
    def test_early_path_attaches_mark_callback_and_routes_inbound_mark(
        self, mock_abandon: MagicMock
    ) -> None:
        client = TestClient(app)
        session = _make_session_mock()

        async def _immediate() -> object:
            return session

        # A completed task can be awaited from the handler's event loop without
        # touching the loop that created it, which lets the sync TestClient
        # drive the early-connection reuse path.
        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(_immediate())
            loop.run_until_complete(task)
        finally:
            loop.close()
        assert task.done()

        _pending_connections["CA_early_mark"] = EarlyConnection(
            call_sid="CA_early_mark",
            caller_phone="+15551234567",
            connection_task=task,  # type: ignore[arg-type]
        )
        try:
            with client.websocket_connect("/api/v1/twilio/media-stream") as ws:
                ws.send_json({"event": "connected"})
                ws.send_json(_start_event(call_sid="CA_early_mark"))
                # The mark is processed only after the early-path callback assignment.
                ws.send_json({
                    "event": "mark",
                    "mark": {"name": "closing-CA_early_mark-1"},
                })
                ws.send_json({"event": "stop"})
        finally:
            _pending_connections.pop("CA_early_mark", None)

        # The early path assigns the real send-closing-mark closure.
        assert inspect.iscoroutinefunction(session.on_closing_mark_requested)
        session.acknowledge_closing_mark.assert_called_once_with("closing-CA_early_mark-1")

    @patch("app.api.twilio.abandon_if_open")
    def test_inbound_mark_before_start_is_ignored(
        self, mock_abandon: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = TestClient(app)
        with caplog.at_level("ERROR"):
            with client.websocket_connect("/api/v1/twilio/media-stream") as ws:
                ws.send_json({"event": "connected"})
                # No session yet: a stray mark must not crash the handler.
                ws.send_json({"event": "mark", "mark": {"name": "closing-unknown-1"}})
                ws.send_json({"event": "stop"})

        mock_abandon.assert_not_called()
        assert _events_named(caplog, "Media Stream error") == []


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
