"""Tests for the Twilio voice webhook endpoint."""

from unittest.mock import MagicMock, patch

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
