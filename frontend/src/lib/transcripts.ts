import type { TranscriptTurn } from '../types'

/** Raw `call_transcripts` row shape from Supabase (speaker is DB-side). */
export interface TranscriptRow {
  id: string
  demo_session_id: string | null
  speaker: string
  text: string
  seq: number
  created_at: string
}

function toSpeaker(speaker: string): TranscriptTurn['speaker'] | null {
  if (speaker === 'caller') {
    return 'You'
  }
  if (speaker === 'assistant') {
    return 'AI Agent'
  }
  return null
}

/**
 * Normalize one raw row into UI shape. Returns null for rows that must
 * never render (unknown speaker, empty text, non-finite seq).
 */
export function normalizeTranscriptRow(row: TranscriptRow): TranscriptTurn | null {
  const speaker = toSpeaker(row.speaker)
  if (!speaker) {
    return null
  }
  if (!row.id || typeof row.text !== 'string' || row.text.trim() === '') {
    return null
  }
  if (!Number.isFinite(row.seq)) {
    return null
  }
  if (!row.created_at) {
    return null
  }
  return {
    id: row.id,
    speaker,
    text: row.text,
    seq: row.seq,
    created_at: row.created_at,
  }
}

function bySeqAsc(a: TranscriptTurn, b: TranscriptTurn): number {
  if (a.seq !== b.seq) {
    return a.seq - b.seq
  }
  if (a.created_at === b.created_at) {
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
  }
  return a.created_at < b.created_at ? -1 : 1
}

/**
 * Merge one realtime INSERT payload into the list, keyed by `id`.
 * Never duplicates a turn; the list stays ordered by `seq`.
 */
export function upsertTranscriptTurn(
  turns: TranscriptTurn[],
  incoming: TranscriptTurn,
): TranscriptTurn[] {
  if (turns.some((turn) => turn.id === incoming.id)) {
    return turns
  }
  return [...turns, incoming].sort(bySeqAsc)
}

/**
 * Merge the backfill query result with realtime rows that arrived while
 * the query was in flight, so neither side wins a race: loaded rows are
 * the snapshot, and rows absent from the snapshot stay listed (no lost or
 * duplicate turns). Ordered by `seq`.
 */
export function mergeLoadedTranscriptTurns(
  current: TranscriptTurn[],
  loaded: TranscriptTurn[],
): TranscriptTurn[] {
  if (current.length === 0) {
    return [...loaded].sort(bySeqAsc)
  }
  const merged = new Map<string, TranscriptTurn>()
  for (const turn of loaded) {
    merged.set(turn.id, turn)
  }
  for (const turn of current) {
    if (!merged.has(turn.id)) {
      merged.set(turn.id, turn)
    }
  }
  return [...merged.values()].sort(bySeqAsc)
}
