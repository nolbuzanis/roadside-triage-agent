"""Tests for the /health endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@patch("app.main.get_settings")
def test_health_returns_ok_by_default(mock_settings: MagicMock) -> None:
    mock_settings.return_value = MagicMock()
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "response_time_ms" in body


@patch("app.main.get_settings")
def test_health_includes_response_time(mock_settings: MagicMock) -> None:
    mock_settings.return_value = MagicMock()
    resp = client.get("/health")
    body = resp.json()
    assert isinstance(body["response_time_ms"], (int, float))
    assert body["response_time_ms"] >= 0


@patch("app.main.get_settings")
def test_health_returns_error_when_config_invalid(mock_settings: MagicMock) -> None:
    mock_settings.side_effect = RuntimeError("missing env vars")
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "configuration_invalid"


@patch("app.services.tickets._get_supabase")
@patch("app.main.get_settings")
def test_health_check_db_ok(
    mock_settings: MagicMock,
    mock_get_supabase: MagicMock,
) -> None:
    mock_settings.return_value = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value.limit.return_value.execute.return_value = MagicMock()
    mock_client = MagicMock()
    mock_client.table.return_value = mock_table
    mock_get_supabase.return_value = mock_client

    resp = client.get("/health?check_db=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


@patch("app.services.tickets._get_supabase")
@patch("app.main.get_settings")
def test_health_check_db_error(
    mock_settings: MagicMock,
    mock_get_supabase: MagicMock,
) -> None:
    mock_settings.return_value = MagicMock()
    mock_get_supabase.side_effect = RuntimeError("connection refused")

    resp = client.get("/health?check_db=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"


@patch("app.main.get_settings")
def test_health_check_twilio_ok(mock_settings: MagicMock) -> None:
    settings = MagicMock()
    settings.TWILIO_ACCOUNT_SID = "AC_test"
    settings.TWILIO_AUTH_TOKEN = "test_token"
    mock_settings.return_value = settings

    mock_fetch = MagicMock()
    mock_accounts = MagicMock()
    mock_accounts.fetch.return_value = mock_fetch

    mock_twilio_client = MagicMock()
    mock_twilio_client.api.accounts.return_value = mock_accounts

    with patch("twilio.rest.Client", return_value=mock_twilio_client):
        resp = client.get("/health?check_twilio=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["twilio"] == "ok"


@patch("app.main.get_settings")
def test_health_check_twilio_error(mock_settings: MagicMock) -> None:
    settings = MagicMock()
    settings.TWILIO_ACCOUNT_SID = "AC_test"
    settings.TWILIO_AUTH_TOKEN = "bad_token"
    mock_settings.return_value = settings

    mock_twilio_client = MagicMock()
    mock_twilio_client.api.accounts.return_value.fetch.side_effect = RuntimeError("auth failed")

    with patch("twilio.rest.Client", return_value=mock_twilio_client):
        resp = client.get("/health?check_twilio=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["twilio"] == "error"


@patch("app.main.get_settings")
def test_health_check_openai_ok(mock_settings: MagicMock) -> None:
    settings = MagicMock()
    settings.OPENAI_API_KEY = "sk-test"
    mock_settings.return_value = settings

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None

    mock_client_instance = MagicMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)

    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.httpx.AsyncClient", mock_client_cls):
        resp = client.get("/health?check_openai=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["openai"] == "ok"


@patch("app.main.get_settings")
def test_health_check_openai_error(mock_settings: MagicMock) -> None:
    settings = MagicMock()
    settings.OPENAI_API_KEY = "sk-bad"
    mock_settings.return_value = settings

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = RuntimeError("unauthorized")

    mock_client_instance = MagicMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)

    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.httpx.AsyncClient", mock_client_cls):
        resp = client.get("/health?check_openai=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["openai"] == "error"
