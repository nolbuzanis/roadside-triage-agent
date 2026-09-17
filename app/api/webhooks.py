"""Webhook handlers for Supabase database triggers."""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Header, HTTPException, Request

from app.core.config import get_settings
from app.services.notifier import notify_dispatcher

logger = structlog.get_logger(__name__)

router = APIRouter()


async def _dispatch_notification(ticket: dict) -> None:
    """Run the Twilio SMS send in a thread so it never blocks."""
    await asyncio.to_thread(
        notify_dispatcher,
        call_id=ticket.get("call_id", ""),
        caller_phone=ticket.get("caller_phone", ""),
        location=ticket.get("location", ""),
        vehicle=ticket.get("vehicle", ""),
        issue=ticket.get("issue", ""),
    )


@router.post("/webhooks/ticket-created")
async def ticket_created_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict:
    """Handle Supabase INSERT webhook for breakdown_tickets.

    Validates the shared secret, then dispatches a dispatcher SMS
    notification in the background so the webhook returns quickly.
    """
    settings = get_settings()

    if settings.WEBHOOK_SECRET and x_webhook_secret != settings.WEBHOOK_SECRET:
        logger.warning("Invalid webhook secret")
        raise HTTPException(status_code=403, detail="Forbidden")

    ticket = await request.json()

    call_id = ticket.get("call_id")
    if not call_id:
        logger.warning("Webhook missing call_id")
        raise HTTPException(status_code=400, detail="Missing call_id")

    logger.info("Ticket webhook received", call_id=call_id)

    # Fire-and-forget: notification must not delay the webhook response
    # and must not fail the ticket insert.
    task = asyncio.create_task(
        _dispatch_notification(ticket),
        name=f"notify-{call_id}",
    )
    task.add_done_callback(
        lambda t: logger.error("Notification task failed", exc_info=t.exception())
        if t.done() and t.exception()
        else None
    )

    return {"status": "accepted"}
