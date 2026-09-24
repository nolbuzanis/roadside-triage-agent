/**
 * Public demo session start flow.
 *
 * The visitor signs in anonymously with Supabase Auth (or reuses the current
 * session, which may be a signed-in account such as the dispatcher) and the
 * access token authorizes POST /api/v1/demo-sessions on the backend. Only
 * public configuration may appear here — never backend secrets such as the
 * service-role key.
 */

import { supabase } from './supabaseClient'

export interface DemoSessionStartResponse {
  id: string
  phone_last4: string
  expires_at: string
  demo_phone: string
}

function apiBaseUrl(): string {
  const base: string | undefined = import.meta.env.VITE_API_BASE_URL
  if (!base) {
    throw new Error(
      'Missing VITE_API_BASE_URL. Copy frontend/.env.example to ' +
        'frontend/.env.local and set it to the backend base URL (for local ' +
        'development: http://localhost:8000). Never put backend secrets here.',
    )
  }
  return base.replace(/\/+$/, '')
}

/**
 * Create or reuse the browser's Supabase session (anonymous for a public
 * visitor, or the signed-in account's session), then start a short-lived
 * demo session for the given call-back-from number.
 *
 * Throws when anonymous sign-in or the backend request fails, so callers can
 * surface the failure instead of silently continuing without a session.
 */
export async function startDemoSession(
  phone: string,
): Promise<DemoSessionStartResponse> {
  const {
    data: { session: existingSession },
  } = await supabase.auth.getSession()

  let accessToken = existingSession?.access_token
  if (!accessToken) {
    const { data, error } = await supabase.auth.signInAnonymously()
    if (error) {
      throw new Error(`Anonymous sign-in failed: ${error.message}`)
    }
    accessToken = data.session?.access_token
    if (!accessToken) {
      throw new Error('Anonymous sign-in returned no access token')
    }
  }

  const response = await fetch(`${apiBaseUrl()}/api/v1/demo-sessions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ phone }),
  })

  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`Demo session start failed (${response.status}): ${detail}`)
  }
  return (await response.json()) as DemoSessionStartResponse
}
