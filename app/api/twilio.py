import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse  # type: ignore[import-untyped]

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
    """Handle Twilio Media Stream WebSocket connection."""
    await websocket.accept()

    call_sid = None
    caller_phone = None
    stream_sid = None

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

            elif event == "media":
                pass  # Audio data handling will be implemented in Phase 2

            elif event in ("stop", "closed"):
                logger.info("Media Stream stopping: call_sid=%s", call_sid)
                break

    except WebSocketDisconnect:
        logger.info("Media Stream disconnected: call_sid=%s", call_sid)
    except Exception:
        logger.exception("Media Stream error: call_sid=%s", call_sid)
        raise
