"""Tests for structured logging configuration and output."""

from __future__ import annotations

import json

import pytest
import structlog


class TestStructuredLoggingConfiguration:
    """Tests for structlog configuration and JSON output format."""

    def test_structlog_produces_json_output(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.json_output")
        with caplog.at_level("INFO"):
            logger.info("test_event", key="value")

        assert len(caplog.records) == 1
        log_data = json.loads(caplog.records[0].message)
        assert log_data["event"] == "test_event"
        assert log_data["key"] == "value"

    def test_log_record_includes_timestamp(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.timestamp")
        with caplog.at_level("INFO"):
            logger.info("timed_event")

        log_data = json.loads(caplog.records[0].message)
        assert "timestamp" in log_data
        assert "T" in log_data["timestamp"]

    def test_log_record_includes_log_level(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.level")
        with caplog.at_level("DEBUG"):
            logger.info("level_event")

        log_data = json.loads(caplog.records[0].message)
        assert log_data["level"] == "info"

    def test_log_record_includes_logger_name(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.module.name")
        with caplog.at_level("INFO"):
            logger.info("named_event")

        log_data = json.loads(caplog.records[0].message)
        assert log_data["logger"] == "test.module.name"

    def test_structured_fields_are_json_keys(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.fields")
        with caplog.at_level("INFO"):
            logger.info(
                "multi_field_event",
                call_sid="CA_test_123",
                caller_phone="+15551234567",
                status="active",
            )

        log_data = json.loads(caplog.records[0].message)
        assert log_data["call_sid"] == "CA_test_123"
        assert log_data["caller_phone"] == "+15551234567"
        assert log_data["status"] == "active"

    def test_warning_and_error_levels_work(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.levels")
        with caplog.at_level("DEBUG"):
            logger.warning("warning_event", reason="test")
            logger.error("error_event", code=500)

        assert len(caplog.records) == 2
        warning_data = json.loads(caplog.records[0].message)
        error_data = json.loads(caplog.records[1].message)

        assert warning_data["event"] == "warning_event"
        assert warning_data["level"] == "warning"
        assert warning_data["reason"] == "test"

        assert error_data["event"] == "error_event"
        assert error_data["level"] == "error"
        assert error_data["code"] == 500


class TestSecretLeakageAudit:
    """Tests to verify the application code does not log secrets."""

    def test_app_code_does_not_log_secrets(self) -> None:
        """Scan app source files for logger calls that include secret-like variables."""
        import re
        from pathlib import Path

        app_dir = Path(__file__).parent.parent / "app"
        # Match logger calls where a secret variable is passed as a value (e.g., call_sid=api_key)
        secret_patterns = re.compile(
            r"logger\.\w+\([^)]*\b(api_key|auth_token|supabase_service_role_key|password|secret_value|credential)\s*=",
            re.IGNORECASE,
        )

        violations = []
        for py_file in app_dir.rglob("*.py"):
            content = py_file.read_text()
            for line_num, line in enumerate(content.splitlines(), 1):
                if secret_patterns.search(line):
                    violations.append(f"{py_file}:{line_num}: {line.strip()}")

        assert not violations, (
            "Found logger calls that may leak secrets:\n" + "\n".join(violations)
        )

    def test_call_sid_is_safe_to_log(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.safe_fields")
        with caplog.at_level("INFO"):
            logger.info("call_event", call_sid="CA_abc123def456")

        log_data = json.loads(caplog.records[0].message)
        assert log_data["call_sid"] == "CA_abc123def456"

    def test_ticket_data_is_safe_to_log(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = structlog.get_logger("test.ticket_data")
        with caplog.at_level("INFO"):
            logger.info(
                "ticket_created",
                ticket_id="uuid-123",
                location="Main St",
                vehicle="Toyota Camry",
                issue="Flat tire",
            )

        log_data = json.loads(caplog.records[0].message)
        assert log_data["ticket_id"] == "uuid-123"
        assert log_data["location"] == "Main St"
        assert log_data["vehicle"] == "Toyota Camry"
        assert log_data["issue"] == "Flat tire"


class TestLatencyStructuredLogging:
    """Tests for latency module structured log output."""

    def test_latency_event_logs_structured_json(self, caplog: pytest.LogCaptureFixture) -> None:
        from app.realtime.latency import CallLatencyTracker

        tracker = CallLatencyTracker(call_id="CA_test_structured")
        with caplog.at_level("INFO"):
            tracker.record_event("call_started")

        log_data = json.loads(caplog.records[0].message)
        assert log_data["event"] == "call_started"
        assert log_data["call_id"] == "CA_test_structured"
        assert "timestamp" in log_data
        assert "elapsed_ms" in log_data

    def test_latency_metrics_logs_structured_json(self, caplog: pytest.LogCaptureFixture) -> None:
        from app.realtime.latency import CallLatencyTracker

        tracker = CallLatencyTracker(call_id="CA_test_metrics")
        tracker._call_started = 1000.0
        tracker._first_twilio_audio_sent = 1200.0

        with caplog.at_level("INFO"):
            tracker.log_latency_metrics()

        latency_entries = [
            r
            for r in caplog.records
            if json.loads(r.message).get("event") == "latency_metrics"
        ]
        assert len(latency_entries) == 1
        log_data = json.loads(latency_entries[0].message)
        assert log_data["call_id"] == "CA_test_metrics"
        assert log_data["total_initial_response_latency_ms"] == 200000

    def test_latency_includes_openai_session_id(self, caplog: pytest.LogCaptureFixture) -> None:
        from app.realtime.latency import CallLatencyTracker

        tracker = CallLatencyTracker(call_id="CA_test_session")
        tracker.openai_session_id = "sess_abc123"

        with caplog.at_level("INFO"):
            tracker.record_event("openai_session_created")

        log_data = json.loads(caplog.records[0].message)
        assert log_data["openai_session_id"] == "sess_abc123"

    def test_latency_extra_context_in_log(self, caplog: pytest.LogCaptureFixture) -> None:
        from app.realtime.latency import CallLatencyTracker

        tracker = CallLatencyTracker(call_id="CA_test_context")

        with caplog.at_level("INFO"):
            tracker.record_event("tool_call_started", tool_call_id="call_xyz")

        log_data = json.loads(caplog.records[0].message)
        assert log_data["tool_call_id"] == "call_xyz"
