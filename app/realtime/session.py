"""OpenAI Realtime session manager for per-call voice interactions."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import websockets
import websockets.exceptions
from websockets.asyncio.client import ClientConnection

from app.core.config import get_settings
from app.realtime.latency import CallLatencyTracker

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"

# Twilio Media Streams use G.711 mu-law at 8kHz.
# OpenAI Realtime supports g711_ulaw, so we match Twilio's native format
# to avoid audio conversion overhead.
TWILIO_AUDIO_RATE = 8000


@dataclass
class RealtimeSession:
    """Manages a single OpenAI Realtime WebSocket session for one phone call.

    Each phone call gets its own isolated session instance. The session handles:
    - WebSocket connection to OpenAI Realtime API
    - Session configuration (model, voice, audio format, turn detection)
    - Streaming audio input from Twilio -> OpenAI
    - Streaming audio output from OpenAI -> Twilio
    - Tool/function call handling
    - Error recovery and cleanup
    """

    call_sid: str
    caller_phone: str
    stream_sid: str = ""
    instructions: str = ""
    greeting: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    on_tool_call: Callable[[str, str, str], Coroutine[Any, Any, str]] | None = None
    on_audio_delta: Callable[[str], Coroutine[Any, Any, None]] | None = None
    on_error: Callable[[Exception], Coroutine[Any, Any, None]] | None = None

    _ws: ClientConnection | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    latency_tracker: CallLatencyTracker | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize the latency tracker for this call."""
        if self.latency_tracker is None:
            self.latency_tracker = CallLatencyTracker(call_id=self.call_sid)

    async def connect(self) -> None:
        """Establish WebSocket connection to OpenAI Realtime API and configure session.

        Connects, configures the session, and sends the greeting if provided.
        For early connection (before Twilio Media Stream), use connect_early()
        followed by set_stream_sid_and_greet().
        """
        await self._connect_websocket()
        await self._configure_session()
        logger.info("OpenAI Realtime session configured: call_sid=%s", self.call_sid)

        if self.greeting:
            await self._trigger_greeting()

    async def connect_early(self) -> None:
        """Start the OpenAI connection before the Twilio Media Stream arrives.

        Establishes the WebSocket and configures the session, but does NOT
        send the greeting. The greeting is deferred until set_stream_sid_and_greet()
        is called after the Media Stream provides the stream_sid.

        This allows the ~1.5s WebSocket connection latency to overlap with
        Twilio's call/media-stream setup time.
        """
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("early_connection_started")
        await self._connect_websocket()
        await self._configure_session()
        self.latency_tracker.record_event("early_connection_completed")
        logger.info(
            "OpenAI Realtime session configured (early): call_sid=%s", self.call_sid
        )

    async def set_stream_sid_and_greet(self, stream_sid: str) -> None:
        """Set the Twilio stream SID and send the greeting.

        Called after the Twilio Media Stream 'start' event provides the stream_sid.
        If a greeting was configured, it is sent here (deferred from connect_early).
        """
        self.stream_sid = stream_sid
        if self.greeting:
            await self._trigger_greeting()

    async def _connect_websocket(self) -> None:
        """Open the WebSocket connection to OpenAI Realtime API."""
        settings = get_settings()
        model = settings.OPENAI_REALTIME_MODEL
        url = f"{REALTIME_URL}?model={model}"

        additional_headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        }

        logger.info(
            "Connecting to OpenAI Realtime: call_sid=%s, model=%s",
            self.call_sid,
            model,
        )

        assert self.latency_tracker is not None
        self.latency_tracker.record_event("openai_connection_started")

        self._ws = await websockets.connect(
            url,
            additional_headers=additional_headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._connected = True
        self.latency_tracker.record_event("openai_websocket_connected")

    async def _configure_session(self, settings: Any = None) -> None:
        """Send session.update to configure model, voice, audio, tools, and turn detection."""
        if settings is None:
            settings = get_settings()
        session_config: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": "marin",
                },
            },
        }

        if self.instructions:
            session_config["instructions"] = self.instructions

        if self.tools:
            session_config["tools"] = self.tools
            session_config["tool_choice"] = "auto"

        await self._send({"type": "session.update", "session": session_config})
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("session_update_sent")

    async def _trigger_greeting(self) -> None:
        """Inject the opening greeting and trigger the model to speak it."""
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.greeting}],
            },
        })
        await self._send({"type": "response.create"})
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("response_create_sent")

    async def _send(self, event: dict[str, Any]) -> None:
        """Send a JSON event to the OpenAI WebSocket."""
        if self._ws is None or not self._connected:
            logger.warning("Cannot send event, WebSocket not connected: %s", event.get("type"))
            return
        try:
            await self._ws.send(json.dumps(event))
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket closed while sending: call_sid=%s", self.call_sid)
            self._connected = False
        except Exception:
            logger.exception("Error sending event: call_sid=%s", self.call_sid)

    async def send_audio(self, audio_b64: str) -> None:
        """Forward base64-encoded audio from Twilio to OpenAI."""
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": audio_b64,
        })

    async def process_events(self) -> None:
        """Main event loop: receive events from OpenAI and dispatch handlers.

        This runs as a long-lived task while the call is active. It handles:
        - Audio output deltas (forwarded via on_audio_delta callback)
        - Tool/function calls (dispatched via on_tool_call callback)
        - Session lifecycle events
        - Errors
        """
        if self._ws is None:
            raise RuntimeError("WebSocket not connected. Call connect() first.")

        try:
            async for raw_message in self._ws:
                event = json.loads(raw_message)
                await self._handle_event(event)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(
                "OpenAI WebSocket closed: call_sid=%s, code=%s",
                self.call_sid,
                e.code,
            )
        except Exception as e:
            logger.exception("Error in event loop: call_sid=%s", self.call_sid)
            if self.on_error:
                await self.on_error(e)
        finally:
            self._connected = False

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Dispatch a single event from the OpenAI Realtime API."""
        event_type = event.get("type", "")

        if event_type == "session.created":
            logger.info("OpenAI session created: call_sid=%s", self.call_sid)
            # Extract session ID if present
            assert self.latency_tracker is not None
            session_id = event.get("session", {}).get("id")
            if session_id:
                self.latency_tracker.openai_session_id = session_id
            self.latency_tracker.record_event("openai_session_created")

        elif event_type == "session.updated":
            logger.debug("OpenAI session updated: call_sid=%s", self.call_sid)
            assert self.latency_tracker is not None
            self.latency_tracker.record_event("openai_session_updated")

        elif event_type == "response.output_audio.delta":
            audio_b64 = event.get("delta", "")
            if audio_b64:
                # Record first audio received (only once per call)
                assert self.latency_tracker is not None
                self.latency_tracker.record_event("first_openai_audio_received")
                if self.on_audio_delta:
                    await self.on_audio_delta(audio_b64)

        elif event_type == "response.done":
            response = event.get("response", {})
            output = response.get("output", [])
            for item in output:
                if item.get("type") == "function_call":
                    await self._handle_function_call(item)

        elif event_type == "error":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            error_code = event.get("error", {}).get("code", "unknown")
            logger.error(
                "OpenAI error: call_sid=%s, code=%s, message=%s",
                self.call_sid,
                error_code,
                error_msg,
            )
            if self.on_error:
                await self.on_error(RuntimeError(f"OpenAI error [{error_code}]: {error_msg}"))

        elif event_type in (
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "response.created",
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
            "response.output_audio.done",
        ):
            # Lifecycle/acknowledgment events — log at debug level
            logger.debug("OpenAI event: %s, call_sid=%s", event_type, self.call_sid)

        else:
            logger.debug("Unhandled OpenAI event: %s, call_sid=%s", event_type, self.call_sid)

    async def _handle_function_call(self, item: dict[str, Any]) -> None:
        """Handle a function_call output item from the model."""
        call_id = item.get("call_id", "")
        func_name = item.get("name", "")
        arguments = item.get("arguments", "{}")

        logger.info(
            "Tool call: call_sid=%s, function=%s, call_id=%s",
            self.call_sid,
            func_name,
            call_id,
        )

        # Record tool call started
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("tool_call_started", tool_call_id=call_id)

        result = ""
        if self.on_tool_call:
            try:
                result = await self.on_tool_call(call_id, func_name, arguments)
            except Exception as e:
                logger.exception(
                    "Tool call error: call_sid=%s, function=%s",
                    self.call_sid,
                    func_name,
                )
                result = json.dumps({"error": str(e)})
        else:
            logger.warning(
                "No tool handler registered: call_sid=%s, function=%s",
                self.call_sid,
                func_name,
            )
            result = json.dumps({"error": "No tool handler registered"})

        # Record tool call completed
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("tool_call_completed", tool_call_id=call_id)

        # Send the function result back to the model
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        })
        await self._send({"type": "response.create"})

    async def close(self) -> None:
        """Cleanly close the OpenAI Realtime session and WebSocket."""
        logger.info("Closing OpenAI Realtime session: call_sid=%s", self.call_sid)
        self._connected = False

        # Record call ended and log latency metrics
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("call_ended")
        self.latency_tracker.log_latency_metrics()

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing WebSocket: call_sid=%s", self.call_sid)
            self._ws = None

    @property
    def is_connected(self) -> bool:
        """Return True if the WebSocket connection is active."""
        return self._connected
