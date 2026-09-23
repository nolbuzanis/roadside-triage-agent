-- Progressive assistance-request lifecycle — schema foundation (P0).
--
-- 1. location / vehicle / issue become nullable so a row can exist before
--    intake is complete.
-- 2. A dedicated intake_status column tracks the intake lifecycle
--    (in_progress / completed / abandoned / escalated), leaving the existing
--    `status` column untouched for the dispatcher/business lifecycle.
-- 3. Existing production rows were only ever created after a full intake,
--    so they are backfilled as 'completed' — never 'in_progress'.
-- 4. Future rows default to 'in_progress' for the early-creation flow.
--
-- Ordering is deliberate: the column is added with no default, existing rows
-- are backfilled explicitly, NOT NULL is enforced, and only then is the
-- future default set. Historical rows can therefore never pick up
-- 'in_progress', even if statements were ever applied outside one
-- transaction.

alter table breakdown_tickets
  alter column location drop not null;

alter table breakdown_tickets
  alter column vehicle drop not null;

alter table breakdown_tickets
  alter column issue drop not null;

alter table breakdown_tickets
  add column if not exists intake_status text;

update breakdown_tickets
set intake_status = 'completed'
where intake_status is null;

alter table breakdown_tickets
  alter column intake_status set not null;

alter table breakdown_tickets
  alter column intake_status set default 'in_progress';

alter table breakdown_tickets
  drop constraint if exists breakdown_tickets_intake_status_check;

alter table breakdown_tickets
  add constraint breakdown_tickets_intake_status_check
  check (intake_status in ('in_progress', 'completed', 'abandoned', 'escalated'));
