/**
 * Per-tab persistence for the public demo session.
 *
 * The backend response is the only place the demo phone number exists, so the
 * safe metadata needed to restore the waiting/live view after a refresh is
 * kept in sessionStorage (per-tab, cleared with the tab). The row itself is
 * re-validated against Supabase RLS on restore — this cache never authorizes
 * access on its own.
 */

export interface StoredDemoSession {
  id: string
  phone_last4: string
  expires_at: string
  demo_phone: string
}

const STORAGE_KEY = 'roadside-demo-session'

function isStoredDemoSession(value: unknown): value is StoredDemoSession {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as Record<string, unknown>
  return (
    typeof candidate.id === 'string' &&
    typeof candidate.phone_last4 === 'string' &&
    typeof candidate.expires_at === 'string' &&
    typeof candidate.demo_phone === 'string'
  )
}

export function readStoredDemoSession(): StoredDemoSession | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) {
      return null
    }
    const parsed: unknown = JSON.parse(raw)
    if (!isStoredDemoSession(parsed)) {
      window.sessionStorage.removeItem(STORAGE_KEY)
      return null
    }
    return parsed
  } catch {
    clearStoredDemoSession()
    return null
  }
}

export function writeStoredDemoSession(session: StoredDemoSession): void {
  window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session))
}

export function clearStoredDemoSession(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Storage may be unavailable in hardened browser modes; the in-memory
    // session still drives the current tab.
  }
}
