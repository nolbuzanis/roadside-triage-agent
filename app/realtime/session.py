"""OpenAI Realtime session manager for per-call voice interactions."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import structlog
import websockets
import websockets.exceptions
from websockets.asyncio.client import ClientConnection

from app.core.config import get_settings
from app.realtime.instructions import CLOSING_MESSAGE
from app.realtime.latency import CallLatencyTracker

logger = structlog.get_logger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"

# Twilio Media Streams use G.711 mu-law at 8kHz.
# OpenAI Realtime supports g711_ulaw, so we match Twilio's native format
# to avoid audio conversion overhead.
TWILIO_AUDIO_RATE = 8000

# Grace period after Twilio acknowledges the closing playback mark (or after the
# bounded mark timeout), giving the media pipeline a final moment to drain. The
# mark acknowledgment — not this timer — is the primary delivery guarantee.
CLOSING_HANGUP_GRACE_SECONDS = 0.75

# Bounded fallback for a missing Twilio playback mark: if the mark event never
# arrives, hangup proceeds after this timeout so the call cannot stay open
# forever. Must exceed the longest expected closing-message playback duration.
CLOSING_MARK_TIMEOUT_SECONDS = 20.0


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
    on_closing_finished: Callable[[str], Coroutine[Any, Any, None]] | None = None
    on_clear_playback: Callable[[], Coroutine[Any, Any, None]] | None = None
    on_closing_mark_requested: Callable[[str], Coroutine[Any, Any, None]] | None = None

    _ws: ClientConnection | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    latency_tracker: CallLatencyTracker | None = field(default=None, init=False, repr=False)
    _greeting_response_done: bool = field(default=False, init=False, repr=False)
    _user_turn_during_greeting: bool = field(default=False, init=False, repr=False)
    _greeting_triggered: bool = field(default=False, init=False, repr=False)

    # Per-call closing-flow state:
    # intake_completed -> closing_response_started -> closing_response_completed
    # -> Twilio closing playback mark acknowledged -> hangup_started.
    # The sequence runs at most once per call.
    intake_completed: bool = field(default=False, init=False, repr=False)
    closing_response_started: bool = field(default=False, init=False, repr=False)
    closing_response_completed: bool = field(default=False, init=False, repr=False)
    hangup_started: bool = field(default=False, init=False, repr=False)
    closing_response_id: str | None = field(default=None, init=False, repr=False)
    _closing_interrupted: bool = field(default=False, init=False, repr=False)
    _caller_speaking: bool = field(default=False, init=False, repr=False)
    _transfer_requested: bool = field(default=False, init=False, repr=False)
    _assistance_request_id: str | None = field(default=None, init=False, repr=False)
    _response_create_reasons: deque[str] = field(default_factory=deque, init=False, repr=False)
    _hangup_grace_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _active_response_id: str | None = field(default=None, init=False, repr=False)
    _interrupted_response_id: str | None = field(default=None, init=False, repr=False)
    _closing_mark_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _closing_mark_name: str | None = field(default=None, init=False, repr=False)
    _closing_mark_seq: int = field(default=0, init=False, repr=False)

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
        logger.info("OpenAI Realtime session configured", call_sid=self.call_sid)

        if self.greeting:
            await self.trigger_greeting()

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
            "OpenAI Realtime session configured (early)", call_sid=self.call_sid
        )

    async def set_stream_sid_and_greet(self, stream_sid: str) -> None:
        """Set the Twilio stream SID and send the greeting.

        Called after the Twilio Media Stream 'start' event provides the stream_sid.
        If a greeting was configured, it is sent here (deferred from connect_early).
        """
        self.stream_sid = stream_sid
        if self.greeting:
            await self.trigger_greeting()

    async def _connect_websocket(self) -> None:
        """Open the WebSocket connection to OpenAI Realtime API."""
        settings = get_settings()
        model = settings.OPENAI_REALTIME_MODEL
        url = f"{REALTIME_URL}?model={model}"

        additional_headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        }

        logger.info(
            "Connecting to OpenAI Realtime",
            call_sid=self.call_sid,
            model=model,
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
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": False,
                    },
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

    async def trigger_greeting(self) -> None:
        """Explicitly trigger the model to deliver the fixed opening line.

        This is the single, dedicated in-code trigger for the opening. It does
        NOT rely on the model spontaneously speaking from the system prompt:
        it sends a response.create carrying per-response instructions that
        require the model to speak exactly ``self.greeting`` and nothing else.

        The greeting is NOT pre-injected as an assistant conversation item.
        Injecting an assistant message would add it to conversation history as
        if already spoken, causing the model's response to skip the greeting
        and begin intake instead. Making the greeting the model's actual output
        under explicit per-response instructions keeps the opening deterministic.

        Guarded so exactly one greeting response is ever created per call.
        """
        if self._greeting_triggered:
            logger.warning("greeting_already_triggered", call_sid=self.call_sid)
            return
        if not self.greeting:
            logger.warning("greeting_not_configured", call_sid=self.call_sid)
            return
        self._greeting_triggered = True

        directive = (
            "The call is starting now. Speak the opening line exactly as "
            "written, in this order, and then stop:\n\n"
            f'"{self.greeting}"\n\n'
            "Do not add any words, do not paraphrase or rephrase it, do not "
            "ask a question, and do not begin collecting information. Say "
            "nothing after the opening line and wait silently for the caller "
            "to speak."
        )
        await self._send_response_create(
            reason="initial_greeting",
            response_source="app.realtime.session.trigger_greeting",
            instructions=directive,
        )
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("response_create_sent")

    async def _send_response_create(
        self,
        *,
        reason: str,
        response_source: str,
        instructions: str | None = None,
    ) -> None:
        """Send a client-triggered response.create and log it with attribution.

        ``instructions`` is an optional per-response instruction override.
        When present, it replaces the session-level instructions for this
        response only (per Realtime API override semantics), scoping what the
        model may say for this single turn; subsequent responses fall back to
        the session configuration.
        """
        event: dict[str, Any] = {"type": "response.create"}
        if instructions:
            event["response"] = {"instructions": instructions}
        logger.info(
            "response_create_sent",
            reason=reason,
            call_id=self.call_sid,
            response_source=response_source,
        )
        sent = await self._send(event)
        # Track the reason only for sends that actually went out, so a failed
        # send cannot shift the response.created attribution queue and cause an
        # unrelated response to be mistaken for the closing response.
        if sent:
            self._response_create_reasons.append(reason)

    @staticmethod
    def _extract_assistant_text(response: dict[str, Any]) -> str:
        """Concatenate the assistant's spoken text from a response.done payload."""
        parts: list[str] = []
        output = response.get("output") or []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                text = content.get("text") or content.get("transcript")
                if text:
                    parts.append(str(text))
        return " ".join(parts).strip()

    @staticmethod
    def _normalize_for_greeting_match(text: str) -> str:
        """Normalize transcript text: lowercase, only alphanumerics."""
        return "".join(ch for ch in text.lower() if ch.isalnum())

    def _verify_greeting_response(self, response: dict[str, Any]) -> None:
        """Fail loudly when a completed greeting response did not speak exactly the opening.

        Runs once when the first (greeting) response completes. If the model
        skipped the greeting or delivered something else, the structured error
        log makes the skip observable instead of silently continuing. Responses
        that did not complete (e.g. the caller barged in and cancelled the
        greeting) are logged at info level, not treated as a skipped opening.
        """
        if not self.greeting:
            return
        status = response.get("status")
        if status != "completed":
            logger.info(
                "greeting_response_not_completed",
                call_sid=self.call_sid,
                status=status or "unknown",
            )
            return
        delivered = self._extract_assistant_text(response)
        expected = self.greeting
        matched = self._normalize_for_greeting_match(delivered) == self._normalize_for_greeting_match(
            expected
        )
        if matched:
            logger.info(
                "greeting_delivery_verified",
                call_sid=self.call_sid,
                matched=True,
                expected_text=expected,
            )
        else:
            logger.error(
                "greeting_delivery_mismatch",
                call_sid=self.call_sid,
                matched=False,
                delivered_text=delivered,
                expected_text=expected,
            )

    async def _send(self, event: dict[str, Any]) -> bool:
        """Send a JSON event to the OpenAI WebSocket. Returns True when sent."""
        if self._ws is None or not self._connected:
            logger.warning("Cannot send event, WebSocket not connected", event_type=event.get("type"))
            return False
        try:
            await self._ws.send(json.dumps(event))
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket closed while sending", call_sid=self.call_sid)
            self._connected = False
            return False
        except Exception:
            logger.exception("Error sending event", call_sid=self.call_sid)
            return False

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
                "OpenAI WebSocket closed",
                call_sid=self.call_sid,
                code=e.code,
            )
        except Exception as e:
            logger.exception("Error in event loop", call_sid=self.call_sid)
            if self.on_error:
                await self.on_error(e)
        finally:
            self._connected = False

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Dispatch a single event from the OpenAI Realtime API."""
        event_type = event.get("type", "")

        if event_type == "session.created":
            # Extract session ID if present
            assert self.latency_tracker is not None
            session_id = event.get("session", {}).get("id")
            if session_id:
                self.latency_tracker.openai_session_id = session_id
            logger.info(
                "session.created",
                call_sid=self.call_sid,
                openai_session_id=session_id,
            )
            self.latency_tracker.record_event("openai_session_created")

        elif event_type == "session.updated":
            logger.info("session.updated", call_sid=self.call_sid)
            assert self.latency_tracker is not None
            self.latency_tracker.record_event("openai_session_updated")

        elif event_type == "conversation.item.created":
            item = event.get("item", {})
            logger.info(
                "conversation.item.created",
                call_sid=self.call_sid,
                item_id=item.get("id"),
                item_type=item.get("type"),
                role=item.get("role"),
            )

        elif event_type == "input_audio_buffer.speech_started":
            self._caller_speaking = True
            logger.info(
                "input_audio_buffer.speech_started",
                call_sid=self.call_sid,
            )
            await self._interrupt_assistant_speech()

        elif event_type == "input_audio_buffer.speech_stopped":
            self._caller_speaking = False
            logger.info(
                "input_audio_buffer.speech_stopped",
                call_sid=self.call_sid,
            )
            self._maybe_arm_hangup()

        elif event_type == "input_audio_buffer.committed":
            self._caller_speaking = False
            logger.info(
                "input_audio_buffer.committed",
                call_sid=self.call_sid,
                item_id=event.get("item_id"),
            )
            if self._greeting_response_done:
                # Normal turn-taking: the caller has finished speaking, so
                # produce exactly one assistant response for that turn.
                await self._send_response_create(
                    reason="caller_turn_complete",
                    response_source="app.realtime.session._handle_event[input_audio_buffer.committed]",
                )
            else:
                self._user_turn_during_greeting = True

        elif event_type == "response.output_audio.delta":
            audio_b64 = event.get("delta", "")
            if audio_b64:
                # Record first audio received (only once per call)
                assert self.latency_tracker is not None
                self.latency_tracker.record_event("first_openai_audio_received")
                if self._interrupted_response_id is not None:
                    logger.debug(
                        "assistant_audio_dropped_after_interruption",
                        call_sid=self.call_sid,
                        response_id=self._interrupted_response_id,
                    )
                elif self.on_audio_delta:
                    await self.on_audio_delta(audio_b64)

        elif event_type == "response.created":
            response = event.get("response", {})
            response_id = response.get("id")
            logger.info(
                "response.created",
                call_sid=self.call_sid,
                response_id=response_id,
                status=response.get("status"),
            )
            if response_id:
                self._active_response_id = response_id
                if (
                    self._interrupted_response_id is not None
                    and response_id != self._interrupted_response_id
                ):
                    self._interrupted_response_id = None
            reason = self._response_create_reasons.popleft() if self._response_create_reasons else None
            if reason == "post_intake_closing":
                self.closing_response_id = response_id

        elif event_type == "response.output_audio.done":
            logger.info(
                "response.output_audio.done",
                call_sid=self.call_sid,
                item_id=event.get("item_id"),
            )

        elif event_type == "response.done":
            response = event.get("response", {})
            response_id = response.get("id")
            status = response.get("status")
            logger.info(
                "response.done",
                call_sid=self.call_sid,
                response_id=response_id,
                status=status,
            )

            if response_id is not None:
                if response_id == self._active_response_id:
                    self._active_response_id = None
                if response_id == self._interrupted_response_id:
                    self._interrupted_response_id = None

            # The greeting gates all further response creation: no assistant
            # output may begin before the fixed greeting finishes streaming and
            # the caller has actually spoken.
            if not self._greeting_response_done:
                self._greeting_response_done = True
                self._verify_greeting_response(response)
                if self._user_turn_during_greeting:
                    self._user_turn_during_greeting = False
                    await self._send_response_create(
                        reason="caller_turn_complete",
                        response_source="app.realtime.session._handle_event[response.done]",
                    )

            await self._handle_closing_response_done(response)

            output = response.get("output", [])
            for item in output:
                if item.get("type") == "function_call":
                    await self._handle_function_call(item)

        elif event_type == "error":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            error_code = event.get("error", {}).get("code", "unknown")
            logger.error(
                "OpenAI error",
                call_sid=self.call_sid,
                error_code=error_code,
                error_message=error_msg,
            )
            if self.on_error:
                await self.on_error(RuntimeError(f"OpenAI error [{error_code}]: {error_msg}"))

        elif event_type in (
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
        ):
            # Lifecycle/acknowledgment events — log at debug level
            logger.debug("OpenAI event", event_type=event_type, call_sid=self.call_sid)

        else:
            logger.debug("Unhandled OpenAI event", event_type=event_type, call_sid=self.call_sid)

    async def _interrupt_assistant_speech(self) -> None:
        """Stop assistant speech now that the caller has started talking.

        Cancels the in-progress OpenAI response via the supported
        ``response.cancel`` client event, drops any audio deltas still
        arriving for the cancelled response, and asks the Twilio bridge to
        clear assistant audio already buffered for playback. Runs on every
        caller speech start so a buffered playback tail is flushed even when
        no response is still generating.
        """
        interrupted_response_id = self._active_response_id
        cancel_sent = False
        if interrupted_response_id is not None:
            self._active_response_id = None
            self._interrupted_response_id = interrupted_response_id
            cancel_sent = await self._send({
                "type": "response.cancel",
                "response_id": interrupted_response_id,
            })
            logger.info(
                "caller_interruption_detected",
                call_sid=self.call_sid,
                response_id=interrupted_response_id,
                response_cancel_sent=cancel_sent,
            )
        if self.on_clear_playback:
            await self.on_clear_playback()

    async def _handle_function_call(self, item: dict[str, Any]) -> None:
        """Handle a function_call output item from the model."""
        call_id = item.get("call_id", "")
        func_name = item.get("name", "")
        arguments = item.get("arguments", "{}")

        logger.info(
            "Tool call",
            call_sid=self.call_sid,
            function=func_name,
            tool_call_id=call_id,
        )

        # Record tool call started
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("tool_call_started", tool_call_id=call_id)

        # An emergency transfer takes ownership of the call. Flag it BEFORE
        # awaiting the handler so the closing-flow hangup can never terminate
        # a transfer that is in flight or about to start.
        if func_name == "transfer_to_emergency" and not self._transfer_requested:
            self._transfer_requested = True
            logger.info(
                "transfer_requested_hangup_suppressed",
                call_sid=self.call_sid,
                tool_call_id=call_id,
            )

        result = ""
        if self.on_tool_call:
            try:
                result = await self.on_tool_call(call_id, func_name, arguments)
            except Exception as e:
                logger.exception(
                    "Tool call error",
                    call_sid=self.call_sid,
                    function=func_name,
                )
                result = json.dumps({"error": str(e)})
        else:
            logger.warning(
                "No tool handler registered",
                call_sid=self.call_sid,
                function=func_name,
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
        logger.info(
            "function_call_output_item_created",
            call_sid=self.call_sid,
            tool_call_id=call_id,
        )

        if func_name == "update_assistance_request":
            if self.intake_completed:
                # Duplicate/retried intake tool after a successful completion:
                # the closing flow owns the end of the conversation, so no new
                # response is created for the repeat call.
                logger.info(
                    "duplicate_assistance_request_tool_call_ignored",
                    call_sid=self.call_sid,
                    tool_call_id=call_id,
                )
                return
            request_data = self._parse_assistance_request_result(result)
            if request_data is not None and request_data.get("status") == "created":
                self.intake_completed = True
                await self._start_closing_response(
                    tool_call_id=call_id,
                    assistance_request_id=request_data.get("assistance_request_id"),
                )
                return

        await self._send_response_create(
            reason="tool_result",
            response_source="app.realtime.session._handle_function_call",
        )

    @staticmethod
    def _parse_assistance_request_result(result: str) -> dict[str, Any] | None:
        """Parse an update_assistance_request tool result, or None if unparseable."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _start_closing_response(
        self, *, tool_call_id: str, assistance_request_id: str | None
    ) -> None:
        """Trigger the fixed post-intake closing line. Runs at most once per call.

        Sends a response.create carrying per-response instructions that require
        the model to speak exactly ``CLOSING_MESSAGE`` and nothing else, mirroring
        the deterministic greeting trigger. The hangup is NOT started here — it
        waits for this response to finish and for Twilio to confirm the audio
        finished playing (see _handle_closing_response_done).
        """
        if self.closing_response_started:
            logger.warning("closing_response_already_started", call_sid=self.call_sid)
            return
        self.closing_response_started = True
        self._assistance_request_id = assistance_request_id

        directive = (
            "The assistance request has been saved. Speak the closing line exactly as "
            "written, in this order, and then stop:\n\n"
            f'"{CLOSING_MESSAGE}"\n\n'
            "Do not add any words, do not paraphrase or rephrase it, do not ask a "
            "question, do not offer an ETA, and do not say anything after the closing "
            "line. The call is ending now."
        )
        await self._send_response_create(
            reason="post_intake_closing",
            response_source="app.realtime.session._start_closing_response",
            instructions=directive,
        )
        logger.info(
            "closing_response_started",
            call_sid=self.call_sid,
            reason="post_intake_closing",
            response_source="app.realtime.session._start_closing_response",
            tool_call_id=tool_call_id,
            assistance_request_id=assistance_request_id,
        )

    async def _handle_closing_response_done(self, response: dict[str, Any]) -> None:
        """Advance the closing flow when its tagged response reaches a terminal state.

        The closing response is identified by the response_id captured when its
        response.create (reason=post_intake_closing) was acknowledged. An
        unrelated assistant response never completes the closing flow. If the
        caller barged in and cancelled the closing response, the flow waits for
        the follow-up turn to finish before allowing the hangup.
        """
        if not self.closing_response_started or self.closing_response_completed:
            return

        response_id = response.get("id")
        status = response.get("status")

        if self.closing_response_id is None:
            # The closing response.created was never observed; fail safe by
            # never completing the flow, so an unrelated response.done can
            # never trigger the hangup.
            logger.info(
                "closing_response_id_unobserved",
                call_sid=self.call_sid,
                response_id=response_id,
            )
            return

        if response_id == self.closing_response_id:
            if status == "completed":
                self.closing_response_completed = True
                logger.info(
                    "closing_response_completed",
                    call_sid=self.call_sid,
                    response_id=response_id,
                    status=status,
                    interrupted=self._closing_interrupted,
                    assistance_request_id=self._assistance_request_id,
                )
                self._maybe_arm_hangup()
            else:
                self._closing_interrupted = True
                logger.info(
                    "closing_response_interrupted",
                    call_sid=self.call_sid,
                    response_id=response_id,
                    status=status or "unknown",
                    assistance_request_id=self._assistance_request_id,
                )
            return

        # The closing response was barged in; wait for the caller's follow-up
        # turn to finish so the hangup never lands mid-conversation.
        if self._closing_interrupted and status == "completed":
            self.closing_response_completed = True
            logger.info(
                "closing_response_completed",
                call_sid=self.call_sid,
                response_id=response_id,
                status=status,
                interrupted=True,
                assistance_request_id=self._assistance_request_id,
            )
            self._maybe_arm_hangup()

    def _maybe_arm_hangup(self) -> None:
        """Start the mark-then-hangup sequence once the closing flow is safe.

        The armed task first requests a Twilio Media Streams mark behind the
        final closing audio and waits (bounded) for Twilio's mark event, so the
        hangup never races the closing playback. Deferred while the caller is
        speaking so the call is never disconnected in the middle of caller
        speech, and suppressed entirely once an emergency transfer has been
        requested so the transfer is never terminated. Re-armed from
        speech_stopped/committed once it is safe again. At most one hangup
        task exists at a time.
        """
        if self.hangup_started or not self.closing_response_completed:
            return
        if self._transfer_requested:
            logger.info("hangup_suppressed_transfer", call_sid=self.call_sid)
            return
        if self._caller_speaking:
            logger.info("hangup_deferred_caller_speaking", call_sid=self.call_sid)
            return
        if self._hangup_grace_task is not None and not self._hangup_grace_task.done():
            return
        self._hangup_grace_task = asyncio.create_task(self._hangup_after_playback())

    async def _hangup_after_playback(self) -> None:
        """Wait for Twilio to confirm closing playback, settle, re-check, hang up."""
        try:
            await self._await_closing_playback_mark()
            await asyncio.sleep(CLOSING_HANGUP_GRACE_SECONDS)
            if self.hangup_started or not self.closing_response_completed:
                return
            if self._transfer_requested:
                # An emergency transfer was requested while waiting for playback
                # confirmation; transferring the call away must win over our hangup.
                logger.info("hangup_suppressed_transfer", call_sid=self.call_sid)
                return
            if self._caller_speaking:
                logger.info("hangup_deferred_caller_speaking", call_sid=self.call_sid)
                return
            self.hangup_started = True
            if self.on_closing_finished:
                await self.on_closing_finished(self.call_sid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Closing hangup callback failed", call_sid=self.call_sid)

    async def _await_closing_playback_mark(self) -> None:
        """Request a Twilio playback mark and wait for its acknowledgment.

        The mark is requested only after the closing response reaches
        ``response.done``, which fires after every audio delta has already been
        forwarded to Twilio — so the mark sits behind the complete closing
        audio in the stream. Twilio echoes the mark event once it has flushed
        the media up to that point, i.e. the caller has received the closing
        message. A missing mark falls back after CLOSING_MARK_TIMEOUT_SECONDS
        so the call can never be left open forever.
        """
        self._closing_mark_seq += 1
        mark_name = f"closing-{self.call_sid}-{self._closing_mark_seq}"
        self._closing_mark_name = mark_name
        event = asyncio.Event()
        self._closing_mark_event = event

        if self.on_closing_mark_requested is None:
            logger.warning(
                "closing_playback_mark_callback_missing",
                call_sid=self.call_sid,
                mark_name=mark_name,
            )
        else:
            try:
                await self.on_closing_mark_requested(mark_name)
            except Exception:
                logger.exception(
                    "closing_playback_mark_callback_errored",
                    call_sid=self.call_sid,
                    mark_name=mark_name,
                )

        try:
            await asyncio.wait_for(event.wait(), timeout=CLOSING_MARK_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "closing_playback_mark_timeout",
                call_sid=self.call_sid,
                mark_name=mark_name,
                timeout_seconds=CLOSING_MARK_TIMEOUT_SECONDS,
            )
            return
        logger.info(
            "closing_playback_mark_acknowledged",
            call_sid=self.call_sid,
            mark_name=mark_name,
        )

    def acknowledge_closing_mark(self, mark_name: str) -> None:
        """Resolve the pending closing playback mark when Twilio echoes it back.

        Called from the Twilio Media Stream receive loop. A mark that does not
        match the currently pending one (stale or foreign) is ignored so an old
        acknowledgment can never unblock a newer wait.
        """
        if not mark_name or mark_name != self._closing_mark_name:
            logger.debug(
                "closing_playback_mark_unexpected",
                call_sid=self.call_sid,
                mark_name=mark_name,
                expected=self._closing_mark_name,
            )
            return
        if self._closing_mark_event is not None:
            self._closing_mark_event.set()

    async def close(self) -> None:
        """Cleanly close the OpenAI Realtime session and WebSocket."""
        logger.info("Closing OpenAI Realtime session", call_sid=self.call_sid)
        self._connected = False

        # Cancel any pending closing-flow hangup task so it cannot fire after teardown.
        if self._hangup_grace_task is not None and not self._hangup_grace_task.done():
            self._hangup_grace_task.cancel()
        self._hangup_grace_task = None
        self._closing_mark_event = None
        self._closing_mark_name = None

        # Record call ended and log latency metrics
        assert self.latency_tracker is not None
        self.latency_tracker.record_event("call_ended")
        self.latency_tracker.log_latency_metrics()

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing WebSocket", call_sid=self.call_sid)
            self._ws = None

    @property
    def is_connected(self) -> bool:
        """Return True if the WebSocket connection is active."""
        return self._connected
