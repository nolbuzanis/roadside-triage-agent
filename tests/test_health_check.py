"""Tests for the /health endpoint."""

from unittest.mock import MagicMock, patch

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
