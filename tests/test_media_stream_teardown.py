"""Tests for Media Stream teardown finalizing the intake status.

Drives the real ``twilio_media_stream`` handler over a WebSocket and asserts
that the ``finally`` block abandons still-open assistance requests without
raising, covering mid-intake disconnects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _make_session_mock() -> MagicMock:
    session = MagicMock()
    session.connect = AsyncMock()
    session.close = AsyncMock()
    session.send_audio = AsyncMock()
    session.process_events = AsyncMock()
    session.is_connected = True
    session.latency_tracker = MagicMock()
    return session


def _start_event(*, call_sid: str, stream_sid: str = "MS_test") -> dict:
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


@patch("app.api.twilio.abandon_if_open")
@patch("app.api.twilio.RealtimeSession")
def test_teardown_abandons_open_row_on_disconnect(
    mock_session_cls: MagicMock,
    mock_abandon: MagicMock,
) -> None:
    """A mid-intake disconnect flips the still-open row to abandoned."""
    mock_session_cls.return_value = _make_session_mock()

    with client.websocket_connect("/api/v1/twilio/media-stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json(_start_event(call_sid="CA_teardown_1"))
    # Context exit closes the socket → WebSocketDisconnect → finally runs.

    mock_abandon.assert_called_once_with(call_id="CA_teardown_1")


@patch("app.api.twilio.abandon_if_open")
def test_teardown_without_start_skips_abandon(
    mock_abandon: MagicMock,
) -> None:
    """No 'start' event means no call_sid, so no status write is attempted."""
    with client.websocket_connect("/api/v1/twilio/media-stream") as ws:
        ws.send_json({"event": "connected"})

    mock_abandon.assert_not_called()


@patch("app.api.twilio.abandon_if_open")
@patch("app.api.twilio.RealtimeSession")
def test_teardown_abandons_when_session_connect_fails(
    mock_session_cls: MagicMock,
    mock_abandon: MagicMock,
) -> None:
    """A stream that fails during OpenAI connect still finalizes on teardown."""
    session = _make_session_mock()
    session.connect = AsyncMock(side_effect=RuntimeError("openai unreachable"))
    mock_session_cls.return_value = session

    with client.websocket_connect("/api/v1/twilio/media-stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json(_start_event(call_sid="CA_teardown_fail"))

    mock_abandon.assert_called_once_with(call_id="CA_teardown_fail")


@patch("app.api.twilio.abandon_if_open", side_effect=RuntimeError("DB down"))
@patch("app.api.twilio.RealtimeSession")
def test_teardown_never_raises_when_finalizer_fails(
    mock_session_cls: MagicMock,
    mock_abandon: MagicMock,
) -> None:
    """A failing finalizer must not crash teardown or the WebSocket handler."""
    mock_session_cls.return_value = _make_session_mock()

    # If abandon_if_open raised out of finally, the handler would error the
    # connection and this block would surface the failure.
    with client.websocket_connect("/api/v1/twilio/media-stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json(_start_event(call_sid="CA_teardown_raise"))

    mock_abandon.assert_called_once_with(call_id="CA_teardown_raise")
