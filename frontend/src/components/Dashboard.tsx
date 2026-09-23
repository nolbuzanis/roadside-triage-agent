import { useEffect, useState } from 'react'
import { REALTIME_SUBSCRIBE_STATES, type Session } from '@supabase/supabase-js'
import { supabase } from '../lib/supabaseClient'
import { mergeLoadedRequests, upsertRequest } from '../lib/requestList'
import type { AssistanceRequest } from '../types'
import RequestCard from './RequestCard'

type RealtimeStatus = 'connecting' | 'live' | 'disconnected'

const STATUS_INDICATOR: Record<
  RealtimeStatus,
  { label: string; className: string }
> = {
  connecting: { label: 'Connecting…', className: 'live-indicator connecting' },
  live: { label: 'Live', className: 'live-indicator live' },
  disconnected: {
    label: 'Disconnected',
    className: 'live-indicator disconnected',
  },
}

export default function Dashboard({ session }: { session: Session }) {
  const [requests, setRequests] = useState<AssistanceRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [realtimeStatus, setRealtimeStatus] =
    useState<RealtimeStatus>('connecting')

  useEffect(() => {
    let cancelled = false
    supabase
      .from('assistance_requests')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(100)
      .then(({ data, error: loadError }) => {
        if (cancelled) {
          return
        }
        if (loadError) {
          setError(loadError.message)
        } else {
          setRequests((current) =>
            mergeLoadedRequests(current, (data ?? []) as AssistanceRequest[]),
          )
        }
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let active = true
    const channel = supabase
      .channel('assistance-requests')
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'assistance_requests' },
        (payload) => {
          const incoming = payload.new as AssistanceRequest
          setRequests((current) => upsertRequest(current, incoming))
        },
      )
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'assistance_requests' },
        (payload) => {
          const incoming = payload.new as AssistanceRequest
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
  }, [])

  async function handleSignOut() {
    const { error: signOutError } = await supabase.auth.signOut()
    if (signOutError) {
      setError(signOutError.message)
    }
  }

  const activeRequests = requests.filter(
    (request) => request.intake_status === 'in_progress',
  )
  const pastRequests = requests.filter(
    (request) => request.intake_status !== 'in_progress',
  )
  const indicator = STATUS_INDICATOR[realtimeStatus]

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <div>
          <h1>Dispatcher Dashboard</h1>
          <p className="dispatcher-email">{session.user.email}</p>
        </div>
        <div className="header-actions">
          <span className={indicator.className} role="status">
            {indicator.label}
          </span>
          <button type="button" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
      </header>

      <main className="dashboard-main">
        {error && (
          <p className="dashboard-error" role="alert">
            {error}
          </p>
        )}
        {loading ? (
          <p className="dashboard-empty">Loading assistance requests…</p>
        ) : (
          <>
            <section className="request-section">
              <h2>
                Active Calls
                <span className="count">{activeRequests.length}</span>
              </h2>
              {activeRequests.length === 0 ? (
                <p className="section-empty">No active calls</p>
              ) : (
                <div className="card-grid">
                  {activeRequests.map((request) => (
                    <RequestCard key={request.id} request={request} />
                  ))}
                </div>
              )}
            </section>

            <section className="request-section">
              <h2>
                Past Requests
                <span className="count">{pastRequests.length}</span>
              </h2>
              {pastRequests.length === 0 ? (
                <p className="section-empty">No past requests</p>
              ) : (
                <div className="card-grid">
                  {pastRequests.map((request) => (
                    <RequestCard key={request.id} request={request} />
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  )
}
