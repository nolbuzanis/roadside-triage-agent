-- Dispatcher dashboard — reconcile intake_status lifecycle (P0).
--
-- intake_status is now the canonical intake lifecycle field the dashboard
-- reads, but application code historically drove lifecycle through the
-- free-text `status` column, so rows finalized since the intake_status
-- column was added still sit at the insert default 'in_progress'. This
-- one-time backfill makes intake_status match each row's actual lifecycle
-- as expressed by `status`:
--   * status 'completed' -> intake_status 'completed'
--   * status 'abandoned' -> intake_status 'abandoned'
--   * status 'escalated' -> intake_status 'escalated'
--
-- Open rows ('pending' / 'in_progress') keep intake_status 'in_progress':
-- they are still open, which is already the truthful value. `status`
-- values, row counts, and all intake/hazard/notification data are untouched.
--
-- Apply together with the application deploy that starts writing
-- intake_status in tandem with `status`, so rows finalized by the previous
-- application code after this backfill cannot remain stuck at the default.
-- The application's completion guard still self-heals intake_status
-- 'abandoned' -> 'completed' for any call that finishes after an earlier
-- open-status backfill marked it abandoned.

update assistance_requests
set intake_status = 'completed',
    updated_at = now()
where status = 'completed'
  and intake_status <> 'completed';

update assistance_requests
set intake_status = 'abandoned',
    updated_at = now()
where status = 'abandoned'
  and intake_status <> 'abandoned';

update assistance_requests
set intake_status = 'escalated',
    updated_at = now()
where status = 'escalated'
  and intake_status <> 'escalated';
