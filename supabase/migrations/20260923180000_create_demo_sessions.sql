-- Public demo session — short-lived demo-session model (P0).
--
-- Associates one browser demo session with at most one inbound Twilio call.
-- The caller's phone number is never stored in plaintext: the entered or
-- inbound number is normalized to E.164 in the application and persisted
-- only as a keyed HMAC (server-side DEMO_PHONE_HMAC_SECRET) plus the last
-- four digits for display. Matching and claiming are driven by the
-- app/services/demo_sessions.py service layer.
--
-- auth_user_id has no foreign key to auth.users: the auth schema exists
-- only on hosted Supabase, and the plain-PostgreSQL migration test chain
-- must apply this file cleanly. Ownership enforcement arrives later with
-- the demo RLS policies (demo sequence step D).
--
-- assistance_requests.demo_session_id is nullable and references
-- demo_sessions with on delete set null, so existing non-demo rows are
-- untouched and future cleanup of expired demo sessions cannot break
-- assistance-request persistence.
--
-- Claim guards: claimed_at/call_id are written together (check constraint),
-- and the service claims via a guarded update (claimed_at is null and
-- expires_at in the future), so an expired or already-claimed session can
-- never be claimed by a second call.

create table demo_sessions (
  id uuid primary key default gen_random_uuid(),
  auth_user_id uuid not null,
  phone_hmac text not null,
  phone_last4 text not null,
  expires_at timestamptz not null,
  claimed_at timestamptz,
  call_id text,
  created_at timestamptz not null default now(),
  constraint demo_sessions_phone_last4_check check (phone_last4 ~ '^[0-9]{4}$'),
  constraint demo_sessions_claim_fields_check
    check (
      (claimed_at is null and call_id is null)
      or (claimed_at is not null and call_id is not null)
    )
);

create index demo_sessions_phone_hmac_idx on demo_sessions (phone_hmac);

alter table assistance_requests
  add column if not exists demo_session_id uuid
  references demo_sessions (id) on delete set null;

-- Service-role writes bypass RLS. No client access is granted yet: the
-- deny-all policy matches the assistance_requests convention until the
-- demo-session start flow and demo RLS policies land later in the sequence.
alter table demo_sessions enable row level security;

create policy "Anonymous cannot access demo sessions"
  on demo_sessions
  for all
  using (false);
