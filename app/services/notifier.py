"""Dispatcher notification service — sends SMS alerts via Twilio."""

from __future__ import annotations

import logging

from twilio.rest import Client as TwilioClient  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.services.tickets import update_notification_status

logger = logging.getLogger(__name__)


def notify_dispatcher(
    *,
    call_id: str,
    caller_phone: str,
    location: str,
    vehicle: str,
    issue: str,
) -> bool:
    """Send an SMS alert to the dispatcher about a new breakdown ticket.

    Returns True if Twilio accepted the message, False otherwise.
    Always updates the notification_status on the ticket.
    """
    settings = get_settings()
    client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    body = (
        f"New breakdown ticket\n"
        f"Call: {call_id}\n"
        f"From: {caller_phone}\n"
        f"Location: {location}\n"
        f"Vehicle: {vehicle}\n"
        f"Issue: {issue}"
    )

    try:
        message = client.messages.create(
            body=body,
            from_=settings.TWILIO_PHONE_NUMBER,
            to=settings.DISPATCHER_ALERT_PHONE,
        )
        logger.info(
            "Dispatcher SMS sent: call_id=%s, sid=%s",
            call_id,
            message.sid,
        )
        update_notification_status(call_id=call_id, status="sent")
        return True
    except Exception:
        logger.exception("Dispatcher SMS failed: call_id=%s", call_id)
        update_notification_status(call_id=call_id, status="failed")
        return False
