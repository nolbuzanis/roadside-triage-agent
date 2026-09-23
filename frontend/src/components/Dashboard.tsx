import { useEffect, useState } from 'react'
import type { Session } from '@supabase/supabase-js'
import { supabase } from '../lib/supabaseClient'
import type { AssistanceRequest } from '../types'
import RequestCard from './RequestCard'

export default function Dashboard({ session }: { session: Session }) {
  const [requests, setRequests] = useState<AssistanceRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    supabase
      .from('assistance_requests')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(100)
      .then(({ data, error: loadError }) => {
        if (loadError) {
          setError(loadError.message)
        } else {
          setRequests((data ?? []) as AssistanceRequest[])
        }
        setLoading(false)
      })
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

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <div>
          <h1>Dispatcher Dashboard</h1>
          <p className="dispatcher-email">{session.user.email}</p>
        </div>
        <button type="button" onClick={handleSignOut}>
          Sign out
        </button>
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
