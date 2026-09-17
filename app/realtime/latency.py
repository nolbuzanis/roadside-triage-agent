"""Lightweight structured latency instrumentation for voice agent calls.

Tracks millisecond-precision timestamps for lifecycle events and calculates
key latency metrics along the Twilio -> FastAPI -> OpenAI Realtime -> Twilio
audio path.

Uses monotonic high-resolution clock for duration measurements to avoid
clock skew issues. Includes ISO/wall-clock timestamps in structured logs
for correlating events across systems.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CallLatencyTracker:
    """Tracks timestamps and calculates latency metrics for a single call.

    Each call gets its own isolated tracker instance. The tracker records
    timestamps for lifecycle events and calculates key latency metrics.

    Attributes:
        call_id: Twilio CallSid for correlation
        openai_session_id: OpenAI session ID (set when received)
    """

    call_id: str
    openai_session_id: str | None = None

    # Monotonic timestamps (seconds) for duration calculations
    _call_started: float = field(default=0.0, init=False)
    _twilio_stream_started: float = field(default=0.0, init=False)
    _early_connection_started: float = field(default=0.0, init=False)
    _openai_connection_started: float = field(default=0.0, init=False)
    _openai_websocket_connected: float = field(default=0.0, init=False)
    _early_connection_completed: float = field(default=0.0, init=False)
    _openai_session_created: float = field(default=0.0, init=False)
    _session_update_sent: float = field(default=0.0, init=False)
    _openai_session_updated: float = field(default=0.0, init=False)
    _response_create_sent: float = field(default=0.0, init=False)
    _first_openai_audio_received: float = field(default=0.0, init=False)
    _first_twilio_audio_sent: float = field(default=0.0, init=False)
    _call_ended: float = field(default=0.0, init=False)

    # Tool call tracking (can have multiple)
    _tool_calls: dict[str, dict[str, float]] = field(default_factory=dict, init=False)

    # Flags to ensure first-audio events are recorded only once
    _first_openai_audio_recorded: bool = field(default=False, init=False)
    _first_twilio_audio_recorded: bool = field(default=False, init=False)

    def record_event(self, event: str, **kwargs: Any) -> None:
        """Record a lifecycle event with current monotonic timestamp.

        Args:
            event: Event name (e.g., 'call_started', 'first_openai_audio_received')
            **kwargs: Additional context to include in the log (e.g., tool_call_id)
        """
        now = time.monotonic()
        wall_now = datetime.now(UTC).isoformat()

        # Record the timestamp based on event type
        if event == "call_started":
            self._call_started = now
        elif event == "twilio_stream_started":
            self._twilio_stream_started = now
        elif event == "early_connection_started":
            self._early_connection_started = now
        elif event == "openai_connection_started":
            self._openai_connection_started = now
        elif event == "openai_websocket_connected":
            self._openai_websocket_connected = now
        elif event == "early_connection_completed":
            self._early_connection_completed = now
        elif event == "openai_session_created":
            self._openai_session_created = now
        elif event == "session_update_sent":
            self._session_update_sent = now
        elif event == "openai_session_updated":
            self._openai_session_updated = now
        elif event == "response_create_sent":
            self._response_create_sent = now
        elif event == "first_openai_audio_received":
            if not self._first_openai_audio_recorded:
                self._first_openai_audio_received = now
                self._first_openai_audio_recorded = True
            else:
                # Already recorded, skip duplicate
                return
        elif event == "first_twilio_audio_sent":
            if not self._first_twilio_audio_recorded:
                self._first_twilio_audio_sent = now
                self._first_twilio_audio_recorded = True
            else:
                # Already recorded, skip duplicate
                return
        elif event == "call_ended":
            self._call_ended = now
        elif event == "tool_call_started":
            tool_call_id = kwargs.get("tool_call_id", "")
            if tool_call_id:
                self._tool_calls[tool_call_id] = {"started": now}
        elif event == "tool_call_completed":
            tool_call_id = kwargs.get("tool_call_id", "")
            if tool_call_id and tool_call_id in self._tool_calls:
                self._tool_calls[tool_call_id]["completed"] = now

        # Calculate elapsed time from call start
        elapsed_ms = 0
        if self._call_started > 0:
            elapsed_ms = int((now - self._call_started) * 1000)

        # Build log entry
        log_entry: dict[str, Any] = {
            "event": event,
            "call_id": self.call_id,
            "timestamp": wall_now,
            "elapsed_ms": elapsed_ms,
        }

        if self.openai_session_id:
            log_entry["openai_session_id"] = self.openai_session_id

        # Add any extra context
        for key, value in kwargs.items():
            log_entry[key] = value

        logger.info(json.dumps(log_entry))

    def log_latency_metrics(self) -> dict[str, Any]:
        """Calculate and log all latency metrics for this call.

        Returns:
            Dictionary of calculated latency metrics
        """
        metrics: dict[str, Any] = {
            "event": "latency_metrics",
            "call_id": self.call_id,
        }

        if self.openai_session_id:
            metrics["openai_session_id"] = self.openai_session_id

        # Calculate key latency metrics
        if self._call_started > 0 and self._twilio_stream_started > 0:
            metrics["twilio_stream_latency_ms"] = int(
                (self._twilio_stream_started - self._call_started) * 1000
            )

        if self._twilio_stream_started > 0 and self._openai_connection_started > 0:
            metrics["stream_to_connection_attempt_ms"] = int(
                (self._openai_connection_started - self._twilio_stream_started) * 1000
            )

        if self._early_connection_started > 0 and self._early_connection_completed > 0:
            metrics["early_connection_latency_ms"] = int(
                (self._early_connection_completed - self._early_connection_started) * 1000
            )

        if self._twilio_stream_started > 0 and self._early_connection_started > 0:
            metrics["twilio_stream_to_early_connection_started_ms"] = int(
                (self._early_connection_started - self._twilio_stream_started) * 1000
            )

        if self._early_connection_started > 0 and self._openai_websocket_connected > 0:
            metrics["early_connection_to_websocket_connected_ms"] = int(
                (self._openai_websocket_connected - self._early_connection_started) * 1000
            )

        if self._openai_connection_started > 0 and self._openai_websocket_connected > 0:
            metrics["websocket_connection_ms"] = int(
                (self._openai_websocket_connected - self._openai_connection_started) * 1000
            )

        if self._openai_websocket_connected > 0 and self._openai_session_created > 0:
            metrics["websocket_to_session_created_ms"] = int(
                (self._openai_session_created - self._openai_websocket_connected) * 1000
            )

        if self._openai_session_created > 0 and self._session_update_sent > 0:
            metrics["session_created_to_update_sent_ms"] = int(
                (self._session_update_sent - self._openai_session_created) * 1000
            )

        if self._session_update_sent > 0 and self._openai_session_updated > 0:
            metrics["session_update_roundtrip_ms"] = int(
                (self._openai_session_updated - self._session_update_sent) * 1000
            )

        if self._openai_session_updated > 0 and self._response_create_sent > 0:
            metrics["session_updated_to_response_create_ms"] = int(
                (self._response_create_sent - self._openai_session_updated) * 1000
            )

        if self._twilio_stream_started > 0 and self._openai_session_created > 0:
            metrics["time_to_openai_session_ms"] = int(
                (self._openai_session_created - self._twilio_stream_started) * 1000
            )

        if self._twilio_stream_started > 0 and self._session_update_sent > 0:
            metrics["twilio_stream_to_session_update_ms"] = int(
                (self._session_update_sent - self._twilio_stream_started) * 1000
            )

        if self._response_create_sent > 0 and self._first_openai_audio_received > 0:
            metrics["response_create_to_first_audio_ms"] = int(
                (self._first_openai_audio_received - self._response_create_sent) * 1000
            )

        if self._first_openai_audio_received > 0 and self._first_twilio_audio_sent > 0:
            metrics["first_openai_audio_to_twilio_ms"] = int(
                (self._first_twilio_audio_sent - self._first_openai_audio_received) * 1000
            )

        if self._call_started > 0 and self._first_openai_audio_received > 0:
            metrics["time_to_first_openai_audio_ms"] = int(
                (self._first_openai_audio_received - self._call_started) * 1000
            )

        if self._call_started > 0 and self._first_twilio_audio_sent > 0:
            metrics["total_initial_response_latency_ms"] = int(
                (self._first_twilio_audio_sent - self._call_started) * 1000
            )

        if self._call_started > 0 and self._call_ended > 0:
            metrics["total_call_duration_ms"] = int(
                (self._call_ended - self._call_started) * 1000
            )

        # Tool call durations
        tool_durations = []
        for tool_call_id, timings in self._tool_calls.items():
            if "started" in timings and "completed" in timings:
                duration_ms = int((timings["completed"] - timings["started"]) * 1000)
                tool_durations.append({
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                })
        if tool_durations:
            metrics["tool_calls"] = tool_durations

        # Add wall-clock timestamps for key events
        if self._call_started > 0:
            metrics["call_started_at"] = datetime.fromtimestamp(
                self._call_started, tz=UTC
            ).isoformat()
        if self._first_openai_audio_received > 0:
            metrics["first_audio_at"] = datetime.fromtimestamp(
                self._first_openai_audio_received, tz=UTC
            ).isoformat()
        if self._call_ended > 0:
            metrics["call_ended_at"] = datetime.fromtimestamp(
                self._call_ended, tz=UTC
            ).isoformat()

        logger.info(json.dumps(metrics))
        return metrics

    def get_metrics(self) -> dict[str, Any]:
        """Return calculated latency metrics without logging.

        Returns:
            Dictionary of calculated latency metrics
        """
        metrics: dict[str, Any] = {}

        if self._call_started > 0 and self._twilio_stream_started > 0:
            metrics["twilio_stream_latency_ms"] = int(
                (self._twilio_stream_started - self._call_started) * 1000
            )

        if self._twilio_stream_started > 0 and self._openai_connection_started > 0:
            metrics["stream_to_connection_attempt_ms"] = int(
                (self._openai_connection_started - self._twilio_stream_started) * 1000
            )

        if self._early_connection_started > 0 and self._early_connection_completed > 0:
            metrics["early_connection_latency_ms"] = int(
                (self._early_connection_completed - self._early_connection_started) * 1000
            )

        if self._twilio_stream_started > 0 and self._early_connection_started > 0:
            metrics["twilio_stream_to_early_connection_started_ms"] = int(
                (self._early_connection_started - self._twilio_stream_started) * 1000
            )

        if self._early_connection_started > 0 and self._openai_websocket_connected > 0:
            metrics["early_connection_to_websocket_connected_ms"] = int(
                (self._openai_websocket_connected - self._early_connection_started) * 1000
            )

        if self._openai_connection_started > 0 and self._openai_websocket_connected > 0:
            metrics["websocket_connection_ms"] = int(
                (self._openai_websocket_connected - self._openai_connection_started) * 1000
            )

        if self._openai_websocket_connected > 0 and self._openai_session_created > 0:
            metrics["websocket_to_session_created_ms"] = int(
                (self._openai_session_created - self._openai_websocket_connected) * 1000
            )

        if self._openai_session_created > 0 and self._session_update_sent > 0:
            metrics["session_created_to_update_sent_ms"] = int(
                (self._session_update_sent - self._openai_session_created) * 1000
            )

        if self._session_update_sent > 0 and self._openai_session_updated > 0:
            metrics["session_update_roundtrip_ms"] = int(
                (self._openai_session_updated - self._session_update_sent) * 1000
            )

        if self._openai_session_updated > 0 and self._response_create_sent > 0:
            metrics["session_updated_to_response_create_ms"] = int(
                (self._response_create_sent - self._openai_session_updated) * 1000
            )

        if self._twilio_stream_started > 0 and self._openai_session_created > 0:
            metrics["time_to_openai_session_ms"] = int(
                (self._openai_session_created - self._twilio_stream_started) * 1000
            )

        if self._twilio_stream_started > 0 and self._session_update_sent > 0:
            metrics["twilio_stream_to_session_update_ms"] = int(
                (self._session_update_sent - self._twilio_stream_started) * 1000
            )

        if self._response_create_sent > 0 and self._first_openai_audio_received > 0:
            metrics["response_create_to_first_audio_ms"] = int(
                (self._first_openai_audio_received - self._response_create_sent) * 1000
            )

        if self._first_openai_audio_received > 0 and self._first_twilio_audio_sent > 0:
            metrics["first_openai_audio_to_twilio_ms"] = int(
                (self._first_twilio_audio_sent - self._first_openai_audio_received) * 1000
            )

        if self._call_started > 0 and self._first_openai_audio_received > 0:
            metrics["time_to_first_openai_audio_ms"] = int(
                (self._first_openai_audio_received - self._call_started) * 1000
            )

        if self._call_started > 0 and self._first_twilio_audio_sent > 0:
            metrics["total_initial_response_latency_ms"] = int(
                (self._first_twilio_audio_sent - self._call_started) * 1000
            )

        if self._call_started > 0 and self._call_ended > 0:
            metrics["total_call_duration_ms"] = int(
                (self._call_ended - self._call_started) * 1000
            )

        # Tool call durations
        tool_durations = []
        for tool_call_id, timings in self._tool_calls.items():
            if "started" in timings and "completed" in timings:
                duration_ms = int((timings["completed"] - timings["started"]) * 1000)
                tool_durations.append({
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                })
        if tool_durations:
            metrics["tool_calls"] = tool_durations

        return metrics
