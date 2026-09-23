import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error(
    'Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY. ' +
      'Copy frontend/.env.example to frontend/.env.local and fill in the ' +
      'public Supabase project URL and the anon/publishable key. ' +
      'Never use the service-role key in the frontend.',
  )
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey)
