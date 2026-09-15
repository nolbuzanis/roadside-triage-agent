import asyncio
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse  # type: ignore[import-untyped]

from app.realtime.session import RealtimeSession

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/twilio/voice")
async def twilio_voice_webhook(request: Request) -> str:
    """Handle incoming Twilio voice webhook and return TwiML to start a Media Stream."""
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller_phone = form.get("From", "unknown")

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

    return str(response)


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
