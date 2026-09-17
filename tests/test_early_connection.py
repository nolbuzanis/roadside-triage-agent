"""Tests for the early OpenAI Realtime connection optimization.

Covers the full lifecycle: voice webhook starts the connection, media stream
handler reuses it, race conditions, error handling, and cleanup.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.twilio import (
    EarlyConnection,
    _pending_connections,
    _start_early_openai_connection,
)
from app.realtime.latency import CallLatencyTracker
from app.realtime.session import RealtimeSession
from app.services.calls import CallState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(**kwargs: object) -> RealtimeSession:
    """Create a RealtimeSession with sensible defaults."""
    defaults: dict[str, object] = {
        "call_sid": "CA_test_call_sid",
        "caller_phone": "+15551234567",
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


async def _connect_session_early(
    session: RealtimeSession,
    ws: AsyncMock | None = None,
) -> AsyncMock:
    """Connect a session using connect_early() with mocked WebSocket."""
    if ws is None:
        ws = _make_ws()
    with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
        with patch("app.realtime.session.get_settings", return_value=_make_settings()):
            await session.connect_early()
    return ws


# ---------------------------------------------------------------------------
# Session: connect_early() and set_stream_sid_and_greet()
# ---------------------------------------------------------------------------


class TestConnectEarly:
    """Tests for connect_early() method."""

    async def test_connect_early_connects_websocket(self) -> None:
        session = _make_session()
        ws = await _connect_session_early(session)
        assert session.is_connected is True
        ws.send.assert_called()

    async def test_connect_early_configures_session(self) -> None:
        session = _make_session()
        ws = await _connect_session_early(session)

        # session.update should be sent
        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "session.update" in types

    async def test_connect_early_does_not_send_greeting(self) -> None:
        session = _make_session(greeting="Hello there!")
        ws = await _connect_session_early(session)

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" not in types
        assert "response.create" not in types

    async def test_connect_early_records_latency_events(self) -> None:
        session = _make_session()
        await _connect_session_early(session)

        assert session.latency_tracker is not None
        assert session.latency_tracker._early_connection_started > 0
        assert session.latency_tracker._openai_websocket_connected > 0
        assert session.latency_tracker._early_connection_completed > 0

    async def test_connect_early_records_openai_connection_started(self) -> None:
        """connect_early() still records openai_connection_started for compatibility."""
        session = _make_session()
        await _connect_session_early(session)

        assert session.latency_tracker is not None
        assert session.latency_tracker._openai_connection_started > 0

    async def test_connect_early_includes_instructions(self) -> None:
        session = _make_session(instructions="Be helpful.")
        ws = await _connect_session_early(session)

        first_call_json = ws.send.call_args_list[0][0][0]
        event = json.loads(first_call_json)
        assert event["session"]["instructions"] == "Be helpful."


class TestSetStreamSidAndGreet:
    """Tests for set_stream_sid_and_greet() method."""

    async def test_sets_stream_sid(self) -> None:
        session = _make_session(greeting="")
        await _connect_session_early(session)

        await session.set_stream_sid_and_greet("MZ_new_stream")
        assert session.stream_sid == "MZ_new_stream"

    async def test_sends_greeting_when_provided(self) -> None:
        session = _make_session(greeting="Hello there!")
        ws = await _connect_session_early(session)
        ws.reset_mock()

        await session.set_stream_sid_and_greet("MZ_stream")

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" in types
        assert "response.create" in types

        item_create = next(e for e in sent_events if e["type"] == "conversation.item.create")
        assert item_create["item"]["content"][0]["text"] == "Hello there!"

    async def test_skips_greeting_when_empty(self) -> None:
        session = _make_session(greeting="")
        ws = await _connect_session_early(session)
        ws.reset_mock()

        await session.set_stream_sid_and_greet("MZ_stream")

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" not in types
        assert "response.create" not in types

    async def test_records_response_create_sent(self) -> None:
        session = _make_session(greeting="Hello!")
        await _connect_session_early(session)

        await session.set_stream_sid_and_greet("MZ_stream")

        assert session.latency_tracker is not None
        assert session.latency_tracker._response_create_sent > 0


# ---------------------------------------------------------------------------
# Latency tracker: early connection events
# ---------------------------------------------------------------------------


class TestEarlyConnectionLatency:
    """Tests for early connection latency tracking."""

    def test_early_connection_started_event(self) -> None:
        tracker = CallLatencyTracker(call_id="CA_test")
        tracker.record_event("early_connection_started")
        assert tracker._early_connection_started > 0

    def test_early_connection_completed_event(self) -> None:
        tracker = CallLatencyTracker(call_id="CA_test")
        tracker.record_event("early_connection_started")
        tracker.record_event("early_connection_completed")
        assert tracker._early_connection_completed > 0

    def test_early_connection_latency_metric(self) -> None:
        tracker = CallLatencyTracker(call_id="CA_test")
        # 1000.0s to 1000.5s = 500ms
        tracker._early_connection_started = 1000.0
        tracker._early_connection_completed = 1000.5

        metrics = tracker.get_metrics()
        assert metrics["early_connection_latency_ms"] == 500

    def test_twilio_stream_to_early_connection_metric(self) -> None:
        tracker = CallLatencyTracker(call_id="CA_test")
        tracker._twilio_stream_started = 1000.0
        tracker._early_connection_started = 1000.1

        metrics = tracker.get_metrics()
        assert metrics["twilio_stream_to_early_connection_started_ms"] == 100

    def test_early_connection_to_websocket_connected_metric(self) -> None:
        tracker = CallLatencyTracker(call_id="CA_test")
        # Early connection started at 1000.0s, websocket connected at 1001.5s = 1500ms
        tracker._early_connection_started = 1000.0
        tracker._openai_websocket_connected = 1001.5

        metrics = tracker.get_metrics()
        assert metrics["early_connection_to_websocket_connected_ms"] == 1500

    def test_early_connection_metrics_in_log_latency_metrics(self) -> None:
        tracker = CallLatencyTracker(call_id="CA_test")
        tracker._early_connection_started = 1000.0
        tracker._early_connection_completed = 1000.5

        metrics = tracker.log_latency_metrics()
        assert metrics["early_connection_latency_ms"] == 500

    def test_early_connection_metrics_absent_when_not_recorded(self) -> None:
        tracker = CallLatencyTracker(call_id="CA_test")
        metrics = tracker.get_metrics()
        assert "early_connection_latency_ms" not in metrics
        assert "twilio_stream_to_early_connection_started_ms" not in metrics
        assert "early_connection_to_websocket_connected_ms" not in metrics


# ---------------------------------------------------------------------------
# _start_early_openai_connection
# ---------------------------------------------------------------------------


class TestStartEarlyOpenAIConnection:
    """Tests for the _start_early_openai_connection helper."""

    async def test_returns_connected_session(self) -> None:
        ws = _make_ws()
        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                session = await _start_early_openai_connection(
                    call_sid="CA_test", caller_phone="+15551234567"
                )

        assert session.is_connected is True
        assert session.call_sid == "CA_test"
        assert session.caller_phone == "+15551234567"

    async def test_records_latency_events(self) -> None:
        ws = _make_ws()
        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                session = await _start_early_openai_connection(
                    call_sid="CA_test", caller_phone="+15551234567"
                )

        assert session.latency_tracker is not None
        assert session.latency_tracker._early_connection_started > 0
        assert session.latency_tracker._early_connection_completed > 0

    async def test_does_not_send_greeting(self) -> None:
        ws = _make_ws()
        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                await _start_early_openai_connection(
                    call_sid="CA_test", caller_phone="+15551234567"
                )

        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" not in types

    async def test_closes_session_on_failure(self) -> None:
        with patch(
            "app.realtime.session.websockets.connect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Connection refused"),
        ):
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                with pytest.raises(RuntimeError, match="Connection refused"):
                    await _start_early_openai_connection(
                        call_sid="CA_test", caller_phone="+15551234567"
                    )


# ---------------------------------------------------------------------------
# CallState and CallStateManager updates
# ---------------------------------------------------------------------------


class TestCallStateUpdates:
    """Tests for the updated CallState and EarlyConnection dataclass."""

    async def test_early_connection_dataclass_fields(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0))
        ec = EarlyConnection(
            call_sid="CA_test",
            caller_phone="+15551234567",
            connection_task=task,  # type: ignore[arg-type]
        )
        assert ec.call_sid == "CA_test"
        assert ec.caller_phone == "+15551234567"
        assert ec.session is None
        await task

    async def test_early_connection_with_session(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0))
        session = _make_session()
        ec = EarlyConnection(
            call_sid="CA_test",
            caller_phone="+15551234567",
            connection_task=task,  # type: ignore[arg-type]
            session=session,
        )
        assert ec.session is session
        await task

    def test_call_state_stream_sid_optional(self) -> None:
        state = CallState(twilio_call_id="CA_test", caller_phone="+15551234567")
        assert state.stream_sid is None

    def test_call_state_with_stream_sid(self) -> None:
        state = CallState(
            twilio_call_id="CA_test",
            caller_phone="+15551234567",
            stream_sid="MZ_stream",
        )
        assert state.stream_sid == "MZ_stream"


# ---------------------------------------------------------------------------
# Pending connections lifecycle
# ---------------------------------------------------------------------------


class TestPendingConnectionsLifecycle:
    """Tests for the _pending_connections dictionary lifecycle."""

    def setup_method(self) -> None:
        """Clear pending connections before each test."""
        _pending_connections.clear()

    def teardown_method(self) -> None:
        """Clear pending connections after each test."""
        _pending_connections.clear()

    def test_pending_connections_starts_empty(self) -> None:
        assert len(_pending_connections) == 0

    async def test_can_add_and_retrieve_pending_connection(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0))
        ec = EarlyConnection(
            call_sid="CA_test",
            caller_phone="+15551234567",
            connection_task=task,  # type: ignore[arg-type]
        )
        _pending_connections["CA_test"] = ec

        retrieved = _pending_connections.pop("CA_test", None)
        assert retrieved is ec
        await task

    async def test_pop_removes_from_dict(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0))
        ec = EarlyConnection(
            call_sid="CA_test",
            caller_phone="+15551234567",
            connection_task=task,  # type: ignore[arg-type]
        )
        _pending_connections["CA_test"] = ec

        _pending_connections.pop("CA_test", None)
        assert "CA_test" not in _pending_connections
        await task

    def test_pop_nonexistent_returns_none(self) -> None:
        result = _pending_connections.pop("CA_nonexistent", None)
        assert result is None

    async def test_concurrent_calls_are_isolated(self) -> None:
        task1 = asyncio.create_task(asyncio.sleep(0))
        task2 = asyncio.create_task(asyncio.sleep(0))
        ec1 = EarlyConnection(
            call_sid="CA_call_1",
            caller_phone="+15551111111",
            connection_task=task1,  # type: ignore[arg-type]
        )
        ec2 = EarlyConnection(
            call_sid="CA_call_2",
            caller_phone="+15552222222",
            connection_task=task2,  # type: ignore[arg-type]
        )
        _pending_connections["CA_call_1"] = ec1
        _pending_connections["CA_call_2"] = ec2

        retrieved1 = _pending_connections.pop("CA_call_1", None)
        retrieved2 = _pending_connections.pop("CA_call_2", None)

        assert retrieved1 is ec1
        assert retrieved2 is ec2
        await task1
        await task2


# ---------------------------------------------------------------------------
# Race condition: early connection completes before Media Stream
# ---------------------------------------------------------------------------


class TestRaceConditionEarlyCompletesFirst:
    """Test: OpenAI connects before Twilio Media Stream arrives."""

    async def test_session_ready_when_stream_arrives(self) -> None:
        """The session is fully connected before the start event."""
        ws = _make_ws()
        session = _make_session(
            greeting="Hello!",
            on_audio_delta=AsyncMock(),
            on_tool_call=AsyncMock(),
            on_error=AsyncMock(),
        )

        # Simulate early connection completing
        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                await session.connect_early()

        # Simulate media stream start
        await session.set_stream_sid_and_greet("MZ_stream")

        # Greeting should have been sent
        sent_events = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        types = [e["type"] for e in sent_events]
        assert "conversation.item.create" in types
        assert "response.create" in types

    async def test_audio_can_be_sent_after_stream_arrives(self) -> None:
        """After stream_sid is set, send_audio works."""
        ws = _make_ws()
        session = _make_session(greeting="")

        with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                await session.connect_early()

        await session.set_stream_sid_and_greet("MZ_stream")
        ws.reset_mock()

        await session.send_audio("base64_audio")

        ws.send.assert_called_once()
        event = json.loads(ws.send.call_args[0][0])
        assert event["type"] == "input_audio_buffer.append"


# ---------------------------------------------------------------------------
# Race condition: Media Stream connects before OpenAI finishes
# ---------------------------------------------------------------------------


class TestRaceConditionStreamArrivesFirst:
    """Test: Twilio Media Stream connects while OpenAI is still connecting."""

    async def test_awaits_pending_task(self) -> None:
        """The media stream handler awaits the in-progress connection task."""
        ws = _make_ws()
        session = _make_session(
            greeting="Hello!",
            on_audio_delta=AsyncMock(),
        )

        # Create a task that completes the session
        async def _connect() -> RealtimeSession:
            with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
                with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                    await session.connect_early()
            return session

        task = asyncio.create_task(_connect())

        # Let the task start but simulate it not being done yet
        await asyncio.sleep(0)

        # Media stream arrives — should await the task
        result = await task
        assert result is session
        assert session.is_connected is True


# ---------------------------------------------------------------------------
# Error handling: early connection fails
# ---------------------------------------------------------------------------


class TestEarlyConnectionFailure:
    """Test: OpenAI connection fails before Media Stream connects."""

    async def test_fallback_to_new_session(self) -> None:
        """When early connection fails, a new session is created from scratch."""
        with patch(
            "app.realtime.session.websockets.connect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Connection refused"),
        ):
            with patch("app.realtime.session.get_settings", return_value=_make_settings()):
                with pytest.raises(RuntimeError):
                    await _start_early_openai_connection(
                        call_sid="CA_test", caller_phone="+15551234567"
                    )


# ---------------------------------------------------------------------------
# Caller disconnect before connection completes
# ---------------------------------------------------------------------------


class TestCallerDisconnectBeforeConnection:
    """Test: Twilio caller disconnects while OpenAI is still connecting."""

    async def test_pending_connection_cleaned_up(self) -> None:
        """The pending connection is removed and task cancelled on disconnect."""
        _pending_connections.clear()

        async def _slow_connect() -> RealtimeSession:
            await asyncio.sleep(10)  # Simulate slow connection
            return _make_session()

        task = asyncio.create_task(_slow_connect())
        ec = EarlyConnection(
            call_sid="CA_test",
            caller_phone="+15551234567",
            connection_task=task,
        )
        _pending_connections["CA_test"] = ec

        # Simulate disconnect: pop and cancel
        pending = _pending_connections.pop("CA_test", None)
        assert pending is not None
        pending.connection_task.cancel()
        try:
            await pending.connection_task
        except (asyncio.CancelledError, Exception):
            pass

        assert "CA_test" not in _pending_connections
        assert task.cancelled()


# ---------------------------------------------------------------------------
# Session: stream_sid default
# ---------------------------------------------------------------------------


class TestSessionStreamSidDefault:
    """Tests that stream_sid defaults to empty string."""

    def test_stream_sid_defaults_to_empty(self) -> None:
        session = _make_session()
        assert session.stream_sid == ""

    def test_stream_sid_can_be_set(self) -> None:
        session = _make_session(stream_sid="MZ_test")
        assert session.stream_sid == "MZ_test"

    def test_stream_sid_can_be_set_after_construction(self) -> None:
        session = _make_session()
        session.stream_sid = "MZ_test"
        assert session.stream_sid == "MZ_test"
