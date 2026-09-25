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


def _parse_expiry_timestamp(value: Any) -> datetime:
    """Parse a demo_sessions ``expires_at`` value into a UTC datetime.

    Rejects anything that is not an ISO-8601 timestamp so a malformed
    database value fails loudly instead of silently corrupting the
    claim-time deadline extension.
    """
    if not isinstance(value, str):
        raise ValueError("demo session expires_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("demo session expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def claim_demo_session(*, session_id: str, call_id: str) -> dict[str, Any] | None:
    """Atomically claim an unexpired, unclaimed demo session for one call.

    Guarded update: the claim only lands while the row still has
    ``claimed_at IS NULL`` and ``expires_at`` in the future, so a second
    call (or a duplicate Twilio webhook) cannot claim the same session and
    an expired session cannot be claimed at all.

    A winning claim also extends the deadline in the same update to
    ``greatest(expires_at, now + DEMO_CLAIMED_SESSION_TTL_SECONDS)``, so
    browser access follows the claimed call instead of the creation clock.
    The pre-update WHERE clause still enforces the original claim deadline,
    and the extension can only ever move ``expires_at`` forward — the
    existing RLS policies (gated on ``expires_at > now()``) pick up the
    longer window with no schema or policy change.

    The ``max`` of the pre-read value and ``now + TTL`` is safe because the
    claim is the only writer of ``expires_at`` (creation writes it once) and
    the ``claimed_at IS NULL`` guard serializes concurrent claims — a losing
    claim writes nothing, so the pre-read value can never be stale-shortened
    by another writer. Any future writer of ``expires_at`` must preserve
    that invariant.

    Returns the claimed row, or None when nothing matched (missing, expired,
    or already claimed).
    """
    try:
        uuid.UUID(session_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("session_id must be a valid UUID") from exc

    claimed_ttl_seconds = get_settings().DEMO_CLAIMED_SESSION_TTL_SECONDS
    if claimed_ttl_seconds <= 0:
        raise ValueError("DEMO_CLAIMED_SESSION_TTL_SECONDS must be positive")

    supabase = _get_supabase()
    current = (
        supabase.table("demo_sessions")
        .select("expires_at")
        .eq("id", session_id)
        .limit(1)
        .execute()
    )

    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    extended_expires_at = now_dt + timedelta(seconds=claimed_ttl_seconds)
    if current.data:
        current_row = dict(current.data[0])  # type: ignore[arg-type]
        extended_expires_at = max(
            _parse_expiry_timestamp(current_row.get("expires_at")),
            extended_expires_at,
        )

    result = (
        supabase.table("demo_sessions")
        .update(
            {
                "claimed_at": now,
                "call_id": call_id,
                "expires_at": extended_expires_at.isoformat(),
            }
        )
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
        expires_at=extended_expires_at.isoformat(),
    )
    return dict(result.data[0])  # type: ignore[arg-type]


# Cap how many claimable candidates one inbound call will race through.
_MAX_CLAIM_CANDIDATES = 5


def match_demo_session_for_call(*, call_id: str, caller_phone: str) -> dict[str, Any] | None:
    """Find and claim at most one active demo session for an inbound Twilio call.

    Normalizes the caller number to E.164 and looks up unexpired, unclaimed
    sessions by keyed phone HMAC (newest first). Candidates are claimed
    through the existing atomic guarded claim, so a duplicate webhook or a
    concurrent call can never claim the same session twice; if this call
    already owns a claim it is returned as-is (duplicate-webhook idempotency).

    Returns the claimed session row, or None when there is nothing to match.
    Never logs the raw phone number or its HMAC; an unparseable caller number
    is treated as "no match" rather than an error.
    """
    try:
        phone_e164 = normalize_phone_e164(phone=caller_phone)
    except ValueError:
        logger.info("Demo session match skipped", call_id=call_id, reason="invalid_phone")
        return None

    supabase = _get_supabase()

    # Duplicate webhooks: this call may already own a claim (the table's
    # claim-fields constraint sets call_id together with claimed_at).
    existing = (
        supabase.table("demo_sessions").select("*").eq("call_id", call_id).limit(1).execute()
    )
    if existing.data:
        row = dict(existing.data[0])  # type: ignore[arg-type]
        logger.info(
            "Demo session already claimed for call",
            call_id=call_id,
            demo_session_id=row.get("id"),
        )
        return row

    now = datetime.now(UTC).isoformat()
    candidates = (
        supabase.table("demo_sessions")
        .select("*")
        .eq("phone_hmac", hash_phone(phone_e164=phone_e164))
        .is_("claimed_at", None)
        .gt("expires_at", now)
        .order("created_at", desc=True)
        .limit(_MAX_CLAIM_CANDIDATES)
        .execute()
    )

    candidate_rows: list[dict[str, Any]] = candidates.data or []  # type: ignore[assignment]
    for candidate in candidate_rows:
        claimed = claim_demo_session(session_id=str(candidate["id"]), call_id=call_id)
        if claimed is not None:
            return claimed

    return None
