import { useState, type FormEvent } from 'react'
import { SPEAKING_BARS, WAVEFORM_BARS } from '../lib/waveform'
import { CarIcon, PinIcon, WrenchIcon } from './icons'
import LandingHeader from './LandingHeader'

interface DemoStartFormProps {
  onSubmit: (phone: string) => Promise<void>
  submitting: boolean
  error: string | null
}

const STEPS = [
  { number: '1', lead: 'Enter', rest: 'your number' },
  { number: '2', lead: 'Call', rest: 'the agent' },
  { number: '3', lead: 'Watch', rest: 'it work' },
]

type IconProps = { size?: number }

function ArrowRightIcon({ size = 18 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <line x1="5" y1="12" x2="19" y2="12" />
      <polyline points="12 5 19 12 12 19" />
    </svg>
  )
}

function LockIcon({ size = 15 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  )
}

const PREVIEW_FIELDS = [
  { label: 'Location', icon: <PinIcon /> },
  { label: 'Vehicle', icon: <CarIcon /> },
  { label: 'Issue', icon: <WrenchIcon /> },
]

export default function DemoStartForm({
  onSubmit,
  submitting,
  error,
}: DemoStartFormProps) {
  const [phone, setPhone] = useState('')

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    await onSubmit(phone)
  }

  return (
    <main className="demo-landing">
      <LandingHeader />

      <section className="landing-hero">
        <div className="hero-copy">
          <p className="hero-eyebrow">AI × VOICE × ROADSIDE ASSISTANCE</p>
          <h1 className="hero-title">
            Your car breaks down.
            <span className="hero-title-accent">An AI picks up.</span>
          </h1>
          <p className="hero-lede">
            Call a real phone number and talk to an AI roadside agent. Watch
            your assistance request update live while you speak.
          </p>
          <ol className="hero-steps" role="list">
            {STEPS.map((step) => (
              <li key={step.number}>
                <span className="step-num">{step.number}</span>
                <span className="step-text">
                  <strong>{step.lead}</strong>
                  <br />
                  {step.rest}
                </span>
              </li>
            ))}
          </ol>
        </div>

        <form className="start-card" onSubmit={handleSubmit}>
          <div className="start-card-top">
            <span className="start-eyebrow">TRY IT YOURSELF</span>
            <span className="ready-pill">
              <span className="pill-dot" aria-hidden="true" />
              Ready
            </span>
          </div>
          <h2 className="start-title">Start a demo</h2>
          <p className="start-lede">
            Enter the phone number you&apos;ll call from so we can connect this
            browser to your demo call.
          </p>
          <label className="start-field">
            Your phone number
            <input
              type="tel"
              autoComplete="tel"
              inputMode="tel"
              placeholder="+1 604 555 1234"
              required
              value={phone}
              onChange={(event) => setPhone(event.target.value)}
            />
          </label>
          {error && (
            <p className="start-error" role="alert">
              {error}
            </p>
          )}
          <button
            type="submit"
            className="start-button"
            disabled={submitting}
          >
            <span>{submitting ? 'Starting…' : 'Start demo'}</span>
            <ArrowRightIcon />
          </button>
          <p className="start-note">
            <LockIcon />
            Your number is only used to match you to your call.
          </p>
        </form>
      </section>

      <section className="preview-panel">
        <div className="preview-head">
          <div className="preview-copy">
            <p className="preview-eyebrow">WHAT YOU&apos;LL SEE</p>
            <h2>The call, as it happens.</h2>
            <p className="preview-lede">
              Your assistance request updates in real time while you talk to
              the AI agent.
            </p>
          </div>
          <div className="preview-state">
            <div className="preview-status-row">
              <span className="waiting-pill">
                <span className="pill-dot pill-dot-accent" aria-hidden="true" />
                Waiting for call
              </span>
              <span className="waveform" aria-hidden="true">
                {WAVEFORM_BARS.map((height, index) => (
                  <span
                    key={index}
                    className={
                      index < SPEAKING_BARS ? 'wave-bar speaking' : 'wave-bar'
                    }
                    style={{ height: `${height}px` }}
                  />
                ))}
              </span>
            </div>
            <div className="preview-agent">
              <p className="preview-eyebrow">AGENT STATE</p>
              <p className="preview-agent-state">Ready to listen</p>
            </div>
          </div>
        </div>
        <div className="preview-grid">
          {PREVIEW_FIELDS.map((field) => (
            <article className="preview-card" key={field.label}>
              <span className="preview-card-icon" aria-hidden="true">
                {field.icon}
              </span>
              <div className="preview-card-body">
                <p className="preview-card-label">{field.label}</p>
                <p className="preview-card-value">Collecting…</p>
                <span className="preview-skeleton wide" />
                <span className="preview-skeleton narrow" />
              </div>
            </article>
          ))}
        </div>
      </section>

      <footer className="landing-footer">
        <span>Built by Nolan Buzanis</span>
        <span>
          Demo only — not for real roadside assistance or emergencies.
        </span>
      </footer>
    </main>
  )
}
