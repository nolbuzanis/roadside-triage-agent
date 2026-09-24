-- Public demo session — restrict demo request access with RLS (P0, demo
-- sequence step D).
--
-- An anonymous Supabase Auth visitor shares the `authenticated` Postgres
-- role with the dispatcher dashboard, and permissive policies are OR-ed,
-- so the existing blanket dispatcher policy ("Dispatcher can read
-- assistance requests": for select, using (true)) would otherwise let any
-- demo visitor read every assistance request. Following the Supabase
-- anonymous-sign-in guidance, this migration adds a RESTRICTIVE SELECT
-- policy on assistance_requests: restrictive policies are AND-ed with the
-- permissive ones, so anonymous sessions can only reach rows whose linked
-- demo session they own and that is still valid, while the dispatcher
-- (is_anonymous = false) keeps reading everything through the unchanged
-- dispatcher policy.
--
-- demo_sessions gains a permissive SELECT policy for its owner only:
-- authenticated anonymous user + auth_user_id = auth.uid() + expires_at >
-- now(). Validity is expiry only — a session is claimed exactly when the
-- linked request becomes visible, so requiring "unclaimed" here would cut
-- access off at call start. Access never derives from caller_phone; the
-- only authorization path is demo_sessions.auth_user_id.
--
-- No INSERT/UPDATE/DELETE policy is added for `authenticated` on either
-- table, so demo users and the dispatcher remain read-only even though
-- Supabase default privileges grant table privileges to `authenticated`.
-- Backend writes keep using the service-role key, which bypasses RLS
-- entirely, so Twilio/webhook/notification behavior is unchanged. The
-- `assistance_requests` table is already in the `supabase_realtime`
-- publication, and Supabase Realtime authorizes every subscriber against
-- the same SELECT RLS for INSERT/UPDATE events, so demo subscribers
-- receive only rows this restrictive policy allows; no publication change
-- is needed. (Supabase documents that DELETE events are not RLS-filtered;
-- nothing in this system deletes rows for client subscribers today.)
--
-- Plain PostgreSQL (used by the migration test chain) ships neither the
-- `auth` schema nor `auth.jwt()`/`auth.uid()`, and policy expressions are
-- parsed at CREATE POLICY time. The guarded blocks below create stubs that
-- read the same `request.jwt.claims` GUC PostgREST sets, only when the
-- real functions are absent; on hosted Supabase both already exist and
-- the block is a no-op. USAGE on the `auth` schema is granted explicitly
-- because SQL functions are inlined into the querying role's context at
-- evaluation time (hosted Supabase already grants this). A missing
-- `is_anonymous` claim is treated as a permanent (non-anonymous) user so
-- plain-PostgreSQL sessions with no JWT — the dispatcher migration-test
-- convention — keep their read access; real anonymous JWTs always carry
-- `is_anonymous: true`.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
end
$$;

grant select on demo_sessions to authenticated;

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

create policy "Demo users read only their linked assistance requests"
  on assistance_requests
  as restrictive
  for select
  to authenticated
  using (
    coalesce((auth.jwt() ->> 'is_anonymous')::boolean, false) = false
    or exists (
      select 1
      from demo_sessions
      where demo_sessions.id = assistance_requests.demo_session_id
        and demo_sessions.auth_user_id = auth.uid()
        and demo_sessions.expires_at > now()
    )
  );

create policy "Demo user can read own demo session"
  on demo_sessions
  for select
  to authenticated
  using (
    coalesce((auth.jwt() ->> 'is_anonymous')::boolean, false) = true
    and auth_user_id = auth.uid()
    and expires_at > now()
  );
