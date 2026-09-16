"""Tests for the ticket-created webhook endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

TICKET_PAYLOAD = {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "call_id": "CA_test_call_123",
    "caller_phone": "+15551234567",
    "location": "123 Main St",
    "vehicle": "Toyota Camry 2020",
    "issue": "Flat tire",
    "status": "pending",
    "created_at": "2026-09-16T12:00:00Z",
}


class TestTicketCreatedWebhook:
    """Tests for POST /api/v1/webhooks/ticket-created."""

    @patch("app.api.webhooks._dispatch_notification", new_callable=AsyncMock)
    @patch("app.api.webhooks.get_settings")
    def test_valid_webhook_returns_accepted(
        self,
        mock_settings: MagicMock,
        mock_dispatch: AsyncMock,
    ) -> None:
        mock_settings.return_value = MagicMock(WEBHOOK_SECRET="")

        response = client.post(
            "/api/v1/webhooks/ticket-created",
            json=TICKET_PAYLOAD,
        )

        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}

    @patch("app.api.webhooks.get_settings")
    def test_missing_call_id_returns_400(
        self,
        mock_settings: MagicMock,
    ) -> None:
        mock_settings.return_value = MagicMock(WEBHOOK_SECRET="")

        response = client.post(
            "/api/v1/webhooks/ticket-created",
            json={"location": "123 Main St"},
        )

        assert response.status_code == 400

    @patch("app.api.webhooks._dispatch_notification", new_callable=AsyncMock)
    @patch("app.api.webhooks.get_settings")
    def test_valid_secret_succeeds(
        self,
        mock_settings: MagicMock,
        mock_dispatch: AsyncMock,
    ) -> None:
        mock_settings.return_value = MagicMock(WEBHOOK_SECRET="my_secret")

        response = client.post(
            "/api/v1/webhooks/ticket-created",
            json=TICKET_PAYLOAD,
            headers={"X-Webhook-Secret": "my_secret"},
        )

        assert response.status_code == 200

    @patch("app.api.webhooks.get_settings")
    def test_invalid_secret_returns_403(
        self,
        mock_settings: MagicMock,
    ) -> None:
        mock_settings.return_value = MagicMock(WEBHOOK_SECRET="my_secret")

        response = client.post(
            "/api/v1/webhooks/ticket-created",
            json=TICKET_PAYLOAD,
            headers={"X-Webhook-Secret": "wrong_secret"},
        )

        assert response.status_code == 403
