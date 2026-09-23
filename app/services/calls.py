"""Per-call session state management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.realtime.session import RealtimeSession

logger = structlog.get_logger(__name__)


@dataclass
class EarlyConnection:
    """Tracks an OpenAI Realtime connection started before the Twilio Media Stream.

    Created in the voice webhook to begin the WebSocket connection early,
    allowing the ~1.5s connection latency to overlap with Twilio call setup.
    """

    call_sid: str
    caller_phone: str
    connection_task: asyncio.Task[RealtimeSession] = field(repr=False)
    session: RealtimeSession | None = field(default=None, repr=False)
    assistance_request_id: str | None = field(default=None, repr=False)


@dataclass
class CallState:
    """Mutable state for a single phone call."""

    twilio_call_id: str
    caller_phone: str
    stream_sid: str | None = None
    openai_session_id: str | None = None
    transfer_state: str = "none"
    assistance_request_id: str | None = None


class CallStateManager:
    """In-memory store mapping call IDs to their isolated state.

    Thread-safety is not required because all access happens on a single
    asyncio event loop. State is created when Twilio starts a media stream
    and removed when the call ends.
    """

    def __init__(self) -> None:
        self._calls: dict[str, CallState] = {}

    def create(
        self,
        *,
        twilio_call_id: str,
        caller_phone: str,
    ) -> CallState:
        """Create and track state for a new call."""
        state = CallState(
            twilio_call_id=twilio_call_id,
            caller_phone=caller_phone,
        )
        self._calls[twilio_call_id] = state
        logger.info("Call state created", call_sid=twilio_call_id)
        return state

    def get(self, call_sid: str) -> CallState | None:
        """Return the state for the given call, or None if not tracked."""
        return self._calls.get(call_sid)

    def remove(self, call_sid: str) -> None:
        """Remove state for a completed call. No-op if already removed."""
        removed = self._calls.pop(call_sid, None)
        if removed is not None:
            logger.info("Call state removed", call_sid=call_sid)

    @property
    def active_count(self) -> int:
        """Return the number of currently tracked calls."""
        return len(self._calls)


call_manager = CallStateManager()
