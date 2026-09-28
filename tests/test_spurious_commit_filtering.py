"""Tests for transcription-gated commit filtering of spurious caller turns."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.realtime.session import RealtimeSession


def _make_session(**kwargs: object) -> RealtimeSession:
    defaults: dict[str, object] = {
        "call_sid": "CA_spurious_test",
        "caller_phone": "+15551234567",
        "stream_sid": "MZ_spurious_test",
    }
    defaults.update(kwargs)
    return RealtimeSession(**defaults)  # type: ignore[arg-type]


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


async def _connect(session: RealtimeSession, ws: AsyncMock) -> None:
    with patch(
        "app.realtime.session.websockets.connect",
        new_callable=AsyncMock,
        return_value=ws,
    ):
        with patch("app.realtime.session.get_settings", return_value=_make_settings()):
            await session.connect()


def _creates(ws: AsyncMock) -> list[dict]:
    return [
        json.loads(c[0][0]) for c in ws.send.call_args_list if json.loads(c[0][0]).get("type") == "response.create"
    ]


async def _commit(session: RealtimeSession, item_id: str) -> None:
    await session._handle_event({"type": "input_audio_buffer.committed", "item_id": item_id})


async def _transcript(session: RealtimeSession, item_id: str, text: str) -> None:
    await session._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": item_id,
        "transcript": text,
    })


class TestSpuriousCommitFiltering:
    async def test_commit_alone_creates_no_response(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect(session, ws)
        try:
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_greet", "status": "completed", "output": []},
            })
            ws.reset_mock()
            await _commit(session, "item_noise")
            assert _creates(ws) == []
        finally:
            await session.close()

    async def test_empty_transcript_filters_commit(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect(session, ws)
        try:
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_greet", "status": "completed", "output": []},
            })
            ws.reset_mock()
            await _commit(session, "item_empty")
            await _transcript(session, "item_empty", "   ")
            assert _creates(ws) == []
        finally:
            await session.close()

    async def test_real_speech_creates_exactly_one(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect(session, ws)
        try:
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_greet", "status": "completed", "output": []},
            })
            ws.reset_mock()
            await _commit(session, "item_real")
            await _transcript(session, "item_real", "I am at Main Street")
            creates = _creates(ws)
            assert len(creates) == 1
        finally:
            await session.close()

    async def test_transcript_before_commit_creates_one(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect(session, ws)
        try:
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_greet", "status": "completed", "output": []},
            })
            ws.reset_mock()
            await _transcript(session, "item_early", "Hello, I need help")
            await _commit(session, "item_early")
            assert len(_creates(ws)) == 1
        finally:
            await session.close()

    async def test_failed_transcript_fails_open(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect(session, ws)
        try:
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_greet", "status": "completed", "output": []},
            })
            ws.reset_mock()
            await _commit(session, "item_fail")
            await session._handle_event({
                "type": "conversation.item.input_audio_transcription.failed",
                "item_id": "item_fail",
                "error": {"code": "boom", "message": "boom"},
            })
            assert len(_creates(ws)) == 1
        finally:
            await session.close()

    async def test_missing_transcript_times_out_open(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect(session, ws)
        try:
            await session._handle_event({
                "type": "response.done",
                "response": {"id": "resp_greet", "status": "completed", "output": []},
            })
            ws.reset_mock()
            with patch("app.realtime.session.COMMIT_TRANSCRIPT_TIMEOUT_SECONDS", 0.05):
                await _commit(session, "item_lost")
                task = session._commit_transcript_tasks.get("item_lost")
                assert task is not None
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert len(_creates(ws)) == 1
        finally:
            await session.close()

    async def test_close_cancels_pending_commit(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect(session, ws)
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_greet", "status": "completed", "output": []},
        })
        ws.reset_mock()
        await _commit(session, "item_pending")
        assert session._commit_transcript_tasks.get("item_pending") is not None
        await session.close()
        assert _creates(ws) == []
