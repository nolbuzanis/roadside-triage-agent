"""Tests for transcript-turn persistence (P1 — Persist transcript turns)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from postgrest.exceptions import APIError


def _mock_insert_supabase() -> tuple[MagicMock, MagicMock]:
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table
    insert_chain = MagicMock()
    insert_chain.execute.return_value = MagicMock(data=[{"id": "turn-1"}])
    table.insert.return_value = insert_chain
    return client, table


def _make_session(**kwargs: object):
    from app.realtime.session import RealtimeSession

    defaults: dict[str, object] = {
        "call_sid": "CA_transcript_persist",
        "caller_phone": "+15551234567",
        "stream_sid": "MZ_transcript_persist",
    }
    defaults.update(kwargs)
    return RealtimeSession(**defaults)  # type: ignore[arg-type]


class TestInsertCallTranscript:
    @patch("app.services.transcripts._get_supabase")
    def test_caller_row_inserted_with_ids_and_seq(self, mock_get_sb: MagicMock) -> None:
        sb, table = _mock_insert_supabase()
        mock_get_sb.return_value = sb

        from app.services.transcripts import insert_call_transcript

        assert (
            insert_call_transcript(
                call_id="CA_1",
                demo_session_id="demo-1",
                speaker="caller",
                text="I am at Main St",
                seq=0,
            )
            is True
        )
        sb.table.assert_called_once_with("call_transcripts")
        payload = table.insert.call_args[0][0]
        assert payload == {
            "call_id": "CA_1",
            "demo_session_id": "demo-1",
            "speaker": "caller",
            "text": "I am at Main St",
            "seq": 0,
        }

    @patch("app.services.transcripts._get_supabase")
    def test_non_demo_insert_uses_null_demo_session(self, mock_get_sb: MagicMock) -> None:
        sb, table = _mock_insert_supabase()
        mock_get_sb.return_value = sb

        from app.services.transcripts import insert_call_transcript

        assert (
            insert_call_transcript(
                call_id="CA_2",
                demo_session_id=None,
                speaker="assistant",
                text="Where is your vehicle?",
                seq=1,
            )
            is True
        )
        payload = table.insert.call_args[0][0]
        assert payload["demo_session_id"] is None
        assert payload["speaker"] == "assistant"

    @patch("app.services.transcripts._get_supabase")
    def test_duplicate_seq_conflict_returns_false_without_raising(
        self, mock_get_sb: MagicMock
    ) -> None:
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table
        insert_chain = MagicMock()
        insert_chain.execute.side_effect = APIError(
            {"code": "23505", "message": "duplicate key", "details": None, "hint": None}
        )
        table.insert.return_value = insert_chain
        mock_get_sb.return_value = sb

        from app.services.transcripts import insert_call_transcript

        assert (
            insert_call_transcript(
                call_id="CA_1",
                demo_session_id=None,
                speaker="caller",
                text="repeat",
                seq=0,
            )
            is False
        )

    @patch("app.services.transcripts._get_supabase")
    def test_db_failure_returns_false_without_raising(
        self, mock_get_sb: MagicMock
    ) -> None:
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table
        insert_chain = MagicMock()
        insert_chain.execute.side_effect = RuntimeError("supabase down")
        table.insert.return_value = insert_chain
        mock_get_sb.return_value = sb

        from app.services.transcripts import insert_call_transcript

        assert (
            insert_call_transcript(
                call_id="CA_1",
                demo_session_id=None,
                speaker="caller",
                text="hello",
                seq=3,
            )
            is False
        )


class TestInterruptedAssistantTranscriptSkipped:
    async def test_interrupted_response_done_does_not_invoke_callback(self) -> None:
        on_assistant = AsyncMock()
        session = _make_session(on_assistant_transcript=on_assistant)
        session._interrupted_response_id = "resp_interrupted"

        await session._handle_event({
            "type": "response.output_audio_transcript.done",
            "response_id": "resp_interrupted",
            "item_id": "item_1",
            "transcript": "partial turn the caller talked over",
        })

        on_assistant.assert_not_called()

    async def test_non_interrupted_response_still_invokes_callback(self) -> None:
        on_assistant = AsyncMock()
        session = _make_session(on_assistant_transcript=on_assistant)
        session._interrupted_response_id = "resp_other"

        await session._handle_event({
            "type": "response.output_audio_transcript.done",
            "response_id": "resp_fresh",
            "item_id": "item_2",
            "transcript": "Where is your vehicle?",
        })

        on_assistant.assert_called_once_with("resp_fresh", "Where is your vehicle?")


class TestScheduleTranscriptPersist:
    async def test_seq_increments_monotonically_and_resolves_demo_session(self) -> None:
        from app.api import twilio as twilio_module

        call_sid = "CA_seq_test"
        twilio_module._transcript_seq.pop(call_sid, None)
        try:
            with (
                patch.object(
                    twilio_module,
                    "get_assistance_request",
                    return_value={"demo_session_id": "demo-9"},
                ) as mock_get,
                patch.object(
                    twilio_module, "insert_call_transcript", return_value=True
                ) as mock_insert,
            ):
                twilio_module._schedule_transcript_persist(
                    call_sid=call_sid, speaker="caller", text="turn one"
                )
                twilio_module._schedule_transcript_persist(
                    call_sid=call_sid, speaker="assistant", text="turn two"
                )
                # Drain background tasks deterministically.
                prefix = f"transcript-{call_sid}-"
                pending = [
                    t
                    for t in list(twilio_module._background_tasks)
                    if t.get_name().startswith(prefix)
                ]
                assert len(pending) == 2
                await asyncio.gather(*pending)

                assert mock_get.call_count == 2
                assert mock_insert.call_count == 2
                by_speaker = {
                    call.kwargs["speaker"]: call.kwargs
                    for call in mock_insert.call_args_list
                }
                assert by_speaker["caller"]["seq"] == 0
                assert by_speaker["assistant"]["seq"] == 1
                assert by_speaker["caller"]["demo_session_id"] == "demo-9"
                assert by_speaker["assistant"]["demo_session_id"] == "demo-9"
                assert by_speaker["caller"]["call_id"] == call_sid
        finally:
            twilio_module._transcript_seq.pop(call_sid, None)
            for task in list(twilio_module._background_tasks):
                if task.done():
                    continue
                task.cancel()

    async def test_missing_row_persists_with_null_demo_session(self) -> None:
        from app.api import twilio as twilio_module

        call_sid = "CA_seq_nondemo"
        twilio_module._transcript_seq.pop(call_sid, None)
        try:
            with (
                patch.object(
                    twilio_module, "get_assistance_request", return_value=None
                ),
                patch.object(
                    twilio_module, "insert_call_transcript", return_value=True
                ) as mock_insert,
            ):
                twilio_module._schedule_transcript_persist(
                    call_sid=call_sid, speaker="caller", text="hello"
                )
                prefix = f"transcript-{call_sid}-"
                pending = [
                    t
                    for t in list(twilio_module._background_tasks)
                    if t.get_name().startswith(prefix)
                ]
                assert len(pending) == 1
                await asyncio.gather(*pending)

                assert mock_insert.call_count == 1
                assert mock_insert.call_args.kwargs["demo_session_id"] is None
        finally:
            twilio_module._transcript_seq.pop(call_sid, None)

    async def test_lookup_and_insert_failures_never_raise(self) -> None:
        from app.api import twilio as twilio_module

        call_sid = "CA_seq_fail"
        twilio_module._transcript_seq.pop(call_sid, None)
        try:
            with (
                patch.object(
                    twilio_module,
                    "get_assistance_request",
                    side_effect=RuntimeError("db down"),
                ),
                patch.object(
                    twilio_module,
                    "insert_call_transcript",
                    side_effect=RuntimeError("db down"),
                ),
            ):
                twilio_module._schedule_transcript_persist(
                    call_sid=call_sid, speaker="caller", text="hello"
                )
                prefix = f"transcript-{call_sid}-"
                pending = [
                    t
                    for t in list(twilio_module._background_tasks)
                    if t.get_name().startswith(prefix)
                ]
                assert len(pending) == 1
                # Must not raise even though both DB calls fail.
                await asyncio.gather(*pending)
        finally:
            twilio_module._transcript_seq.pop(call_sid, None)
