import type { AssistanceRequest } from '../types'
import type { StoredDemoSession } from '../lib/demoStorage'
import {
  STATUS_INDICATOR,
  type RealtimeStatus,
} from '../lib/realtimeStatus'
import DemoRequestCard from './DemoRequestCard'

interface DemoActiveViewProps {
  demo: StoredDemoSession
  request: AssistanceRequest | null
  nowMs: number
  realtimeStatus: RealtimeStatus
}

function formatCountdown(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainingSeconds = seconds % 60
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
  nowMs,
  realtimeStatus,
}: DemoActiveViewProps) {
  const remainingSeconds = Math.floor(
    (Date.parse(demo.expires_at) - nowMs) / 1000,
  )
  const indicator = STATUS_INDICATOR[realtimeStatus]
  const maskedPhone = `••• ••• ${demo.phone_last4}`

  return (
    <>
      <div className="demo-meta">
        <span className={indicator.className} role="status">
          {indicator.label}
        </span>
        <span className="demo-countdown" role="timer">
          Session expires in {formatCountdown(remainingSeconds)}
        </span>
      </div>

      {request ? (
        <DemoRequestCard request={request} />
      ) : (
        <section className="demo-panel">
          <h2>Ready for your call</h2>
          <dl className="demo-call">
            <div>
              <dt>Call</dt>
              <dd className="demo-call-number">
                {formatDemoPhone(demo.demo_phone)}
              </dd>
            </div>
            <div>
              <dt>Waiting for a call from</dt>
              <dd className="demo-masked-phone">{maskedPhone}</dd>
            </div>
          </dl>
        </section>
      )}
    </>
  )
}
