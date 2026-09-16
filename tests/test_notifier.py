"""Tests for the dispatcher notification service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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

    @patch("app.services.notifier.update_notification_status")
    @patch("app.services.notifier.TwilioClient")
    @patch("app.services.notifier.get_settings")
    def test_twilio_failure_returns_false(
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
