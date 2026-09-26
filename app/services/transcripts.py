"""Call-transcript persistence service using Supabase.

Each completed caller / assistant turn is stored as one row in
``call_transcripts``. Writes are best-effort and non-blocking: failures are
logged loudly but never raised into the live voice loop.
"""

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


def insert_call_transcript(
    *,
    call_id: str,
    demo_session_id: str | None,
    speaker: str,
    text: str,
    seq: int,
) -> bool:
    """Insert one transcript turn for a call.

    Returns True when the row landed, False otherwise (including a
    duplicate ``(call_id, seq)`` conflict, which is treated as a dedup
    no-op). Never raises: DB failures are logged loudly so the live call
    is never broken by transcript persistence.
    """
    row: dict[str, Any] = {
        "call_id": call_id,
        "demo_session_id": demo_session_id,
        "speaker": speaker,
        "text": text,
        "seq": seq,
    }
    try:
        supabase = _get_supabase()
        supabase.table("call_transcripts").insert(row).execute()
    except APIError as exc:
        # SQLSTATE 23505 — duplicate (call_id, seq): another writer already
        # stored this turn; keep exactly-one-row-per-turn without failing.
        code = str(getattr(exc, "code", "") or "")
        message = str(getattr(exc, "message", "") or "")
        details = f"{code} {message}".strip()
        if code == "23505" or "23505" in details or "duplicate" in message.lower():
            logger.warning(
                "call_transcript_seq_conflict",
                call_id=call_id,
                speaker=speaker,
                seq=seq,
            )
            return False
        logger.exception(
            "call_transcript_persist_failed",
            call_id=call_id,
            speaker=speaker,
            seq=seq,
            demo_session_id=demo_session_id,
        )
        return False
    except Exception:
        logger.exception(
            "call_transcript_persist_failed",
            call_id=call_id,
            speaker=speaker,
            seq=seq,
            demo_session_id=demo_session_id,
        )
        return False
    logger.info(
        "call_transcript_persisted",
        call_id=call_id,
        speaker=speaker,
        seq=seq,
    )
    return True
