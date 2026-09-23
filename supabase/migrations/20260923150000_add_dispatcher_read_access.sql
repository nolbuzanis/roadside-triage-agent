-- Dispatcher dashboard — single-dispatcher authentication and read access (P0).
--
-- The dispatcher dashboard is a read-only view over assistance_requests
-- behind one Supabase Auth account. This grants the `authenticated` role
-- SELECT while the existing deny-all policy ("Anonymous cannot access
-- tickets": for all, using (false)) keeps every other role — including
-- anon — blocked. Permissive policies are OR-ed, so an authenticated
-- SELECT succeeds via the new policy alone; anon and any other role
-- evaluate only the deny policy and still see zero rows. No INSERT,
-- UPDATE, or DELETE policy is added, so authenticated sessions cannot
-- write even though Supabase default privileges grant table privileges to
-- `authenticated`.
--
-- Backend writes keep using the service-role key, which bypasses RLS
-- entirely, so notification, closing, emergency, and Twilio behavior is
-- unchanged.
--
-- Plain PostgreSQL (used by the migration test chain) does not ship the
-- Supabase API roles. Supabase projects already define `authenticated`,
-- so the guarded CREATE ROLE and the explicit grant are no-ops there;
-- they only make the policy usable on a vanilla PostgreSQL server.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
end
$$;

grant select on assistance_requests to authenticated;

create policy "Dispatcher can read assistance requests"
  on assistance_requests
  for select
  to authenticated
  using (true);
