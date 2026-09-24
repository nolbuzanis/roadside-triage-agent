"""Short-lived public demo sessions: phone matching and at-most-once claims."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import get_settings
from app.services.tickets import _get_supabase

logger = structlog.get_logger(__name__)

_SEPARATORS_RE = re.compile(r"[\s().\-.]+")


def normalize_phone_e164(*, phone: str) -> str:
    """Normalize a phone number to E.164, or raise ValueError.

    Accepts a leading-``+`` E.164 number, or NANP-style bare digits
    (10 digits assumed ``+1``, or 11 digits starting with ``1``).
    Common separators (spaces, dots, dashes, parentheses) are stripped.
    Anything else is rejected loudly. Error messages never echo the
    raw number so it cannot leak into logs.
    """
    if not isinstance(phone, str):
        raise ValueError("phone must be a string")
    cleaned = _SEPARATORS_RE.sub("", phone.strip())
    if not cleaned:
        raise ValueError("phone number is empty")

    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if not digits.isdigit() or not (8 <= len(digits) <= 15):
            raise ValueError("phone number is not a valid E.164 number")
        return f"+{digits}"

    if not cleaned.isdigit():
        raise ValueError("phone number contains invalid characters")
    if len(cleaned) == 10:
        return f"+1{cleaned}"
    if len(cleaned) == 11 and cleaned.startswith("1"):
        return f"+{cleaned}"
    raise ValueError("phone number is not a valid E.164 or NANP number")


def hash_phone(*, phone_e164: str) -> str:
    """Return the keyed HMAC-SHA256 (hex) of a normalized E.164 number.

    The key is the server-side DEMO_PHONE_HMAC_SECRET; the raw number is
    never persisted. The input must already be normalized so the same
    number always produces the same HMAC.
    """
    if not phone_e164.startswith("+"):
        raise ValueError("phone_e164 must be normalized (start with +)")
    settings = get_settings()
    return hmac.new(
        settings.DEMO_PHONE_HMAC_SECRET.encode("utf-8"),
        phone_e164.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def phone_last4(*, phone_e164: str) -> str:
    """Return the last four digits of a normalized E.164 number for display."""
    digits = phone_e164.lstrip("+")
    if len(digits) < 4 or not digits.isdigit():
        raise ValueError("phone_e164 must contain at least four digits")
    return digits[-4:]


def create_demo_session(
    *,
    auth_user_id: str,
    phone: str,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Create a short-lived demo session for an authenticated demo visitor.

    The raw phone number is normalized to E.164, then persisted only as a
    keyed HMAC plus its last four digits — the plaintext number is never
    written. Expiry defaults to the configured DEMO_SESSION_TTL_SECONDS
    (15 minutes) and is always set on insert.

    Returns the inserted row.
    """
    try:
        uuid.UUID(auth_user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("auth_user_id must be a valid UUID") from exc

    if ttl_seconds is None:
        ttl_seconds = get_settings().DEMO_SESSION_TTL_SECONDS
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    phone_e164 = normalize_phone_e164(phone=phone)
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()

    row: dict[str, Any] = {
        "auth_user_id": auth_user_id,
        "phone_hmac": hash_phone(phone_e164=phone_e164),
        "phone_last4": phone_last4(phone_e164=phone_e164),
        "expires_at": expires_at,
    }

    result = _get_supabase().table("demo_sessions").insert(row).execute()
    created = dict(result.data[0]) if result.data else row  # type: ignore[arg-type]
    logger.info(
        "Demo session created",
        demo_session_id=created.get("id"),
        expires_at=expires_at,
    )
    return created


def claim_demo_session(*, session_id: str, call_id: str) -> dict[str, Any] | None:
    """Atomically claim an unexpired, unclaimed demo session for one call.

    Guarded update: the claim only lands while the row still has
    ``claimed_at IS NULL`` and ``expires_at`` in the future, so a second
    call (or a duplicate Twilio webhook) cannot claim the same session and
    an expired session cannot be claimed at all. Returns the claimed row,
    or None when nothing matched (missing, expired, or already claimed).
    """
    try:
        uuid.UUID(session_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("session_id must be a valid UUID") from exc

    now = datetime.now(UTC).isoformat()
    result = (
        _get_supabase()
        .table("demo_sessions")
        .update({"claimed_at": now, "call_id": call_id})
        .eq("id", session_id)
        .is_("claimed_at", None)
        .gt("expires_at", now)
        .execute()
    )

    if not result.data:
        logger.info(
            "Demo session claim rejected",
            demo_session_id=session_id,
            call_id=call_id,
        )
        return None

    logger.info(
        "Demo session claimed",
        demo_session_id=session_id,
        call_id=call_id,
    )
    return dict(result.data[0])  # type: ignore[arg-type]
