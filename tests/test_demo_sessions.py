"""Tests for the short-lived demo-session model service."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

AUTH_USER_ID = "11111111-2222-3333-4444-555555555555"
SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CALL_ID = "CA_DEMO_CALL_1"
PHONE_E164 = "+16045551234"
HMAC_SECRET = "test-demo-hmac-secret"


def _make_settings(
    *, ttl_seconds: int = 900, claimed_ttl_seconds: int = 1800
) -> MagicMock:
    settings = MagicMock()
    settings.DEMO_PHONE_HMAC_SECRET = HMAC_SECRET
    settings.DEMO_SESSION_TTL_SECONDS = ttl_seconds
    settings.DEMO_CLAIMED_SESSION_TTL_SECONDS = claimed_ttl_seconds
    return settings


def _mock_supabase_insert(row: dict[str, Any]) -> MagicMock:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[row])
    return sb


def _mock_supabase_claim(
    row: dict[str, Any] | None,
    *,
    current_expires_at: str | None = None,
) -> MagicMock:
    """Mock the two Supabase chains used by claim_demo_session.

    - current-deadline read: select -> eq(id) -> limit -> execute
    - guarded claim:         update -> eq(id) -> is_(claimed_at) -> gt(expires_at) -> execute
    """
    sb = MagicMock()
    read = sb.table.return_value.select.return_value.eq.return_value.limit.return_value
    read.execute.return_value = MagicMock(
        data=[{"id": SESSION_ID, "expires_at": current_expires_at}] if current_expires_at else []
    )
    chain = (
        sb.table.return_value.update.return_value.eq.return_value.is_.return_value.gt.return_value
    )
    chain.execute.return_value = MagicMock(data=[row] if row is not None else [])
    return sb


class TestNormalizePhoneE164:
    """E.164 normalization accepts valid numbers and rejects everything else."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (PHONE_E164, PHONE_E164),
            ("6045551234", "+16045551234"),
            ("16045551234", "+16045551234"),
            (" (604) 555-1234 ", "+16045551234"),
            ("604.555.1234", "+16045551234"),
            ("+1 604 555 1234", "+16045551234"),
            ("+160455512345678", "+160455512345678"),
        ],
    )
    def test_valid_numbers(self, raw: str, expected: str) -> None:
        from app.services.demo_sessions import normalize_phone_e164

        assert normalize_phone_e164(phone=raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "not-a-phone", "+", "+abc", "12345", "12345678901234567890", "604555123x"],
    )
    def test_invalid_numbers_raise(self, raw: str) -> None:
        from app.services.demo_sessions import normalize_phone_e164

        with pytest.raises(ValueError):
            normalize_phone_e164(phone=raw)

    def test_error_message_does_not_echo_raw_phone(self) -> None:
        from app.services.demo_sessions import normalize_phone_e164

        with pytest.raises(ValueError) as excinfo:
            normalize_phone_e164(phone="boom-secret-number")
        assert "boom-secret-number" not in str(excinfo.value)


class TestHashPhone:
    """The HMAC is keyed, deterministic, and never stores the raw number."""

    @patch("app.services.demo_sessions.get_settings")
    def test_same_phone_same_hmac(self, mock_settings: MagicMock) -> None:
        from app.services.demo_sessions import hash_phone

        mock_settings.return_value = _make_settings()
        first = hash_phone(phone_e164=PHONE_E164)
        second = hash_phone(phone_e164=PHONE_E164)
        assert first == second
        assert len(first) == 64
        assert first.isalnum()

    @patch("app.services.demo_sessions.get_settings")
    def test_different_phones_different_hmac(self, mock_settings: MagicMock) -> None:
        from app.services.demo_sessions import hash_phone

        mock_settings.return_value = _make_settings()
        assert hash_phone(phone_e164=PHONE_E164) != hash_phone(phone_e164="+16045559999")

    @patch("app.services.demo_sessions.get_settings")
    def test_different_secret_different_hmac(self, mock_settings: MagicMock) -> None:
        from app.services.demo_sessions import hash_phone

        mock_settings.return_value = _make_settings()
        first = hash_phone(phone_e164=PHONE_E164)
        mock_settings.return_value = _make_settings()
        mock_settings.return_value.DEMO_PHONE_HMAC_SECRET = "another-secret"
        assert hash_phone(phone_e164=PHONE_E164) != first

    @patch("app.services.demo_sessions.get_settings")
    def test_hmac_never_contains_raw_phone(self, mock_settings: MagicMock) -> None:
        from app.services.demo_sessions import hash_phone

        mock_settings.return_value = _make_settings()
        digest = hash_phone(phone_e164=PHONE_E164)
        assert PHONE_E164 not in digest
        assert PHONE_E164.lstrip("+") not in digest

    @patch("app.services.demo_sessions.get_settings")
    def test_unnormalized_input_rejected(self, mock_settings: MagicMock) -> None:
        from app.services.demo_sessions import hash_phone

        mock_settings.return_value = _make_settings()
        with pytest.raises(ValueError):
            hash_phone(phone_e164="6045551234")


class TestPhoneLast4:
    def test_last_four_digits(self) -> None:
        from app.services.demo_sessions import phone_last4

        assert phone_last4(phone_e164=PHONE_E164) == "1234"

    def test_too_short_raises(self) -> None:
        from app.services.demo_sessions import phone_last4

        with pytest.raises(ValueError):
            phone_last4(phone_e164="+123")


class TestCreateDemoSession:
    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_create_persists_hmac_and_last4_only(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        from app.services.demo_sessions import create_demo_session

        mock_settings.return_value = _make_settings()
        sb = _mock_supabase_insert({"id": SESSION_ID})
        mock_get_sb.return_value = sb

        created = create_demo_session(auth_user_id=AUTH_USER_ID, phone=PHONE_E164)

        sb.table.assert_called_once_with("demo_sessions")
        payload = sb.table.return_value.insert.call_args[0][0]
        assert payload["auth_user_id"] == AUTH_USER_ID
        assert payload["phone_last4"] == "1234"
        assert len(payload["phone_hmac"]) == 64
        raw_phone_blob = json.dumps(payload)
        assert PHONE_E164 not in raw_phone_blob
        assert PHONE_E164.lstrip("+") not in raw_phone_blob
        assert created["id"] == SESSION_ID

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_default_ttl_is_fifteen_minutes(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        from app.services.demo_sessions import create_demo_session

        mock_settings.return_value = _make_settings(ttl_seconds=900)
        mock_get_sb.return_value = _mock_supabase_insert({"id": SESSION_ID})

        before = datetime.now(UTC)
        create_demo_session(auth_user_id=AUTH_USER_ID, phone=PHONE_E164)
        after = datetime.now(UTC)

        payload = mock_get_sb.return_value.table.return_value.insert.call_args[0][0]
        expires_at = datetime.fromisoformat(payload["expires_at"])
        assert before + timedelta(seconds=900) <= expires_at <= after + timedelta(seconds=900)

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_custom_ttl_overrides_default(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        from app.services.demo_sessions import create_demo_session

        mock_settings.return_value = _make_settings(ttl_seconds=900)
        mock_get_sb.return_value = _mock_supabase_insert({"id": SESSION_ID})

        before = datetime.now(UTC)
        create_demo_session(auth_user_id=AUTH_USER_ID, phone=PHONE_E164, ttl_seconds=60)
        after = datetime.now(UTC)

        payload = mock_get_sb.return_value.table.return_value.insert.call_args[0][0]
        expires_at = datetime.fromisoformat(payload["expires_at"])
        assert before + timedelta(seconds=60) <= expires_at <= after + timedelta(seconds=60)

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_invalid_phone_rejected_before_insert(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        from app.services.demo_sessions import create_demo_session

        mock_settings.return_value = _make_settings()
        with pytest.raises(ValueError):
            create_demo_session(auth_user_id=AUTH_USER_ID, phone="not-a-phone")
        mock_get_sb.return_value.table.return_value.insert.assert_not_called()

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_invalid_auth_user_id_rejected(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        from app.services.demo_sessions import create_demo_session

        mock_settings.return_value = _make_settings()
        with pytest.raises(ValueError):
            create_demo_session(auth_user_id="not-a-uuid", phone=PHONE_E164)
        mock_get_sb.return_value.table.return_value.insert.assert_not_called()

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_non_positive_ttl_rejected(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        from app.services.demo_sessions import create_demo_session

        mock_settings.return_value = _make_settings()
        with pytest.raises(ValueError):
            create_demo_session(auth_user_id=AUTH_USER_ID, phone=PHONE_E164, ttl_seconds=0)
        mock_get_sb.return_value.table.return_value.insert.assert_not_called()

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_create_logs_without_raw_phone_or_hmac(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from app.services.demo_sessions import create_demo_session

        mock_settings.return_value = _make_settings()
        mock_get_sb.return_value = _mock_supabase_insert({"id": SESSION_ID})

        with caplog.at_level("INFO"):
            created = create_demo_session(auth_user_id=AUTH_USER_ID, phone=PHONE_E164)

        events = [json.loads(r.message) for r in caplog.records]
        created_events = [e for e in events if e.get("event") == "Demo session created"]
        assert len(created_events) == 1
        assert created_events[0]["demo_session_id"] == SESSION_ID
        payload = mock_get_sb.return_value.table.return_value.insert.call_args[0][0]
        log_blob = json.dumps(events)
        assert PHONE_E164 not in log_blob
        assert payload["phone_hmac"] not in log_blob
        assert created["id"] == SESSION_ID


class TestParseExpiryTimestamp:
    """The claim-time deadline extension rejects malformed database values."""

    def test_aware_iso_timestamp_parses_to_same_instant(self) -> None:
        from app.services.demo_sessions import _parse_expiry_timestamp

        raw = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC).isoformat()
        assert _parse_expiry_timestamp(raw) == datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        from app.services.demo_sessions import _parse_expiry_timestamp

        assert _parse_expiry_timestamp("2026-09-24T12:00:00") == datetime(
            2026, 9, 24, 12, 0, 0, tzinfo=UTC
        )

    def test_invalid_timestamp_raises(self) -> None:
        from app.services.demo_sessions import _parse_expiry_timestamp

        with pytest.raises(ValueError, match="ISO-8601"):
            _parse_expiry_timestamp("not-a-timestamp")

    def test_non_string_value_raises(self) -> None:
        from app.services.demo_sessions import _parse_expiry_timestamp

        with pytest.raises(ValueError, match="ISO-8601"):
            _parse_expiry_timestamp(None)


class TestClaimDemoSession:
    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_claim_updates_guarded_row(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings(claimed_ttl_seconds=1800)
        claimed_row = {
            "id": SESSION_ID,
            "claimed_at": "2026-09-23T12:00:00+00:00",
            "call_id": CALL_ID,
        }
        current_expires_at = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
        sb = _mock_supabase_claim(claimed_row, current_expires_at=current_expires_at)
        mock_get_sb.return_value = sb

        before = datetime.now(UTC)
        result = claim_demo_session(session_id=SESSION_ID, call_id=CALL_ID)
        after = datetime.now(UTC)

        assert result == claimed_row
        # The current deadline is read first, then the guarded claim runs.
        assert sb.table.call_args_list == [call("demo_sessions"), call("demo_sessions")]
        select_eq = sb.table.return_value.select.return_value.eq
        select_eq.assert_called_once_with("id", SESSION_ID)
        select_eq.return_value.limit.assert_called_once_with(1)

        update = sb.table.return_value.update
        payload = update.call_args[0][0]
        assert payload["call_id"] == CALL_ID
        assert "claimed_at" in payload
        # greatest(expires_at, now + 1800): the claimed TTL wins here.
        extended = datetime.fromisoformat(payload["expires_at"])
        assert before + timedelta(seconds=1800) <= extended <= after + timedelta(seconds=1800)
        assert extended > datetime.fromisoformat(current_expires_at)

        update.return_value.eq.assert_called_once_with("id", SESSION_ID)
        update.return_value.eq.return_value.is_.assert_called_once_with("claimed_at", None)
        gt = update.return_value.eq.return_value.is_.return_value.gt
        assert gt.call_args[0][0] == "expires_at"
        datetime.fromisoformat(gt.call_args[0][1])

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_claim_extends_expiry_to_claimed_ttl(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """A winning claim moves the deadline to now + DEMO_CLAIMED_SESSION_TTL_SECONDS."""
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings(claimed_ttl_seconds=1800)
        current_expires_at = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
        sb = _mock_supabase_claim(
            {"id": SESSION_ID, "call_id": CALL_ID}, current_expires_at=current_expires_at
        )
        mock_get_sb.return_value = sb

        before = datetime.now(UTC)
        claim_demo_session(session_id=SESSION_ID, call_id=CALL_ID)
        after = datetime.now(UTC)

        payload = sb.table.return_value.update.call_args[0][0]
        extended = datetime.fromisoformat(payload["expires_at"])
        assert before + timedelta(seconds=1800) <= extended <= after + timedelta(seconds=1800)
        assert extended > datetime.fromisoformat(current_expires_at)

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_claim_falls_back_to_now_plus_claimed_ttl_when_no_current_row(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """With no current-deadline row the claim still writes now + TTL."""
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings(claimed_ttl_seconds=1800)
        sb = _mock_supabase_claim({"id": SESSION_ID, "call_id": CALL_ID})
        mock_get_sb.return_value = sb

        before = datetime.now(UTC)
        claim_demo_session(session_id=SESSION_ID, call_id=CALL_ID)
        after = datetime.now(UTC)

        payload = sb.table.return_value.update.call_args[0][0]
        extended = datetime.fromisoformat(payload["expires_at"])
        assert before + timedelta(seconds=1800) <= extended <= after + timedelta(seconds=1800)

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_claim_never_shortens_existing_deadline(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """greatest() keeps an already-longer deadline untouched."""
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings(claimed_ttl_seconds=1800)
        current_expires_at = (datetime.now(UTC) + timedelta(seconds=100_000)).isoformat()
        sb = _mock_supabase_claim(
            {"id": SESSION_ID, "call_id": CALL_ID}, current_expires_at=current_expires_at
        )
        mock_get_sb.return_value = sb

        claim_demo_session(session_id=SESSION_ID, call_id=CALL_ID)

        payload = sb.table.return_value.update.call_args[0][0]
        assert payload["expires_at"] == current_expires_at

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_expired_or_claimed_session_returns_none(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """A claim after the original deadline matches no row."""
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings()
        past_expires_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        sb = _mock_supabase_claim(None, current_expires_at=past_expires_at)
        mock_get_sb.return_value = sb

        result = claim_demo_session(session_id=SESSION_ID, call_id="CA_SECOND_CALL")

        assert result is None
        # The pre-update WHERE still enforces the original claim deadline.
        gt = sb.table.return_value.update.return_value.eq.return_value.is_.return_value.gt
        assert gt.call_args[0][0] == "expires_at"
        assert datetime.fromisoformat(gt.call_args[0][1]) > datetime.fromisoformat(
            past_expires_at
        )

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_second_claim_of_same_session_yields_none(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """A claimed session can never be claimed by a second call."""
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings()
        current_expires_at = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
        first = _mock_supabase_claim(
            {"id": SESSION_ID, "call_id": CALL_ID}, current_expires_at=current_expires_at
        )
        mock_get_sb.return_value = first
        assert claim_demo_session(session_id=SESSION_ID, call_id=CALL_ID) is not None

        second = _mock_supabase_claim(None, current_expires_at=current_expires_at)
        mock_get_sb.return_value = second
        assert claim_demo_session(session_id=SESSION_ID, call_id="CA_SECOND_CALL") is None

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_non_positive_claimed_ttl_rejected(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """A misconfigured claimed TTL fails loudly before touching the database."""
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings(claimed_ttl_seconds=0)

        with pytest.raises(ValueError, match="DEMO_CLAIMED_SESSION_TTL_SECONDS"):
            claim_demo_session(session_id=SESSION_ID, call_id=CALL_ID)
        mock_get_sb.assert_not_called()

    @patch("app.services.demo_sessions._get_supabase")
    def test_invalid_session_id_rejected(self, mock_get_sb: MagicMock) -> None:
        from app.services.demo_sessions import claim_demo_session

        with pytest.raises(ValueError):
            claim_demo_session(session_id="not-a-uuid", call_id=CALL_ID)
        mock_get_sb.return_value.table.return_value.update.assert_not_called()

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_rejection_is_logged_without_phone_data(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from app.services.demo_sessions import claim_demo_session

        mock_settings.return_value = _make_settings()
        mock_get_sb.return_value = _mock_supabase_claim(None)

        with caplog.at_level("INFO"):
            assert claim_demo_session(session_id=SESSION_ID, call_id=CALL_ID) is None

        events = [json.loads(r.message) for r in caplog.records]
        rejected = [e for e in events if e.get("event") == "Demo session claim rejected"]
        assert len(rejected) == 1
        assert rejected[0]["call_id"] == CALL_ID


class TestDemoSessionSettings:
    """Demo configuration fails loudly when the HMAC secret is missing."""

    def test_hmac_secret_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        from app.core.config import Settings

        monkeypatch.delenv("DEMO_PHONE_HMAC_SECRET", raising=False)
        with pytest.raises(ValidationError) as excinfo:
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                SUPABASE_URL="https://example.supabase.co",
                SUPABASE_SERVICE_ROLE_KEY="service-role-key",
                OPENAI_API_KEY="openai-key",
                OPENAI_REALTIME_MODEL="gpt-realtime",
                TWILIO_ACCOUNT_SID="ACxxxx",
                TWILIO_AUTH_TOKEN="token",
                TWILIO_PHONE_NUMBER="+16045550100",
                DISPATCHER_ALERT_PHONE="+16045550101",
                EMERGENCY_TRANSFER_PHONE="911",
            )
        assert "DEMO_PHONE_HMAC_SECRET" in str(excinfo.value)

    def test_empty_hmac_secret_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        from app.core.config import Settings

        monkeypatch.setenv("DEMO_PHONE_HMAC_SECRET", "")
        with pytest.raises(ValidationError) as excinfo:
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                SUPABASE_URL="https://example.supabase.co",
                SUPABASE_SERVICE_ROLE_KEY="service-role-key",
                OPENAI_API_KEY="openai-key",
                OPENAI_REALTIME_MODEL="gpt-realtime",
                TWILIO_ACCOUNT_SID="ACxxxx",
                TWILIO_AUTH_TOKEN="token",
                TWILIO_PHONE_NUMBER="+16045550100",
                DISPATCHER_ALERT_PHONE="+16045550101",
                EMERGENCY_TRANSFER_PHONE="911",
                DEMO_PHONE_HMAC_SECRET="",
            )
        assert "DEMO_PHONE_HMAC_SECRET" in str(excinfo.value)

    def test_session_ttl_defaults_to_fifteen_minutes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import Settings

        monkeypatch.delenv("DEMO_SESSION_TTL_SECONDS", raising=False)
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_SERVICE_ROLE_KEY="service-role-key",
            OPENAI_API_KEY="openai-key",
            OPENAI_REALTIME_MODEL="gpt-realtime",
            TWILIO_ACCOUNT_SID="ACxxxx",
            TWILIO_AUTH_TOKEN="token",
            TWILIO_PHONE_NUMBER="+16045550100",
            DISPATCHER_ALERT_PHONE="+16045550101",
            EMERGENCY_TRANSFER_PHONE="911",
            DEMO_PHONE_HMAC_SECRET="secret",
        )
        assert settings.DEMO_SESSION_TTL_SECONDS == 900

    def test_claimed_session_ttl_defaults_to_thirty_minutes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import Settings

        monkeypatch.delenv("DEMO_CLAIMED_SESSION_TTL_SECONDS", raising=False)
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_SERVICE_ROLE_KEY="service-role-key",
            OPENAI_API_KEY="openai-key",
            OPENAI_REALTIME_MODEL="gpt-realtime",
            TWILIO_ACCOUNT_SID="ACxxxx",
            TWILIO_AUTH_TOKEN="token",
            TWILIO_PHONE_NUMBER="+16045550100",
            DISPATCHER_ALERT_PHONE="+16045550101",
            EMERGENCY_TRANSFER_PHONE="911",
            DEMO_PHONE_HMAC_SECRET="secret",
        )
        assert settings.DEMO_CLAIMED_SESSION_TTL_SECONDS == 1800

    @pytest.mark.parametrize("bad_value", [0, -1])
    def test_claimed_session_ttl_rejects_non_positive_values(
        self,
        bad_value: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-positive claimed TTL fails startup validation loudly."""
        from pydantic import ValidationError

        from app.core.config import Settings

        monkeypatch.setenv("DEMO_CLAIMED_SESSION_TTL_SECONDS", str(bad_value))
        with pytest.raises(ValidationError) as excinfo:
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                SUPABASE_URL="https://example.supabase.co",
                SUPABASE_SERVICE_ROLE_KEY="service-role-key",
                OPENAI_API_KEY="openai-key",
                OPENAI_REALTIME_MODEL="gpt-realtime",
                TWILIO_ACCOUNT_SID="ACxxxx",
                TWILIO_AUTH_TOKEN="token",
                TWILIO_PHONE_NUMBER="+16045550100",
                DISPATCHER_ALERT_PHONE="+16045550101",
                EMERGENCY_TRANSFER_PHONE="911",
                DEMO_PHONE_HMAC_SECRET="secret",
            )
        assert "DEMO_CLAIMED_SESSION_TTL_SECONDS" in str(excinfo.value)
