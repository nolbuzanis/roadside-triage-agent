-- Progressive assistance-request lifecycle — one-time backfill of open rows (P0).
--
-- Finalizes every row still stuck in an open `status` so none remains
-- pending/in_progress after this migration:
--   * pending/in_progress with a full intake (location, vehicle, issue
--     non-empty) -> completed
--   * pending/in_progress still missing intake fields -> abandoned
--
-- Legacy `pending` rows were only ever created on a completed intake, so they
-- end `completed`. Terminal rows (`completed`, `escalated`, `abandoned`) and
-- all intake/hazard/notification data are untouched.
--
-- Calls in flight while this backfill runs are healed by the application:
-- the completion hook self-heals abandoned -> completed when intake finishes,
-- and both finalization guards never overwrite completed/escalated rows.

update breakdown_tickets
set status = 'completed',
    updated_at = now()
where status in ('pending', 'in_progress')
  and coalesce(location, '') <> ''
  and coalesce(vehicle, '') <> ''
  and coalesce(issue, '') <> '';

update breakdown_tickets
set status = 'abandoned',
    updated_at = now()
where status in ('pending', 'in_progress');
