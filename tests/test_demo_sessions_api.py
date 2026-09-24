"""Tests for the secure demo-session start endpoint (POST /api/v1/demo-sessions)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from supabase import AuthApiError

client = TestClient(app)

AUTH_USER_ID = "11111111-2222-3333-4444-555555555555"
SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TOKEN = "valid-supabase-access-token"
PHONE_E164 = "+16045551234"
HMAC_SECRET = "test-demo-hmac-secret"
DEMO_PHONE = "+16045550199"

PATH = "/api/v1/demo-sessions"


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.DEMO_PHONE_HMAC_SECRET = HMAC_SECRET
    settings.DEMO_SESSION_TTL_SECONDS = 900
    settings.TWILIO_PHONE_NUMBER = DEMO_PHONE
    return settings


def _mock_supabase_client(*, user: MagicMock | None, get_user: MagicMock) -> MagicMock:
    client = MagicMock()
    if user is not None:
        get_user.return_value = MagicMock(user=user)
    client.auth.get_user = get_user
    return client


def _anonymous_user() -> MagicMock:
    user = MagicMock()
    user.id = AUTH_USER_ID
    user.is_anonymous = True
    return user


def _insert_row() -> dict[str, Any]:
    return {
        "id": SESSION_ID,
        "auth_user_id": AUTH_USER_ID,
        "phone_hmac": "a" * 64,
        "phone_last4": "1234",
        "expires_at": "2026-09-23T19:00:00+00:00",
        "claimed_at": None,
        "call_id": None,
    }


def _db_supabase() -> MagicMock:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[_insert_row()]
    )
    return sb


class TestAuthRejections:
    def test_missing_authorization_header_returns_401(self) -> None:
        response = client.post(PATH, json={"phone": PHONE_E164})
        assert response.status_code == 401

    def test_non_bearer_scheme_returns_401(self) -> None:
        response = client.post(
            PATH,
            json={"phone": PHONE_E164},
            headers={"Authorization": f"Basic {TOKEN}"},
        )
        assert response.status_code == 401

    def test_empty_bearer_token_returns_401(self) -> None:
        response = client.post(
            PATH,
            json={"phone": PHONE_E164},
            headers={"Authorization": "Bearer "},
        )
        assert response.status_code == 401

    def test_invalid_token_returns_401(self) -> None:
        get_user = MagicMock(side_effect=AuthApiError("invalid access token", 401, None))
        supabase = _mock_supabase_client(user=None, get_user=get_user)
        with (
            patch("app.api.demo_sessions._get_supabase", return_value=supabase),
            patch(
                "app.api.demo_sessions.get_settings",
                return_value=_mock_settings(),
            ),
        ):
            response = client.post(PATH, json={"phone": PHONE_E164}, headers=_auth_header())
        assert response.status_code == 401
        get_user.assert_called_once_with(jwt=TOKEN)

    def test_non_anonymous_user_returns_403(self) -> None:
        user = _anonymous_user()
        user.is_anonymous = False
        get_user = MagicMock()
        supabase = _mock_supabase_client(user=user, get_user=get_user)
        with patch("app.api.demo_sessions._get_supabase", return_value=supabase):
            response = client.post(PATH, json={"phone": PHONE_E164}, headers=_auth_header())
        assert response.status_code == 403


class TestPhoneValidation:
    def test_invalid_phone_returns_422(self) -> None:
        get_user = MagicMock()
        supabase = _mock_supabase_client(user=_anonymous_user(), get_user=get_user)
        with patch("app.api.demo_sessions._get_supabase", return_value=supabase):
            response = client.post(
                PATH, json={"phone": "not-a-phone"}, headers=_auth_header()
            )
        assert response.status_code == 422

    def test_empty_phone_returns_422(self) -> None:
        get_user = MagicMock()
        supabase = _mock_supabase_client(user=_anonymous_user(), get_user=get_user)
        with patch("app.api.demo_sessions._get_supabase", return_value=supabase):
            response = client.post(PATH, json={"phone": ""}, headers=_auth_header())
        assert response.status_code == 422

    def test_missing_phone_returns_422(self) -> None:
        get_user = MagicMock()
        supabase = _mock_supabase_client(user=_anonymous_user(), get_user=get_user)
        with patch("app.api.demo_sessions._get_supabase", return_value=supabase):
            response = client.post(PATH, json={}, headers=_auth_header())
        assert response.status_code == 422


class TestStartDemoSession:
    def test_happy_path_returns_safe_session_metadata(self) -> None:
        get_user = MagicMock()
        auth_supabase = _mock_supabase_client(user=_anonymous_user(), get_user=get_user)
        db_supabase = _db_supabase()
        with (
            patch("app.api.demo_sessions._get_supabase", return_value=auth_supabase),
            patch(
                "app.api.demo_sessions.get_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "app.services.demo_sessions.get_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "app.services.demo_sessions._get_supabase",
                return_value=db_supabase,
            ),
        ):
            response = client.post(PATH, json={"phone": PHONE_E164}, headers=_auth_header())

        assert response.status_code == 201
        body = response.json()
        assert set(body) == {"id", "phone_last4", "expires_at", "demo_phone"}
        assert body["id"] == SESSION_ID
        assert body["phone_last4"] == "1234"
        assert body["expires_at"] == "2026-09-23T19:00:00+00:00"
        assert body["demo_phone"] == DEMO_PHONE
        assert "phone_hmac" not in response.text
        assert PHONE_E164 not in response.text
        assert HMAC_SECRET not in response.text
        get_user.assert_called_once_with(jwt=TOKEN)

    def test_session_owned_by_authenticated_anonymous_user(self) -> None:
        get_user = MagicMock()
        auth_supabase = _mock_supabase_client(user=_anonymous_user(), get_user=get_user)
        db_supabase = _db_supabase()
        with (
            patch("app.api.demo_sessions._get_supabase", return_value=auth_supabase),
            patch(
                "app.api.demo_sessions.get_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "app.services.demo_sessions.get_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "app.services.demo_sessions._get_supabase",
                return_value=db_supabase,
            ),
        ):
            response = client.post(PATH, json={"phone": PHONE_E164}, headers=_auth_header())

        assert response.status_code == 201
        insert_payload = db_supabase.table.return_value.insert.call_args[0][0]
        assert insert_payload["auth_user_id"] == AUTH_USER_ID
        assert PHONE_E164 not in str(insert_payload)
        assert insert_payload["phone_last4"] == "1234"
        assert len(insert_payload["phone_hmac"]) == 64
        assert HMAC_SECRET not in insert_payload["phone_hmac"]

    def test_insert_without_id_fails_loudly(self) -> None:
        get_user = MagicMock()
        auth_supabase = _mock_supabase_client(user=_anonymous_user(), get_user=get_user)
        db_supabase = _db_supabase()
        db_supabase.table.return_value.insert.return_value.execute.return_value = (
            MagicMock(data=[])
        )
        with (
            patch("app.api.demo_sessions._get_supabase", return_value=auth_supabase),
            patch(
                "app.api.demo_sessions.get_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "app.services.demo_sessions.get_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "app.services.demo_sessions._get_supabase",
                return_value=db_supabase,
            ),
        ):
            response = client.post(PATH, json={"phone": PHONE_E164}, headers=_auth_header())

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to create demo session"
