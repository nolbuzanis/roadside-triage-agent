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
