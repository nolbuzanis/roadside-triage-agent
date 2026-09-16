"""Ticket persistence service using Supabase."""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from supabase import Client, create_client

logger = logging.getLogger(__name__)

_client: Client | None = None


def _get_supabase() -> Client:
    """Return a cached Supabase client using the service-role key."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return _client


def create_ticket(
    *,
    call_id: str,
    caller_phone: str,
    location: str,
    vehicle: str,
    issue: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create a breakdown ticket in Supabase.

    Idempotent: if a ticket with the same call_id already exists, returns the
    existing ticket instead of creating a duplicate.

    Returns the inserted/existing row as a dict.
    """
    supabase = _get_supabase()
    table = supabase.table("breakdown_tickets")

    existing = table.select("*").eq("call_id", call_id).execute()
    if existing.data:
        logger.info("Ticket already exists for call_id=%s, returning existing", call_id)
        return dict(existing.data[0])  # type: ignore[arg-type]

    row: dict[str, Any] = {
        "call_id": call_id,
        "caller_phone": caller_phone,
        "location": location,
        "vehicle": vehicle,
        "issue": issue,
        "status": "pending",
        "notification_status": "pending",
    }
    if session_id:
        row["session_id"] = session_id

    result = table.insert(row).execute()
    ticket: dict[str, Any] = dict(result.data[0]) if result.data else row  # type: ignore[arg-type]

    logger.info(
        "Ticket created: id=%s, call_id=%s, location=%s",
        ticket.get("id"),
        call_id,
        location,
    )
    return ticket


def update_ticket_hazard(
    *,
    call_id: str,
    hazard_reason: str,
) -> None:
    """Mark a ticket as escalated due to hazard detection.

    Non-blocking: logs failures but does not raise.
    """
    try:
        supabase = _get_supabase()
        supabase.table("breakdown_tickets").update(
            {
                "hazard_detected": True,
                "hazard_reason": hazard_reason,
                "status": "escalated",
            }
        ).eq("call_id", call_id).execute()
        logger.info("Ticket escalated: call_id=%s, reason=%s", call_id, hazard_reason)
    except Exception:
        logger.exception("Failed to update hazard state: call_id=%s", call_id)
