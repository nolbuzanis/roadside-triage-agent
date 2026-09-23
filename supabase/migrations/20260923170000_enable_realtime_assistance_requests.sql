-- Dispatcher dashboard — Supabase Realtime assistance-request updates (P0).
--
-- The dashboard subscribes to INSERT/UPDATE events on assistance_requests
-- from the browser. Supabase Realtime only streams tables that are members
-- of the `supabase_realtime` publication, and tables created by plain SQL
-- migrations are not added automatically. Realtime authorizes each
-- subscriber against RLS with that subscriber's session claims, so the
-- dispatcher only receives rows it can SELECT; anonymous clients stay
-- denied and backend service-role write behavior is unchanged.
--
-- Plain PostgreSQL (used by the migration test chain) has no
-- `supabase_realtime` publication, so the guarded block is a no-op there —
-- the same convention as the guarded role creation in the dispatcher
-- read-access migration. On Supabase the publication always exists, and
-- the membership check keeps this migration idempotent on re-run.

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
          and tablename = 'assistance_requests'
      ) then
    alter publication supabase_realtime add table assistance_requests;
  end if;
end
$$;
