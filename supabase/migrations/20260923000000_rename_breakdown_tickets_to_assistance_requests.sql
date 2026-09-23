-- Domain rename: breakdown_tickets → assistance_requests (P0).
--
-- Production rows, primary key, unique call_id, the intake_status check
-- constraint, and the RLS deny-all policy all move with the table; no row
-- data is touched. Historical migrations are not rewritten — this is a
-- forward-only rename.
--
-- Postgres does not rename constraint identifiers automatically, so each
-- constraint that carries the old table name is renamed explicitly. The RLS
-- policy ("Anonymous cannot access tickets") is table-attached, keeps its
-- name, and moves with the table — verify it is still attached after apply.
--
-- Deploy coordination: apply this migration immediately before deploying the
-- application build that queries `assistance_requests` — code still querying
-- `breakdown_tickets` fails after the rename. Order: migrate → deploy (or a
-- brief ordered deploy window if ordering cannot be guaranteed).

alter table breakdown_tickets rename to assistance_requests;

alter table assistance_requests
  rename constraint breakdown_tickets_pkey
  to assistance_requests_pkey;

alter table assistance_requests
  rename constraint breakdown_tickets_call_id_key
  to assistance_requests_call_id_key;

alter table assistance_requests
  rename constraint breakdown_tickets_intake_status_check
  to assistance_requests_intake_status_check;
