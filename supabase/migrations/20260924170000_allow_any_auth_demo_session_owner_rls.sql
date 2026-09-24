-- Public demo session — allow any authenticated owner to read their own
-- demo session row.
--
-- The demo start flow was previously locked to Supabase anonymous sessions:
-- the backend rejected non-anonymous tokens with 403, and the demo-session
-- SELECT policy additionally required `is_anonymous = true`. That made a
-- dispatcher signed in at /admin unable to run the public demo at all — and
-- even if a row had been created for that account, RLS would have hidden it
-- from its owner, so DemoScreen restore would immediately show the expired
-- state. The backend gate is removed in the same change set; this migration
-- relaxes the ownership rule so the two stay consistent.
--
-- Ownership now derives from identity + validity only:
--
--   auth_user_id = auth.uid() and expires_at > now()
--
-- Anonymous visitors keep exactly the same own-row scoping they had before
-- (only the redundant anonymity clause is dropped), and a non-anonymous
-- authenticated user — e.g. the dispatcher — can read back the unexpired demo
-- session they own.
--
-- No other policy changes are required:
--
--  * assistance_requests stays guarded by the restrictive "Demo users read
--    only their linked assistance requests" policy: anonymous sessions remain
--    restricted to their own unexpired linked row, while non-anonymous users
--    keep the blanket read they already have through the permissive
--    "Dispatcher can read assistance requests" policy. The demo UI filters by
--    demo_session_id client-side, so extra visible rows never surface there.
--  * No INSERT/UPDATE/DELETE policy is added for `authenticated`, so demo
--    sessions remain read-only for every client; backend writes keep using the
--    service-role key, which bypasses RLS.
--  * Expired rows stay unreadable for every identity.
--
-- The policy name is unchanged so existing references (README, migration
-- tests) keep resolving.

drop policy if exists "Demo user can read own demo session" on demo_sessions;

create policy "Demo user can read own demo session"
  on demo_sessions
  for select
  to authenticated
  using (
    auth_user_id = auth.uid()
    and expires_at > now()
  );
