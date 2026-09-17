"""Tests for the OpenAI Realtime session manager."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets.exceptions

from app.realtime.session import RealtimeSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(**kwargs: object) -> RealtimeSession:
    """Create a RealtimeSession with sensible defaults."""
    defaults: dict[str, object] = {
        "call_sid": "CA_test_call_sid",
        "caller_phone": "+15551234567",
        "stream_sid": "MZ_test_stream_sid",
    }
    defaults.update(kwargs)
    return RealtimeSession(**defaults)  # type: ignore[arg-type]


def _make_settings() -> MagicMock:
    """Return a mock Settings object with required fields."""
    settings = MagicMock()
    settings.OPENAI_API_KEY = "sk-test-key"
    settings.OPENAI_REALTIME_MODEL = "gpt-4o-realtime-preview"
    return settings


def _make_ws() -> AsyncMock:
    """Return an async mock WebSocket connection."""
    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()

    async def _empty_aiter() -> object:
        return iter([])

    ws.__aiter__ = MagicMock(side_effect=_empty_aiter)
    return ws


async def _connect_session(
    session: RealtimeSession,
    ws: AsyncMock | None = None,
) -> AsyncMock:
    """Connect a session with a mocked WebSocket and return the ws mock."""
    if ws is None:
        ws = _make_ws()
    with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
        with patch("app.realtime.session.get_settings", return_value=_make_settings()):
            await session.connect()
    return ws


# ---------------------------------------------------------------------------
# Session setup
# ---------------------------------------------------------------------------


class TestSessionSetup:
    """Tests for RealtimeSession.connect() and _configure_session()."""

    async def test_connect_creates_websocket_with_correct_url(self) -> None:
        session = _make_session()
        ws = _make_ws()
        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws) as mock_connect:
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                await session.connect()

        mock_connect.assert_called_once()
        call_args = mock_connect.call_args
        url = call_args[0][0]
        assert url.startswith("wss://api.openai.com/v1/realtime")
        assert "model=gpt-4o-realtime-preview" in url

    async def test_connect_sets_authorization_header(self) -> None:
        session = _make_session()
        ws = _make_ws()
        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws) as mock_connect:
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                await session.connect()

        call_kwargs = mock_connect.call_args[1]
        headers = call_kwargs["additional_headers"]
        assert headers["Authorization"] == "Bearer sk-test-key"

    async def test_connect_sets_ping_parameters(self) -> None:
        session = _make_session()
        ws = _make_ws()
        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws) as mock_connect:
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                await session.connect()

        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs["ping_interval"] == 20
        assert call_kwargs["ping_timeout"] == 10
        assert call_kwargs["close_timeout"] == 5

    async def test_connect_sets_connected_flag(self) -> None:
        session = _make_session()
        assert session.is_connected is False
        await _connect_session(session)
        assert session.is_connected is True

    async def test_connect_sends_session_update(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)

        ws.send.assert_called()
        first_call_json = ws.send.call_args_list[0][0][0]
        event = json.loads(first_call_json)
        assert event["type"] == "session.update"
        session_config = event["session"]
        assert session_config["type"] == "realtime"
        assert session_config["output_modalities"] == ["audio"]
        assert session_config["audio"]["input"]["format"]["type"] == "audio/pcmu"
        assert "rate" not in session_config["audio"]["input"]["format"]
        assert session_config["audio"]["output"]["voice"] == "marin"
        assert session_config["audio"]["input"]["turn_detection"]["type"] == "server_vad"

    async def test_connect_includes_instructions_when_provided(self) -> None:
        session = _make_session(instructions="Be helpful.")
        ws = _make_ws()
        await _connect_session(session, ws)

        first_call_json = ws.send.call_args_list[0][0][0]
        event = json.loads(first_call_json)
        assert event["session"]["instructions"] == "Be helpful."

    async def test_connect_excludes_instructions_when_empty(self) -> None:
        session = _make_session(instructions="")
        ws = _make_ws()
        await _connect_session(session, ws)

        first_call_json = ws.send.call_args_list[0][0][0]
        event = json.loads(first_call_json)
        assert "instructions" not in event["session"]

    async def test_connect_includes_tools_when_provided(self) -> None:
        tools = [{"type": "function", "name": "my_tool", "parameters": {}}]
        session = _make_session(tools=tools)
        ws = _make_ws()
        await _connect_session(session, ws)

        first_call_json = ws.send.call_args_list[0][0][0]
        event = json.loads(first_call_json)
        assert event["session"]["tools"] == tools
        assert event["session"]["tool_choice"] == "auto"

    async def test_connect_excludes_tools_when_empty(self) -> None:
        session = _make_session(tools=[])
        ws = _make_ws()
        await _connect_session(session, ws)

        first_call_json = ws.send.call_args_list[0][0][0]
        event = json.loads(first_call_json)
        assert "tools" not in event["session"]
        assert "tool_choice" not in event["session"]

    async def test_connect_sends_greeting_when_provided(self) -> None:
        session = _make_session(greeting="Hello there!")
        ws = _make_ws()
        await _connect_session(session, ws)

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" in types
        assert "response.create" in types

        item_create = next(e for e in sent_events if e["type"] == "conversation.item.create")
        assert item_create["item"]["type"] == "message"
        assert item_create["item"]["role"] == "assistant"
        assert item_create["item"]["content"][0]["type"] == "output_text"
        assert item_create["item"]["content"][0]["text"] == "Hello there!"

    async def test_connect_skips_greeting_when_empty(self) -> None:
        session = _make_session(greeting="")
        ws = _make_ws()
        await _connect_session(session, ws)

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" not in types
        assert "response.create" not in types

    async def test_connect_skips_greeting_by_default(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" not in types


# ---------------------------------------------------------------------------
# Audio forwarding
# ---------------------------------------------------------------------------


class TestAudioForwarding:
    """Tests for send_audio()."""

    async def test_send_audio_sends_correct_event(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)
        ws.reset_mock()

        await session.send_audio("base64_audio_data")

        ws.send.assert_called_once()
        event = json.loads(ws.send.call_args[0][0])
        assert event["type"] == "input_audio_buffer.append"
        assert event["audio"] == "base64_audio_data"

    async def test_send_audio_noop_when_not_connected(self) -> None:
        session = _make_session()
        await session.send_audio("base64_audio_data")
        # Should not raise — _send returns early when _ws is None


# ---------------------------------------------------------------------------
# Model audio forwarding
# ---------------------------------------------------------------------------


class TestModelAudioForwarding:
    """Tests for on_audio_delta callback dispatch."""

    async def test_audio_delta_calls_callback(self) -> None:
        on_audio_delta = AsyncMock()
        session = _make_session(on_audio_delta=on_audio_delta)
        await _connect_session(session)

        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_chunk_123",
        })

        on_audio_delta.assert_called_once_with("audio_chunk_123")

    async def test_audio_delta_skips_empty_delta(self) -> None:
        on_audio_delta = AsyncMock()
        session = _make_session(on_audio_delta=on_audio_delta)
        await _connect_session(session)

        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "",
        })

        on_audio_delta.assert_not_called()

    async def test_audio_delta_noop_when_no_callback(self) -> None:
        session = _make_session(on_audio_delta=None)
        await _connect_session(session)

        await session._handle_event({
            "type": "response.output_audio.delta",
            "delta": "audio_chunk_123",
        })
        # Should not raise


# ---------------------------------------------------------------------------
# Tool call events
# ---------------------------------------------------------------------------


class TestToolCallEvents:
    """Tests for _handle_function_call() and response.done dispatch."""

    async def test_tool_call_invokes_callback(self) -> None:
        on_tool_call = AsyncMock(return_value='{"status": "created"}')
        session = _make_session(on_tool_call=on_tool_call)
        ws = _make_ws()
        await _connect_session(session, ws)

        await session._handle_function_call({
            "call_id": "call_abc",
            "name": "create_breakdown_ticket",
            "arguments": '{"location": "Main St", "vehicle": "Honda", "issue": "Flat tire"}',
        })

        on_tool_call.assert_called_once_with(
            "call_abc",
            "create_breakdown_ticket",
            '{"location": "Main St", "vehicle": "Honda", "issue": "Flat tire"}',
        )

    async def test_tool_call_sends_result_and_response_create(self) -> None:
        on_tool_call = AsyncMock(return_value='{"status": "created"}')
        session = _make_session(on_tool_call=on_tool_call)
        ws = _make_ws()
        await _connect_session(session, ws)
        ws.reset_mock()

        await session._handle_function_call({
            "call_id": "call_abc",
            "name": "create_breakdown_ticket",
            "arguments": "{}",
        })

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" in types
        assert "response.create" in types

        item_create = next(e for e in sent_events if e["type"] == "conversation.item.create")
        assert item_create["item"]["type"] == "function_call_output"
        assert item_create["item"]["call_id"] == "call_abc"
        assert item_create["item"]["output"] == '{"status": "created"}'

    async def test_tool_call_no_handler_returns_error(self) -> None:
        session = _make_session(on_tool_call=None)
        ws = _make_ws()
        await _connect_session(session, ws)
        ws.reset_mock()

        await session._handle_function_call({
            "call_id": "call_abc",
            "name": "create_breakdown_ticket",
            "arguments": "{}",
        })

        item_create_event = next(
            e for e in (json.loads(c[0][0]) for c in ws.send.call_args_list)
            if e["type"] == "conversation.item.create"
        )
        output = json.loads(item_create_event["item"]["output"])
        assert "error" in output
        assert "No tool handler registered" in output["error"]

    async def test_tool_call_handler_exception_returns_error(self) -> None:
        on_tool_call = AsyncMock(side_effect=RuntimeError("DB down"))
        session = _make_session(on_tool_call=on_tool_call)
        ws = _make_ws()
        await _connect_session(session, ws)
        ws.reset_mock()

        await session._handle_function_call({
            "call_id": "call_abc",
            "name": "create_breakdown_ticket",
            "arguments": "{}",
        })

        item_create_event = next(
            e for e in (json.loads(c[0][0]) for c in ws.send.call_args_list)
            if e["type"] == "conversation.item.create"
        )
        output = json.loads(item_create_event["item"]["output"])
        assert "error" in output
        assert "DB down" in output["error"]

    async def test_response_done_dispatches_function_calls(self) -> None:
        on_tool_call = AsyncMock(return_value='{"status": "created"}')
        session = _make_session(on_tool_call=on_tool_call)
        ws = _make_ws()
        await _connect_session(session, ws)

        await session._handle_event({
            "type": "response.done",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "create_breakdown_ticket",
                        "arguments": '{"location":"A","vehicle":"B","issue":"C"}',
                    },
                ],
            },
        })

        on_tool_call.assert_called_once()

    async def test_response_done_ignores_non_function_output(self) -> None:
        on_tool_call = AsyncMock()
        session = _make_session(on_tool_call=on_tool_call)
        ws = _make_ws()
        await _connect_session(session, ws)

        await session._handle_event({
            "type": "response.done",
            "response": {
                "output": [
                    {"type": "message", "role": "assistant", "content": "Hello"},
                ],
            },
        })

        on_tool_call.assert_not_called()


# ---------------------------------------------------------------------------
# Session errors
# ---------------------------------------------------------------------------


class TestSessionErrors:
    """Tests for error event handling and WebSocket error recovery."""

    async def test_error_event_calls_on_error(self) -> None:
        on_error = AsyncMock()
        session = _make_session(on_error=on_error)
        await _connect_session(session)

        await session._handle_event({
            "type": "error",
            "error": {"code": "rate_limit_exceeded", "message": "Too many requests"},
        })

        on_error.assert_called_once()
        exc = on_error.call_args[0][0]
        assert isinstance(exc, RuntimeError)
        assert "rate_limit_exceeded" in str(exc)
        assert "Too many requests" in str(exc)

    async def test_error_event_noop_when_no_callback(self) -> None:
        session = _make_session(on_error=None)
        await _connect_session(session)

        await session._handle_event({
            "type": "error",
            "error": {"code": "unknown", "message": "Something went wrong"},
        })
        # Should not raise

    async def test_send_handles_connection_closed(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)

        ws.send.side_effect = websockets.exceptions.ConnectionClosed(None, None)

        await session.send_audio("audio_data")
        assert session.is_connected is False

    async def test_process_events_handles_connection_closed(self) -> None:
        on_error = AsyncMock()
        session = _make_session(on_error=on_error)
        ws = _make_ws()
        await _connect_session(session, ws)

        ws.__aiter__ = MagicMock(
            side_effect=websockets.exceptions.ConnectionClosed(None, None)
        )

        await session.process_events()
        assert session.is_connected is False
        on_error.assert_not_called()

    async def test_process_events_handles_general_exception(self) -> None:
        on_error = AsyncMock()
        session = _make_session(on_error=on_error)
        ws = _make_ws()
        await _connect_session(session, ws)

        ws.__aiter__ = MagicMock(side_effect=RuntimeError("unexpected"))

        await session.process_events()
        assert session.is_connected is False
        on_error.assert_called_once()
        assert isinstance(on_error.call_args[0][0], RuntimeError)

    async def test_process_events_raises_when_ws_is_none(self) -> None:
        session = _make_session()
        with pytest.raises(RuntimeError, match="WebSocket not connected"):
            await session.process_events()

    async def test_send_returns_early_when_ws_is_none(self) -> None:
        session = _make_session()
        await session.send_audio("audio_data")
        # Should not raise

    async def test_send_returns_early_when_not_connected(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)
        ws.reset_mock()
        session._connected = False

        await session.send_audio("audio_data")
        ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Disconnect cleanup
# ---------------------------------------------------------------------------


class TestDisconnectCleanup:
    """Tests for close() and cleanup behavior."""

    async def test_close_sets_disconnected(self) -> None:
        session = _make_session()
        await _connect_session(session)
        assert session.is_connected is True

        await session.close()
        assert session.is_connected is False

    async def test_close_closes_websocket(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)

        await session.close()
        ws.close.assert_called_once()

    async def test_close_clears_ws_reference(self) -> None:
        session = _make_session()
        await _connect_session(session)
        assert session._ws is not None

        await session.close()
        assert session._ws is None

    async def test_close_noop_when_ws_is_none(self) -> None:
        session = _make_session()
        await session.close()
        # Should not raise

    async def test_close_handles_exception_during_ws_close(self) -> None:
        session = _make_session()
        ws = _make_ws()
        ws.close.side_effect = RuntimeError("close failed")
        await _connect_session(session, ws)

        await session.close()
        assert session.is_connected is False
        assert session._ws is None

    async def test_process_events_finally_sets_disconnected(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)

        ws.__aiter__ = MagicMock(
            side_effect=websockets.exceptions.ConnectionClosed(None, None)
        )

        await session.process_events()
        assert session.is_connected is False


# ---------------------------------------------------------------------------
# Event dispatching
# ---------------------------------------------------------------------------


class TestEventDispatching:
    """Tests for _handle_event() routing of different event types."""

    async def test_session_created_event_logs(self) -> None:
        session = _make_session()
        await _connect_session(session)
        # Should not raise
        await session._handle_event({"type": "session.created"})

    async def test_session_updated_event_logs(self) -> None:
        session = _make_session()
        await _connect_session(session)
        await session._handle_event({"type": "session.updated"})

    async def test_lifecycle_events_are_handled(self) -> None:
        session = _make_session()
        await _connect_session(session)

        lifecycle_types = [
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "response.created",
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
            "response.output_audio.done",
        ]
        for event_type in lifecycle_types:
            await session._handle_event({"type": event_type})

    async def test_unhandled_event_type_does_not_raise(self) -> None:
        session = _make_session()
        await _connect_session(session)
        await session._handle_event({"type": "some.future.event"})
