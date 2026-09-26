"""Dispatcher notification service — sends SMS alerts via Twilio."""

from __future__ import annotations

import time

import structlog
from twilio.rest import Client as TwilioClient  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.services.tickets import claim_notification_status, update_notification_status

logger = structlog.get_logger(__name__)

_MAX_SEND_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 1.0
_TERMINAL_WRITE_ATTEMPTS = 3


def notify_dispatcher(
    *,
    call_id: str,
    caller_phone: str,
    location: str,
    vehicle: str,
    issue: str,
    max_attempts: int = _MAX_SEND_ATTEMPTS,
    retry_delay: float = _RETRY_DELAY_SECONDS,
) -> bool:
    """Send an SMS alert to the dispatcher about a new assistance request.

    The caller must already hold the send claim (claim_notification_status);
    every attempt resolves the claim with a single terminal status value
    ('sent' or 'failed') written through _release_claim (retried, alarmed on
    exhaustion), and any unexpected escape resolves it with 'failed' before
    re-raising, so within a live process the row never silently stays in
    'sending' — a process crash mid-send is the one exception, tracked as
    the stale-claim recovery follow-up. A released 'failed' row is
    claimable again by a later confirmation, while a 'sent' row is never
    re-sent. Returns True if Twilio accepted the message, False otherwise.
    If the terminal write itself cannot land after retries, an error-level
    'Dispatcher notification claim stranded' event is logged so operators
    can see the unreleased claim.
    """
    try:
        attempt = 0
        while True:
            attempt += 1
            if _send_dispatcher_sms(
                call_id=call_id,
                caller_phone=caller_phone,
                location=location,
                vehicle=vehicle,
                issue=issue,
            ):
                return True
            if attempt >= max_attempts:
                logger.warning(
                    "Dispatcher SMS failed; giving up",
                    call_id=call_id,
                    attempts=attempt,
                )
                return False
            if not claim_notification_status(call_id=call_id):
                logger.info(
                    "Dispatcher SMS retry skipped; notification not claimable",
                    call_id=call_id,
                    attempts=attempt,
                )
                return False
            time.sleep(retry_delay)
    except Exception:
        logger.exception(
            "Dispatcher notification aborted; releasing claim",
            call_id=call_id,
        )
        _release_claim(call_id=call_id, status="failed")
        raise


def _release_claim(*, call_id: str, status: str) -> None:
    """Release the send claim with a terminal notification_status write.

    Retries a swallowed write _TERMINAL_WRITE_ATTEMPTS times; if the row can
    never leave 'sending', logs an error-level claim-stranded event so the
    unrecovered claim is alarm-worthy rather than silent.
    """
    for attempt in range(1, _TERMINAL_WRITE_ATTEMPTS + 1):
        if update_notification_status(call_id=call_id, status=status):
            return
        if attempt < _TERMINAL_WRITE_ATTEMPTS:
            time.sleep(_RETRY_DELAY_SECONDS)
    logger.error(
        "Dispatcher notification claim stranded",
        call_id=call_id,
        status=status,
        attempts=_TERMINAL_WRITE_ATTEMPTS,
    )


def _send_dispatcher_sms(
    *,
    call_id: str,
    caller_phone: str,
    location: str,
    vehicle: str,
    issue: str,
) -> bool:
    """Attempt a single dispatcher SMS and record the sent/failed outcome."""
    settings = get_settings()
    client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    body = (
        f"New assistance request\n"
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
            "Dispatcher SMS sent",
            call_id=call_id,
            sms_sid=message.sid,
        )
        _release_claim(call_id=call_id, status="sent")
        return True
    except Exception:
        logger.exception("Dispatcher SMS failed", call_id=call_id)
        _release_claim(call_id=call_id, status="failed")
        return False
