"""Tests for emergency speak-then-redirect: message plays before Twilio redirect."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.realtime.instructions import TRANSFER_MESSAGE
from app.realtime.session import RealtimeSession

TRANSFER_ITEM = {
    "type": "function_call",
    "call_id": "call_em_1",
    "name": "transfer_to_emergency",
    "arguments": '{"reason": "Car on fire"}',
}
TRANSFER_RESULT = '{"status": "transferred", "message": "Emergency transfer in progress. Stay on the line."}'
ERROR_RESULT = '{"status": "error", "error": "Invalid arguments"}'


def _make_session(**kwargs: object) -> RealtimeSession:
    defaults: dict[str, object] = {
        "call_sid": "CA_transfer_test",
        "caller_phone": "+15551234567",
        "stream_sid": "MZ_transfer_test",
    }
    defaults.update(kwargs)
    session = RealtimeSession(**defaults)  # type: ignore[arg-type]
    return session


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    ws.__aiter__ = MagicMock(side_effect=lambda: iter([]))
    return ws


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.OPENAI_API_KEY = "sk-test-key"
    settings.OPENAI_REALTIME_MODEL = "gpt-4o-realtime-preview"
    return settings


async def _connect(session: RealtimeSession) -> AsyncMock:
    ws = _make_ws()
    with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
        with patch("app.realtime.session.get_settings", return_value=_make_settings()):
            await session.connect()
    ws.reset_mock()
    return ws


def _sent(ws: AsyncMock) -> list[dict]:
    return [json.loads(c[0][0]) for c in ws.send.call_args_list]


class TestSpeakThenRedirectOrdering:
    async def test_transfer_creates_message_and_arms_redirect_without_immediate_call(self) -> None:
        on_finished = AsyncMock()
        session = _make_session(on_transfer_finished=on_finished)
        ws = await _connect(session)
        try:
            session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
            await session._handle_function_call(dict(TRANSFER_ITEM))
            creates = [e for e in _sent(ws) if e.get("type") == "response.create"]
            assert len(creates) == 1
            # Transfer uses its own attribution, not the generic tool_result path.
            assert session._response_create_reasons and session._response_create_reasons[0] == "transfer_message"
            assert session._transfer_task is not None
            on_finished.assert_not_called()
            assert session.transfer_redirect_started is False
        finally:
            await session.close()

    async def test_transfer_message_create_pins_fixed_script(self) -> None:
        session = _make_session()
        ws = await _connect(session)
        try:
            session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
            await session._handle_function_call(dict(TRANSFER_ITEM))
            creates = [e for e in _sent(ws) if e.get("type") == "response.create"]
            assert len(creates) == 1
            directive = creates[0].get("response", {}).get("instructions", "")
            assert TRANSFER_MESSAGE in directive
            assert "Do not add any words" in directive
        finally:
            await session.close()

    async def test_message_completion_triggers_mark_then_redirect(self) -> None:
        marks: list[str] = []
        finished = AsyncMock()

        session = _make_session()
        await _connect(session)
        try:
            async def _mark(name: str) -> None:
                marks.append(name)
                session.acknowledge_transfer_mark(name)

            session.on_transfer_mark_requested = _mark
            session.on_transfer_finished = finished
            session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
            await session._handle_function_call(dict(TRANSFER_ITEM))
            await session._handle_event({
                "type": "response.created",
                "response": {"id": "resp_t1", "status": "in_progress"},
            })
            assert session.transfer_response_id == "resp_t1"
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_t1", "status": "completed", "output": []},
            })
            assert session.transfer_response_completed is True
            assert session._transfer_task is not None
            await asyncio.wait_for(asyncio.shield(session._transfer_task), timeout=5)
            assert len(marks) == 1
            assert marks[0].startswith("transfer-")
            finished.assert_awaited_once_with("CA_transfer_test")
            assert session.transfer_redirect_started is True
        finally:
            await session.close()

    async def test_missing_mark_falls_back_to_redirect(self) -> None:
        finished = AsyncMock()
        session = _make_session()
        await _connect(session)
        try:
            session.on_transfer_mark_requested = AsyncMock()  # never acks
            session.on_transfer_finished = finished
            with patch("app.realtime.session.TRANSFER_MARK_TIMEOUT_SECONDS", 0.05):
                session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
                await session._handle_function_call(dict(TRANSFER_ITEM))
                await session._handle_event({
                    "type": "response.created",
                    "response": {"id": "resp_t2", "status": "in_progress"},
                })
                await session._handle_event({
                    "type": "response.done",
                    "response": {"id": "resp_t2", "status": "completed", "output": []},
                })
                assert session._transfer_task is not None
                await asyncio.wait_for(asyncio.shield(session._transfer_task), timeout=5)
            finished.assert_awaited_once()
        finally:
            await session.close()

    async def test_failed_message_still_redirects_via_timeout(self) -> None:
        finished = AsyncMock()
        session = _make_session()
        await _connect(session)
        try:
            session.on_transfer_mark_requested = AsyncMock()
            session.on_transfer_finished = finished
            with patch("app.realtime.session.TRANSFER_RESPONSE_TIMEOUT_SECONDS", 0.05):
                with patch("app.realtime.session.TRANSFER_MARK_TIMEOUT_SECONDS", 0.05):
                    session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
                    await session._handle_function_call(dict(TRANSFER_ITEM))
                    # No response.created / response.done ever arrives.
                    assert session._transfer_task is not None
                    await asyncio.wait_for(asyncio.shield(session._transfer_task), timeout=5)
            finished.assert_awaited_once()
            assert session.transfer_redirect_started is True
        finally:
            await session.close()

    async def test_duplicate_transfer_arms_only_once(self) -> None:
        finished = AsyncMock()
        session = _make_session()
        ws = await _connect(session)
        try:
            session.on_transfer_finished = finished
            session.on_transfer_mark_requested = AsyncMock()
            session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
            await session._handle_function_call(dict(TRANSFER_ITEM))
            first_task = session._transfer_task
            await session._handle_function_call(dict({**TRANSFER_ITEM, "call_id": "call_em_2"}))
            assert session._transfer_task is first_task
            creates = [e for e in _sent(ws) if e.get("type") == "response.create"]
            assert len(creates) == 2
        finally:
            await session.close()

    async def test_error_result_arms_no_redirect(self) -> None:
        finished = AsyncMock()
        session = _make_session(on_transfer_finished=finished)
        await _connect(session)
        try:
            session.on_tool_call = AsyncMock(return_value=ERROR_RESULT)
            await session._handle_function_call(dict(TRANSFER_ITEM))
            assert session._transfer_task is None
            assert session.transfer_redirect_started is False
            finished.assert_not_called()
        finally:
            await session.close()

    async def test_close_cancels_pending_redirect(self) -> None:
        finished = AsyncMock()
        finished.return_value = True
        session = _make_session(on_transfer_finished=finished)
        await _connect(session)
        session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
        await session._handle_function_call(dict(TRANSFER_ITEM))
        assert session._transfer_task is not None
        await session.close()
        assert session._transfer_task is None
        finished.assert_not_called()

    async def test_failed_redirect_speaks_911_fallback(self) -> None:
        async def _fail(_call_sid: str) -> bool:
            return False

        session = _make_session()
        ws = await _connect(session)
        try:
            session.on_transfer_mark_requested = AsyncMock()
            session.on_transfer_finished = _fail
            session.on_tool_call = AsyncMock(return_value=TRANSFER_RESULT)
            with patch("app.realtime.session.TRANSFER_MARK_TIMEOUT_SECONDS", 0.05):
                await session._handle_function_call(dict(TRANSFER_ITEM))
                await session._handle_event({
                    "type": "response.created",
                    "response": {"id": "resp_fail", "status": "in_progress"},
                })
                await session._handle_event({
                    "type": "response.done",
                    "response": {"id": "resp_fail", "status": "completed", "output": []},
                })
                assert session._transfer_task is not None
                await asyncio.wait_for(asyncio.shield(session._transfer_task), timeout=5)
            creates = [e for e in _sent(ws) if e.get("type") == "response.create"]
            # Transfer message + 911 fallback.
            assert len(creates) == 2
            assert "911" in json.dumps(creates[1])
        finally:
            await session.close()
