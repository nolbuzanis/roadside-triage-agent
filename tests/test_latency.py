"""Tests for the latency instrumentation module."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.realtime.latency import CallLatencyTracker

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_tracker(call_id: str = "CA_test_call_sid") -> CallLatencyTracker:
    """Create a CallLatencyTracker with sensible defaults."""
    return CallLatencyTracker(call_id=call_id)


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


class TestEventRecording:
    """Tests for recording lifecycle events."""

    def test_record_event_sets_timestamp(self) -> None:
        tracker = _make_tracker()
        tracker.record_event("call_started")
        assert tracker._call_started > 0

    def test_record_event_logs_structured_entry(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = _make_tracker()
        with caplog.at_level("INFO"):
            tracker.record_event("call_started")
        
        assert len(caplog.records) == 1
        log_entry = json.loads(caplog.records[0].message)
        assert log_entry["event"] == "call_started"
        assert log_entry["call_id"] == "CA_test_call_sid"
        assert "timestamp" in log_entry
        assert "elapsed_ms" in log_entry

    def test_record_event_includes_openai_session_id(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = _make_tracker()
        tracker.openai_session_id = "sess_abc123"
        
        with caplog.at_level("INFO"):
            tracker.record_event("openai_session_created")
        
        log_entry = json.loads(caplog.records[0].message)
        assert log_entry["openai_session_id"] == "sess_abc123"

    def test_record_event_includes_extra_context(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = _make_tracker()
        with caplog.at_level("INFO"):
            tracker.record_event("tool_call_started", tool_call_id="call_abc", function="create_ticket")
        
        log_entry = json.loads(caplog.records[0].message)
        assert log_entry["tool_call_id"] == "call_abc"
        assert log_entry["function"] == "create_ticket"

    def test_first_openai_audio_recorded_only_once(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = _make_tracker()
        with caplog.at_level("INFO"):
            tracker.record_event("first_openai_audio_received")
            tracker.record_event("first_openai_audio_received")
        
        # Should only have one log entry
        assert len(caplog.records) == 1
        assert tracker._first_openai_audio_recorded is True

    def test_first_twilio_audio_recorded_only_once(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = _make_tracker()
        with caplog.at_level("INFO"):
            tracker.record_event("first_twilio_audio_sent")
            tracker.record_event("first_twilio_audio_sent")
        
        # Should only have one log entry
        assert len(caplog.records) == 1
        assert tracker._first_twilio_audio_recorded is True


# ---------------------------------------------------------------------------
# Latency calculations
# ---------------------------------------------------------------------------


class TestLatencyCalculations:
    """Tests for latency metric calculations."""

    def test_time_to_openai_session(self) -> None:
        tracker = _make_tracker()
        tracker._twilio_stream_started = 1000.0
        tracker._openai_session_created = 1050.0
        
        metrics = tracker.get_metrics()
        assert metrics["time_to_openai_session_ms"] == 50000

    def test_response_create_to_first_audio(self) -> None:
        tracker = _make_tracker()
        tracker._response_create_sent = 1000.0
        tracker._first_openai_audio_received = 1100.0
        
        metrics = tracker.get_metrics()
        assert metrics["response_create_to_first_audio_ms"] == 100000

    def test_first_openai_audio_to_twilio(self) -> None:
        tracker = _make_tracker()
        tracker._first_openai_audio_received = 1000.0
        tracker._first_twilio_audio_sent = 1005.0
        
        metrics = tracker.get_metrics()
        assert metrics["first_openai_audio_to_twilio_ms"] == 5000

    def test_total_initial_response_latency(self) -> None:
        tracker = _make_tracker()
        tracker._call_started = 1000.0
        tracker._first_twilio_audio_sent = 1200.0
        
        metrics = tracker.get_metrics()
        assert metrics["total_initial_response_latency_ms"] == 200000

    def test_total_call_duration(self) -> None:
        tracker = _make_tracker()
        tracker._call_started = 1000.0
        tracker._call_ended = 1500.0
        
        metrics = tracker.get_metrics()
        assert metrics["total_call_duration_ms"] == 500000

    def test_tool_call_duration(self) -> None:
        tracker = _make_tracker()
        with patch("app.realtime.latency.time.monotonic", return_value=1000.0):
            tracker.record_event("tool_call_started", tool_call_id="call_abc")
        with patch("app.realtime.latency.time.monotonic", return_value=1025.0):
            tracker.record_event("tool_call_completed", tool_call_id="call_abc")

        metrics = tracker.get_metrics()
        assert "tool_calls" in metrics
        assert len(metrics["tool_calls"]) == 1
        assert metrics["tool_calls"][0]["tool_call_id"] == "call_abc"
        assert metrics["tool_calls"][0]["duration_ms"] == 25000

    def test_empty_metrics_when_no_events(self) -> None:
        tracker = _make_tracker()
        metrics = tracker.get_metrics()
        assert metrics == {}


# ---------------------------------------------------------------------------
# Log latency metrics
# ---------------------------------------------------------------------------


class TestLogLatencyMetrics:
    """Tests for log_latency_metrics() method."""

    def test_log_latency_metrics_returns_metrics(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = _make_tracker()
        tracker._call_started = 1000.0
        tracker._first_twilio_audio_sent = 1200.0
        
        with caplog.at_level("INFO"):
            metrics = tracker.log_latency_metrics()
        
        assert metrics["total_initial_response_latency_ms"] == 200000

    def test_log_latency_metrics_logs_entry(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = _make_tracker()
        tracker._call_started = 1000.0
        tracker._first_twilio_audio_sent = 1200.0
        
        with caplog.at_level("INFO"):
            tracker.log_latency_metrics()
        
        # Find the latency_metrics log entry
        latency_entries = [
            r for r in caplog.records 
            if json.loads(r.message).get("event") == "latency_metrics"
        ]
        assert len(latency_entries) == 1
        log_entry = json.loads(latency_entries[0].message)
        assert log_entry["call_id"] == "CA_test_call_sid"
        assert log_entry["total_initial_response_latency_ms"] == 200000


# ---------------------------------------------------------------------------
# Concurrent calls
# ---------------------------------------------------------------------------


class TestConcurrentCalls:
    """Tests for independent timing state across concurrent calls."""

    def test_separate_trackers_have_independent_state(self) -> None:
        tracker1 = _make_tracker(call_id="CA_call_1")
        tracker2 = _make_tracker(call_id="CA_call_2")
        
        # Simulate different timing for each call
        tracker1._call_started = 1000.0
        tracker1._first_twilio_audio_sent = 1100.0
        
        tracker2._call_started = 2000.0
        tracker2._first_twilio_audio_sent = 2150.0
        
        metrics1 = tracker1.get_metrics()
        metrics2 = tracker2.get_metrics()
        
        assert metrics1["total_initial_response_latency_ms"] == 100000
        assert metrics2["total_initial_response_latency_ms"] == 150000

    def test_separate_trackers_have_independent_first_audio_flags(self) -> None:
        tracker1 = _make_tracker(call_id="CA_call_1")
        tracker2 = _make_tracker(call_id="CA_call_2")
        
        # Record first audio for tracker1
        tracker1.record_event("first_openai_audio_received")
        
        # tracker2 should not be affected
        assert tracker1._first_openai_audio_recorded is True
        assert tracker2._first_openai_audio_recorded is False


# ---------------------------------------------------------------------------
# Monotonic clock usage
# ---------------------------------------------------------------------------


class TestMonotonicClock:
    """Tests that monotonic clock is used for duration calculations."""

    def test_uses_monotonic_clock(self) -> None:
        tracker = _make_tracker()
        
        with patch("app.realtime.latency.time.monotonic", return_value=1000.0):
            tracker.record_event("call_started")
        
        with patch("app.realtime.latency.time.monotonic", return_value=1050.0):
            tracker.record_event("twilio_stream_started")
        
        metrics = tracker.get_metrics()
        assert metrics["twilio_stream_latency_ms"] == 50000
