"""Twilio call termination service using Twilio call control."""

from __future__ import annotations

import structlog
from twilio.rest import Client as TwilioClient  # type: ignore[import-untyped]

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


def hangup_call(*, call_sid: str) -> bool:
    """Terminate a live Twilio call.

    Uses Twilio's call update to set the call status to ``completed``, which
    hangs up the active call. Reuses the same Twilio client and credentials
    as the emergency transfer service.

    Returns True if Twilio accepted the termination, False otherwise.
    """
    settings = get_settings()
    client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    try:
        call = client.calls(call_sid).update(status="completed")
        logger.info(
            "Twilio call termination accepted",
            call_sid=call_sid,
            status=call.status,
        )
        return True
    except Exception:
        logger.exception("Twilio call termination failed", call_sid=call_sid)
        return False
