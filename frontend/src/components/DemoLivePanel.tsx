import type { AssistanceRequest, IntakeStatus } from '../types'
import { fieldValue } from '../lib/requestFields'
import { SPEAKING_BARS, WAVEFORM_BARS } from '../lib/waveform'
import { CarIcon, CheckIcon, PinIcon, WrenchIcon } from './icons'

const STEPS = [
  'Collecting details',
  'Confirming information',
  'Finding nearby provider',
  'Dispatching assistance',
  'Request complete',
]

const TRANSCRIPT = [
  {
    speaker: 'AI Agent',
    time: '00:28',
    text: 'Thanks. Just to confirm, is that at Great Northern Way and Clark Drive in Vancouver?',
    agent: true,
  },
  {
    speaker: 'You',
    time: '00:36',
    text: "Yes, that's correct.",
    agent: false,
  },
  {
    speaker: 'AI Agent',
    time: '00:39',
    text: 'Got it. And is it a flat tire on your RAV4?',
    agent: true,
  },
  {
    speaker: 'You',
    time: '00:42',
    text: 'Yes, the front right tire.',
    agent: false,
  },
]

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
}: {
  request: AssistanceRequest | null
}) {
  const connected = request !== null
  const intakeStatus: IntakeStatus = request ? request.intake_status : 'in_progress'
  const { activeIndex, completedCount } = stepperState(request)

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
              {connected ? 'On the call' : 'Waiting for call'}
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

        <aside className="live-transcript" aria-label="Sample transcript">
          <p className="preview-eyebrow">SAMPLE TRANSCRIPT</p>
          <p className="transcript-note">
            Example conversation — your live call isn&apos;t transcribed here.
          </p>
          <ol className="transcript-list" role="list">
            {TRANSCRIPT.map((entry) => (
              <li
                key={entry.time}
                className={entry.agent ? 'transcript-entry' : 'transcript-entry you'}
              >
                <div className="transcript-head">
                  <span className="transcript-dot" aria-hidden="true" />
                  <span className="transcript-speaker">{entry.speaker}</span>
                  <span className="transcript-time">{entry.time}</span>
                </div>
                <p className="transcript-text">{entry.text}</p>
              </li>
            ))}
          </ol>
        </aside>
      </div>
    </section>
  )
}
