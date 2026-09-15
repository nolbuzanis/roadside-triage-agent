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

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"

# OpenAI Realtime expects 24kHz PCM16 audio.
OPENAI_SAMPLE_RATE = 24000


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
    stream_sid: str
    instructions: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    on_tool_call: Callable[[str, str, str], Coroutine[Any, Any, str]] | None = None
    on_audio_delta: Callable[[str], Coroutine[Any, Any, None]] | None = None
    on_error: Callable[[Exception], Coroutine[Any, Any, None]] | None = None

    _ws: ClientConnection | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)

    async def connect(self) -> None:
        """Establish WebSocket connection to OpenAI Realtime API and configure session."""
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

        self._ws = await websockets.connect(
            url,
            additional_headers=additional_headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._connected = True

        await self._configure_session(settings)
        logger.info("OpenAI Realtime session configured: call_sid=%s", self.call_sid)

    async def _configure_session(self, settings: Any) -> None:
        """Send session.update to configure model, voice, audio, tools, and turn detection."""
        session_config: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_SAMPLE_RATE},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_SAMPLE_RATE},
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
        """Forward base64-encoded PCM16 audio from Twilio to OpenAI."""
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

        elif event_type == "session.updated":
            logger.debug("OpenAI session updated: call_sid=%s", self.call_sid)

        elif event_type == "response.output_audio.delta":
            audio_b64 = event.get("delta", "")
            if audio_b64 and self.on_audio_delta:
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
