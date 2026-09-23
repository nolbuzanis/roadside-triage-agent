"""Tests for the Twilio voice webhook endpoint."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

TWILIO_PARAMS = {
    "CallSid": "CA_test_call_sid_123",
    "From": "+15551234567",
    "To": "+15559876543",
    "CallStatus": "ringing",
    "Direction": "inbound",
}


def _make_twilio_headers(signature: str = "valid_signature") -> dict[str, str]:
    """Create headers simulating a Twilio webhook request."""
    return {
        "X-Twilio-Signature": signature,
        "Host": "example.com",
        "x-forwarded-proto": "https",
    }


@pytest.fixture(autouse=True)
def _mock_start_assistance_request() -> MagicMock:
    """Mock early assistance-request creation so webhook tests never hit Supabase."""
    with patch(
        "app.api.twilio.start_assistance_request",
        return_value={
            "id": "req-test-123",
            "call_id": "CA_test_call_sid_123",
            "caller_phone": "+15551234567",
            "status": "in_progress",
            "location": None,
            "vehicle": None,
            "issue": None,
        },
    ) as mock_start:
        yield mock_start


@patch("app.api.twilio._validate_twilio_request")
def test_valid_twilio_request_returns_twiml(mock_validate: MagicMock) -> None:
    """A valid Twilio request should return TwiML XML with a Media Stream."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]
    body = response.text
    assert "<Connect>" in body
    assert "<Stream" in body
    assert "/api/v1/twilio/media-stream" in body
    assert "CA_test_call_sid_123" in body
    assert "+15551234567" in body


@patch("app.api.twilio._validate_twilio_request")
def test_invalid_twilio_signature_returns_403(mock_validate: MagicMock) -> None:
    """An invalid Twilio signature should return 403."""
    mock_validate.return_value = False

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(signature="bad_signature"),
    )

    assert response.status_code == 403
    assert "Invalid request" in response.text


@patch("app.api.twilio._validate_twilio_request")
def test_missing_twilio_signature_returns_403(mock_validate: MagicMock) -> None:
    """A request with no Twilio signature should be rejected."""
    mock_validate.return_value = False

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers={"Host": "example.com", "x-forwarded-proto": "https"},
    )

    assert response.status_code == 403


@patch("app.api.twilio._validate_twilio_request")
def test_caller_phone_extracted_from_form(mock_validate: MagicMock) -> None:
    """The caller phone number should be extracted from the 'From' field."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data={**TWILIO_PARAMS, "From": "+15559998888"},
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "+15559998888" in response.text


@patch("app.api.twilio._validate_twilio_request")
def test_call_sid_passed_to_stream(mock_validate: MagicMock) -> None:
    """The CallSid should be passed as a stream parameter."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data={**TWILIO_PARAMS, "CallSid": "CA_unique_sid_abc"},
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "CA_unique_sid_abc" in response.text


@patch("app.api.twilio.get_settings")
@patch("app.api.twilio.RequestValidator")
def test_validate_twilio_request_uses_auth_token(
    mock_validator_cls: MagicMock,
    mock_get_settings: MagicMock,
) -> None:
    """The validator should be constructed with the Twilio auth token."""
    mock_settings = MagicMock()
    mock_settings.TWILIO_AUTH_TOKEN = "test_auth_token_123"
    mock_get_settings.return_value = mock_settings

    mock_validator = MagicMock()
    mock_validator.validate.return_value = True
    mock_validator_cls.return_value = mock_validator

    from app.api.twilio import _validate_twilio_request

    result = _validate_twilio_request(
        "https://example.com/api/v1/twilio/voice",
        "test_signature",
        {"CallSid": "CA_test"},
    )

    assert result is True
    mock_validator_cls.assert_called_with("test_auth_token_123")
    mock_validator.validate.assert_called_once()


@patch("app.api.twilio._validate_twilio_request")
def test_twiml_contains_stream_parameters(mock_validate: MagicMock) -> None:
    """The TwiML stream should include call_sid and caller_phone parameters."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert "call_sid" in body
    assert "caller_phone" in body


@patch("app.api.twilio._reconstruct_twilio_url")
@patch("app.api.twilio._validate_twilio_request")
def test_url_reconstruction_from_forwarded_headers(
    mock_validate: MagicMock,
    mock_reconstruct: MagicMock,
) -> None:
    """The URL should be reconstructed from forwarded headers, not request.url."""
    mock_reconstruct.return_value = "https://example.com/api/v1/twilio/voice"
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    mock_reconstruct.assert_called_once()


@patch("app.api.twilio._validate_twilio_request")
def test_empty_form_data_returns_valid_twiml(mock_validate: MagicMock) -> None:
    """Missing form fields should default to 'unknown' in the TwiML."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data={},
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert "<Connect>" in body
    assert "<Stream" in body
    assert "unknown" in body


@patch("app.api.twilio._validate_twilio_request")
def test_missing_from_defaults_to_unknown(mock_validate: MagicMock) -> None:
    """When From is missing, caller_phone should default to 'unknown'."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data={"CallSid": "CA_test_sid"},
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "unknown" in response.text
    assert "CA_test_sid" in response.text


@patch("app.api.twilio._validate_twilio_request")
def test_missing_callsid_defaults_to_unknown(mock_validate: MagicMock) -> None:
    """When CallSid is missing, call_sid should default to 'unknown'."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data={"From": "+15551234567"},
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "unknown" in response.text
    assert "+15551234567" in response.text


@patch("app.api.twilio._validate_twilio_request")
def test_twiml_xml_structure_is_valid(mock_validate: MagicMock) -> None:
    """The TwiML response should be well-formed XML."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert body.startswith("<?xml")
    assert "<Response>" in body
    assert "</Response>" in body
    assert "</Connect>" in body
    assert "</Stream>" in body


@patch("app.api.twilio._validate_twilio_request")
def test_stream_url_is_websocket(mock_validate: MagicMock) -> None:
    """The stream URL should use ws:// or wss:// protocol."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert "wss://" in body or "ws://" in body


@patch("app.api.twilio._validate_twilio_request")
def test_stream_url_uses_host_header_not_request_url(mock_validate: MagicMock) -> None:
    """The stream URL should use the Host header, not the internal request URL."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers={
            "X-Twilio-Signature": "valid_signature",
            "Host": "roadside-agent-946792421750.us-central1.run.app",
            "x-forwarded-proto": "https",
        },
    )

    assert response.status_code == 200
    body = response.text
    assert "wss://roadside-agent-946792421750.us-central1.run.app/api/v1/twilio/media-stream" in body
    # Must NOT contain internal addresses
    assert "0.0.0.0" not in body
    assert "localhost" not in body
    assert "127.0.0.1" not in body


def test_reconstruct_url_without_forwarded_headers() -> None:
    """URL reconstruction should fall back to request.url when headers are absent."""
    from app.api.twilio import _reconstruct_twilio_url

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.scheme = "http"
    mock_request.url.hostname = "localhost"
    mock_request.url.path = "/api/v1/twilio/voice"

    url = _reconstruct_twilio_url(mock_request)

    assert url == "http://localhost/api/v1/twilio/voice"


def test_reconstruct_url_with_forwarded_proto() -> None:
    """URL reconstruction should use x-forwarded-proto when present."""
    from app.api.twilio import _reconstruct_twilio_url

    mock_request = MagicMock()
    mock_request.headers = {"x-forwarded-proto": "https", "host": "example.com"}
    mock_request.url.path = "/api/v1/twilio/voice"

    url = _reconstruct_twilio_url(mock_request)

    assert url == "https://example.com/api/v1/twilio/voice"


def test_reconstruct_url_missing_host_header() -> None:
    """URL reconstruction should handle missing host header gracefully."""
    from app.api.twilio import _reconstruct_twilio_url

    mock_request = MagicMock()
    mock_request.headers = {"x-forwarded-proto": "https"}
    mock_request.url.hostname = None
    mock_request.url.path = "/api/v1/twilio/voice"

    url = _reconstruct_twilio_url(mock_request)

    assert url == "https:///api/v1/twilio/voice"


def test_build_ws_url_uses_forwarded_headers() -> None:
    """Media Stream WS URL should use Host and x-forwarded-proto headers."""
    from app.api.twilio import _build_media_stream_ws_url

    mock_request = MagicMock()
    mock_request.headers = {
        "host": "roadside-agent-946792421750.us-central1.run.app",
        "x-forwarded-proto": "https",
    }

    url = _build_media_stream_ws_url(mock_request)

    assert url == "wss://roadside-agent-946792421750.us-central1.run.app/api/v1/twilio/media-stream"


def test_build_ws_url_with_ngrok_host() -> None:
    """Media Stream WS URL should work with ngrok host."""
    from app.api.twilio import _build_media_stream_ws_url

    mock_request = MagicMock()
    mock_request.headers = {
        "host": "abc123.ngrok-free.app",
        "x-forwarded-proto": "https",
    }

    url = _build_media_stream_ws_url(mock_request)

    assert url == "wss://abc123.ngrok-free.app/api/v1/twilio/media-stream"


def test_build_ws_url_falls_back_to_request_url() -> None:
    """Media Stream WS URL should fall back to request.url when headers are absent."""
    from app.api.twilio import _build_media_stream_ws_url

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.scheme = "http"
    mock_request.url.hostname = "localhost"

    url = _build_media_stream_ws_url(mock_request)

    assert url == "ws://localhost/api/v1/twilio/media-stream"


def test_build_ws_url_no_internal_port() -> None:
    """Media Stream WS URL should not include port from request.url."""
    from app.api.twilio import _build_media_stream_ws_url

    mock_request = MagicMock()
    mock_request.headers = {
        "host": "roadside-agent-946792421750.us-central1.run.app",
        "x-forwarded-proto": "https",
    }
    mock_request.url.port = 8080  # internal port should be ignored

    url = _build_media_stream_ws_url(mock_request)

    assert url == "wss://roadside-agent-946792421750.us-central1.run.app/api/v1/twilio/media-stream"
    assert ":8080" not in url


@patch("app.api.twilio._validate_twilio_request")
def test_request_with_extra_twilio_params(mock_validate: MagicMock) -> None:
    """Extra Twilio parameters should not break the handler."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data={**TWILIO_PARAMS, "AccountSid": "AC_extra", "ApiVersion": "2010-04-01"},
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "<Connect>" in response.text


@patch("app.api.twilio._validate_twilio_request")
def test_caller_phone_with_special_characters(mock_validate: MagicMock) -> None:
    """Special characters in the From field should be preserved in TwiML."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data={**TWILIO_PARAMS, "From": "+15551234567;phone=true"},
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "+15551234567;phone=true" in response.text


# ---------------------------------------------------------------------------
# Assistance request created at call start (progressive intake)
# ---------------------------------------------------------------------------


@patch("app.api.twilio._validate_twilio_request")
def test_valid_request_starts_assistance_request(
    mock_validate: MagicMock, _mock_start_assistance_request: MagicMock
) -> None:
    """A valid webhook idempotently creates one open assistance-request row."""
    mock_validate.return_value = True

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    _mock_start_assistance_request.assert_called_once_with(
        call_id="CA_test_call_sid_123",
        caller_phone="+15551234567",
    )


@patch("app.api.twilio._validate_twilio_request")
def test_invalid_signature_starts_no_assistance_request(
    mock_validate: MagicMock, _mock_start_assistance_request: MagicMock
) -> None:
    """An invalid signature returns 403 and never creates a row."""
    mock_validate.return_value = False

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(signature="bad_signature"),
    )

    assert response.status_code == 403
    _mock_start_assistance_request.assert_not_called()


@patch("app.api.twilio._validate_twilio_request")
def test_webhook_logs_assistance_request_started(
    mock_validate: MagicMock,
    _mock_start_assistance_request: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful early creation logs the assistance_request_started event."""
    mock_validate.return_value = True

    with caplog.at_level("INFO"):
        client.post(
            "/api/v1/twilio/voice",
            data=TWILIO_PARAMS,
            headers=_make_twilio_headers(),
        )

    events = [
        json.loads(r.message)["event"]
        for r in caplog.records
        if r.message.startswith("{")
    ]
    assert "assistance_request_started" in events


@patch("app.api.twilio._validate_twilio_request")
def test_assistance_request_failure_does_not_block_call(
    mock_validate: MagicMock, _mock_start_assistance_request: MagicMock
) -> None:
    """A creation failure is logged but the call still gets TwiML."""
    mock_validate.return_value = True
    _mock_start_assistance_request.side_effect = RuntimeError("DB down")

    response = client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert response.status_code == 200
    assert "<Connect>" in response.text


@patch("app.api.twilio.notify_dispatcher")
@patch("app.api.twilio._validate_twilio_request")
def test_webhook_sends_zero_dispatcher_sms(
    mock_validate: MagicMock,
    mock_notify: MagicMock,
    _mock_start_assistance_request: MagicMock,
) -> None:
    """Early creation at call start must not fire the dispatcher SMS."""
    mock_validate.return_value = True

    client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    mock_notify.assert_not_called()


@patch("app.api.twilio._validate_twilio_request")
def test_concurrent_calls_create_isolated_requests(
    mock_validate: MagicMock, _mock_start_assistance_request: MagicMock
) -> None:
    """Two concurrent calls map to two isolated rows keyed by their call ids."""
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

    assert _mock_start_assistance_request.call_count == 2
    _mock_start_assistance_request.assert_any_call(
        call_id="CA_call_1", caller_phone="+15551111111"
    )
    _mock_start_assistance_request.assert_any_call(
        call_id="CA_call_2", caller_phone="+15552222222"
    )


@patch("app.api.twilio._validate_twilio_request")
def test_webhook_retry_calls_start_with_same_call_id(
    mock_validate: MagicMock, _mock_start_assistance_request: MagicMock
) -> None:
    """A Twilio retry re-invokes start with the same call_id (idempotent at DB)."""
    mock_validate.return_value = True

    client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )
    client.post(
        "/api/v1/twilio/voice",
        data=TWILIO_PARAMS,
        headers=_make_twilio_headers(),
    )

    assert _mock_start_assistance_request.call_count == 2
    for call in _mock_start_assistance_request.call_args_list:
        assert call.kwargs["call_id"] == "CA_test_call_sid_123"
