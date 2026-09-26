import { useEffect, useRef } from 'react'
import type { AssistanceRequest, IntakeStatus, TranscriptTurn } from '../types'
import { demoCallPhase } from '../lib/callStatus'
import { fieldValue } from '../lib/requestFields'
import {
  STATUS_INDICATOR,
  type RealtimeStatus,
} from '../lib/realtimeStatus'
import { SPEAKING_BARS, WAVEFORM_BARS } from '../lib/waveform'
import { CarIcon, CheckIcon, PinIcon, WrenchIcon } from './icons'

const STEPS = [
  'Collecting details',
  'Confirming information',
  'Finding nearby provider',
  'Dispatching assistance',
  'Request complete',
]

function formatElapsed(createdAt: string, originMs: number | null): string {
  const turnMs = Date.parse(createdAt)
  const baseMs =
    originMs !== null && Number.isFinite(originMs) ? originMs : turnMs
  const elapsedSeconds = Number.isFinite(turnMs)
    ? Math.max(0, Math.floor((turnMs - baseMs) / 1000))
    : 0
  const minutes = Math.floor(elapsedSeconds / 60)
  const seconds = elapsedSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

interface StepperState {
  activeIndex: number
  completedCount: number
}

/**
 * Map the live request onto the five-step progress track:
 * - no request / missing fields → still collecting details
 * - all fields collected → confirming information
 * - completed intake → every step done
 * - escalated (hazard) → dispatching assistance
 * - abandoned → progress stops wherever intake left off
 */
function stepperState(request: AssistanceRequest | null): StepperState {
  if (!request) {
    return { activeIndex: 0, completedCount: 0 }
  }
  const fieldsCollected = Boolean(
    request.location && request.vehicle && request.issue,
  )
  const collected: StepperState = {
    activeIndex: fieldsCollected ? 1 : 0,
    completedCount: fieldsCollected ? 1 : 0,
  }
  switch (request.intake_status) {
    case 'completed':
      return { activeIndex: 4, completedCount: STEPS.length }
    case 'escalated':
      return { activeIndex: 3, completedCount: 3 }
    case 'abandoned':
    case 'in_progress':
      return collected
    default:
      return collected
  }
}

function fieldBadge(label: string, value: string | null): string | null {
  if (!value || value.trim() === '') {
    return null
  }
  return label === 'Location' ? 'Verified' : 'Confirmed'
}

const CARDS = [
  { key: 'location', label: 'Location', icon: <PinIcon /> },
  { key: 'vehicle', label: 'Vehicle', icon: <CarIcon /> },
  { key: 'issue', label: 'Issue', icon: <WrenchIcon /> },
] as const

export default function DemoLivePanel({
  request,
  transcripts,
  realtimeStatus,
}: {
  request: AssistanceRequest | null
  transcripts: TranscriptTurn[]
  realtimeStatus: RealtimeStatus
}) {
  const phase = demoCallPhase(request)
  const connected = phase === 'active'
  const ended = phase === 'ended'
  const intakeStatus: IntakeStatus = request ? request.intake_status : 'in_progress'
  const { activeIndex, completedCount } = stepperState(request)
  const indicator = STATUS_INDICATOR[realtimeStatus]
  const callStartMs = request ? Date.parse(request.created_at) : null
  const originMs =
    callStartMs !== null && Number.isFinite(callStartMs)
      ? callStartMs
      : transcripts.length > 0
        ? Date.parse(transcripts[0].created_at)
        : null
  const listRef = useRef<HTMLOListElement | null>(null)

  useEffect(() => {
    const list = listRef.current
    if (list) {
      list.scrollTop = list.scrollHeight
    }
  }, [transcripts.length])

  return (
    <section className="preview-panel">
      <div className="preview-head">
        <div className="preview-copy">
          <p className="preview-eyebrow">LIVE ASSISTANCE REQUEST</p>
          <h2>The call, as it happens.</h2>
          <p className="preview-lede">
            Your assistance request updates in real time while you talk to the
            AI agent.
          </p>
        </div>
        <div className="preview-state">
          <div className="preview-status-row">
            <span className="waiting-pill">
              <span
                className={connected ? 'pill-dot' : 'pill-dot pill-dot-accent'}
                aria-hidden="true"
              />
              {connected
                ? 'On the call'
                : ended
                  ? 'Call ended'
                  : 'Waiting for call'}
            </span>
            <span className="waveform" aria-hidden="true">
              {WAVEFORM_BARS.map((height, index) => (
                <span
                  key={index}
                  className={
                    connected && index < SPEAKING_BARS
                      ? 'wave-bar speaking'
                      : 'wave-bar'
                  }
                  style={{ height: `${height}px` }}
                />
              ))}
            </span>
          </div>
        </div>
      </div>

      <div className="live-body">
        <div className="live-main">
          <div className="preview-grid">
            {CARDS.map((card) => {
              const raw = request ? (request[card.key] ?? null) : null
              const value = fieldValue(raw, intakeStatus)
              const badge = fieldBadge(card.label, raw)
              return (
                <article className="preview-card" key={card.label}>
                  <span className="preview-card-icon" aria-hidden="true">
                    {card.icon}
                  </span>
                  <div className="preview-card-body">
                    <p className="preview-card-label">{card.label}</p>
                    <p className="preview-card-value">{value}</p>
                    {badge && (
                      <span className="field-badge">
                        <CheckIcon />
                        {badge}
                      </span>
                    )}
                  </div>
                </article>
              )
            })}
          </div>

          <ol className="live-stepper" role="list">
            {STEPS.map((label, index) => {
              const done = index < completedCount
              const current = !done && index === activeIndex
              const lineDone = index <= completedCount || index === activeIndex
              const state = done
                ? 'stepper-step done'
                : current
                  ? 'stepper-step current'
                  : 'stepper-step'
              return (
                <li
                  key={label}
                  className={state}
                  aria-current={current ? 'step' : undefined}
                >
                  <span className="stepper-node">
                    {done ? <CheckIcon size={13} /> : <span />}
                  </span>
                  <span className="stepper-label">{label}</span>
                  <span
                    className={lineDone ? 'stepper-line done' : 'stepper-line'}
                    aria-hidden="true"
                  />
                </li>
              )
            })}
          </ol>
        </div>

        <aside className="live-transcript" aria-label="Live transcript">
          <p className="preview-eyebrow">LIVE TRANSCRIPT</p>
          <p className="transcript-note" role="status">
            <span className={indicator.className}>{indicator.label}</span>
            {transcripts.length === 0
              ? connected
                ? ' — Listening, turns appear here as you talk.'
                : ' — Call the demo number, your conversation appears here.'
              : ` — ${transcripts.length} turn${transcripts.length === 1 ? '' : 's'}`}
          </p>
          {transcripts.length === 0 ? (
            <p className="transcript-note">
              {connected
                ? 'Waiting for speech — nothing heard yet.'
                : 'No conversation yet.'}
            </p>
          ) : (
            <ol className="transcript-list" role="list" ref={listRef}>
              {transcripts.map((entry) => (
                <li
                  key={entry.id}
                  className={
                    entry.speaker === 'AI Agent'
                      ? 'transcript-entry'
                      : 'transcript-entry you'
                  }
                >
                  <div className="transcript-head">
                    <span className="transcript-dot" aria-hidden="true" />
                    <span className="transcript-speaker">{entry.speaker}</span>
                    <span className="transcript-time">
                      {formatElapsed(entry.created_at, originMs)}
                    </span>
                  </div>
                  <p className="transcript-text">{entry.text}</p>
                </li>
              ))}
            </ol>
          )}
        </aside>
      </div>
    </section>
  )
}
