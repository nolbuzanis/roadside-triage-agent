-- Live call transcript — call_transcripts table with RLS + Realtime (P1,
-- transcript sequence step B).
--
-- Stores one row per completed transcript turn, isolated per demo session:
-- `demo_session_id` links the turn to the browser demo session that owns the
-- call (nullable: non-demo/production calls have no demo session),
-- `call_id` is the Twilio call identifier, `speaker` is `caller` or
-- `assistant`, `text` is the completed turn text, and `seq` orders turns
-- within a call (`unique (call_id, seq)` gives ordering/dedup).
--
-- RLS mirrors the demo-request isolation on assistance_requests
-- (20260924120000_restrict_demo_request_access_rls.sql): the dispatcher
-- keeps blanket read access through a permissive `using (true)` policy,
-- while a RESTRICTIVE policy AND-ed with it limits anonymous demo sessions
-- to rows whose linked demo session they own and that is still valid
-- (`expires_at > now()`). Rows with a null `demo_session_id` (non-demo
-- calls) are therefore visible to the dispatcher but to no anonymous demo
-- session. No INSERT/UPDATE/DELETE policy is added for `authenticated`, so
-- browsers stay read-only; backend transcript writes keep using the
-- service-role key, which bypasses RLS entirely.
--
-- The table is added to the `supabase_realtime` publication so the demo UI
-- can subscribe to INSERT events with the same pattern as
-- assistance_requests. Supabase Realtime authorizes each subscriber against
-- the same SELECT RLS for INSERT events, so a demo subscriber receives only
-- its own session's turns. (Supabase documents that DELETE events are not
-- RLS-filtered; nothing in this system deletes transcript rows.)
--
-- Retention is indefinite: transcript rows are kept forever for
-- debugging/history, matching the demo-data-retention decision — no expiry,
-- cleanup job, cron, or background task deletes them.
--
-- Plain PostgreSQL (used by the migration test chain) ships neither the
-- Supabase API roles nor the `auth` schema / `auth.jwt()` / `auth.uid()`
-- functions, and policy expressions are parsed at CREATE POLICY time. The
-- guarded blocks below create the role and stubs only when absent; on
-- hosted Supabase they already exist and the blocks are no-ops. Policy
-- creation is guarded on `pg_policies` so the migration is idempotent on
-- re-run. A missing `is_anonymous` claim is treated as a permanent
-- (non-anonymous) user so plain-PostgreSQL sessions with no JWT keep their
-- read access; real anonymous JWTs always carry `is_anonymous: true`.

create table if not exists call_transcripts (
  id uuid primary key default gen_random_uuid(),
  demo_session_id uuid references demo_sessions (id) on delete set null,
  call_id text not null,
  speaker text not null constraint call_transcripts_speaker_check
    check (speaker in ('caller', 'assistant')),
  text text not null constraint call_transcripts_text_check
    check (char_length(text) > 0),
  seq integer not null constraint call_transcripts_seq_check
    check (seq >= 0),
  created_at timestamptz not null default now(),
  constraint call_transcripts_call_seq_unique unique (call_id, seq)
);

create index if not exists call_transcripts_demo_session_id_idx
  on call_transcripts (demo_session_id);

create index if not exists call_transcripts_call_id_seq_idx
  on call_transcripts (call_id, seq);

alter table call_transcripts enable row level security;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
end
$$;

grant select on call_transcripts to authenticated;

do $$
begin
  if not exists (
    select 1
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'auth'
      and p.proname = 'jwt'
  ) then
    if not exists (select 1 from pg_namespace where nspname = 'auth') then
      execute 'create schema auth';
    end if;
    execute $f$
      create function auth.jwt() returns jsonb
      language sql stable
      as $fn$
        select coalesce(
          nullif(current_setting('request.jwt.claims', true), ''),
          '{}'
        )::jsonb
      $fn$
    $f$;
  end if;

  if not exists (
    select 1
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'auth'
      and p.proname = 'uid'
  ) then
    execute $f$
      create function auth.uid() returns uuid
      language sql stable
      as $fn$
        select nullif(auth.jwt() ->> 'sub', '')::uuid
      $fn$
    $f$;
  end if;
end
$$;

grant usage on schema auth to authenticated;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'call_transcripts'
      and policyname = 'Anonymous cannot access call transcripts'
  ) then
    create policy "Anonymous cannot access call transcripts"
      on call_transcripts
      for all
      using (false);
  end if;
end
$$;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'call_transcripts'
      and policyname = 'Dispatcher can read call transcripts'
  ) then
    create policy "Dispatcher can read call transcripts"
      on call_transcripts
      for select
      to authenticated
      using (true);
  end if;
end
$$;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'call_transcripts'
      and policyname = 'Demo users read only their linked call transcripts'
  ) then
    create policy "Demo users read only their linked call transcripts"
      on call_transcripts
      as restrictive
      for select
      to authenticated
      using (
        coalesce((auth.jwt() ->> 'is_anonymous')::boolean, false) = false
        or exists (
          select 1
          from demo_sessions
          where demo_sessions.id = call_transcripts.demo_session_id
            and demo_sessions.auth_user_id = auth.uid()
            and demo_sessions.expires_at > now()
        )
      );
  end if;
end
$$;

do $$
begin
  if exists (
        select 1 from pg_publication where pubname = 'supabase_realtime'
      )
      and not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'call_transcripts'
      ) then
    alter publication supabase_realtime add table call_transcripts;
  end if;
end
$$;
