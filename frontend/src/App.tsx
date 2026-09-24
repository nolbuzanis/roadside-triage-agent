import { useEffect, useState } from 'react'
import type { Session } from '@supabase/supabase-js'
import { supabase } from './lib/supabaseClient'
import AuthScreen from './components/AuthScreen'
import Dashboard from './components/Dashboard'
import DemoScreen from './components/DemoScreen'
import './App.css'

function isAdminPath(pathname: string): boolean {
  return pathname.replace(/\/+$/, '') === '/admin'
}

function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    document.title = isAdminPath(window.location.pathname)
      ? 'Dispatcher Dashboard'
      : 'Roadside AI Demo'
  }, [])

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

  if (isAdminPath(window.location.pathname)) {
    if (!session) {
      return <AuthScreen />
    }
    return <Dashboard session={session} />
  }

  return <DemoScreen />
}

export default App
