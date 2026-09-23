"""Ticket persistence service using Supabase."""

from __future__ import annotations

from typing import Any

import structlog
from postgrest.exceptions import APIError

from app.core.config import get_settings
from supabase import Client, create_client

logger = structlog.get_logger(__name__)

_client: Client | None = None


def _get_supabase() -> Client:
    """Return a cached Supabase client using the service-role key."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return _client


def is_intake_complete(ticket: dict[str, Any]) -> bool:
    """Return True when the ticket row has non-empty location, vehicle, and issue."""
    return all(ticket.get(field) for field in ("location", "vehicle", "issue"))


def create_ticket(
    *,
    call_id: str,
    caller_phone: str,
    location: str | None = None,
    vehicle: str | None = None,
    issue: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create or merge-upsert a breakdown ticket in Supabase, keyed by call_id.

    Inserts a new row when missing; when the row exists, merges only the
    non-empty provided intake fields (omitted/empty fields are preserved and
    never nulled out). Raises ValueError when there is nothing to save.

    Concurrent duplicate inserts on call_id are handled atomically: a unique
    violation (SQLSTATE 23505) falls back to merging into the existing row.

    Returns the inserted/merged row as a dict.
    """
    provided: dict[str, str] = {
        field: value
        for field, value in (
            ("location", location),
            ("vehicle", vehicle),
            ("issue", issue),
        )
        if value
    }
    if not provided:
        raise ValueError("nothing to save: at least one intake field is required")

    supabase = _get_supabase()
    table = supabase.table("breakdown_tickets")

    existing = table.select("*").eq("call_id", call_id).execute()
    if existing.data:
        return _merge_into_existing(table, call_id, dict(existing.data[0]), provided)  # type: ignore[arg-type]

    row: dict[str, Any] = {
        "call_id": call_id,
        "caller_phone": caller_phone,
        **provided,
        "status": "pending",
        "notification_status": "pending",
    }
    if session_id:
        row["session_id"] = session_id

    try:
        result = table.insert(row).execute()
    except APIError as exc:
        if exc.code != "23505":
            raise
        raced = table.select("*").eq("call_id", call_id).execute()
        if not raced.data:
            raise
        logger.info("Ticket insert race resolved", call_id=call_id)
        return _merge_into_existing(table, call_id, dict(raced.data[0]), provided)  # type: ignore[arg-type]

    ticket: dict[str, Any] = dict(result.data[0]) if result.data else row  # type: ignore[arg-type]

    logger.info(
        "Ticket created",
        ticket_id=ticket.get("id"),
        call_id=call_id,
        location=provided.get("location"),
    )
    return ticket


def _merge_into_existing(
    table: Any,
    call_id: str,
    existing: dict[str, Any],
    provided: dict[str, str],
) -> dict[str, Any]:
    """Merge non-empty provided intake fields into an existing row.

    Only the provided intake fields are written; status, notification_status,
    and omitted intake fields are left untouched.
    """
    table.update(provided).eq("call_id", call_id).execute()
    merged = {**existing, **provided}
    logger.info(
        "Ticket updated",
        ticket_id=existing.get("id"),
        call_id=call_id,
        fields=sorted(provided),
    )
    return merged


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
        logger.info("Ticket escalated", call_id=call_id, reason=hazard_reason)
    except Exception:
        logger.exception("Failed to update hazard state", call_id=call_id)


def update_notification_status(*, call_id: str, status: str) -> None:
    """Update the notification_status field on a ticket.

    Non-blocking: logs failures but does not raise.
    """
    try:
        supabase = _get_supabase()
        supabase.table("breakdown_tickets").update(
            {"notification_status": status}
        ).eq("call_id", call_id).execute()
        logger.info("Notification status updated", call_id=call_id, status=status)
    except Exception:
        logger.exception("Failed to update notification status", call_id=call_id)
