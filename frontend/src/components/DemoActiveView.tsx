import { useEffect, useRef, useState } from 'react'
import type { AssistanceRequest, TranscriptTurn } from '../types'
import { demoCallPhase } from '../lib/callStatus'
import type { StoredDemoSession } from '../lib/demoStorage'
import {
  STATUS_INDICATOR,
  type RealtimeStatus,
} from '../lib/realtimeStatus'
import DemoLivePanel from './DemoLivePanel'
import LandingHeader from './LandingHeader'
import { CheckIcon, CopyIcon, PhoneIcon } from './icons'

interface DemoActiveViewProps {
  demo: StoredDemoSession
  request: AssistanceRequest | null
  transcripts: TranscriptTurn[]
  nowMs: number
  realtimeStatus: RealtimeStatus
  error: string | null
  onRestart: () => void
}

function formatCountdown(totalSeconds: number): string {
  const safeSeconds = Number.isFinite(totalSeconds)
    ? Math.max(0, Math.floor(totalSeconds))
    : 0
  const hours = Math.floor(safeSeconds / 3600)
  const minutes = Math.floor((safeSeconds % 3600) / 60)
  const remainingSeconds = safeSeconds % 60
  const mm = String(minutes).padStart(2, '0')
  const ss = String(remainingSeconds).padStart(2, '0')
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`
}

function formatDemoPhone(phone: string): string {
  const digits = phone.replace(/\D/g, '')
  if (digits.length === 11 && digits.startsWith('1')) {
    return `(${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7)}`
  }
  return phone
}

export default function DemoActiveView({
  demo,
  request,
  transcripts,
  nowMs,
  realtimeStatus,
  error,
  onRestart,
}: DemoActiveViewProps) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>(
    'idle',
  )
  const copyTimerRef = useRef<number | null>(null)
  const remainingSeconds = Math.floor(
    (Date.parse(demo.expires_at) - nowMs) / 1000,
  )
  const indicator = STATUS_INDICATOR[realtimeStatus]
  const maskedPhone = `••• ••• ${demo.phone_last4}`
  const phase = demoCallPhase(request)
  const connected = phase === 'active'
  const ended = phase === 'ended'
  const endedTitle =
    request?.intake_status === 'escalated' ? 'Call transferred' : 'Call ended'
  const endedLabel =
    request?.intake_status === 'escalated'
      ? 'Transferred to emergency support'
      : request?.intake_status === 'completed'
        ? 'Assistance request logged'
        : 'Call ended before completion'
  const elapsedSeconds =
    connected && request
      ? (nowMs - Date.parse(request.created_at)) / 1000
      : 0

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current)
      }
    }
  }, [])

  async function handleCopy() {
    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current)
      copyTimerRef.current = null
    }
    try {
      await navigator.clipboard.writeText(demo.demo_phone)
      setCopyState('copied')
      copyTimerRef.current = window.setTimeout(() => setCopyState('idle'), 2000)
    } catch {
      setCopyState('failed')
    }
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
        </div>

        <aside className="call-card" aria-label="Demo call status">
          <div className="call-card-top">
            <span className="start-eyebrow">DEMO IN PROGRESS</span>
            <span className={indicator.className} role="status">
              {indicator.label}
            </span>
          </div>
          <h2 className="call-card-title">
            {connected
              ? "You're on the call"
              : ended
                ? endedTitle
                : 'Waiting for your call'}
          </h2>
          <div className="call-status-row">
            <span className="call-status-label">
              <PhoneIcon size={16} />
              {connected
                ? 'Connected to AI agent'
                : ended
                  ? endedLabel
                  : 'Call the demo number to connect'}
            </span>
            <span className="call-status-meta">
              {connected && (
                <span className="call-timer">{formatCountdown(elapsedSeconds)}</span>
              )}
              <span
                className={connected ? 'call-status-dot live' : 'call-status-dot'}
                aria-hidden="true"
              />
            </span>
          </div>
          <div className="call-number-block">
            <p className="call-number-label">Call this number from your phone:</p>
            <div className="call-number-row">
              <PhoneIcon size={20} />
              <span className="call-number">{formatDemoPhone(demo.demo_phone)}</span>
              <button
                type="button"
                className="call-copy"
                onClick={handleCopy}
                aria-label={
                  copyState === 'copied'
                    ? 'Copied'
                    : 'Copy demo phone number'
                }
              >
                {copyState === 'copied' ? <CheckIcon size={16} /> : <CopyIcon size={16} />}
              </button>
            </div>
            <span className="call-copy-status" role="status">
              {copyState === 'copied' ? 'Copied to clipboard.' : ''}
            </span>
            {copyState === 'failed' && (
              <p className="call-copy-error" role="alert">
                Couldn&apos;t copy — select the number to copy it manually.
              </p>
            )}
          </div>
          <p className="call-meta">
            Calls from {maskedPhone} match this session ·{' '}
            <span role="timer">
              expires in {formatCountdown(remainingSeconds)}
            </span>
          </p>
          {error && (
            <p className="demo-error" role="alert">
              {error}
            </p>
          )}
          {ended && (
            <div className="call-restart-block">
              <p className="call-restart-note">
                Want to try again? Reset this view, then start a new demo
                before calling again.
              </p>
              <button
                type="button"
                className="call-restart-button"
                onClick={onRestart}
              >
                Start another call
              </button>
            </div>
          )}
        </aside>
      </section>

      <DemoLivePanel
        request={request}
        transcripts={transcripts}
        realtimeStatus={realtimeStatus}
      />

      <footer className="landing-footer">
        <span>
          Built by Nolan Buzanis — Senior software engineer focused on
          realtime systems, AI, and product engineering.{' '}
          <a
            href="https://www.linkedin.com/in/nolanbuzanis/"
            target="_blank"
            rel="noreferrer"
          >
            LinkedIn
          </a>
          {' · '}
          <a
            href="https://github.com/nolbuzanis"
            target="_blank"
            rel="noreferrer"
          >
            GitHub
          </a>
        </span>
        <span>
          Demo only — not for real roadside assistance or emergencies.
        </span>
      </footer>
    </main>
  )
}
