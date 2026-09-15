import asyncio
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator  # type: ignore[import-untyped]
from twilio.twiml.voice_response import VoiceResponse  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.realtime.session import RealtimeSession

logger = logging.getLogger(__name__)

router = APIRouter()

_validator: RequestValidator | None = None


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


def _validate_twilio_request(url: str, signature: str, params: dict[str, str]) -> bool:
    """Validate that a request was signed by Twilio using the auth token."""
    validator = _get_validator()
    return bool(validator.validate(url, params, signature))


@router.post("/twilio/voice")
async def twilio_voice_webhook(request: Request) -> PlainTextResponse:
    """Handle incoming Twilio voice webhook and return TwiML to start a Media Stream.

    Validates the Twilio request signature before processing.
    """
    twilio_signature = request.headers.get("X-Twilio-Signature", "")
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    url = _reconstruct_twilio_url(request)

    if not _validate_twilio_request(url, twilio_signature, params):
        logger.warning("Invalid Twilio signature: url=%s", url)
        return PlainTextResponse("Invalid request", status_code=403)

    call_sid = params.get("CallSid", "unknown")
    caller_phone = params.get("From", "unknown")

    logger.info("Incoming call: CallSid=%s, From=%s", call_sid, caller_phone)

    host = request.url.hostname
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    ws_protocol = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{ws_protocol}://{host}:{port}/api/v1/twilio/media-stream"

    response = VoiceResponse()
    connect = response.connect()
    stream = connect.stream(url=ws_url)
    stream.parameter(name="call_sid", value=call_sid)
    stream.parameter(name="caller_phone", value=caller_phone)

    return PlainTextResponse(str(response), media_type="application/xml")


@router.websocket("/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket) -> None:
    """Handle Twilio Media Stream WebSocket connection.

    Bridges Twilio audio to/from an OpenAI Realtime session.
    Each call gets an isolated session instance.
    """
    await websocket.accept()

    session: RealtimeSession | None = None
    process_task: asyncio.Task[None] | None = None
    stream_sid: str | None = None
    call_sid: str | None = None

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
        except Exception:
            logger.warning("Failed to send audio to Twilio: call_sid=%s", call_sid)

    async def handle_session_error(error: Exception) -> None:
        """Log errors from the OpenAI Realtime session."""
        logger.error("Realtime session error: call_sid=%s, error=%s", call_sid, error)

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
                    "Media Stream started: stream_sid=%s, call_sid=%s, caller=%s",
                    stream_sid,
                    call_sid,
                    caller_phone,
                )

                session = RealtimeSession(
                    call_sid=call_sid or "unknown",
                    caller_phone=caller_phone or "unknown",
                    stream_sid=stream_sid or "unknown",
                    on_audio_delta=send_audio_to_twilio,
                    on_error=handle_session_error,
                )

                try:
                    await session.connect()
                    process_task = asyncio.create_task(session.process_events())
                    logger.info("OpenAI Realtime session connected: call_sid=%s", call_sid)
                except Exception:
                    logger.exception("Failed to connect OpenAI Realtime session: call_sid=%s", call_sid)
                    session = None

            elif event == "media":
                if session and session.is_connected:
                    payload = data.get("media", {}).get("payload", "")
                    if payload:
                        await session.send_audio(payload)

            elif event in ("stop", "closed"):
                logger.info("Media Stream stopping: call_sid=%s", call_sid)
                break

    except WebSocketDisconnect:
        logger.info("Media Stream disconnected: call_sid=%s", call_sid)
    except Exception:
        logger.exception("Media Stream error: call_sid=%s", call_sid)
    finally:
        if session:
            await session.close()
        if process_task:
            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
