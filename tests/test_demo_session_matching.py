"""Tests for matching inbound Twilio calls to demo sessions and linking the row."""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_SESSION_ID = "99999999-8888-7777-6666-555555555555"
CALL_ID = "CA_test_call_sid_123"
PHONE_E164 = "+16045551234"
HMAC_SECRET = "test-demo-hmac-secret"

TWILIO_PARAMS = {
    "CallSid": CALL_ID,
    "From": PHONE_E164,
    "To": "+15559876543",
    "CallStatus": "ringing",
    "Direction": "inbound",
}


def _make_twilio_headers(signature: str = "valid_signature") -> dict[str, str]:
    return {
        "X-Twilio-Signature": signature,
        "Host": "example.com",
        "x-forwarded-proto": "https",
    }


def _make_settings(*, ttl_seconds: int = 900, claimed_ttl_seconds: int = 1800) -> MagicMock:
    settings = MagicMock()
    settings.DEMO_PHONE_HMAC_SECRET = HMAC_SECRET
    settings.DEMO_SESSION_TTL_SECONDS = ttl_seconds
    settings.DEMO_CLAIMED_SESSION_TTL_SECONDS = claimed_ttl_seconds
    return settings


def _expected_hmac() -> str:
    import hashlib
    import hmac as hmac_mod

    return hmac_mod.new(
        HMAC_SECRET.encode("utf-8"),
        PHONE_E164.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _mock_supabase_match(
    *,
    existing: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any] | None] | None = None,
) -> MagicMock:
    """Mock the three Supabase chains used by match_demo_session_for_call.

    - existing-claim lookup: select -> eq(call_id) -> limit -> execute
    - candidate lookup:      select -> eq(phone_hmac) -> is_ -> gt -> order -> limit -> execute
    - guarded claim:         update -> eq(id) -> is_ -> gt -> execute

    The claim's current-deadline read (select -> eq(id) -> limit) shares the
    existing-claim lookup's mock chain, so it returns the same `existing`
    data — normally an empty list, which models "no prior deadline read" and
    lets the claim fall back to now + DEMO_CLAIMED_SESSION_TTL_SECONDS.
    """
    sb = MagicMock()
    select_eq = sb.table.return_value.select.return_value.eq

    select_eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=existing or []
    )

    candidate_execute = (
        select_eq.return_value.is_.return_value.gt.return_value.order.return_value.limit.return_value.execute
    )
    candidate_execute.return_value = MagicMock(data=candidates or [])

    claim_execute = (
        sb.table.return_value.update.return_value.eq.return_value.is_.return_value.gt.return_value.execute
    )
    if claims is None:
        claim_execute.return_value = MagicMock(data=[])
    else:
        claim_execute.side_effect = [
            MagicMock(data=[row] if row is not None else []) for row in claims
        ]
    return sb


# ---------------------------------------------------------------------------
# Service: match_demo_session_for_call
# ---------------------------------------------------------------------------


class TestMatchDemoSessionForCall:
    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_invalid_phone_skips_without_db_access(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unparseable caller number is a no-match, never an error."""
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()

        with caplog.at_level("INFO"):
            result = match_demo_session_for_call(call_id=CALL_ID, caller_phone="client:xyz")

        assert result is None
        mock_get_sb.assert_not_called()
        events = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
        skipped = [e for e in events if e.get("event") == "Demo session match skipped"]
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "invalid_phone"
        assert skipped[0]["call_id"] == CALL_ID

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_existing_claim_for_same_call_is_idempotent(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """A duplicate webhook returns this call's existing claim without re-claiming."""
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()
        claimed_row = {
            "id": SESSION_ID,
            "call_id": CALL_ID,
            "claimed_at": "2026-09-23T12:00:00+00:00",
        }
        mock_get_sb.return_value = _mock_supabase_match(existing=[claimed_row])

        result = match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)

        assert result == claimed_row
        sb = mock_get_sb.return_value
        sb.table.return_value.update.assert_not_called()
        # Candidate lookup never runs once this call already owns a claim.
        select_eq = sb.table.return_value.select.return_value.eq
        select_eq.return_value.is_.assert_not_called()

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_claims_candidate_matching_caller_hmac(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """The caller's HMAC finds the active session and the guarded claim wins."""
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()
        candidate = {"id": SESSION_ID}
        claimed_row = {"id": SESSION_ID, "call_id": CALL_ID, "claimed_at": "now"}
        sb = _mock_supabase_match(candidates=[candidate], claims=[claimed_row])
        mock_get_sb.return_value = sb

        result = match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)

        assert result == claimed_row

        select_eq = sb.table.return_value.select.return_value.eq
        # First eq: idempotent existing-claim lookup; second eq: HMAC lookup.
        assert select_eq.call_args_list[0].args == ("call_id", CALL_ID)
        assert select_eq.call_args_list[1].args == ("phone_hmac", _expected_hmac())

        # Expired and already-claimed sessions are excluded from candidates.
        select_eq.return_value.is_.assert_called_once_with("claimed_at", None)
        gt = select_eq.return_value.is_.return_value.gt
        assert gt.call_args.args[0] == "expires_at"
        # The bound itself must exclude rows expiring before ~now.
        bound = datetime.fromisoformat(str(gt.call_args.args[1]))
        assert abs((datetime.now(UTC) - bound).total_seconds()) < 5

        # Newest session first, bounded candidate set.
        order = select_eq.return_value.is_.return_value.gt.return_value.order
        order.assert_called_once_with("created_at", desc=True)
        limit = order.return_value.limit
        assert limit.call_args.args[0] >= 1

        # The claim itself is the atomic guarded update by session id.
        update = sb.table.return_value.update
        payload = update.call_args.args[0]
        assert payload["call_id"] == CALL_ID
        # The winning claim also extends the deadline in the same update.
        datetime.fromisoformat(payload["expires_at"])
        update.return_value.eq.assert_called_once_with("id", SESSION_ID)
        update.return_value.eq.return_value.is_.assert_called_once_with("claimed_at", None)

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_no_candidates_returns_none(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """No claimable session for this number means no match and no claim."""
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()
        sb = _mock_supabase_match(candidates=[])
        mock_get_sb.return_value = sb

        result = match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)

        assert result is None
        sb.table.return_value.update.assert_not_called()

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_lost_claim_race_falls_through_to_next_candidate(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """If the first candidate is claimed concurrently, the next one is tried."""
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()
        claimed_row = {"id": OTHER_SESSION_ID, "call_id": CALL_ID, "claimed_at": "now"}
        sb = _mock_supabase_match(
            candidates=[{"id": SESSION_ID}, {"id": OTHER_SESSION_ID}],
            claims=[None, claimed_row],
        )
        mock_get_sb.return_value = sb

        result = match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)

        assert result == claimed_row
        update_eq = sb.table.return_value.update.return_value.eq
        assert update_eq.call_count == 2
        assert update_eq.call_args_list[0].args == ("id", SESSION_ID)
        assert update_eq.call_args_list[1].args == ("id", OTHER_SESSION_ID)

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_all_claims_lost_returns_none(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """When every candidate claim loses the race, nothing is matched."""
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()
        sb = _mock_supabase_match(
            candidates=[{"id": SESSION_ID}, {"id": OTHER_SESSION_ID}],
            claims=[None, None],
        )
        mock_get_sb.return_value = sb

        result = match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)

        assert result is None

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_sequential_duplicate_webhook_claims_only_once(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
    ) -> None:
        """A second delivery finds this call's claim and never re-claims.

        Models the realistic sequential Twilio retry: delivery one claims a
        candidate, delivery two sees the claim via the call_id lookup and
        returns it without a second guarded update.
        """
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()
        claimed_row = {"id": SESSION_ID, "call_id": CALL_ID, "claimed_at": "now"}
        sb = _mock_supabase_match(candidates=[{"id": SESSION_ID}], claims=[claimed_row])
        # The existing-claim lookup chain is shared with the claim's current-
        # deadline read, so: delivery one lookup, delivery one claim read,
        # delivery two lookup (sees delivery one's claim).
        sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[{"id": SESSION_ID, "expires_at": "2099-01-01T00:00:00+00:00"}]),
            MagicMock(data=[claimed_row]),
        ]
        mock_get_sb.return_value = sb

        first = match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)
        second = match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)

        assert first == claimed_row
        assert second == claimed_row
        # Exactly one guarded claim across both deliveries.
        sb.table.return_value.update.assert_called_once()

    @patch("app.services.demo_sessions._get_supabase")
    @patch("app.services.demo_sessions.get_settings")
    def test_logs_never_contain_phone_or_hmac(
        self,
        mock_settings: MagicMock,
        mock_get_sb: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Matching logs correlate by call/session ids only — never phone material."""
        from app.services.demo_sessions import match_demo_session_for_call

        mock_settings.return_value = _make_settings()
        claimed_row = {"id": SESSION_ID, "call_id": CALL_ID, "claimed_at": "now"}
        mock_get_sb.return_value = _mock_supabase_match(
            candidates=[{"id": SESSION_ID}],
            claims=[claimed_row],
        )

        with caplog.at_level("INFO"):
            match_demo_session_for_call(call_id=CALL_ID, caller_phone=PHONE_E164)

        events = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
        assert events, "expected structured log events from matching"
        log_blob = json.dumps(events)
        assert PHONE_E164 not in log_blob
        assert PHONE_E164.lstrip("+") not in log_blob
        assert _expected_hmac() not in log_blob


# ---------------------------------------------------------------------------
# Service: link_demo_session
# ---------------------------------------------------------------------------


class TestLinkDemoSession:
    @patch("app.services.tickets._get_supabase")
    def test_links_with_guarded_update(self, mock_get_sb: MagicMock) -> None:
        from app.services.tickets import link_demo_session

        sb = MagicMock()
        chain = sb.table.return_value.update.return_value.eq.return_value.is_
        chain.return_value.execute.return_value = MagicMock(data=[{"id": "req-1"}])
        mock_get_sb.return_value = sb

        link_demo_session(call_id=CALL_ID, demo_session_id=SESSION_ID)

        sb.table.assert_called_once_with("assistance_requests")
        payload = sb.table.return_value.update.call_args[0][0]
        assert payload == {"demo_session_id": SESSION_ID}
        sb.table.return_value.update.return_value.eq.assert_called_once_with("call_id", CALL_ID)
        chain.assert_called_once_with("demo_session_id", None)

    @patch("app.services.tickets._get_supabase")
    def test_zero_rows_warns_without_raising(
        self,
        mock_get_sb: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Matching no row (missing or already linked) is surfaced, not raised."""
        from app.services.tickets import link_demo_session

        sb = MagicMock()
        chain = sb.table.return_value.update.return_value.eq.return_value.is_
        chain.return_value.execute.return_value = MagicMock(data=[])
        mock_get_sb.return_value = sb

        with caplog.at_level("WARNING"):
            link_demo_session(call_id=CALL_ID, demo_session_id=SESSION_ID)

        events = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
        warnings = [
            e
            for e in events
            if e.get("event") == "Demo session link matched no assistance request"
        ]
        assert len(warnings) == 1
        assert warnings[0]["call_id"] == CALL_ID
        assert warnings[0]["demo_session_id"] == SESSION_ID

    @patch("app.services.tickets._get_supabase")
    def test_failure_logged_without_raising(self, mock_get_sb: MagicMock) -> None:
        from app.services.tickets import link_demo_session

        sb = MagicMock()
        sb.table.return_value.update.return_value.eq.return_value.is_.return_value.execute.side_effect = (
            RuntimeError("DB down")
        )
        mock_get_sb.return_value = sb

        link_demo_session(call_id=CALL_ID, demo_session_id=SESSION_ID)  # must not raise

    @patch("app.services.tickets._get_supabase")
    def test_link_logs_contain_no_phone_data(
        self,
        mock_get_sb: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from app.services.tickets import link_demo_session

        sb = MagicMock()
        chain = sb.table.return_value.update.return_value.eq.return_value.is_
        chain.return_value.execute.return_value = MagicMock(data=[{"id": "req-1"}])
        mock_get_sb.return_value = sb

        with caplog.at_level("INFO"):
            link_demo_session(call_id=CALL_ID, demo_session_id=SESSION_ID)

        events = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
        linked = [e for e in events if e.get("event") == "Demo session linked"]
        assert len(linked) == 1
        assert PHONE_E164 not in json.dumps(events)


# ---------------------------------------------------------------------------
# Voice webhook integration
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_start_assistance_request() -> Generator[MagicMock]:
    with patch(
        "app.api.twilio.start_assistance_request",
        return_value={
            "id": "req-test-123",
            "call_id": CALL_ID,
            "caller_phone": PHONE_E164,
            "status": "in_progress",
        },
    ) as mock_start:
        yield mock_start


@pytest.fixture(autouse=True)
def _mock_match_demo_session_for_call() -> Generator[MagicMock]:
    with patch(
        "app.api.twilio.match_demo_session_for_call",
        return_value=None,
    ) as mock_match:
        yield mock_match


@pytest.fixture(autouse=True)
def _mock_link_demo_session() -> Generator[MagicMock]:
    with patch("app.api.twilio.link_demo_session") as mock_link:
        yield mock_link


class TestWebhookDemoSessionMatching:
    @patch("app.api.twilio._validate_twilio_request")
    def test_valid_webhook_attempts_match(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
        _mock_start_assistance_request: MagicMock,
    ) -> None:
        """A validated webhook matches the caller after the row is created."""
        mock_validate.return_value = True

        response = client.post(
            "/api/v1/twilio/voice",
            data=TWILIO_PARAMS,
            headers=_make_twilio_headers(),
        )

        assert response.status_code == 200
        _mock_match_demo_session_for_call.assert_called_once_with(
            call_id=CALL_ID,
            caller_phone=PHONE_E164,
        )

    @patch("app.api.twilio._validate_twilio_request")
    def test_invalid_signature_never_matches(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
    ) -> None:
        """An invalid signature returns 403 before any matching runs."""
        mock_validate.return_value = False

        response = client.post(
            "/api/v1/twilio/voice",
            data=TWILIO_PARAMS,
            headers=_make_twilio_headers(signature="bad_signature"),
        )

        assert response.status_code == 403
        _mock_match_demo_session_for_call.assert_not_called()

    @patch("app.api.twilio._validate_twilio_request")
    def test_claimed_session_links_assistance_request(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
        _mock_link_demo_session: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A claimed session links the assistance request and logs the match."""
        mock_validate.return_value = True
        _mock_match_demo_session_for_call.return_value = {
            "id": SESSION_ID,
            "call_id": CALL_ID,
            "claimed_at": "now",
        }

        with caplog.at_level("INFO"):
            response = client.post(
                "/api/v1/twilio/voice",
                data=TWILIO_PARAMS,
                headers=_make_twilio_headers(),
            )

        assert response.status_code == 200
        _mock_link_demo_session.assert_called_once_with(
            call_id=CALL_ID,
            demo_session_id=SESSION_ID,
        )

        events = [
            json.loads(r.message) for r in caplog.records if r.message.startswith("{")
        ]
        matched = [e for e in events if e.get("event") == "demo_session_matched"]
        assert len(matched) == 1
        assert matched[0]["demo_session_id"] == SESSION_ID
        assert matched[0]["call_sid"] == CALL_ID
        assert PHONE_E164 not in json.dumps(matched)

    @patch("app.api.twilio._validate_twilio_request")
    def test_no_match_continues_normal_call_without_link(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
        _mock_link_demo_session: MagicMock,
        _mock_start_assistance_request: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Calls outside the public demo proceed normally with no link."""
        mock_validate.return_value = True
        _mock_match_demo_session_for_call.return_value = None

        with caplog.at_level("INFO"):
            response = client.post(
                "/api/v1/twilio/voice",
                data=TWILIO_PARAMS,
                headers=_make_twilio_headers(),
            )

        assert response.status_code == 200
        assert "<Connect>" in response.text
        _mock_start_assistance_request.assert_called_once()
        _mock_link_demo_session.assert_not_called()

        events = [
            json.loads(r.message) for r in caplog.records if r.message.startswith("{")
        ]
        misses = [e for e in events if e.get("event") == "demo_session_match_miss"]
        assert len(misses) == 1
        assert misses[0]["call_sid"] == CALL_ID

    @patch("app.api.twilio._validate_twilio_request")
    def test_match_failure_does_not_block_call(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
        _mock_link_demo_session: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A matching exception is logged loudly; TwiML still returns."""
        mock_validate.return_value = True
        _mock_match_demo_session_for_call.side_effect = RuntimeError("DB down")

        with caplog.at_level("ERROR"):
            response = client.post(
                "/api/v1/twilio/voice",
                data=TWILIO_PARAMS,
                headers=_make_twilio_headers(),
            )

        assert response.status_code == 200
        assert "<Connect>" in response.text
        _mock_link_demo_session.assert_not_called()

        events = [
            json.loads(r.message) for r in caplog.records if r.message.startswith("{")
        ]
        failures = [
            e for e in events if e.get("event") == "demo_session_match_failed"
        ]
        assert failures

    @patch("app.api.twilio._DEMO_SESSION_MATCH_TIMEOUT_SECONDS", 0.05)
    @patch("app.api.twilio._validate_twilio_request")
    def test_match_timeout_does_not_block_call(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
        _mock_link_demo_session: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stalled match is timeboxed; TwiML still returns promptly."""
        mock_validate.return_value = True
        _mock_match_demo_session_for_call.side_effect = lambda **kwargs: time.sleep(0.5)

        with caplog.at_level("INFO"):
            response = client.post(
                "/api/v1/twilio/voice",
                data=TWILIO_PARAMS,
                headers=_make_twilio_headers(),
            )

        assert response.status_code == 200
        assert "<Connect>" in response.text
        _mock_link_demo_session.assert_not_called()

        events = [
            json.loads(r.message) for r in caplog.records if r.message.startswith("{")
        ]
        timeout_logs = [
            e for e in events if e.get("event") == "demo_session_match_failed"
        ]
        assert timeout_logs
        assert timeout_logs[0].get("reason") == "timeout"

    @patch("app.api.twilio._validate_twilio_request")
    def test_duplicate_webhook_reinvokes_matching_with_same_call_id(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
        _mock_link_demo_session: MagicMock,
    ) -> None:
        """A retried webhook re-invokes matching with stable call/phone args.

        Claim dedup itself lives at the service level
        (test_sequential_duplicate_webhook_claims_only_once); this asserts
        the webhook keeps feeding the same identifiers on every retry.
        """
        mock_validate.return_value = True

        for _ in range(2):
            client.post(
                "/api/v1/twilio/voice",
                data=TWILIO_PARAMS,
                headers=_make_twilio_headers(),
            )

        assert _mock_match_demo_session_for_call.call_count == 2
        for call in _mock_match_demo_session_for_call.call_args_list:
            assert call.kwargs["call_id"] == CALL_ID
            assert call.kwargs["caller_phone"] == PHONE_E164
        # With no claimed session configured, the webhook never links.
        _mock_link_demo_session.assert_not_called()

    @patch("app.api.twilio._validate_twilio_request")
    def test_concurrent_callers_stay_isolated(
        self,
        mock_validate: MagicMock,
        _mock_match_demo_session_for_call: MagicMock,
    ) -> None:
        """Two simultaneous callers are matched independently by their own numbers."""
        mock_validate.return_value = True

        client.post(
            "/api/v1/twilio/voice",
            data={**TWILIO_PARAMS, "CallSid": "CA_call_1", "From": "+15551111111"},
            headers=_make_twilio_headers(),
        )
        client.post(
            "/api/v1/twilio/voice",
            data={**TWILIO_PARAMS, "CallSid": "CA_call_2", "From": "+15552222222"},
            headers=_make_twilio_headers(),
        )

        assert _mock_match_demo_session_for_call.call_count == 2
        _mock_match_demo_session_for_call.assert_any_call(
            call_id="CA_call_1", caller_phone="+15551111111"
        )
        _mock_match_demo_session_for_call.assert_any_call(
            call_id="CA_call_2", caller_phone="+15552222222"
        )
