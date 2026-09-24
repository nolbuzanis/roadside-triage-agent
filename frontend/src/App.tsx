import { useEffect, useState } from 'react'
import type { Session } from '@supabase/supabase-js'
import { supabase } from './lib/supabaseClient'
import AuthScreen from './components/AuthScreen'
import Dashboard from './components/Dashboard'
import DemoScreen from './components/DemoScreen'
import './App.css'

function isDemoPath(pathname: string): boolean {
  return pathname.replace(/\/+$/, '') === '/demo'
}

function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session)
      setReady(true)
    })
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession)
      setReady(true)
    })
    return () => subscription.unsubscribe()
  }, [])

  if (!ready) {
    return <div className="app-loading">Loading…</div>
  }

  if (isDemoPath(window.location.pathname)) {
    return <DemoScreen />
  }

  if (!session) {
    return <AuthScreen />
  }

  return <Dashboard session={session} />
}

export default App
