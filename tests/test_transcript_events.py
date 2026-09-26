"""Tests for caller + assistant transcript events in the realtime session."""

from __future__ import annotations

import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from app.realtime.session import CALLER_TRANSCRIPTION_MODEL, RealtimeSession


def _make_session(**kwargs: object) -> RealtimeSession:
    defaults: dict[str, object] = {
        "call_sid": "CA_transcript_test",
        "caller_phone": "+15551234567",
        "stream_sid": "MZ_transcript_test",
    }
    defaults.update(kwargs)
    return RealtimeSession(**defaults)  # type: ignore[arg-type]


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.OPENAI_API_KEY = "sk-test-key"
    settings.OPENAI_REALTIME_MODEL = "gpt-4o-realtime-preview"
    return settings


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    ws.__aiter__ = MagicMock(side_effect=lambda: iter([]))
    return ws


async def _connect_session(session: RealtimeSession, ws: AsyncMock | None = None) -> AsyncMock:
    if ws is None:
        ws = _make_ws()
    with patch("app.realtime.session.websockets.connect", new_callable=AsyncMock, return_value=ws):
        with patch("app.realtime.session.get_settings", return_value=_make_settings()):
            await session.connect()
    return ws


class TestCallerTranscriptionConfig:
    async def test_session_update_enables_caller_transcription(self) -> None:
        session = _make_session()
        ws = _make_ws()
        await _connect_session(session, ws)

        first_call_json = ws.send.call_args_list[0][0][0]
        event = json.loads(first_call_json)
        transcription = event["session"]["audio"]["input"]["transcription"]
        assert transcription["model"] == CALLER_TRANSCRIPTION_MODEL
        assert transcription["model"] == "gpt-4o-mini-transcribe"


class TestCallerTranscriptEvents:
    async def test_completed_event_invokes_callback_with_text(self) -> None:
        on_caller = AsyncMock()
        session = _make_session(on_caller_transcript=on_caller)
        await _connect_session(session)

        await session._handle_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_123",
            "content_index": 0,
            "transcript": "I am at Main Street",
        })

        on_caller.assert_called_once_with("item_123", "I am at Main Street")

    async def test_empty_transcript_does_not_invoke_callback(self) -> None:
        on_caller = AsyncMock()
        session = _make_session(on_caller_transcript=on_caller)
        await _connect_session(session)

        await session._handle_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_empty",
            "content_index": 0,
            "transcript": "   ",
        })

        on_caller.assert_not_called()

    async def test_failed_event_does_not_break_turn_taking(self) -> None:
        on_caller = AsyncMock()
        session = _make_session(greeting="Hello!", on_caller_transcript=on_caller)
        ws = _make_ws()
        await _connect_session(session, ws)

        # Failure must not raise and must not affect greeting state.
        await session._handle_event({
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": "item_fail",
            "error": {"code": "transcription_failed", "message": "boom"},
        })
        on_caller.assert_not_called()
        assert session._greeting_response_done is False

        # Greeting flow still works after the failure.
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_greet", "status": "completed", "output": []},
        })
        assert session._greeting_response_done is True

    async def test_callback_failure_does_not_break_call(self) -> None:
        async def _boom(_item_id: str, _text: str) -> None:
            raise RuntimeError("downstream down")

        session = _make_session(on_caller_transcript=_boom)
        await _connect_session(session)

        # Must not raise.
        await session._handle_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_1",
            "transcript": "hello",
        })
        # Turn-taking still works: flip greeting done then commit creates a response.
        session._greeting_response_done = True
        ws = cast(AsyncMock, session._ws)
        assert ws is not None
        before = ws.send.call_count
        await session._handle_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item_1",
        })
        assert ws.send.call_count == before + 1


class TestAssistantTranscriptEvents:
    async def test_done_event_invokes_callback_with_response_id(self) -> None:
        on_assistant = AsyncMock()
        session = _make_session(on_assistant_transcript=on_assistant)
        await _connect_session(session)

        await session._handle_event({
            "type": "response.output_audio_transcript.done",
            "event_id": "evt_1",
            "response_id": "resp_abc",
            "item_id": "item_xyz",
            "output_index": 0,
            "content_index": 0,
            "transcript": "Where is your vehicle located?",
        })

        on_assistant.assert_called_once_with("resp_abc", "Where is your vehicle located?")

    async def test_empty_done_does_not_invoke_callback(self) -> None:
        on_assistant = AsyncMock()
        session = _make_session(on_assistant_transcript=on_assistant)
        await _connect_session(session)

        await session._handle_event({
            "type": "response.output_audio_transcript.done",
            "response_id": "resp_empty",
            "item_id": "item_empty",
            "transcript": "",
        })

        on_assistant.assert_not_called()

    async def test_delta_does_not_invoke_callback(self) -> None:
        on_assistant = AsyncMock()
        session = _make_session(on_assistant_transcript=on_assistant)
        await _connect_session(session)

        await session._handle_event({
            "type": "response.output_audio_transcript.delta",
            "response_id": "resp_abc",
            "item_id": "item_xyz",
            "delta": "partial",
        })

        on_assistant.assert_not_called()

    async def test_callback_failure_does_not_break_closing(self) -> None:
        async def _boom(_response_id: str, _text: str) -> None:
            raise RuntimeError("downstream down")

        session = _make_session(on_assistant_transcript=_boom)
        await _connect_session(session)

        await session._handle_event({
            "type": "response.output_audio_transcript.done",
            "response_id": "resp_1",
            "item_id": "item_1",
            "transcript": "hello caller",
        })
        # Closing flow still advances normally afterwards.
        session.closing_response_started = True
        session.closing_response_id = "resp_close"
        await session._handle_event({
            "type": "response.done",
            "response": {"id": "resp_close", "status": "completed", "output": []},
        })
        assert session.closing_response_completed is True
