import asyncio

import structlog
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from twilio.request_validator import RequestValidator  # type: ignore[import-untyped]
from twilio.twiml.voice_response import VoiceResponse  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.realtime.instructions import OPENING_GREETING, ROADSIDE_ASSISTANT_INSTRUCTIONS
from app.realtime.session import RealtimeSession
from app.realtime.tools import (
    REALTIME_TOOLS,
    AssistanceRequestArgs,
    AssistanceRequestToolResult,
    EmergencyTransferArgs,
    EmergencyTransferResult,
)
from app.services.calls import EarlyConnection, call_manager
from app.services.demo_sessions import match_demo_session_for_call
from app.services.emergency import transfer_call
from app.services.hangup import hangup_call
from app.services.notifier import notify_dispatcher
from app.services.tickets import (
    abandon_if_open,
    complete_intake,
    create_ticket,
    is_intake_complete,
    link_demo_session,
    start_assistance_request,
    update_ticket_hazard,
)

logger = structlog.get_logger(__name__)

_background_tasks: set[asyncio.Task[object]] = set()

# Bound the early assistance-request insert so a stalled Supabase call can
# never hang the TwiML response (Twilio expects a voice webhook reply in ~15s).
_ASSISTANCE_REQUEST_START_TIMEOUT_SECONDS = 5.0

# Bound demo-session matching + linking for the same reason; the two webhook
# timeboxes run sequentially but stay within Twilio's ~15s webhook budget.
_DEMO_SESSION_MATCH_TIMEOUT_SECONDS = 5.0

# Pending early OpenAI connections, keyed by Twilio CallSid.
# Created in the voice webhook; consumed in the media stream handler.
_pending_connections: dict[str, EarlyConnection] = {}


async def handle_update_assistance_request(
    *,
    call_sid: str | None,
    caller_phone: str | None,
    arguments: str,
) -> AssistanceRequestToolResult:
    """Parse, validate, and persist an assistance request from a tool call.

    This function bridges the OpenAI Realtime boundary (JSON arguments) to the
    assistance-request service (typed Python values). It handles argument
    validation, calls the service, and returns a typed result.
    """
    try:
        args = AssistanceRequestArgs.model_validate_json(arguments)
    except ValidationError:
        logger.warning("Invalid tool arguments", call_sid=call_sid)
        return AssistanceRequestToolResult(status="error", error="Invalid arguments")

    try:
        request = await asyncio.to_thread(
            create_ticket,
            call_id=call_sid or "unknown",
            caller_phone=caller_phone or "unknown",
            location=args.location,
            vehicle=args.vehicle,
            issue=args.issue,
        )
    except Exception:
        logger.exception("Failed to save assistance request", call_sid=call_sid)
        return AssistanceRequestToolResult(
            status="error", error="Unable to save the assistance request"
        )

    if not is_intake_complete(request):
        logger.info(
            "assistance_request_updated",
            call_sid=call_sid,
            assistance_request_id=request.get("id"),
        )
        return AssistanceRequestToolResult(
            status="updated",
            assistance_request_id=request.get("id"),
            message="Intake details saved.",
        )

    logger.info(
        "intake_completed",
        call_sid=call_sid,
        assistance_request_id=request.get("id"),
    )

    # Finalize the intake lifecycle status alongside the completion path.
    # Guarded update: open rows become completed, abandoned rows self-heal
    # (backfill-in-flight calls), and escalated/completed are never touched.
    await asyncio.to_thread(complete_intake, call_id=call_sid or "unknown")

    # Completion-gated SMS: fire only while notification_status is still pending
    # so a retried completing call never re-sends.
    if request.get("notification_status") == "pending":
        task = asyncio.create_task(
            asyncio.to_thread(
                notify_dispatcher,
                call_id=call_sid or "unknown",
                caller_phone=caller_phone or "unknown",
                location=str(request.get("location") or ""),
                vehicle=str(request.get("vehicle") or ""),
                issue=str(request.get("issue") or ""),
            ),
            name=f"notify-{call_sid}",
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return AssistanceRequestToolResult(
        status="created",
        assistance_request_id=request.get("id"),
        message="Assistance request saved successfully. You may now close the call.",
    )


async def _record_escalation(*, call_sid: str, arguments: str) -> None:
    """Record hazard escalation state on the assistance request if one exists.

    Runs as a background task so it never blocks the live transfer.
    """
    try:
        args = EmergencyTransferArgs.model_validate_json(arguments)
        await asyncio.to_thread(
            update_ticket_hazard,
            call_id=call_sid,
            hazard_reason=args.reason,
        )
        logger.info("Escalation state recorded", call_sid=call_sid)
    except Exception:
        logger.exception("Failed to record escalation state", call_sid=call_sid)


async def handle_transfer_to_emergency(
    *,
    call_sid: str | None,
    arguments: str,
) -> EmergencyTransferResult:
    """Parse, validate, and execute an emergency call transfer."""
    try:
        EmergencyTransferArgs.model_validate_json(arguments)
    except ValidationError:
        logger.warning("Invalid emergency transfer arguments", call_sid=call_sid)
        return EmergencyTransferResult(status="error", error="Invalid arguments")

    settings = get_settings()
    destination = settings.EMERGENCY_TRANSFER_PHONE

    logger.info(
        "Emergency transfer requested",
        call_sid=call_sid,
        destination=destination,
    )

    try:
        success = await asyncio.to_thread(
            transfer_call,
            call_sid=call_sid or "unknown",
            destination_phone=destination,
        )
    except Exception:
        logger.exception("Emergency transfer exception", call_sid=call_sid)
        return EmergencyTransferResult(
            status="error",
            error="Unable to complete transfer. Please call 911 directly.",
        )

    if success:
        if call_sid:
            state = call_manager.get(call_sid)
            if state:
                state.transfer_state = "transferred"
            task = asyncio.create_task(
                _record_escalation(call_sid=call_sid, arguments=arguments),
                name=f"escalation-record-{call_sid}",
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        return EmergencyTransferResult(
            status="transferred",
            message="Emergency transfer in progress. Stay on the line.",
        )
    else:
        return EmergencyTransferResult(
            status="error",
            error="Unable to complete transfer. Please call 911 directly.",
        )


router = APIRouter()


async def send_clear_to_twilio(
    websocket: WebSocket, *, stream_sid: str, call_sid: str | None
) -> None:
    """Send Twilio's clear message to empty the outbound audio buffer.

    Twilio buffers media messages before playing them to the caller, so
    cancelling the OpenAI response alone leaves already-forwarded assistant
    audio audibly playing. The clear message empties that buffer so an
    interrupted assistant stops speaking immediately. Failures are logged
    and never raised into the event loop.
    """
    try:
        await websocket.send_json({
            "event": "clear",
            "streamSid": stream_sid,
        })
        logger.info(
            "twilio_playback_cleared",
            call_sid=call_sid,
            stream_sid=stream_sid,
        )
    except Exception:
        logger.warning("Failed to clear Twilio playback", call_sid=call_sid)


async def handle_closing_finished(call_sid: str) -> None:
    """Hang up the Twilio call after the closing flow safely finished.

    Invoked by the RealtimeSession once the closing response has completed,
    Twilio has acknowledged the closing playback mark (or the bounded mark
    timeout elapsed), and the grace period has elapsed without the caller
    speaking. Skips the Twilio call when the call already disconnected
    naturally (state cleaned up) or when an emergency transfer owns the call,
    so neither case is treated as an error and a live transfer is never
    terminated.
    """
    if not call_sid:
        logger.warning("call_hangup_skipped_missing_call_sid")
        return

    state = call_manager.get(call_sid)
    if state is None:
        logger.info("call_hangup_skipped_call_ended", call_sid=call_sid)
        return

    if state.transfer_state != "none":
        logger.info(
            "call_hangup_skipped_transferred",
            call_sid=call_sid,
            transfer_state=state.transfer_state,
        )
        return

    logger.info("call_hangup_started", call_sid=call_sid)
    success = await asyncio.to_thread(hangup_call, call_sid=call_sid)
    if success:
        logger.info("call_hangup_completed", call_sid=call_sid)
    else:
        logger.warning("call_hangup_failed", call_sid=call_sid)


async def _send_media_stream_mark(
    websocket: WebSocket,
    *,
    stream_sid: str | None,
    mark_name: str,
    call_sid: str | None,
) -> None:
    """Send a Twilio Media Streams mark and log the attempt.

    Sent after the final closing audio frames so Twilio's echoed mark event
    confirms the caller received the complete closing message before hangup.
    Failures are logged loudly; the session's bounded mark timeout remains the
    fallback so a lost mark can never leave the call open forever.
    """
    if not stream_sid:
        logger.warning(
            "closing_playback_mark_skipped_no_stream",
            call_sid=call_sid,
            mark_name=mark_name,
        )
        return
    try:
        await websocket.send_json({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": mark_name},
        })
        logger.info(
            "closing_playback_mark_sent",
            call_sid=call_sid,
            mark_name=mark_name,
        )
    except Exception:
        logger.warning(
            "closing_playback_mark_send_failed",
            call_sid=call_sid,
            mark_name=mark_name,
        )


_validator: RequestValidator | None = None


async def _start_early_openai_connection(
    *,
    call_sid: str,
    caller_phone: str,
) -> RealtimeSession:
    """Create and connect an OpenAI Realtime session before the Twilio Media Stream arrives.

    This runs as a background task started from the voice webhook, allowing the
    ~1.5s WebSocket connection latency to overlap with Twilio call setup.

    Returns the connected session on success. Raises on failure so the caller
    can fall back to creating a new session.
    """
    session = RealtimeSession(
        call_sid=call_sid,
        caller_phone=caller_phone,
        instructions=ROADSIDE_ASSISTANT_INSTRUCTIONS,
        greeting=OPENING_GREETING,
        tools=REALTIME_TOOLS,
    )

    try:
        await session.connect_early()
        logger.info("Early OpenAI connection ready", call_sid=call_sid)
        return session
    except Exception:
        logger.exception("Early OpenAI connection failed", call_sid=call_sid)
        await session.close()
        raise


def _get_validator() -> RequestValidator:
    """Return a cached RequestValidator instance using the Twilio auth token."""
    global _validator
    if _validator is None:
        settings = get_settings()
        _validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
    return _validator


def _reconstruct_twilio_url(request: Request) -> str:
    """Reconstruct the original URL Twilio used to reach this endpoint.

    Behind TLS-terminating proxies (ngrok, nginx, Cloud Run, etc.),
    ASGI sees plain HTTP. Twilio signs the HTTPS URL it used, so we
    must reconstruct from forwarded headers.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.hostname or "")
    path = request.url.path
    return f"{proto}://{host}{path}"


def _build_media_stream_ws_url(request: Request) -> str:
    """Build the public WSS URL for the Twilio Media Stream endpoint.

    Cloud Run terminates TLS and forwards plain HTTP internally, so
    request.url.scheme is "http" and request.url.hostname is the
    internal address (e.g. 0.0.0.0).  We must reconstruct the public
    URL from the Host and X-Forwarded-Proto headers that Cloud Run
    sets on the incoming request.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.hostname or "")
    ws_scheme = "wss" if proto == "https" else "ws"
    return f"{ws_scheme}://{host}/api/v1/twilio/media-stream"


def _validate_twilio_request(url: str, signature: str, params: dict[str, str]) -> bool:
    """Validate that a request was signed by Twilio using the auth token."""
    validator = _get_validator()
    return bool(validator.validate(url, params, signature))


async def _match_and_link_demo_session(*, call_sid: str, caller_phone: str) -> None:
    """Claim a demo session for this call and link the assistance request.

    Never raises: each step logs its own failure with call correlation only
    (no phone or HMAC material), so a matching failure can never fail or
    delay the voice path beyond the caller's timebox. A miss (no active
    session for this number) is a normal outcome for non-demo calls.
    """
    try:
        claimed = await asyncio.to_thread(
            match_demo_session_for_call,
            call_id=call_sid,
            caller_phone=caller_phone,
        )
    except Exception:
        logger.exception("demo_session_match_failed", call_sid=call_sid)
        return

    if not claimed:
        logger.info("demo_session_match_miss", call_sid=call_sid)
        return

    demo_session_id = str(claimed.get("id") or "")
    if not demo_session_id:
        logger.error("demo_session_match_failed", call_sid=call_sid, reason="no_id")
        return

    logger.info(
        "demo_session_matched",
        call_sid=call_sid,
        demo_session_id=demo_session_id,
    )
    # link_demo_session is non-blocking internally (logs, never raises).
    await asyncio.to_thread(
        link_demo_session,
        call_id=call_sid,
        demo_session_id=demo_session_id,
    )


@router.post("/twilio/voice")
async def twilio_voice_webhook(request: Request) -> PlainTextResponse:
    """Handle incoming Twilio voice webhook and return TwiML to start a Media Stream.

    Validates the Twilio request signature before processing.
    Starts the OpenAI Realtime connection early so the ~1.5s WebSocket latency
    overlaps with Twilio call/media-stream setup.
    """
    twilio_signature = request.headers.get("X-Twilio-Signature", "")
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    url = _reconstruct_twilio_url(request)

    if not _validate_twilio_request(url, twilio_signature, params):
        logger.warning("Invalid Twilio signature", url=url)
        return PlainTextResponse("Invalid request", status_code=403)

    call_sid = params.get("CallSid", "unknown")
    caller_phone = params.get("From", "unknown")

    logger.info("Incoming call", call_sid=call_sid, caller_phone=caller_phone)

    # Start OpenAI Realtime connection early to overlap with Twilio call setup.
    # The connection task runs concurrently; the media stream handler will
    # await it when the Twilio Media Stream arrives.
    connection_task = asyncio.create_task(
        _start_early_openai_connection(call_sid=call_sid, caller_phone=caller_phone),
        name=f"early-openai-{call_sid}",
    )

    # Idempotently create the open assistance-request row for this call.
    # Failure or timeout is logged loudly but must never block the call path.
    assistance_request_id: str | None = None
    try:
        assistance_request = await asyncio.wait_for(
            asyncio.to_thread(
                start_assistance_request,
                call_id=call_sid,
                caller_phone=caller_phone,
            ),
            timeout=_ASSISTANCE_REQUEST_START_TIMEOUT_SECONDS,
        )
        assistance_request_id = assistance_request.get("id")
        logger.info(
            "assistance_request_started",
            call_sid=call_sid,
            assistance_request_id=assistance_request_id,
        )
    except TimeoutError:
        logger.error(
            "assistance_request_start_failed",
            call_sid=call_sid,
            reason="timeout",
        )
    except Exception:
        logger.exception("assistance_request_start_failed", call_sid=call_sid)

    # Match the inbound caller to an active demo session and link the row.
    # Failure or timeout is logged loudly but must never block the call path.
    try:
        await asyncio.wait_for(
            _match_and_link_demo_session(call_sid=call_sid, caller_phone=caller_phone),
            timeout=_DEMO_SESSION_MATCH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.error(
            "demo_session_match_failed",
            call_sid=call_sid,
            reason="timeout",
        )
    except Exception:
        logger.exception("demo_session_match_failed", call_sid=call_sid)

    _pending_connections[call_sid] = EarlyConnection(
        call_sid=call_sid,
        caller_phone=caller_phone,
        connection_task=connection_task,
        assistance_request_id=assistance_request_id,
    )

    ws_url = _build_media_stream_ws_url(request)

    logger.info(
        "TwiML response",
        call_sid=call_sid,
        media_stream_url=ws_url,
    )

    response = VoiceResponse()
    connect = response.connect()
    stream = connect.stream(url=ws_url)
    stream.parameter(name="call_sid", value=call_sid)
    stream.parameter(name="caller_phone", value=caller_phone)

    twiml = str(response)
    logger.debug("TwiML body", call_sid=call_sid, twiml=twiml)
    return PlainTextResponse(twiml, media_type="application/xml")


# Terminal Twilio call statuses that mean the call is over. Covers calls that
# end before the Media Stream connects, where no WebSocket teardown ever runs.
_TERMINAL_CALL_STATUSES = frozenset(
    {"completed", "busy", "failed", "no-answer", "canceled"}
)


@router.post("/twilio/status")
async def twilio_status_callback(request: Request) -> PlainTextResponse:
    """Finalize the assistance request when Twilio reports a terminal call status.

    Covers pre-stream termination (caller hangs up before the Media Stream
    connects), where no WebSocket teardown ever runs. Validated with the same
    Twilio signature check as the voice webhook. The abandon update is guarded
    so only still-open rows flip to 'abandoned' — 'completed' and 'escalated'
    rows are never overwritten, and retried callbacks are idempotent.
    """
    twilio_signature = request.headers.get("X-Twilio-Signature", "")
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    url = _reconstruct_twilio_url(request)

    if not _validate_twilio_request(url, twilio_signature, params):
        logger.warning("Invalid Twilio signature", url=url)
        return PlainTextResponse("Invalid request", status_code=403)

    call_sid = params.get("CallSid", "unknown")
    call_status = params.get("CallStatus", "unknown")

    logger.info(
        "twilio_status_callback",
        call_sid=call_sid,
        call_status=call_status,
    )

    if call_status in _TERMINAL_CALL_STATUSES:
        try:
            await asyncio.to_thread(abandon_if_open, call_id=call_sid)
        except Exception:
            logger.exception(
                "Failed to finalize intake status from status callback",
                call_sid=call_sid,
                call_status=call_status,
            )

    return PlainTextResponse("", status_code=200)


@router.websocket("/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket) -> None:
    """Handle Twilio Media Stream WebSocket connection.

    Bridges Twilio audio to/from an OpenAI Realtime session.
    Each call gets an isolated session instance. If an early OpenAI connection
    was started from the voice webhook, it is reused here.
    """
    await websocket.accept()

    session: RealtimeSession | None = None
    process_task: asyncio.Task[None] | None = None
    stream_sid: str | None = None
    call_sid: str | None = None
    early_connection: EarlyConnection | None = None

    async def send_audio_to_twilio(audio_b64: str) -> None:
        """Forward OpenAI audio delta to Twilio Media Stream."""
        if stream_sid is None:
            return
        try:
            await websocket.send_json({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": audio_b64},
            })
            # Record first audio sent to Twilio (only once per call)
            if session and session.latency_tracker:
                session.latency_tracker.record_event("first_twilio_audio_sent")
        except Exception:
            logger.warning("Failed to send audio to Twilio", call_sid=call_sid)

    async def clear_playback() -> None:
        """Flush assistant audio buffered for Twilio playback on interruption."""
        if stream_sid is None:
            return
        await send_clear_to_twilio(websocket, stream_sid=stream_sid, call_sid=call_sid)

    async def send_closing_mark(mark_name: str) -> None:
        """Request a playback mark for the closing audio on this stream."""
        await _send_media_stream_mark(
            websocket,
            stream_sid=stream_sid,
            mark_name=mark_name,
            call_sid=call_sid,
        )

    async def handle_session_error(error: Exception) -> None:
        """Log errors from the OpenAI Realtime session."""
        logger.error("Realtime session error", call_sid=call_sid, error=str(error))

    async def handle_tool_call(call_id: str, func_name: str, arguments: str) -> str:
        """Thin dispatcher: route tool calls to the appropriate handler."""
        logger.info("Tool call received", call_sid=call_sid, function=func_name)

        if func_name == "update_assistance_request":
            request_result = await handle_update_assistance_request(
                call_sid=call_sid,
                caller_phone=caller_phone,
                arguments=arguments,
            )
            return request_result.model_dump_json()

        if func_name == "transfer_to_emergency":
            transfer_result = await handle_transfer_to_emergency(
                call_sid=call_sid,
                arguments=arguments,
            )
            return transfer_result.model_dump_json()

        logger.warning("Unknown tool", function=func_name)
        return AssistanceRequestToolResult(status="error", error="Unknown tool").model_dump_json()

    try:
        while True:
            data = await websocket.receive_json()
            event = data.get("event")

            if event == "connected":
                logger.info("Media Stream connected")

            elif event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid")
                call_sid = (
                    start_data.get("customParameters", {}).get("call_sid")
                    or start_data.get("callSid")
                )
                caller_phone = start_data.get("customParameters", {}).get(
                    "caller_phone"
                )

                logger.info(
                    "Media Stream started",
                    stream_sid=stream_sid,
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                )

                # Check for an existing early OpenAI connection
                early_connection = _pending_connections.pop(call_sid, None)

                if early_connection is not None:
                    # Await the early connection task
                    early_task: asyncio.Task[None] | None = None
                    try:
                        session = await early_connection.connection_task
                        # Attach callbacks that need the WebSocket
                        session.on_audio_delta = send_audio_to_twilio
                        session.on_clear_playback = clear_playback
                        session.on_tool_call = handle_tool_call
                        session.on_error = handle_session_error
                        session.on_closing_finished = handle_closing_finished
                        session.on_closing_mark_requested = send_closing_mark
                        # Start draining OpenAI events BEFORE the greeting so
                        # the greeting audio and its response.done are always
                        # observed, then deliver the deferred greeting.
                        early_task = asyncio.create_task(
                            session.process_events()
                        )
                        process_task = early_task
                        await session.set_stream_sid_and_greet(
                            stream_sid or "unknown"
                        )
                        logger.info(
                            "Reused early OpenAI connection", call_sid=call_sid
                        )
                    except Exception:
                        logger.exception(
                            "Early connection failed, falling back",
                            call_sid=call_sid,
                        )
                        if early_task is not None:
                            early_task.cancel()
                            try:
                                await early_task
                            except asyncio.CancelledError:
                                pass
                            if process_task is early_task:
                                process_task = None
                        if session is not None:
                            await session.close()
                            session = None

                if session is None:
                    # Fallback: create session from scratch (no early connection
                    # or early connection failed)
                    call_state = call_manager.create(
                        twilio_call_id=call_sid or "unknown",
                        caller_phone=caller_phone or "unknown",
                    )
                    call_state.stream_sid = stream_sid
                    if early_connection is not None:
                        call_state.assistance_request_id = (
                            early_connection.assistance_request_id
                        )

                    session = RealtimeSession(
                        call_sid=call_sid or "unknown",
                        caller_phone=caller_phone or "unknown",
                        stream_sid=stream_sid or "unknown",
                        instructions=ROADSIDE_ASSISTANT_INSTRUCTIONS,
                        greeting=OPENING_GREETING,
                        tools=REALTIME_TOOLS,
                        on_audio_delta=send_audio_to_twilio,
                        on_clear_playback=clear_playback,
                        on_tool_call=handle_tool_call,
                        on_error=handle_session_error,
                        on_closing_finished=handle_closing_finished,
                        on_closing_mark_requested=send_closing_mark,
                    )

                    # Record call started and twilio stream started events
                    assert session.latency_tracker is not None
                    session.latency_tracker.record_event("call_started")
                    session.latency_tracker.record_event("twilio_stream_started")

                    try:
                        await session.connect()
                        process_task = asyncio.create_task(
                            session.process_events()
                        )
                        logger.info(
                            "OpenAI Realtime session connected",
                            call_sid=call_sid,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to connect OpenAI Realtime session",
                            call_sid=call_sid,
                        )
                        session = None
                else:
                    # Early connection succeeded — create call state.
                    # Event processing was already started above, BEFORE the
                    # greeting, so it must not be started a second time.
                    call_state = call_manager.create(
                        twilio_call_id=call_sid or "unknown",
                        caller_phone=caller_phone or "unknown",
                    )
                    call_state.stream_sid = stream_sid
                    if early_connection is not None:
                        call_state.assistance_request_id = (
                            early_connection.assistance_request_id
                        )

            elif event == "media":
                if session and session.is_connected:
                    payload = data.get("media", {}).get("payload", "")
                    if payload:
                        await session.send_audio(payload)

            elif event == "mark":
                mark_name = data.get("mark", {}).get("name") or ""
                if session is not None and mark_name:
                    session.acknowledge_closing_mark(mark_name)

            elif event in ("stop", "closed"):
                logger.info("Media Stream stopping", call_sid=call_sid)
                break

    except WebSocketDisconnect:
        logger.info("Media Stream disconnected", call_sid=call_sid)
    except Exception:
        logger.exception("Media Stream error", call_sid=call_sid)
    finally:
        # Clean up pending early connection if it was never consumed
        if early_connection is None and call_sid is not None:
            pending = _pending_connections.pop(call_sid, None)
            if pending is not None:
                pending.connection_task.cancel()
                try:
                    await pending.connection_task
                except (asyncio.CancelledError, Exception):
                    pass
                # Close session if it was created before task was cancelled
                if pending.session is not None:
                    await pending.session.close()

        if call_sid:
            # Finalize the intake row on disconnect: the guarded update flips
            # only still-open rows to abandoned (completed/escalated are never
            # overwritten) and never raises out of teardown.
            try:
                await asyncio.to_thread(abandon_if_open, call_id=call_sid)
            except Exception:
                logger.exception(
                    "Failed to finalize intake status on teardown", call_sid=call_sid
                )
            call_manager.remove(call_sid)
        if session:
            await session.close()
        if process_task:
            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
