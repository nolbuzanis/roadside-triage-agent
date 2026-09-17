"""Emergency call transfer service using Twilio call control."""

from __future__ import annotations

import structlog
from twilio.rest import Client as TwilioClient  # type: ignore[import-untyped]

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


def transfer_call(*, call_sid: str, destination_phone: str) -> bool:
    """Transfer a live Twilio call to the given phone number.

    Uses Twilio's call update with TwiML to redirect the active call.
    The caller hears ringing as they are connected to the destination.

    Returns True if the transfer was accepted by Twilio, False otherwise.
    """
    settings = get_settings()
    client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    twiml = f"<Response><Dial>{destination_phone}</Dial></Response>"

    try:
        call = client.calls(call_sid).update(twiml=twiml)
        logger.info(
            "Emergency transfer initiated",
            call_sid=call_sid,
            destination=destination_phone,
            status=call.status,
        )
        return True
    except Exception:
        logger.exception(
            "Emergency transfer failed",
            call_sid=call_sid,
            destination=destination_phone,
        )
        return False
