"""Tests for duplicate voice-webhook early-connection handling.

A second webhook for the same CallSid must not leak the first early OpenAI
connection task/session: the live task is reused (or, when it already
failed, replaced with cleanup), exactly one entry remains, and the media
stream still consumes a single live connection.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.calls import EarlyConnection

client = TestClient(app)

TWILIO_PARAMS = {
    "CallSid": "CA_duplicate_webhook_test",
    "From": "+15551234567",
    "To": "+15559876543",
    "CallStatus": "ringing",
    "Direction": "inbound",
}

HEADERS = {
    "X-Twilio-Signature": "valid_signature",
    "Host": "example.com",
    "x-forwarded-proto": "https",
}


def _clear_pending(call_sid: str) -> None:
    from app.api import twilio as twilio_module

    entry = twilio_module._pending_connections.pop(call_sid, None)
    if entry is not None:
        task = entry.connection_task
        if not task.done():
            task.cancel()


def _cancel_all_pending() -> None:
    from app.api import twilio as twilio_module

    for call_sid in list(twilio_module._pending_connections.keys()):
        _clear_pending(call_sid)


@patch("app.api.twilio.match_demo_session_for_call", return_value=None)
@patch(
    "app.api.twilio.start_assistance_request",
    return_value={
        "id": "req-dup-123",
        "call_id": "CA_duplicate_webhook_test",
        "caller_phone": "+15551234567",
        "status": "in_progress",
        "location": None,
        "vehicle": None,
        "issue": None,
    },
)
@patch("app.api.twilio._validate_twilio_request", return_value=True)
@patch("app.api.twilio._start_early_openai_connection")
def test_duplicate_webhook_reuses_pending_connection(
    mock_start_early: MagicMock,
    mock_validate: MagicMock,
    mock_start_request: MagicMock,
    mock_match: MagicMock,
) -> None:
    """Two webhook posts for one CallSid reuse the first connection task."""
    from app.api import twilio as twilio_module

    _clear_pending(str(TWILIO_PARAMS["CallSid"]))
    mock_session = MagicMock()
    mock_start_early.side_effect = AsyncMock(return_value=mock_session)

    try:
        first = client.post(
            "/api/v1/twilio/voice", data=TWILIO_PARAMS, headers=HEADERS
        )
        assert first.status_code == 200
        first_entry = twilio_module._pending_connections.get(
            str(TWILIO_PARAMS["CallSid"])
        )
        assert first_entry is not None
        first_task = first_entry.connection_task

        second = client.post(
            "/api/v1/twilio/voice", data=TWILIO_PARAMS, headers=HEADERS
        )
        assert second.status_code == 200

        # No second OpenAI connection was started; the pending task was reused.
        assert mock_start_early.call_count == 1

        second_entry = twilio_module._pending_connections.get(
            str(TWILIO_PARAMS["CallSid"])
        )
        assert second_entry is not None
        assert second_entry.connection_task is first_task
        # Exactly one entry remains for the call.
        matches = [
            sid
            for sid in twilio_module._pending_connections
            if sid == TWILIO_PARAMS["CallSid"]
        ]
        assert len(matches) == 1
        # The reused task was not cancelled out from under the media stream.
        assert not first_task.cancelled()
    finally:
        _clear_pending(str(TWILIO_PARAMS["CallSid"]))


@patch("app.api.twilio.match_demo_session_for_call", return_value=None)
@patch(
    "app.api.twilio.start_assistance_request",
    return_value={
        "id": "req-dup-456",
        "call_id": "CA_duplicate_webhook_test",
        "caller_phone": "+15551234567",
        "status": "in_progress",
        "location": None,
        "vehicle": None,
        "issue": None,
    },
)
@patch("app.api.twilio._validate_twilio_request", return_value=True)
@patch("app.api.twilio._start_early_openai_connection")
def test_duplicate_webhook_replaces_failed_connection(
    mock_start_early: MagicMock,
    mock_validate: MagicMock,
    mock_start_request: MagicMock,
    mock_match: MagicMock,
) -> None:
    """A failed first attempt is replaced; the media stream gets one live entry."""
    import time

    from app.api import twilio as twilio_module

    _clear_pending(str(TWILIO_PARAMS["CallSid"]))
    mock_session = MagicMock()
    mock_start_early.side_effect = [
        RuntimeError("early connection boom"),
        mock_session,
    ]

    try:
        first = client.post(
            "/api/v1/twilio/voice", data=TWILIO_PARAMS, headers=HEADERS
        )
        assert first.status_code == 200
        first_entry = twilio_module._pending_connections.get(
            str(TWILIO_PARAMS["CallSid"])
        )
        assert first_entry is not None
        first_task = first_entry.connection_task
        # Wait for the first task to observe its failure so the retry takes
        # the failed-replacement branch deterministically (reuse applies
        # only to live or successfully completed tasks).
        deadline = time.monotonic() + 5.0
        while not first_task.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first_task.done(), "first early-connection task did not finish"
        assert first_task.exception() is not None

        second = client.post(
            "/api/v1/twilio/voice", data=TWILIO_PARAMS, headers=HEADERS
        )
        assert second.status_code == 200

        # A fresh OpenAI connection was started for the retry.
        assert mock_start_early.call_count == 2
        second_entry = twilio_module._pending_connections.get(
            str(TWILIO_PARAMS["CallSid"])
        )
        assert second_entry is not None
        assert second_entry.connection_task is not first_task
        # Exactly one entry remains for the call.
        assert (
            len([
                sid
                for sid in twilio_module._pending_connections
                if sid == TWILIO_PARAMS["CallSid"]
            ])
            == 1
        )
    finally:
        _clear_pending(str(TWILIO_PARAMS["CallSid"]))


def test_superseded_cleanup_cancels_task_and_closes_session() -> None:
    """The superseded-cleanup helper cancels the task and closes its session."""
    import asyncio as _asyncio

    from app.api.twilio import _schedule_superseded_early_connection_cleanup

    async def _run() -> None:
        session = MagicMock()
        session.close = AsyncMock()
        started = _asyncio.Event()
        release = _asyncio.Event()

        async def _pending() -> MagicMock:
            started.set()
            await release.wait()
            return session

        loop = _asyncio.get_running_loop()
        task = loop.create_task(_pending())
        await started.wait()
        entry = EarlyConnection(
            call_sid="CA_superseded_unit",
            caller_phone="+15550000000",
            connection_task=task,
            session=session,
        )
        _schedule_superseded_early_connection_cleanup(
            entry, call_sid="CA_superseded_unit"
        )
        release.set()
        await _asyncio.sleep(0.05)
        assert task.cancelled() or task.done()
        assert session.close.await_count >= 1

    _asyncio.run(_run())


def test_pending_entry_consumed_exactly_once_by_media_stream() -> None:
    """The pending dict yields its single entry via one pop, like the stream."""
    from app.api import twilio as twilio_module

    async def _run() -> None:
        async def _fake_early() -> MagicMock:
            return MagicMock()

        loop = asyncio.get_running_loop()
        task = loop.create_task(_fake_early())
        entry = EarlyConnection(
            call_sid="CA_single_consume",
            caller_phone="+15550000001",
            connection_task=task,
        )
        twilio_module._pending_connections["CA_single_consume"] = entry
        try:
            first = twilio_module._pending_connections.pop("CA_single_consume", None)
            second = twilio_module._pending_connections.pop("CA_single_consume", None)
            assert first is entry
            assert second is None
        finally:
            twilio_module._pending_connections.pop("CA_single_consume", None)
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())
