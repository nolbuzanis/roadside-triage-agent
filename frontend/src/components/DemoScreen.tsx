import { useEffect, useState } from 'react'
import { REALTIME_SUBSCRIBE_STATES } from '@supabase/supabase-js'
import { supabase } from '../lib/supabaseClient'
import { startDemoSession } from '../lib/demoSession'
import {
  clearStoredDemoSession,
  readStoredDemoSession,
  writeStoredDemoSession,
  type StoredDemoSession,
} from '../lib/demoStorage'
import { mergeLoadedRequests, upsertRequest } from '../lib/requestList'
import type { RealtimeStatus } from '../lib/realtimeStatus'
import type { AssistanceRequest } from '../types'
import DemoStartForm from './DemoStartForm'
import DemoActiveView from './DemoActiveView'

type DemoPhase = 'start' | 'active' | 'expired'

export default function DemoScreen() {
  const [phase, setPhase] = useState<DemoPhase>('start')
  const [demo, setDemo] = useState<StoredDemoSession | null>(null)
  const [requests, setRequests] = useState<AssistanceRequest[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [realtimeStatus, setRealtimeStatus] =
    useState<RealtimeStatus>('connecting')
  const [nowMs, setNowMs] = useState(() => Date.now())

  useEffect(() => {
    const previousTitle = document.title
    document.title = 'Roadside AI Demo'
    return () => {
      document.title = previousTitle
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    async function restore() {
      const stored = readStoredDemoSession()
      if (!stored) {
        return
      }
      const {
        data: { session },
      } = await supabase.auth.getSession()
      if (cancelled) {
        return
      }
      if (!session) {
        clearStoredDemoSession()
        return
      }

      const { data: sessionRow, error: sessionRowError } = await supabase
        .from('demo_sessions')
        .select('id, expires_at')
        .eq('id', stored.id)
        .maybeSingle()
      if (cancelled) {
        return
      }
      if (sessionRowError) {
        // Keep storage so a refresh retries restore; only a missing row
        // (RLS-hidden = expired/foreign) means the session is gone.
        setError(sessionRowError.message)
        return
      }
      if (!sessionRow) {
        // RLS hides expired or foreign rows — the session can no longer view data.
        clearStoredDemoSession()
        setPhase('expired')
        return
      }

      const refreshed: StoredDemoSession = {
        ...stored,
        expires_at: sessionRow.expires_at,
      }
      setDemo(refreshed)
      setPhase('active')

      const { data: linkedRows, error: linkedError } = await supabase
        .from('assistance_requests')
        .select('*')
        .eq('demo_session_id', refreshed.id)
        .limit(1)
      if (cancelled) {
        return
      }
      if (linkedError) {
        setError(linkedError.message)
        return
      }
      const linked = ((linkedRows ?? []) as AssistanceRequest[]).filter(
        (request) => request.demo_session_id === refreshed.id,
      )
      setRequests((current) => mergeLoadedRequests(current, linked))
    }

    void restore()
    return () => {
      cancelled = true
    }
  }, [])

  const activeDemoId = phase === 'active' && demo ? demo.id : null

  useEffect(() => {
    if (!activeDemoId) {
      return
    }
    let active = true
    const channel = supabase
      .channel(`demo-request-${activeDemoId}`)
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'assistance_requests' },
        (payload) => {
          const incoming = payload.new as AssistanceRequest
          if (incoming.demo_session_id !== activeDemoId) {
            return
          }
          setRequests((current) => upsertRequest(current, incoming))
        },
      )
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'assistance_requests' },
        (payload) => {
          const incoming = payload.new as AssistanceRequest
          if (incoming.demo_session_id !== activeDemoId) {
            return
          }
          setRequests((current) => upsertRequest(current, incoming))
        },
      )
      .subscribe((status) => {
        if (!active) {
          return
        }
        if (status === REALTIME_SUBSCRIBE_STATES.SUBSCRIBED) {
          setRealtimeStatus('live')
        } else {
          setRealtimeStatus('disconnected')
        }
      })
    return () => {
      active = false
      supabase.removeChannel(channel)
    }
  }, [activeDemoId])

  const expiresAtMs = demo ? Date.parse(demo.expires_at) : null

  useEffect(() => {
    if (phase !== 'active' || expiresAtMs === null) {
      return
    }
    function expire() {
      clearStoredDemoSession()
      setDemo(null)
      setRequests([])
      setPhase('expired')
    }
    if (expiresAtMs - Date.now() <= 0) {
      expire()
      return
    }
    const timer = window.setInterval(() => {
      if (expiresAtMs - Date.now() <= 0) {
        expire()
      } else {
        setNowMs(Date.now())
      }
    }, 1000)
    return () => window.clearInterval(timer)
  }, [phase, expiresAtMs])

  async function handleStart(phone: string) {
    setSubmitting(true)
    setError(null)
    try {
      const started = await startDemoSession(phone.trim())
      const stored: StoredDemoSession = {
        id: started.id,
        phone_last4: started.phone_last4,
        expires_at: started.expires_at,
        demo_phone: started.demo_phone,
      }
      writeStoredDemoSession(stored)
      setRequests([])
      setDemo(stored)
      setNowMs(Date.now())
      setRealtimeStatus('connecting')
      setPhase('active')
    } catch (startError) {
      setError(
        startError instanceof Error ? startError.message : String(startError),
      )
    }
    setSubmitting(false)
  }

  function handleRestart() {
    clearStoredDemoSession()
    setDemo(null)
    setRequests([])
    setError(null)
    setPhase('start')
  }

  if (phase === 'start') {
    return (
      <DemoStartForm
        onSubmit={handleStart}
        submitting={submitting}
        error={error}
      />
    )
  }

  return (
    <main className="demo-screen">
      <header className="demo-header">
        <h1>Try the Roadside AI Demo</h1>
        <p className="demo-subtitle">
          Your live assistance request appears here during the call.
        </p>
      </header>

      {error && (
        <p className="demo-error" role="alert">
          {error}
        </p>
      )}

      {phase === 'active' && demo ? (
        <DemoActiveView
          demo={demo}
          request={requests[0] ?? null}
          nowMs={nowMs}
          realtimeStatus={realtimeStatus}
        />
      ) : (
        <section className="demo-panel demo-expired">
          <h2>Demo session expired</h2>
          <p>
            This demo session has expired. Start a new demo to try again.
          </p>
          <button type="button" onClick={handleRestart}>
            Start a new demo
          </button>
        </section>
      )}
    </main>
  )
}
