"""Tests for caller interruption stopping assistant speech immediately."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.twilio import _pending_connections, send_clear_to_twilio, twilio_media_stream
from app.realtime.session import RealtimeSession
from app.services.calls import EarlyConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(**kwargs: object) -> RealtimeSession:
    defaults: dict[str, object] = {
        "call_sid": "CA_interruption_test",
        "caller_phone": "+15551234567",
        "stream_sid": "MZ_interruption_test",
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
    with patch(
        "app.realtime.session.websockets.connect",
        new_callable=AsyncMock,
        return_value=ws,
    ):
        with patch("app.realtime.session.get_settings", return_value=_make_settings()):
            await session.connect()
    return ws


def _sent_events(ws: AsyncMock) -> list[dict]:
    return [json.loads(c[0][0]) for c in ws.send.call_args_list]


def _response_creates(ws: AsyncMock) -> list[dict]:
    return [e for e in _sent_events(ws) if e.get("type") == "response.create"]


def _response_cancels(ws: AsyncMock) -> list[dict]:
    return [e for e in _sent_events(ws) if e.get("type") == "response.cancel"]


def _events_named(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict]:
    return [
        json.loads(r.message)
        for r in caplog.records
        if json.loads(r.message).get("event") == event_name
    ]


async def _start_response(session: RealtimeSession, response_id: str) -> None:
    """Deliver response.created so the response is tracked as active."""
    await session._handle_event({
        "type": "response.created",
        "response": {"id": response_id, "status": "in_progress"},
    })


async def _finish_response(
    session: RealtimeSession,
    response_id: str,
    status: str = "completed",
) -> None:
    await session._handle_event({
        "type": "response.done",
        "response": {"id": response_id, "status": status, "output": []},
    })


async def _open_greeting_gate(session: RealtimeSession) -> None:
    """Open the greeting gate with a completed greeting response.done."""
    await _finish_response(session, "resp_greeting", "completed")


# ---------------------------------------------------------------------------
# Cancellation of the active OpenAI response
# ---------------------------------------------------------------------------


class TestActiveResponseCancellation:
    async def test_speech_started_cancels_active_response(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        await _start_response(session, "resp_active")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        cancels = _response_cancels(ws)
        assert len(cancels) == 1
        assert cancels[0]["response_id"] == "resp_active"

    async def test_speech_started_without_active_response_sends_no_cancel(
        self,
    ) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        assert _response_cancels(ws) == []

    async def test_interruption_logged_with_response_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_session(call_sid="CA_interruption_log")
        await _connect_session(session)

        await _start_response(session, "resp_log")
        with caplog.at_level("INFO"):
            await session._handle_event({"type": "input_audio_buffer.speech_started"})

        entries = _events_named(caplog, "caller_interruption_detected")
        assert len(entries) == 1
        assert entries[0]["call_sid"] == "CA_interruption_log"
        assert entries[0]["response_id"] == "resp_log"
        assert entries[0]["response_cancel_sent"] is True

    async def test_cancel_send_failure_is_reported_as_not_sent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_session(call_sid="CA_cancel_fail")
        ws = await _connect_session(session)
        ws.send.side_effect = RuntimeError("send failed")

        await _start_response(session, "resp_fail")
        with caplog.at_level("INFO"):
            await session._handle_event({"type": "input_audio_buffer.speech_started"})

        entries = _events_named(caplog, "caller_interruption_detected")
        assert len(entries) == 1
        assert entries[0]["response_cancel_sent"] is False
        assert session._interrupted_response_id == "resp_fail"

    async def test_repeated_interruptions_send_single_cancel(self) -> None:
        on_clear_playback = AsyncMock()
        session = _make_session(on_clear_playback=on_clear_playback)
        ws = await _connect_session(session)

        await _start_response(session, "resp_repeat")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        cancels = _response_cancels(ws)
        assert len(cancels) == 1
        assert cancels[0]["response_id"] == "resp_repeat"
        assert on_clear_playback.call_count == 3
        assert session._interrupted_response_id == "resp_repeat"


# ---------------------------------------------------------------------------
# Cancellation of a response whose response.create is still in flight
# ---------------------------------------------------------------------------


class TestCreateInFlightCancellation:
    async def test_speech_started_cancels_create_in_flight_response(self) -> None:
        session = _make_session(greeting="Hello there!")
        ws = await _connect_session(session)
        assert len(_response_creates(ws)) == 1

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        assert _response_cancels(ws) == []

        await _start_response(session, "resp_greeting_in_flight")

        cancels = _response_cancels(ws)
        assert len(cancels) == 1
        assert cancels[0]["response_id"] == "resp_greeting_in_flight"

    async def test_in_flight_cancel_is_logged_with_pending_response_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_session(greeting="Hello there!", call_sid="CA_in_flight_log")
        await _connect_session(session)

        with caplog.at_level("INFO"):
            await session._handle_event({"type": "input_audio_buffer.speech_started"})
            await _start_response(session, "resp_in_flight_log")

        entries = _events_named(caplog, "caller_interruption_detected")
        assert len(entries) == 1
        assert entries[0]["call_sid"] == "CA_in_flight_log"
        assert entries[0]["response_id"] == "resp_in_flight_log"
        assert entries[0]["response_cancel_sent"] is True
        assert entries[0]["create_in_flight"] is True

    async def test_in_flight_cancelled_response_drops_audio_deltas(self) -> None:
        on_audio_delta = AsyncMock()
        session = _make_session(greeting="Hello there!", on_audio_delta=on_audio_delta)
        await _connect_session(session)

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _start_response(session, "resp_greeting_drop")
        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_stale",
        })

        on_audio_delta.assert_not_awaited()
        assert session._interrupted_response_id == "resp_greeting_drop"

        await _finish_response(session, "resp_greeting_drop", "cancelled")
        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_next",
        })
        on_audio_delta.assert_awaited_once_with("audio_next")

    async def test_repeated_speech_before_created_sends_single_cancel(self) -> None:
        on_clear_playback = AsyncMock()
        session = _make_session(greeting="Hello there!", on_clear_playback=on_clear_playback)
        ws = await _connect_session(session)

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _start_response(session, "resp_in_flight_once")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        cancels = _response_cancels(ws)
        assert len(cancels) == 1
        assert cancels[0]["response_id"] == "resp_in_flight_once"
        assert on_clear_playback.call_count == 3

    async def test_no_cancel_when_no_create_is_pending(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)
        await _open_greeting_gate(session)

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _start_response(session, "resp_untracked")

        assert _response_cancels(ws) == []

    async def test_server_side_create_failure_sends_no_cancel(self) -> None:
        session = _make_session(greeting="Hello there!")
        ws = await _connect_session(session)

        await session._handle_event({
            "type": "error",
            "error": {"code": "invalid_response_create", "message": "create failed"},
        })
        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        assert _response_cancels(ws) == []

    async def test_create_failure_after_speech_started_sends_no_cancel(self) -> None:
        session = _make_session(greeting="Hello there!")
        ws = await _connect_session(session)

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({
            "type": "error",
            "error": {"code": "invalid_response_create", "message": "create failed"},
        })
        await _start_response(session, "resp_after_failure")

        assert _response_cancels(ws) == []

    async def test_failed_create_send_does_not_arm_pending_cancel(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)
        await _open_greeting_gate(session)

        ws.send.side_effect = RuntimeError("send failed")
        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_send_failure",
        })
        ws.send.side_effect = None

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _start_response(session, "resp_unsent")

        assert _response_cancels(ws) == []

    async def test_foreign_done_does_not_disarm_in_flight_cancel(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)
        await _open_greeting_gate(session)

        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_first_turn",
        })
        await _start_response(session, "resp_first")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        assert len(_response_cancels(ws)) == 1

        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_second_turn",
        })
        await _finish_response(session, "resp_first", "cancelled")

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _start_response(session, "resp_second")

        cancels = _response_cancels(ws)
        assert len(cancels) == 2
        assert cancels[1]["response_id"] == "resp_second"

    async def test_in_flight_cancel_targets_closing_response_keeping_state(self) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        session.on_tool_call = AsyncMock(
            return_value='{"status": "confirmed", "assistance_request_id": "tkt_1"}'
        )
        await session._handle_function_call({
            "call_id": "call_close_in_flight",
            "type": "function_call",
            "name": "confirm_assistance_request",
            "arguments": "{}",
        })
        assert len(_response_creates(ws)) == 1

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        assert _response_cancels(ws) == []

        await _start_response(session, "resp_closing_in_flight")

        cancels = _response_cancels(ws)
        assert len(cancels) == 1
        assert cancels[0]["response_id"] == "resp_closing_in_flight"
        assert session.closing_response_id == "resp_closing_in_flight"
        assert session.closing_response_started is True
        assert session.closing_response_completed is False
        assert session.hangup_started is False

        await _finish_response(session, "resp_closing_in_flight", "cancelled")
        assert session.closing_response_completed is False
        assert session.hangup_started is False


# ---------------------------------------------------------------------------
# Twilio playback clearing
# ---------------------------------------------------------------------------


class TestTwilioPlaybackClearing:
    async def test_speech_started_clears_playback_during_active_response(
        self,
    ) -> None:
        on_clear_playback = AsyncMock()
        session = _make_session(on_clear_playback=on_clear_playback)
        await _connect_session(session)

        await _start_response(session, "resp_clear")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        on_clear_playback.assert_awaited_once()

    async def test_speech_started_clears_buffered_tail_without_active_response(
        self,
    ) -> None:
        on_clear_playback = AsyncMock()
        session = _make_session(on_clear_playback=on_clear_playback)
        await _connect_session(session)

        await _start_response(session, "resp_tail")
        await _finish_response(session, "resp_tail", "completed")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        on_clear_playback.assert_awaited_once()

    async def test_speech_started_without_clear_callback_does_not_raise(
        self,
    ) -> None:
        session = _make_session(on_clear_playback=None)
        await _connect_session(session)

        await session._handle_event({"type": "input_audio_buffer.speech_started"})

    async def test_send_clear_sends_twilio_clear_message(self) -> None:
        ws = AsyncMock()

        await send_clear_to_twilio(ws, stream_sid="MZ_clear_1", call_sid="CA_clear_1")

        ws.send_json.assert_awaited_once_with({
            "event": "clear",
            "streamSid": "MZ_clear_1",
        })

    async def test_send_clear_logs_telemetry(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = AsyncMock()

        with caplog.at_level("INFO"):
            await send_clear_to_twilio(ws, stream_sid="MZ_clear_2", call_sid="CA_clear_2")

        entries = _events_named(caplog, "twilio_playback_cleared")
        assert len(entries) == 1
        assert entries[0]["call_sid"] == "CA_clear_2"
        assert entries[0]["stream_sid"] == "MZ_clear_2"

    async def test_send_clear_failure_logs_warning_without_raising(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = AsyncMock()
        ws.send_json.side_effect = RuntimeError("websocket gone")

        with caplog.at_level("WARNING"):
            await send_clear_to_twilio(ws, stream_sid="MZ_clear_3", call_sid="CA_clear_3")

        assert any(
            "Failed to clear Twilio playback" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Stale audio suppression after interruption
# ---------------------------------------------------------------------------


class TestStaleAudioSuppression:
    async def test_audio_dropped_after_interruption_until_response_done(self) -> None:
        on_audio_delta = AsyncMock()
        session = _make_session(on_audio_delta=on_audio_delta)
        await _connect_session(session)

        await _start_response(session, "resp_audio")
        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_before",
        })
        on_audio_delta.assert_awaited_once_with("audio_before")

        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_stale",
        })
        on_audio_delta.assert_awaited_once_with("audio_before")

        await _finish_response(session, "resp_audio", "cancelled")
        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_after_done",
        })
        on_audio_delta.assert_awaited_with("audio_after_done")
        assert on_audio_delta.await_count == 2

    async def test_audio_flows_for_new_response_after_interruption(self) -> None:
        on_audio_delta = AsyncMock()
        session = _make_session(on_audio_delta=on_audio_delta)
        await _connect_session(session)

        await _start_response(session, "resp_old")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _start_response(session, "resp_new")
        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_new",
        })

        on_audio_delta.assert_awaited_once_with("audio_new")
        assert session._interrupted_response_id is None

    async def test_audio_flows_normally_without_interruption(self) -> None:
        on_audio_delta = AsyncMock()
        session = _make_session(on_audio_delta=on_audio_delta)
        await _connect_session(session)

        await _start_response(session, "resp_quiet")
        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_normal",
        })

        on_audio_delta.assert_awaited_once_with("audio_normal")


# ---------------------------------------------------------------------------
# No duplicate assistant responses after interruption
# ---------------------------------------------------------------------------


class TestNoDuplicateResponses:
    async def test_interruption_then_commit_creates_exactly_one_response(
        self,
    ) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        await _open_greeting_gate(session)
        await _start_response(session, "resp_turn_a")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _finish_response(session, "resp_turn_a", "cancelled")
        assert _response_creates(ws) == []

        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_turn",
        })

        creates = _response_creates(ws)
        assert len(creates) == 1

        await _finish_response(session, "resp_turn_a", "cancelled")
        assert len(_response_creates(ws)) == 1

    async def test_greeting_interruption_creates_exactly_one_response_for_turn(
        self,
    ) -> None:
        session = _make_session(greeting="Hello there!")
        ws = await _connect_session(session)
        assert len(_response_creates(ws)) == 1

        await _start_response(session, "resp_greeting")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_interrupted_greeting",
        })
        await _finish_response(session, "resp_greeting", "cancelled")

        creates = _response_creates(ws)
        assert len(creates) == 2
        assert "instructions" not in creates[-1].get("response", {})

        await session._handle_event({
            "type": "input_audio_buffer.speech_started",
        })
        assert len(_response_creates(ws)) == 2

    async def test_repeated_interruptions_do_not_corrupt_turn_taking(
        self,
    ) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        await _open_greeting_gate(session)
        await _start_response(session, "resp_first")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _finish_response(session, "resp_first", "cancelled")

        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_one",
        })
        assert len(_response_creates(ws)) == 1

        await _start_response(session, "resp_second")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _finish_response(session, "resp_second", "cancelled")
        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_two",
        })

        assert len(_response_creates(ws)) == 2
        assert session._interrupted_response_id is None
        assert session._active_response_id is None


# ---------------------------------------------------------------------------
# Greeting and closing state machine compatibility
# ---------------------------------------------------------------------------


class TestStateMachineCompatibility:
    async def test_interruption_targets_closing_response_without_mutating_state(
        self,
    ) -> None:
        session = _make_session()
        ws = await _connect_session(session)

        session.on_tool_call = AsyncMock(
            return_value='{"status": "confirmed", "assistance_request_id": "tkt_1"}'
        )
        await session._handle_function_call({
            "call_id": "call_close",
            "type": "function_call",
            "name": "confirm_assistance_request",
            "arguments": "{}",
        })
        await _start_response(session, "resp_closing")

        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        cancels = _response_cancels(ws)
        assert len(cancels) == 1
        assert cancels[0]["response_id"] == "resp_closing"
        assert session.closing_response_started is True
        assert session.closing_response_completed is False
        assert session.hangup_started is False

        await _finish_response(session, "resp_closing", "cancelled")
        assert session.closing_response_completed is False
        assert session.hangup_started is False

    async def test_speech_started_does_not_open_greeting_gate(self) -> None:
        session = _make_session(greeting="Hello there!")
        await _connect_session(session)

        await _start_response(session, "resp_greeting")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})

        assert session._greeting_response_done is False

    async def test_cancelled_greeting_done_still_opens_gate_exactly_once(self) -> None:
        session = _make_session(greeting="Hello there!")
        ws = await _connect_session(session)

        await _start_response(session, "resp_greeting")
        await session._handle_event({"type": "input_audio_buffer.speech_started"})
        await _finish_response(session, "resp_greeting", "cancelled")

        assert session._greeting_response_done is True
        assert len(_response_creates(ws)) == 1

        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_after_cancel",
        })
        assert len(_response_creates(ws)) == 2

    async def test_interrupted_state_is_isolated_per_session(self) -> None:
        on_audio_a = AsyncMock()
        on_audio_b = AsyncMock()
        session_a = _make_session(
            call_sid="CA_iso_a",
            caller_phone="+15550000001",
            on_audio_delta=on_audio_a,
        )
        session_b = _make_session(
            call_sid="CA_iso_b",
            caller_phone="+15550000002",
            on_audio_delta=on_audio_b,
        )
        await _connect_session(session_a)
        await _connect_session(session_b)

        await _start_response(session_a, "resp_iso_a")
        await _start_response(session_b, "resp_iso_b")
        await session_a._handle_event({"type": "input_audio_buffer.speech_started"})

        await session_a._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_a",
        })
        await session_b._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_b",
        })

        on_audio_a.assert_not_awaited()
        on_audio_b.assert_awaited_once_with("audio_b")
        assert session_b._interrupted_response_id is None


# ---------------------------------------------------------------------------
# Media Stream handler wiring of on_clear_playback (both construction paths)
# ---------------------------------------------------------------------------


class _FakeTwilioWebSocket:
    """Minimal WebSocket stand-in driving the twilio_media_stream handler."""

    def __init__(self, incoming: list[dict]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict:
        if not self._incoming:
            raise RuntimeError("no more incoming messages")
        return self._incoming.pop(0)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _media_stream_messages(*, call_sid: str, stream_sid: str) -> list[dict]:
    return [
        {"event": "connected"},
        {
            "event": "start",
            "start": {
                "streamSid": stream_sid,
                "callSid": call_sid,
                "customParameters": {
                    "call_sid": call_sid,
                    "caller_phone": "+15551234567",
                },
            },
        },
        {"event": "stop"},
    ]


def _make_handler_session_mock() -> MagicMock:
    session = MagicMock()
    session.connect = AsyncMock()
    session.close = AsyncMock()
    session.send_audio = AsyncMock()
    session.process_events = AsyncMock()
    session.set_stream_sid_and_greet = AsyncMock()
    session.is_connected = True
    session.latency_tracker = MagicMock()
    return session


class TestClearPlaybackHandlerWiring:
    """Pin on_clear_playback wiring on both twilio_media_stream paths.

    The session-level tests prove the interruption behavior, but the audible
    fix only works if the handler attaches the Twilio clear callback — these
    tests fail if either wiring site is removed.
    """

    async def test_fallback_path_wires_clear_playback_and_sends_clear(self) -> None:
        fake_ws = _FakeTwilioWebSocket(
            _media_stream_messages(call_sid="CA_wiring_fb", stream_sid="MZ_wiring_fb")
        )
        session_mock = _make_handler_session_mock()

        with patch("app.api.twilio.RealtimeSession") as mock_session_cls:
            mock_session_cls.return_value = session_mock
            with patch("app.api.twilio.abandon_if_open"):
                await twilio_media_stream(fake_ws)  # type: ignore[arg-type]

        kwargs = mock_session_cls.call_args.kwargs
        callback = kwargs.get("on_clear_playback")
        assert callback is not None
        assert callable(callback)

        await callback()
        assert {"event": "clear", "streamSid": "MZ_wiring_fb"} in fake_ws.sent

    async def test_early_path_wires_clear_playback_and_sends_clear(self) -> None:
        call_sid = "CA_wiring_early"
        session_mock = _make_handler_session_mock()

        async def _immediate_connect() -> MagicMock:
            return session_mock

        task = asyncio.create_task(_immediate_connect())
        _pending_connections[call_sid] = EarlyConnection(
            call_sid=call_sid,
            caller_phone="+15551234567",
            connection_task=task,
        )

        fake_ws = _FakeTwilioWebSocket(
            _media_stream_messages(call_sid=call_sid, stream_sid="MZ_wiring_early")
        )
        try:
            with patch("app.api.twilio.abandon_if_open"):
                await twilio_media_stream(fake_ws)  # type: ignore[arg-type]
        finally:
            _pending_connections.pop(call_sid, None)

        callback = session_mock.on_clear_playback
        assert callable(callback)
        assert not isinstance(callback, MagicMock)

        await callback()
        assert {"event": "clear", "streamSid": "MZ_wiring_early"} in fake_ws.sent
