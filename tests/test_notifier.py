"""Tests for the dispatcher notification service."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.notifier import notify_dispatcher


class TestNotifyDispatcher:
    """Tests for the notify_dispatcher function."""

    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_sends_sms_and_updates_status(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        mock_settings.return_value = MagicMock(
            TWILIO_ACCOUNT_SID="AC_test",
            TWILIO_AUTH_TOKEN="test_token",
            TWILIO_PHONE_NUMBER="+15550000000",
            DISPATCHER_ALERT_PHONE="+15551111111",
        )
        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_message.sid = "SM_test_sid"
        mock_client.messages.create.return_value = mock_message
        mock_twilio_cls.return_value = mock_client

        result = notify_dispatcher(
            call_id="CA_test_call",
            caller_phone="+15552222222",
            location="123 Main St",
            vehicle="Toyota Camry 2020",
            issue="Flat tire",
        )

        assert result is True
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["to"] == "+15551111111"
        assert call_kwargs["from_"] == "+15550000000"
        assert "CA_test_call" in call_kwargs["body"]
        assert "123 Main St" in call_kwargs["body"]
        mock_update.assert_called_once_with(call_id="CA_test_call", status="sent")

    @patch("app.services.notifier.claim_notification_status", return_value=False)
    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_twilio_failure_returns_false(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
        mock_claim: MagicMock,
    ) -> None:
        mock_settings.return_value = MagicMock(
            TWILIO_ACCOUNT_SID="AC_test",
            TWILIO_AUTH_TOKEN="test_token",
            TWILIO_PHONE_NUMBER="+15550000000",
            DISPATCHER_ALERT_PHONE="+15551111111",
        )
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("Twilio API error")
        mock_twilio_cls.return_value = mock_client

        result = notify_dispatcher(
            call_id="CA_test_call",
            caller_phone="+15552222222",
            location="123 Main St",
            vehicle="Toyota Camry 2020",
            issue="Flat tire",
        )

        assert result is False
        mock_update.assert_called_once_with(call_id="CA_test_call", status="failed")
        mock_claim.assert_called_once_with(call_id="CA_test_call")


class TestNotifyDispatcherRetry:
    """A failed send re-claims the 'failed' row and retries; claim loss stops it."""

    @staticmethod
    def _settings() -> MagicMock:
        return MagicMock(
            TWILIO_ACCOUNT_SID="AC_test",
            TWILIO_AUTH_TOKEN="test_token",
            TWILIO_PHONE_NUMBER="+15550000000",
            DISPATCHER_ALERT_PHONE="+15551111111",
        )

    @patch("app.services.notifier.time.sleep")
    @patch("app.services.notifier.claim_notification_status", return_value=True)
    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_failed_send_is_reclaimed_and_retried_until_success(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
        mock_claim: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        mock_settings.return_value = self._settings()
        mock_client = MagicMock()
        ok_message = MagicMock()
        ok_message.sid = "SM_recovered"
        mock_client.messages.create.side_effect = [
            Exception("Twilio API error"),
            ok_message,
        ]
        mock_twilio_cls.return_value = mock_client

        result = notify_dispatcher(
            call_id="CA_retry",
            caller_phone="+15552222222",
            location="123 Main St",
            vehicle="Toyota Camry 2020",
            issue="Flat tire",
            retry_delay=0.25,
        )

        assert result is True
        assert mock_client.messages.create.call_count == 2
        mock_claim.assert_called_once_with(call_id="CA_retry")
        mock_sleep.assert_called_once_with(0.25)
        assert [c.kwargs for c in mock_update.call_args_list] == [
            {"call_id": "CA_retry", "status": "failed"},
            {"call_id": "CA_retry", "status": "sent"},
        ]

    @patch("app.services.notifier.time.sleep")
    @patch("app.services.notifier.claim_notification_status", return_value=False)
    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_retry_stops_when_the_claim_is_lost(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
        mock_claim: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        mock_settings.return_value = self._settings()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("Twilio API error")
        mock_twilio_cls.return_value = mock_client

        result = notify_dispatcher(
            call_id="CA_claim_lost",
            caller_phone="+15552222222",
            location="123 Main St",
            vehicle="Toyota Camry 2020",
            issue="Flat tire",
        )

        assert result is False
        mock_client.messages.create.assert_called_once()
        mock_claim.assert_called_once_with(call_id="CA_claim_lost")
        mock_sleep.assert_not_called()
        mock_update.assert_called_once_with(call_id="CA_claim_lost", status="failed")

    @patch("app.services.notifier.time.sleep")
    @patch("app.services.notifier.claim_notification_status", return_value=True)
    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_gives_up_after_max_attempts(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
        mock_claim: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        mock_settings.return_value = self._settings()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("Twilio API error")
        mock_twilio_cls.return_value = mock_client

        result = notify_dispatcher(
            call_id="CA_exhausted",
            caller_phone="+15552222222",
            location="123 Main St",
            vehicle="Toyota Camry 2020",
            issue="Flat tire",
            max_attempts=2,
            retry_delay=0,
        )

        assert result is False
        assert mock_client.messages.create.call_count == 2
        assert [c.kwargs for c in mock_update.call_args_list] == [
            {"call_id": "CA_exhausted", "status": "failed"},
            {"call_id": "CA_exhausted", "status": "failed"},
        ]

    @patch("app.services.notifier.claim_notification_status")
    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_successful_first_attempt_never_reclaims(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
        mock_claim: MagicMock,
    ) -> None:
        mock_settings.return_value = self._settings()
        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_message.sid = "SM_first_try"
        mock_client.messages.create.return_value = mock_message
        mock_twilio_cls.return_value = mock_client

        result = notify_dispatcher(
            call_id="CA_first_try",
            caller_phone="+15552222222",
            location="123 Main St",
            vehicle="Toyota Camry 2020",
            issue="Flat tire",
        )

        assert result is True
        mock_client.messages.create.assert_called_once()
        mock_claim.assert_not_called()
        mock_update.assert_called_once_with(call_id="CA_first_try", status="sent")


def _json_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Parse structlog JSON records captured by caplog."""
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]


class TestClaimRelease:
    """Every notify path resolves the 'sending' claim: a terminal write or an alarm."""

    @staticmethod
    def _settings() -> MagicMock:
        return MagicMock(
            TWILIO_ACCOUNT_SID="AC_test",
            TWILIO_AUTH_TOKEN="test_token",
            TWILIO_PHONE_NUMBER="+15550000000",
            DISPATCHER_ALERT_PHONE="+15551111111",
        )

    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_unexpected_exception_releases_the_claim(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        """An exception before any send still writes 'failed' so the row leaves 'sending'."""
        mock_settings.side_effect = RuntimeError("bad config")

        with pytest.raises(RuntimeError):
            notify_dispatcher(
                call_id="CA_abort",
                caller_phone="+15552222222",
                location="123 Main St",
                vehicle="Toyota Camry 2020",
                issue="Flat tire",
            )

        mock_update.assert_called_once_with(call_id="CA_abort", status="failed")

    @patch("app.services.notifier.time.sleep")
    @patch("app.services.notifier.claim_notification_status", return_value=False)
    @patch("app.services.notifier.update_notification_status", return_value=False)
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_terminal_write_failure_is_alarmed(
        self,
        mock_settings: MagicMock,
        mock_twilio_cls: MagicMock,
        mock_update: MagicMock,
        mock_claim: MagicMock,
        mock_sleep: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the terminal write never lands, the claim is retried then alarmed."""
        mock_settings.return_value = self._settings()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("Twilio API error")
        mock_twilio_cls.return_value = mock_client

        with caplog.at_level("ERROR"):
            result = notify_dispatcher(
                call_id="CA_stranded",
                caller_phone="+15552222222",
                location="123 Main St",
                vehicle="Toyota Camry 2020",
                issue="Flat tire",
            )

        assert result is False
        assert [c.kwargs for c in mock_update.call_args_list] == [
            {"call_id": "CA_stranded", "status": "failed"},
            {"call_id": "CA_stranded", "status": "failed"},
            {"call_id": "CA_stranded", "status": "failed"},
        ]
        stranded = [
            e
            for e in _json_events(caplog)
            if e.get("event") == "Dispatcher notification claim stranded"
        ]
        assert stranded, "expected an error-level claim-stranded event"
        assert stranded[0]["call_id"] == "CA_stranded"
        assert stranded[0]["status"] == "failed"
