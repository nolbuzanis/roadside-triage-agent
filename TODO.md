# TODO.md — Roadside Assistance Triage AI Voice Agent

Source of truth:

- `PRODUCT.md` — product requirements
- `ARCHITECTURE.md` — system architecture
- `README.md` — setup and developer workflow

## Architecture Direction

The MVP uses a self-hosted voice orchestration path instead of Vapi:

```text
Inbound PSTN Call
        |
        v
     Twilio
        |
   Media Streams
        |
        v
  FastAPI Voice Server
        |
        v
 OpenAI Realtime API
        |
   +----+------------------+
   |                       |
 normal intake        emergency branch
   |                       |
   v                       v
save intake fields     transfer call
summarize + confirm        |
   |                       v
   v                  Twilio Call Transfer
Supabase
(completed request)
   |
   v
 Twilio SMS
   |
   v
 Dispatcher
```

The backend owns Twilio call/webhook handling, the realtime audio WebSocket bridge, conversation/session state, assistance-request persistence, emergency transfer control, and dispatcher notification integration.

OpenAI Realtime owns the live conversational audio/model loop.

Supabase owns persistence.

Twilio owns PSTN calling and dispatcher SMS.

---

## Priority Legend

- `[ ] P0` = required for MVP
- `[ ] P1` = important, not blocking MVP
- `[ ] P2` = post-MVP

---

# Demo Ready

The highest-priority items gating the demo (other demo-visible follow-ups remain in Post-MVP → P1). Open items are listed in the order they should be worked; the completed production-wiring item is retained here for the record.

## P1 — Cancel a create-in-flight response when the caller interrupts

`input_audio_buffer.speech_started` arriving after `response.create` is sent but before `response.created` is observed sends no `response.cancel` (the app is the sole cancellation authority with `interrupt_response: False`), so a response created in that brief window can keep speaking over the caller.

- Track the outstanding `response.create` so a `speech_started` arriving before `response.created` can cancel the pending/just-created response
- Drop the cancelled response's audio deltas instead of forwarding them to Twilio
- Send no `response.cancel` when no create is pending, and none when a create fails server-side
- Keep normal turn-taking, greeting, and closing flows unchanged

### Acceptance Criteria

- Caller speech in the create-in-flight window cancels the pending/just-created response and its audio deltas are not forwarded
- No spurious `response.cancel` when no create is pending or when a create fails server-side
- Normal turn-taking, greeting, and closing flows are unchanged
- A unit test drives the exact interleaving (`response.create` sent → `speech_started` → `response.created`) and asserts the cancel targets the pending response
- Existing interruption, greeting, and closing suites pass

### Dependencies

- P0 — Make caller interruption stop assistant speech immediately

### Status

- [x] Completed in `feat/cancel-in-flight-response-on-speech` PR

## P1 — Claim the dispatcher-SMS notification atomically and retry failed sends

The `notification_status == "pending"` guard is check-then-act, so a duplicate `confirm_assistance_request` call arriving while the SMS task is still in flight (or after a swallowed `update_notification_status` failure) can double-send, and a `failed` status is permanently suppressed with no recovery.

- Claim the row with a conditional update before sending: only a row still in its pre-send state wins the claim
- Exactly one claim winner fires `notify_dispatcher`; a losing concurrent duplicate still returns `status = "confirmed"` without sending
- Add a defined retry path that re-attempts `failed` sends, while a `sent` row is never re-sent

### Acceptance Criteria

- Two concurrent confirmation handler calls produce exactly one SMS
- A claim-lost duplicate returns `status = "confirmed"` without sending
- A `failed` notification can be retried; a `sent` row is never re-sent
- Unit tests drive the concurrent claim, claim-lost, and failed-then-retry paths
- Existing completion-gating and retry tests still pass

### Dependencies

- P0 — Make persistence partial-safe with completion-gated notification

### Status

- [x] Completed in `feat/atomic-notification-claim-retry` PR

## P1 — Open the greeting gate when no greeting response will ever complete

With `create_response: False`, `_greeting_response_done` only flips on a first `response.done`, so `greeting=""` (or a silently failed greeting `response.create`) leaves the session permanently deaf to caller turns.

- With no greeting configured, allow the first caller commit to create a response
- If the configured greeting response never arrives within a bounded time, fall back so later caller turns still get responses
- Keep the normal greeting path unchanged when the greeting response completes

### Acceptance Criteria

- With `greeting=""`, the first caller commit creates a response
- A greeting response that never arrives within a bounded time does not leave the session permanently deaf
- The configured-greeting happy path is unchanged
- Unit tests cover the empty-greeting first turn and the greeting-timeout/fallback path

### Dependencies

- none

### Status

- [ ] Not started

## P1 — Wire the demo-session start flow into the deployed frontend

Inject `VITE_API_BASE_URL` (the backend base URL) into the `deploy-frontend.yml` build, and configure the backend's `FRONTEND_ORIGINS` with the Firebase Hosting origin so the browser can call `POST /api/v1/demo-sessions` cross-origin.

- Workflow wiring landed in `feat/wire-prod-demo-origin-variables` (config checks + value passing in both deploy workflows)
- Repository variables `VITE_API_BASE_URL` and `FRONTEND_ORIGINS` set, backend and frontend redeployed
- Production preflight and `startDemoSession` checks verified live

### Acceptance Criteria

- The production frontend build receives `VITE_API_BASE_URL` and the config check fails without it
- A cross-origin preflight and POST from the Hosting origin succeed against the deployed backend; an unlisted origin is denied
- `startDemoSession` against production no longer raises the missing-`VITE_API_BASE_URL` error

### Dependencies

- P1 — Deploy the dispatcher dashboard to Firebase Hosting

### Status

- [x] Completed — workflow wiring in `feat/wire-prod-demo-origin-variables`; production variables set, backend and frontend redeployed, and the preflight / `startDemoSession` checks verified in prod

## P1 — Reuse or cancel the superseded early OpenAI connection on duplicate voice webhooks

A second webhook currently overwrites `_pending_connections[call_sid]`, orphaning the first `connection_task` (and the realtime session/WebSocket it creates), which is never awaited, cancelled, or closed.

- A retried/duplicate voice webhook for a call with a pending or live early connection must not leak the superseded task or session
- The media stream must still consume exactly one live connection

### Acceptance Criteria

- A duplicate webhook does not leak the superseded connection task or its realtime session/WebSocket
- The media stream still consumes exactly one live connection
- Exactly one entry remains in `_pending_connections` for the call
- A unit test issues two webhook posts for the same `CallSid` and asserts the first task is cancelled/closed (or reused)
- Existing early-connection and webhook suites still pass

### Dependencies

- none

### Status

- [ ] Not started

---

# Bugs

## P0 — Make caller interruption stop assistant speech immediately

When the caller begins speaking while the assistant is talking, stop both the active OpenAI response and any assistant audio still buffered for Twilio playback.

- Detect caller speech start during an active assistant response
- Cancel/truncate the active OpenAI response using the supported Realtime interruption flow
- Clear queued outbound Twilio media so previously generated assistant audio does not continue playing
- Preserve the caller's new audio/turn normally
- Do not create duplicate assistant responses after interruption
- Keep greeting and closing state machines compatible with interruption behavior

### Acceptance Criteria

- Caller speech during assistant playback stops audible assistant speech immediately
- No stale assistant audio continues after interruption
- Caller can finish a correction or additional detail without being talked over
- Exactly one assistant response is generated for the completed caller turn
- Repeated interruptions do not corrupt session state
- Existing normal-call, emergency, greeting, and closing tests still pass

### Dependencies

- none

### Status

- [x] Completed in `feat/caller-interruption-stop-speech` PR

## P0 — Hang up only after the closing audio finishes playback

Do not hang up based only on OpenAI `response.done`. Verify that the final closing audio has actually finished playing to the caller before completing the Twilio call.

- Keep the fixed closing-message flow
- After the final closing audio has been sent to Twilio, send a Twilio Media Streams `mark`
- Track the mark for the current closing response/call
- Wait for Twilio's corresponding `mark` event before starting hangup
- Remove the fixed 750ms grace period as the primary delivery guarantee
- Add a bounded fallback timeout so a missing mark cannot leave the call open forever
- Preserve caller barge-in behavior: if the caller speaks before final playback completes, do not hang up underneath the new turn

### Acceptance Criteria

- The caller hears the entire closing message before disconnect
- Twilio hangup starts only after the closing playback mark is acknowledged
- A missing mark falls back safely after a bounded timeout
- Caller interruption during the closing cancels/defer hangup correctly
- No duplicate hangup occurs
- Existing closing/emergency teardown tests continue to pass

### Dependencies

- P0 — Make caller interruption stop assistant speech immediately

### Status

- [x] Completed in `feat/hangup-after-closing-audio` PR

## P0 — Show escalated calls on the demo screen during the call and after hangup

Reported against a live demo call: after the call escalated to emergency, no request ever appeared on the demo screen, and it was still absent after hangup. This violates the shipped P0 demo acceptance that completed/abandoned/escalated state appears without refreshing.

What is already ruled out by inspection (so the fix must look elsewhere): demo-session claim and `link_demo_session` run once in the voice webhook (`_match_and_link_demo_session` in `app/api/twilio.py`), before any emergency is known; `update_ticket_hazard` (`app/services/tickets.py`) never touches `demo_session_id`; the demo RLS policy (`20260924120000_restrict_demo_request_access_rls.sql`) has no status filter; and `DemoScreen.tsx` renders whatever linked rows it receives with no status filter. A linked escalated row would therefore render — non-appearance means the row was never linked, never created, unclaimed, unreadable, or never delivered.

- Diagnose the reported call from structured logs and row state: claim outcome (`demo_session_matched` / `demo_session_match_miss`), link outcome, and the escalated row's `demo_session_id` for that `call_id`
- Prime suspects to confirm or rule out: claim miss (wrong caller number, expired/already-claimed session), the link-miss race where `link_demo_session` matches zero rows because the request row did not exist yet (see the related Post-MVP item on recovering a claimed-but-unlinked session — if this is the root cause, that item may be subsumed here), and a realtime/backfill delivery gap on the escalation timeline
- Fix the confirmed cause so the escalated request appears live with its escalated state, persists after hangup, and survives refresh without duplicates; do not change dispatcher or emergency-transfer behavior

### Acceptance Criteria

- Root cause for the reported call is identified from logs/row state and recorded on this item
- A demo call that escalates shows its request on the demo screen live with escalated state, without refresh
- After hangup the escalated request remains visible, and refresh restores it with no duplicates
- Unit tests cover the fixed path (claim/link/escalation visibility); existing demo-session, webhook, escalation, and closing suites still pass
- The call-status card reflects terminal state after hangup (no stuck "connected" display or running timer for escalated/completed/abandoned rows)
- Verification: live demo call with real escalation plus hangup, correlating the voice-webhook claim/link logs with the demo-screen realtime/backfill reads

### Dependencies

- none

### Diagnosis evidence (2026-09-26)

- Live call `CAcdf391ac3b5f191ce3983b19e838282c` escalated ("Escalation state recorded", `app.api.twilio`, 23:30:32Z) after the caller reported a smoking hood; transfer to emergency services was initiated.
- After hangup, refreshing the demo page still showed the call as connected. Refresh re-reads the linked row from the database, so the row is linked and readable — ruling out never-linked/RLS causes for this call.
- Confirmed frontend gap (code inspection): `DemoActiveView.tsx` derives `connected = request !== null`, so any terminal row (escalated/completed/abandoned) still renders the on-call card ("You're on the call" / "Connected to AI agent" with a running elapsed timer). The call card never reflects terminal state.
- Unconfirmed: the accompanying timeline analysis claims teardown finalized the row as `abandoned` before escalation was recorded — verify the row's final `status`/`intake_status` for this `call_id` before relying on it; only the "Escalation state recorded" line is a verbatim log entry.

### Status

- [ ] Not started

---

# Progressive `assistance_request` Lifecycle — P0 Sequence

Target lifecycle:

```text
valid inbound call
→ create one assistance_request immediately
→ progressively persist location / vehicle / issue
→ summarize the three fields and complete only after the caller confirms them
→ complete normally, abandon on disconnect, or escalate on emergency
```

Items below are listed in dependency order (A → B → C → D → E → F). The domain rename was deliberately staged after the behavioral changes were stable, to minimize production risk.

## P0 — Allow nullable intake columns (migration)

Migration-first prerequisite: drop the `NOT NULL` constraints on `location`, `vehicle`, and `issue` so a mostly-empty request row can be inserted at call start, and add a dedicated `intake_status` column for the intake lifecycle. Schema-only change; application code, existing production rows, and `status` semantics are untouched.

- Add a forward-only Supabase migration: `alter table breakdown_tickets alter column location drop not null;` (same for `vehicle`, `issue`)
- Add `intake_status text not null default 'in_progress'` with a check constraint (`in_progress` / `completed` / `abandoned` / `escalated`); backfill existing rows as `completed` before enforcing `NOT NULL` so historical rows are never marked `in_progress`
- Leave `status`, the `call_id` unique constraint, RLS deny-all policy, other defaults, and all existing rows unchanged
- Apply the migration before any application change that can write null intake fields is deployed

### Acceptance Criteria

- Migration applies cleanly locally and in production; row count and stored values are unchanged; historical rows have `intake_status = 'completed'`
- A raw insert with null `location`/`vehicle`/`issue` succeeds and defaults to `intake_status = 'in_progress'`; invalid `intake_status` values are rejected; duplicate `call_id` still rejected; anonymous access still denied by RLS; `status` behavior unchanged
- Existing application test suite passes without modification

### Dependencies

- none

### Status

- [x] Completed in `feat/nullable-intake-intake-status` PR

## P0 — Make persistence partial-safe with completion-gated notification

Convert `create_ticket` into a merge-upsert keyed by `call_id` so an existing row is updated instead of returned unchanged (the current duplicate path silently discards new fields), and gate dispatcher SMS on intake completion. Behavior-preserving for today's single post-intake tool call; the model-facing tool schema and instructions stay unchanged in this step.

- Upsert keyed by `call_id`: insert when missing, merge only non-empty provided fields when present, never null-out previously saved values
- `TicketArgs` accepts optional `location`/`vehicle`/`issue` (at least one required); reject only when there is nothing to save
- Completion = row has non-empty location, vehicle, and issue after merge; only then fire `notify_dispatcher` once (guard on `notification_status == "pending"`) and return tool result `status = "created"` — preserving the closing-flow contract in `session.py`
- Partial saves return a distinct result status (e.g. `"updated"`): no SMS, no closing trigger; the row's `status` column is left unchanged
- Handle the concurrent duplicate `call_id` insert race atomically (on-conflict / `IntegrityError` → return existing row)

### Acceptance Criteria

- Partial update on an existing row overwrites only provided fields; omitted/empty fields are preserved (no null-outs)
- First completing update sends exactly one SMS and returns `status = "created"`; a retried completing call does not re-send SMS
- Partial save sends no SMS and does not start the closing/hangup flow; all existing closing-flow tests still pass
- Concurrent duplicate `call_id` insert yields exactly one row without an unhandled error
- Persistence, tool-handling, notifier, and closing-flow test suites pass (updated for optional args and merge semantics)

### Dependencies

- P0 — Allow nullable intake columns (migration)

### Status

- [x] Completed in `feat/partial-safe-persistence` PR

## P0 — Create assistance request at call start with progressive intake

Every valid inbound call (after Twilio signature validation) idempotently creates exactly one request row with null intake fields, and the assistant persists each field as it is collected through the existing tool. Call state gains only an optional request-id field for log correlation — no new infrastructure.

- After signature validation in the voice webhook, idempotently insert `{call_id, caller_phone}` with `status = "in_progress"` and null intake fields (on-conflict by `call_id` → return existing); log a distinct event (e.g. `assistance_request_started`); a creation failure is logged loudly but must not block the call path
- Every request insert while intake is open (early creation, or the tool's insert-when-missing fallback) sets `status = "in_progress"` explicitly — the new flow never relies on the legacy `pending` default, keeping open rows unambiguous for finalization and the future dashboard
- Carry `assistance_request_id` through `EarlyConnection` → `CallState` (one optional field each); keep keying all DB writes by `call_id`
- Open the model-facing tool schema (all-optional `required`), update tool description and system instructions: save each piece as it is collected and call again to update; the completing call must still return `status = "created"`
- Keep notification logic where it is (completion path from the previous task); explicitly verify the initial insert sends no SMS
- Teardown/disconnect behavior unchanged in this task

### Acceptance Criteria

- Valid voice webhook → exactly one row per `call_id` with null intake fields, status `in_progress`, and zero dispatcher SMS; invalid signature → 403 and no row
- Twilio webhook retry/duplicate for the same call still yields exactly one row
- Two concurrent calls create two isolated rows mapped to the correct calls
- Progressive tool calls persist fields as collected and preserve previously saved fields
- Completing intake updates the same row (no second record), fires exactly one SMS, and triggers the existing closing flow (closing/emergency tests pass)
- Instructions/prompt tests reflect progressive persistence while still collecting all three fields before close

### Dependencies

- P0 — Make persistence partial-safe with completion-gated notification

### Status

- [x] Completed in `feat/call-start-progressive-intake` PR

## P0 — Finalize intake status on completion, disconnect, and emergency

Ensure every created request eventually leaves the open (`in_progress`) status — including calls that terminate before the Media Stream is established, where no WebSocket teardown ever runs. Use the existing Media Stream teardown where available, and add the minimum Twilio call-status handling needed for pre-stream termination. `status` stays free text — no column changes required.

- Completion path (same place SMS fires) sets `status = "completed"` when intake first completes
- Media Stream teardown `finally`: if the row is still `in_progress`, non-blocking update to `abandoned`; log the result and never raise from teardown
- Pre-stream termination: add a Twilio Voice status-callback route (e.g. `POST /api/v1/twilio/status`) for terminal call statuses (`completed`, `busy`, `failed`, `no-answer`, `canceled`), configured on the Twilio number alongside the existing voice webhook and validated with the same `RequestValidator` signature check; finalize the row by `call_id` only if still open — one route plus Twilio config, not a new subsystem; document it in README's Twilio setup
- Both finalization paths share the same guard: only open rows flip to `abandoned`; never overwrite `completed` or `escalated` (covers the Twilio `completed` callback arriving after a normal or emergency call); duplicate/retried callbacks are idempotent
- One-time backfill in the same migration so no pre-existing row is stuck open: rows in `pending`/`in_progress` with non-empty location, vehicle, and issue → `completed`; rows still missing intake fields → `abandoned` (legacy `pending` rows were only ever created on full intake, so they end `completed`); the completion hook must self-heal `abandoned → completed` for any call in flight during the backfill
- Live emergency transfer remains independent of any database write

### Acceptance Criteria

- Mid-intake Media Stream disconnect → collected partial fields preserved, status `abandoned`, still exactly one row per call
- Caller hangs up before the Media Stream connects → status-callback path flips the open row to `abandoned` with no WebSocket teardown involved
- Successful completion → status `completed`; later teardown and/or Twilio status callbacks do not flip it to `abandoned`
- Emergency call → status `escalated` with `hazard_reason` plus whatever intake fields were collected; subsequent teardown/status-callback events do not overwrite it
- Status-callback route rejects invalid Twilio signatures; retried callbacks are idempotent
- Backfill converts only `status` values per the rules above; row count and all intake/hazard/notification data unchanged; no row remains stuck in `pending` or `in_progress` after migration
- DB failures during teardown/callback are logged and never crash request handling
- Closing-flow and emergency suites pass; new tests cover abandonment, pre-stream termination, no-overwrite rules, callback signature/idempotency, and backfill

### Dependencies

- P0 — Create assistance request at call start with progressive intake

### Status

- [x] Completed in `feat/finalize-intake-status` PR

## P0 — Rename `breakdown_ticket` to `assistance_request`

Stage the domain rename as a separate atomic change after the lifecycle behavior is stable, minimizing production risk and keeping mechanical churn out of the behavior PRs.

- Migration: `alter table breakdown_tickets rename to assistance_requests;` — production rows, PK, unique `call_id`, and the RLS policy move with the table; verify the policy is still attached
- Update service/table references, health check, handler and tool name (`create_breakdown_ticket` → `update_assistance_request`), system instructions, session `func_name` checks, structured log events, and call/session flag names
- Rename completion semantics explicitly: `RealtimeSession.ticket_created` → `intake_completed`, matching `ticket_created` structured-log events, and remove the write-only `CallState.ticket_created` — the flag must mean "intake completed," never "a row exists"
- Sweep terminology in README, PRODUCT, ARCHITECTURE, and this TODO's active specs; do not rewrite historical migration files or completed-history entries
- Coordinate migration and application deploy (old code querying the old table name fails after rename — brief ordered deploy window, or a temporary compatibility view if ordering cannot be guaranteed)

### Acceptance Criteria

- Zero `breakdown_ticket` references remain in `app/`, `tests/`, `supabase/` current schema expectations, or active docs (historical migration files and completed-history notes exempt)
- No `ticket_created` flag/event names remain in code, tests, or active docs — replaced by `intake_completed` equivalents
- Production row count, ids, and data unchanged after the rename migration; unique `call_id` and RLS still enforced
- Full test suite passes under the new names; live smoke confirms one Twilio `call_id` maps to exactly one assistance request

### Dependencies

- P0 — Finalize intake status on completion, disconnect, and emergency

### Status

- [x] Completed in `feat/rename-breakdown-ticket-assistance-request` PR

## P0 — Confirm the summarized intake before completion

Saving all three fields currently completes the intake: `update_assistance_request` returns `status = "created"`, fires the dispatcher SMS, and starts the fixed closing line the moment location, vehicle, and issue exist. The caller never hears the collected details read back, so a mis-collected field reaches a dispatcher with no chance to correct it.

- Split saving from completion: `update_assistance_request` only persists fields and returns `ready_for_confirmation` once all three are present — no lifecycle write, no SMS, no closing
- Add a no-argument `confirm_assistance_request` tool the model calls only after the caller explicitly confirms a verbal summary of all three fields
- The confirm handler reads the row back, requires all three fields, refuses escalated rows, completes idempotently, and fires the dispatcher SMS exactly once (gated on `notification_status == "pending"`)
- A correction re-saves the affected field, then the full summary is repeated and asked again until the caller clearly confirms
- Emergency transfer outranks confirmation: the session never invokes the confirm handler for a transfer-owned call and never starts the closing flow on top of one
- System prompt, tool descriptions, README, and this TODO describe the confirmation step

### Acceptance Criteria

- Saving all three fields returns `ready_for_confirmation` and never completes the intake, sends an SMS, or starts the closing/hangup flow
- Only `confirm_assistance_request` completes the intake, and it fires exactly one dispatcher SMS per call (a duplicate confirmation re-saves nothing and does not re-send)
- The fixed closing message and hangup start only on a `confirmed` result; duplicate confirmations and confirmations after an emergency transfer start no closing and never reach the completion backend
- Incomplete and escalated rows are refused by the confirm handler with no lifecycle write and no SMS
- `python -m pytest tests/ -v`, `python -m ruff check .`, and `python -m mypy .` pass

### Dependencies

- P0 — Finalize intake status on completion, disconnect, and emergency

### Status

- [x] Completed in `feat/confirm-intake-before-completion` PR

---

# Phase 1 — Supabase & Application Foundation

## P0 — Create Supabase Project

- Create Supabase project
- Record project URL
- Create/obtain service-role key for backend use
- Add Supabase credentials to local environment
- Verify backend can connect to Supabase

### Acceptance Criteria

- Supabase project is accessible
- Backend can authenticate using the service-role key
- Secrets are excluded from git

### Status

- [x] Completed in `feat/setup-supabase-cli` PR

---

## P0 — Create `breakdown_tickets` Table

Create the MVP ticket table directly in Supabase Postgres.

```sql
create table breakdown_tickets (
  id uuid primary key default gen_random_uuid(),
  call_id text not null unique,
  session_id text,
  caller_phone text,
  location text not null,
  vehicle text not null,
  issue text not null,
  status text not null default 'pending',
  hazard_detected boolean not null default false,
  hazard_reason text,
  notification_status text not null default 'pending',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

- Create table in Supabase
- Add unique constraint on `call_id`
- Add `session_id`
- Add `status`
- Add `hazard_detected`
- Add `hazard_reason`
- Add `notification_status`
- Add timestamp columns
- Verify generated UUID primary keys work
- Verify duplicate `call_id` values are rejected

### Acceptance Criteria

- Table exists in Supabase
- Schema matches the MVP model
- Duplicate `call_id` cannot create duplicate tickets

### Status

- [x] Completed in `feat/create-breakdown-tickets-table` PR

---

## P0 — Configure Supabase Security

- Enable Row Level Security on `breakdown_tickets`
- Do not expose the service-role key to clients
- Create only the minimum policies required for MVP
- Verify anonymous/public clients cannot modify tickets

### Acceptance Criteria

- Backend service-role access works
- Public/anonymous access cannot insert or modify tickets
- Supabase credentials are stored only as server-side secrets

### Status

- [x] Completed in `feat/setup-supabase-cli` PR

---

## P0 — Simplify Python Project Structure

Create only the modules required by the realtime MVP.

```text
app/
  __init__.py
  main.py
  api/
    __init__.py
    twilio.py
    webhooks.py
  services/
    __init__.py
    tickets.py
    notifier.py
    calls.py
    emergency.py
  realtime/
    __init__.py
    session.py
  core/
    __init__.py
    config.py
```

- Remove SQLAlchemy model layer
- Remove Alembic configuration
- Remove database session factory
- Remove `app/models/`
- Remove unused PostgreSQL ORM dependencies
- Add Supabase Python client
- Add OpenAI SDK/client dependency
- Add Twilio dependency
- Add WebSocket support required by the realtime media bridge
- Keep FastAPI as the application server

### Acceptance Criteria

- `python -c "import app"` succeeds
- Application has no SQLAlchemy/Alembic dependency
- Supabase is the only database integration
- OpenAI and Twilio clients can be imported successfully

### Status

- [x] Completed in `feat/simplify-project-structure` PR
- [x] Update remaining dependencies/imports for realtime implementation in `fix/update-dependencies-imports` PR

---

## P0 — Configure Environment Variables

Use:

```text
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

OPENAI_API_KEY=
OPENAI_REALTIME_MODEL=

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=
DISPATCHER_ALERT_PHONE=
EMERGENCY_TRANSFER_PHONE=
```

- Update `.env.example`
- Update `app/core/config.py`
- Validate required configuration at application startup
- Remove `DATABASE_URL`
- Remove `VAPI_API_KEY`
- Remove `VAPI_WEBHOOK_SECRET`
- Add configuration for the OpenAI Realtime model
- Add separate emergency transfer destination

### Acceptance Criteria

- `.env.example` contains only required MVP configuration
- Application fails clearly when required variables are missing
- No secrets are committed
- Emergency transfer destination is configurable independently from dispatcher SMS

### Status

- [x] Completed in `feat/configure-env-variables` PR
- [x] Replace Vapi-specific variables with OpenAI/Twilio variables in `fix/update-dependencies-imports` PR

---

## P0 — Create FastAPI Application

- Create `app/main.py`
- Instantiate `FastAPI(title="Roadside Triage Agent")`
- Add `/health`
- Register Twilio voice routes
- Register ticket/webhook routes where needed
- Configure basic application logging
- Do not add unnecessary CORS configuration unless a browser client actually requires it
- Add WebSocket route for Twilio Media Streams

### Acceptance Criteria

- `uvicorn app.main:app --reload --port 8000` starts successfully
- `GET /health` returns `{"status": "ok"}`
- Twilio voice webhook is registered
- Twilio Media Stream WebSocket route is registered

### Status

- [x] Completed in `feat/create-fastapi-app` PR
- [x] Add Twilio voice/WebSocket routes (PR #10)

---

# Phase 2 — Twilio Telephony & Realtime Voice

## P0 — Configure Twilio Inbound Phone Number

- Purchase/configure a Twilio phone number
- Point the number's incoming voice webhook to FastAPI
- Return TwiML that starts a bidirectional Media Stream
- Verify the call reaches FastAPI
- Capture the incoming caller number from Twilio

### Acceptance Criteria

- Calling the Twilio number reaches the application
- FastAPI receives the caller phone number
- Twilio receives valid TwiML
- The call can establish a Media Stream to the backend

### Status

- [x] Completed in `feat/configure-twilio-inbound-phone` PR

---

## P0 — Implement Twilio Media Stream WebSocket

Create the server-side WebSocket bridge.

- Accept Twilio Media Stream connections
- Handle `connected` event
- Handle `start` event
- Handle incoming `media` audio events
- Forward caller audio to OpenAI Realtime
- Receive model audio from OpenAI
- Convert/forward audio back to Twilio in the format required by the active stream
- Handle `stop`/disconnect events
- Clean up the OpenAI realtime session when the call ends

### Acceptance Criteria

- Caller audio reaches the realtime model
- Model audio reaches the caller
- Two-way audio works
- Call teardown closes both sides cleanly

### Status

- [x] Completed in `feat/complete-twilio-media-stream-audio` PR

---

## P0 — Implement OpenAI Realtime Session

Create `app/realtime/session.py`.

- Establish an authenticated realtime connection to OpenAI
- Configure the selected realtime model
- Configure audio input/output format
- Configure voice
- Configure turn detection/interruption behavior
- Send the system instructions
- Handle streaming audio events
- Handle model response events
- Handle tool/function call events
- Handle errors and disconnects
- Keep session state isolated per phone call

### Acceptance Criteria

- A caller can have a two-way realtime conversation
- Audio responses stream without waiting for the full response
- Model errors do not crash the FastAPI process
- Each phone call has isolated realtime state

### Status

- [x] Completed (implemented in earlier PRs, registered with tools in `feat/openai-realtime-session`)

---

## P0 — Implement Realtime Conversation Instructions

The assistant collects exactly:

1. Location
2. Vehicle details
3. Issue

- Write concise system instructions
- Ask one required question at a time
- Confirm ambiguous answers
- Keep responses short and natural
- Avoid collecting payment information
- Avoid promising a truck ETA
- Confirm that the caller is in a safe situation before continuing normal intake when appropriate
- Tell the assistant to prioritize safety over completing intake
- Configure interruption behavior so the caller can speak naturally

### Acceptance Criteria

- Normal call completes in under 90 seconds in typical cases
- Assistant collects all three required fields
- Assistant does not invent missing information
- Assistant asks follow-up questions when an answer is ambiguous
- Caller can interrupt the assistant naturally

### Status

- [x] Completed in `feat/realtime-conversation-instructions` PR

---

## P0 — Implement `create_breakdown_ticket` Tool

Instead of Vapi function tools, expose a local application tool/function to the realtime session.

Arguments:

```json
{
  "location": "string",
  "vehicle": "string",
  "issue": "string"
}
```

- Define tool name `create_breakdown_ticket`
- Define required arguments
- Register tool with the OpenAI realtime session
- Validate tool arguments with Pydantic
- Capture current Twilio call ID
- Capture caller phone number
- Persist ticket to Supabase
- Return a compact tool result to the model

### Acceptance Criteria

- Realtime model can invoke the tool
- Backend receives structured location, vehicle, and issue
- Exactly one ticket is created for a completed intake
- Model receives a successful tool result and can close the call naturally

### Status

- [x] Completed in `feat/openai-realtime-session` PR

---

# Phase 3 — Emergency Handling

## P0 — Implement Server-Side Emergency Transfer

Emergency escalation should not depend on ticket persistence.

- Create `app/services/emergency.py`
- Provide a direct server-side transfer mechanism using Twilio call control
- Store the active Twilio call identifier in the per-call session state
- Allow the realtime assistant to invoke an emergency transfer action
- Transfer immediately to `EMERGENCY_TRANSFER_PHONE`
- Stop normal intake after transfer begins

### Acceptance Criteria

- Emergency transfer does not require ticket creation
- Transfer can occur during an active call
- Caller is not forced to complete normal intake
- Transfer failure is surfaced/logged without crashing the process

### Status

- [x] Completed in `feat/implement-server-side-emergency-transfer` PR

---

## P0 — Define Emergency Conditions

The realtime assistant should immediately escalate when the caller indicates situations such as:

- Fire or vehicle fire
- Active collision/accident
- Injury or bleeding
- Trapped occupants
- Unsafe position in active traffic
- Explosion or similar immediate hazard
- Other circumstances clearly requiring immediate human/emergency assistance

- Explicitly distinguish safety hazards from harmless mentions of words such as "traffic" or "smoke"
- Instruct the assistant to prioritize safety over completing intake
- Do not implement safety handling as a simple substring matcher

### Acceptance Criteria

- Emergency scenarios trigger the transfer action immediately
- Normal roadside problems continue through the normal intake flow
- The assistant does not rely on exact keyword matching alone

### Status

- [x] Completed in `feat/define-emergency-conditions` PR

---

## P0 — Record Escalation State

When an emergency transfer occurs:

- Set `hazard_detected = true` where a ticket already exists
- Store concise `hazard_reason`
- Set `status = "escalated"` when a record exists
- Log the transfer event with call ID
- Do not block the live transfer on a database write

### Acceptance Criteria

- Emergency cases can be identified in the database when a ticket exists
- Escalation reason is available for review
- Database recording does not block the live transfer

### Status

- [x] Completed in `feat/record-escalation-state` PR

---

# Phase 4 — Ticket Persistence

## P0 — Implement Ticket Service

Create `app/services/tickets.py`.

- Accept validated realtime tool arguments
- Create ticket in Supabase
- Handle duplicate `call_id` idempotently
- Return existing ticket when the same call retries the operation
- Set initial status to `pending`
- Set initial notification status to `pending`
- Store caller phone number
- Store session/call identifiers necessary for troubleshooting

### Acceptance Criteria

- Successful intake creates exactly one ticket
- Retrying the same call does not create another ticket
- Ticket data matches the collected information

### Status

- [x] Completed in `feat/openai-realtime-session` PR

---

## P0 — Implement Call Session State

Create `app/services/calls.py` or equivalent call-session component.

Per-call state should include at minimum:

```text
twilio_call_id
caller_phone
openai_session_id
stream_sid
transfer_state
ticket_created
```

- Create state when Twilio starts the call
- Reuse state throughout the call
- Clear state when the call ends
- Prevent cross-call state leakage

### Acceptance Criteria

- Multiple concurrent calls do not share state
- Ticket tool can identify the correct call
- Emergency transfer acts on the correct live call

### Status

- [x] Completed in `feat/implement-call-session-state` PR

---

# Phase 5 — Dispatcher Notifications

## P0 — Create Supabase Ticket Insert Webhook

- Configure Supabase database webhook on `breakdown_tickets INSERT`
- Trigger notification handler after ticket creation
- Ensure notification processing does not delay the voice interaction

### Acceptance Criteria

- Every newly created ticket triggers notification processing
- Notification failures do not affect ticket creation
- Voice response is independent of Twilio SMS latency

### Status

- [x] Completed in `feat/create-ticket-insert-webhook` PR

## P0 — Implement Dispatcher Notification Service

Create `app/services/notifier.py` or a small notification function/Edge Function.

- Initialize Twilio using server-side credentials
- Format concise SMS containing:
  - Call ID
  - Caller phone
  - Location
  - Vehicle
  - Issue
- Send SMS to `DISPATCHER_ALERT_PHONE`
- Handle Twilio errors gracefully
- Log notification failure

### Acceptance Criteria

- Successful ticket produces an SMS
- SMS contains all critical intake information
- Twilio failure does not fail ticket creation

### Status

- [x] Completed in `feat/create-ticket-insert-webhook` PR

## P0 — Track Notification Status

Update the ticket after notification processing:

```text
pending
sent
failed
```

- Set `notification_status = "sent"` after Twilio accepts the message
- Set `notification_status = "failed"` when notification fails
- Log the Twilio error
- Do not mark failed notifications as successfully delivered

### Acceptance Criteria

- Database clearly shows notification state
- Failed notifications can be identified and retried later

### Status

- [x] Completed in `feat/create-ticket-insert-webhook` PR

# Phase 6 — Testing

## P0 — Test Twilio Voice Webhook

Create tests for:

- Valid incoming call webhook
- Caller phone extraction
- TwiML response
- Media Stream URL generation
- Invalid/malformed request handling

### Acceptance Criteria

- Valid Twilio request produces expected TwiML
- Invalid requests fail safely
- Media Stream is configured correctly

### Status

- [x] Completed in `feat/test-twilio-voice-webhook` PR

---

## P0 — Test Realtime Session

Create tests/mocks for:

- Realtime session setup
- Audio forwarding
- Model audio forwarding
- Tool call events
- Session errors
- Disconnect cleanup

### Acceptance Criteria

- Realtime client can be mocked without external API calls
- Tool calls are parsed correctly
- Session cleanup occurs on call termination

### Status

- [x] Completed in `feat/test-realtime-session` PR

---

## P0 — Test Ticket Persistence

- Valid tool call creates ticket
- Duplicate `call_id` is idempotent
- Caller phone is stored correctly
- Correct location/vehicle/issue are stored
- Default status is `pending`
- Default notification status is `pending`

### Acceptance Criteria

- Database state is correct after each test
- Duplicate requests do not duplicate records

### Status

- [x] Completed in `feat/test-ticket-persistence` PR

---

## P0 — Test Emergency Scenarios

Test the actual voice assistant with at least:

- "My car is on fire"
- "I'm bleeding"
- "We were just in an accident"
- "I'm trapped in the vehicle"
- "I'm stopped in the middle of traffic"
- "Cars are flying past me and I can't get out"
- "There's smoke coming from the car"
- "There's heavy traffic but I'm safely parked"

### Acceptance Criteria

- True emergencies transfer immediately
- Non-emergency mentions do not automatically transfer
- Normal triage still works after benign mentions

### Status

- [x] Completed via live e2e testing

---

## P0 — Test Full Normal Call

Perform a real end-to-end call:

```text
phone call
→ Twilio
→ FastAPI Media Stream
→ OpenAI Realtime
→ three-step intake
→ create_breakdown_ticket tool
→ Supabase
→ tool result
→ call completion
→ dispatcher SMS
```

- Measure total call duration
- Verify ticket contents
- Verify SMS contents
- Verify notification status
- Verify caller can interrupt the assistant naturally

### Acceptance Criteria

- Complete call produces exactly one ticket
- Ticket appears in Supabase
- Dispatcher receives SMS
- Typical successful intake completes in <90 seconds
- Conversation remains responsive throughout the call

### Status

- [x] Completed via live e2e testing

---

## P0 — Test Full Emergency Call

Perform a real emergency scenario:

```text
phone call
→ Twilio
→ FastAPI Media Stream
→ OpenAI Realtime detects hazard
→ emergency transfer tool/action
→ Twilio call transfer
→ human/emergency destination
```

- Verify transfer starts immediately
- Verify normal intake is interrupted
- Verify escalation state is recorded where supported
- Verify a failed database write does not prevent transfer

### Acceptance Criteria

- Caller reaches the configured human/emergency destination
- No database dependency exists for the live transfer
- Emergency transfer latency is acceptable

### Status

- [x] Completed via live e2e testing

---

## P0 — Test Concurrent Calls

- Start at least two simultaneous calls
- Verify audio/session state remains isolated
- Verify tickets are associated with the correct calls
- Verify emergency transfer targets the correct live call

### Acceptance Criteria

- No cross-talk between sessions
- No shared mutable call state
- Both calls can complete independently

### Status

- [x] Completed via live e2e testing

---

# Phase 7 — Observability & Reliability

## P0 — Basic Structured Logging

Log:

- Twilio call start
- Twilio call ID
- caller phone where appropriate
- OpenAI realtime session creation
- OpenAI session ID
- tool invocation
- ticket creation
- notification success/failure
- emergency transfer events
- call teardown
- errors

- Do not log secrets
- Avoid unnecessary full-call transcript logging

### Acceptance Criteria

- A ticket can be traced from call start through notification
- Failures contain enough context to debug
- Call/session IDs allow correlation across services

### Status

- [x] Completed in `feat/basic-structured-logging` PR

---

## P0 — Health Check

- Keep `/health`
- Verify application process is running
- Optionally verify Supabase connectivity separately
- Optionally verify OpenAI/Twilio configuration without making billable API calls

### Acceptance Criteria

- Health endpoint responds quickly
- Deployment platform can use it for health monitoring

### Status

- [x] Completed in `feat/health-check-endpoint` PR

---

## P1 — Error Recovery

- Add retry strategy for transient notification failures
- Add retry/replay mechanism for failed notifications
- Prevent duplicate SMS on webhook retries
- Add timeout handling for Supabase/Twilio calls
- Add bounded reconnect/error handling for OpenAI realtime sessions

---

## P1 — Load/Latency Validation

Measure:

- Call connection latency
- Time-to-first-model-response
- Tool-call latency
- Ticket persistence latency
- Emergency transfer latency
- End-to-end conversation latency

### Acceptance Criteria

- No obvious latency spikes prevent natural conversation
- Emergency transfer remains responsive
- Database writes do not block normal conversation unnecessarily

---

# Phase 8 — Documentation

## P0 — Update README

Replace the Vapi/local PostgreSQL setup with:

- Supabase project setup
- Supabase table creation
- Environment variable setup
- Twilio phone number setup
- Twilio Voice webhook setup
- Twilio Media Streams setup
- OpenAI Realtime configuration
- FastAPI startup
- Local WebSocket development/testing
- Dispatcher SMS configuration
- End-to-end phone test instructions

### Acceptance Criteria

- A fresh developer can follow README from zero to running backend
- No README instructions reference Vapi
- No README instructions reference SQLAlchemy/Alembic
- README configuration matches the actual application

### Status

- [x] Completed in `feat/update-readme` PR

---

## P1 — Add Voice Configuration Documentation

Document:

- Realtime system prompt
- Realtime model configuration
- Voice/audio settings
- Tool schema for `update_assistance_request`
- Emergency transfer behavior
- Twilio webhook configuration
- Media Stream configuration
- Example normal conversation
- Example emergency conversation

---

# MVP Definition of Done

The MVP is complete when all of the following work:

- A driver can call the roadside number
- Twilio answers and streams audio to the application
- OpenAI Realtime provides the live voice interaction
- The assistant collects location
- The assistant collects vehicle details
- The assistant collects issue
- Emergency situations are transferred immediately
- Normal calls invoke `update_assistance_request`
- Exactly one assistance request is created in Supabase
- Duplicate assistance-request creation is idempotent
- Dispatcher receives an SMS
- Notification status is recorded
- Real end-to-end normal call succeeds
- Real end-to-end emergency call succeeds
- At least two simultaneous calls remain isolated
- Typical completed intake is under 90 seconds
- README setup instructions work from a clean environment

---

# Post-MVP

## P1

- Surface a field-complete intake that is never confirmed: since the dispatcher SMS is now gated on `confirm_assistance_request`, a caller who supplies location/vehicle/issue and then hangs up (or never answers the summary) ends as `abandoned` with `notification_status` still `pending`, so a fully-collected request can reach no dispatcher. Acceptance: an `abandoned` row with all three fields present either sends the dispatcher notification on abandonment or raises an explicit log/dashboard alert, the chosen behavior is documented in the README, and observability exists for the never-confirmed case (distinct log event or metric). Verification: unit tests drive abandonment of a fully populated open row and assert the chosen notification/alert behavior; the existing completion-gated SMS tests still pass.
- [x] Harden the prompt for a declined or unanswered confirmation: the confirmation rules require a clear "yes" but never say what the model does when the caller declines, says stop, or does not answer, leaving the turn undefined. Acceptance: `app/realtime/system_prompt.md` states explicitly that a declined/ambiguous/dead-air confirmation must not call `confirm_assistance_request` and defines the fallback turn, and the definition is consistent with the confirmation and emergency rules. Verification: `tests/test_instructions.py` asserts the guidance is present, and the full suite passes.
  - Status: completed in `feat/harden-intake-confirmation` PR — `system_prompt.md` gained a "Reading the Caller's Answer" section (clear affirmative → confirm once; clear rejection → ask what to change; correction → save only changed fields → full re-summary → re-ask; ambiguous → explicit yes/no; dead air → re-ask; emergency still outranks), `CONFIRM_ASSISTANCE_REQUEST_TOOL` repeats the rule, `tests/test_instructions.py` asserts the guidance, and `tests/test_intake_confirmation.py::TestConfirmationAnswerScenarios` covers clear yes, clear no, ambiguous, unanswered, and correction → reconfirmation; reviewer APPROVE after a fix round, `python -m pytest tests/ -v` (656), `ruff`, and `mypy` pass.
- Re-ask the final confirmation after dead air: the prompt tells the model to ask the confirmation question once more when the caller does not answer, but `session.py` only creates a response on `input_audio_buffer.committed`, so pure silence after the summary produces no turn and the re-ask is unreachable (the call just sits until the caller speaks or hangs up). Acceptance: a summary that receives no caller reply within a bounded interval triggers a single scripted re-ask turn (at most one per call), ordinary mid-intake pauses still produce no unsolicited responses, and silence never completes the intake. Verification: unit tests drive summary completion → no-answer interval → `response.create` for the re-ask, and assert no re-ask when the caller answers; confirmation, closing, and interruption suites pass.
- Backstop confirmation classification with a transcript-level guard: the affirmative/rejection/ambiguous lists live only in the system prompt, so a model that confirms on a hedge would still complete the intake and fire the dispatcher SMS. Acceptance: `confirm_assistance_request` never reaches the backend unless the last committed caller turn is an unambiguous affirmative (per the input-audio transcription the transcription-gating item above enables), and the refusal path leaves the intake open with a re-ask turn. Verification: unit tests feed hedged, empty, and affirmative last transcripts and assert the confirm handler's reachability; the existing confirmation, emergency, and closing suites pass.
- [x] Fail loudly when the lifecycle write fails during confirmation: `complete_intake` swallows DB exceptions, so the confirm handler logs `assistance_request_confirmed`, fires the dispatcher SMS, and starts the closing even when `intake_status` never left `in_progress`. Acceptance: a failed lifecycle update is distinguishable from success (distinct error-level event and/or a non-success confirm result, per the chosen design) instead of being logged as confirmation success. Verification: a unit test forces `complete_intake` to raise and asserts the surfaced failure; existing confirm, closing, and teardown tests still pass.
  - Status: completed in `fix/complete-intake-persistence-failure` PR — `complete_intake` returns `False` when the guarded write fails and `True` when it lands (including the idempotent 0-row no-op), `handle_confirm_assistance_request` returns a `status="error"` result with an error-level `assistance_request_completion_not_persisted` event before any notification claim, SMS, or closing, `system_prompt.md` tells the model to report the failure and wait rather than claim success, and `tests/test_intake_confirmation.py::TestCompletionPersistenceFailure` plus the `TestCompleteIntake` return-value and failure-then-retry tests cover both the failure and the retry path; reviewer APPROVE (no blocking findings), `python -m pytest tests/ -v` (680), `ruff`, and `mypy` pass.
- Distinguish an idempotent completion no-op from a row that changed under the writer: `complete_intake` returns success for any 0-row guarded update, so a row flipped to `escalated` between the confirm handler's read-back and the completion write would still be treated as confirmed (claim, SMS, closing). This is unreachable in practice today — escalation and confirmation are serialized within one session — and returning failure for 0 rows would break idempotent retries, so it is deferred. Acceptance: a 0-row completion write reports a distinct no-op outcome that the confirm handler resolves by re-reading the row (still-open/abandoned → retry the write, escalated → escalated result, already-completed → confirmed), and duplicate confirmations still send exactly one SMS. Verification: unit tests drive each observed status against a 0-row write and assert the resolved result; the existing duplicate-confirmation, emergency, and closing suites pass.
- Gate the post-closing hangup on the caller's follow-up turn: after `closing_response_completed`, a caller question still creates a `caller_turn_complete` response, and the re-armed grace task can fire while the assistant's reply is mid-generation/mid-playback, cutting it off. Acceptance: when the caller speaks after the closing response completes, the hangup waits until that follow-up assistant response reaches a terminal state (or the call ends naturally); no disconnect occurs while a post-closing assistant response is in progress. Verification: unit tests drive closing completion → speech_started/stopped → committed → follow-up `response.created`/`response.done` and assert `on_closing_finished` is not called until the follow-up `response.done` arrives, plus a test asserting the grace task does not fire while a response is outstanding.
- Verify the spoken closing delivery the same way the greeting is verified: compare the closing response's delivered transcript against `CLOSING_MESSAGE` on completion. Acceptance: matching deliveries log `closing_delivery_verified` and mismatches log `closing_delivery_mismatch` at error level, both with delivered/expected text and `call_sid`; no audio payloads logged. Verification: unit tests feed `response.done` with matching and mismatching transcripts and assert the two log events.
- Extract a shared Twilio client factory (e.g. `get_twilio_client()`) used by `app/services/emergency.py` and `app/services/hangup.py` so call-control operations do not each construct `TwilioClient(settings...)` independently. Acceptance: a single construction site builds the client from settings; transfer and hangup behavior unchanged. Verification: existing emergency and hangup unit tests pass (patch points updated to the factory); grep shows one `TwilioClient(` construction in `app/`.
- Live end-to-end regression check for the closing flow: place a real call, confirm the summarized intake, and confirm the agent speaks exactly the fixed closing line and Twilio hangs up after the audio finishes with no extra questions. Acceptance: for N test calls, the spoken closing matches `CLOSING_MESSAGE` and the call terminates after `closing_response_completed` + grace. Verification: manual telephony test correlating the `assistance_request_confirmed` → `closing_response_started` → `closing_response_completed` → `call_hangup_started` → `call_hangup_completed` structured log sequence.
- Post-deploy smoke check for the `breakdown_tickets` → `assistance_requests` rename: apply the rename migration in the deployed environment, then place one real call that confirms the summarized intake. Acceptance: exactly one row lands in `assistance_requests`, the `assistance_requests_pkey` and `assistance_requests_call_id_key` constraints exist, the `assistance_requests` RLS policy is attached and enforced, dispatcher SMS arrives with the "New assistance request" copy, and no query or write touches a `breakdown_tickets` table. Verification: live Twilio call plus SQL inspection of table name, constraints, row contents, and policy attachment in the deployed database.
- Post-deploy smoke check for single-dispatcher authentication: after pushing the dispatcher read-access migration to the hosted Supabase project, create the one dispatcher Auth user per the README setup, confirm public sign-ups are disabled in the hosted dashboard (local `config.toml` only affects local development), and verify the account can sign in and `SELECT` from `assistance_requests` using only the public Supabase URL plus the anon/publishable key. Acceptance: anon/unauthenticated queries return no rows, the dispatcher account authenticates and reads requests, and no service-role key is used client-side. Verification: hosted Supabase dashboard steps plus a PostgREST/curl check with the anon key (zero rows) and with the dispatcher's session token (rows returned).
- Post-deploy smoke check for demo request RLS: after pushing the demo access-RLS migration to the hosted Supabase project, verify with a real anonymous Supabase Auth session (browser or PostgREST with an anonymous access token) that read access is isolated to the caller's own valid demo session. Acceptance: an anonymous session's `SELECT` on `assistance_requests` returns exactly its own linked row and zero rows for a foreign request id; `demo_sessions` reads return only its own unexpired row; insert/update attempts on either table are rejected or match no rows; the dispatcher account still reads every row; and a demo subscriber receives only its own rows' realtime INSERT/UPDATE events (not another session's). Verification: hosted PostgREST/curl checks with an anonymous token plus a browser realtime subscription, recorded on this item.
- Post-deploy smoke check for call-transcript RLS + Realtime: after pushing the `call_transcripts` migration to the hosted Supabase project, verify with a real anonymous Supabase Auth session (browser or PostgREST with an anonymous access token) that transcript reads are isolated to the caller's own valid demo session. Acceptance: an anonymous session's `SELECT` on `call_transcripts` returns exactly its own session's turns (ordered by `seq`) and zero rows for a foreign session id, an expired session, or rows with a null `demo_session_id`; client insert/update/delete attempts are rejected or match no rows; the dispatcher account reads every row; and a demo subscriber receives only its own session's realtime INSERT events (not another session's). Verification: hosted PostgREST/curl checks with an anonymous token plus a browser realtime subscription during a live demo call, recorded on this item.
- Post-deploy smoke check for the Firebase Hosting dashboard deployment: after the one-time Firebase project/Hosting site setup and repository configuration (`FIREBASE_SERVICE_ACCOUNT` secret; `FIREBASE_PROJECT_ID`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` variables per README), trigger the `Deploy Dispatcher Dashboard` workflow and validate the production site. Acceptance: the workflow passes its configuration check, lint, build, live-channel deploy, and post-deploy HTML verification; `https://<project-id>.web.app` serves the public demo without a 404, `https://<project-id>.web.app/admin` serves the auth screen, the dispatcher account signs in, loads Active/Past sections, receives a live INSERT/UPDATE without a manual refresh, and refresh restores the same database-backed state; the served bundle contains no `service_role`, `sb_secret_`, Twilio, or OpenAI material; the Cloud Run deploy workflow still runs unchanged on the same push; the concrete production URL is recorded in the root README. Verification: workflow run link plus recorded manual browser checks against the Hosting URL on this item.
- Add frontend build/lint CI on pull requests: `ci.yml` is Python-only today, so a broken frontend build is first caught after merge by the deploy workflow. Acceptance: a `frontend` job (in `ci.yml` or a dedicated workflow) runs `npm ci`, `npm run lint`, and `npm run build` on `pull_request` to `main` when `frontend/**` changes, failing the PR check on type/lint regressions, while Python-only changes are unaffected. Verification: a PR touching `frontend/` shows the job passing; a deliberately introduced `tsc` error fails it; the existing Python jobs still pass unchanged.
- Add a Docker image build check to CI so packaging regressions for non-Python data files (e.g. `app/realtime/system_prompt.md`, loaded at import time but never imported by Python) are caught on the PR instead of at deploy: a job in `ci.yml` runs `docker build` on `pull_request` to `main` and then asserts the prompt file exists in the built image (e.g. `docker run --rm <image> test -f /app/app/realtime/system_prompt.md`). Acceptance: the job passes on current main; a Dockerfile break fails the build step, and a `.dockerignore` change that excludes the prompt file fails the image assertion, while the existing Python test/lint/typecheck jobs are unchanged. Verification: a PR shows the build job passing; deliberately removing the `!app/**/*.md` negation while adding `**/*.md` to `.dockerignore` makes it fail; existing CI jobs pass unchanged.
- Pin `firebaseToolsVersion` in `.github/workflows/deploy-frontend.yml` to a specific `firebase-tools` version (the action currently defaults to `latest`; the repo already pins the Supabase CLI). Acceptance: the deploy workflow passes an explicit `firebaseToolsVersion`, so deploys no longer depend on a moving `latest`. Verification: workflow config shows the pinned version and a subsequent workflow run deploys successfully.
- Scan the built frontend bundle for backend-secret markers before every Firebase Hosting deploy: after `npm run build` in `.github/workflows/deploy-frontend.yml`, fail the workflow if any file under `frontend/dist/` contains `service_role`, `sb_secret_`, `TWILIO_`, or `OPENAI_` material, so the "no backend secrets in the bundle" criterion is mechanically enforced on each deploy rather than only by the one-time manual smoke inspection. Acceptance: the deploy workflow includes a post-build grep step that exits non-zero on a match; a deliberately planted marker in `dist/` fails the step. Verification: workflow step exists and its matching logic is exercised against a sample marker file; a clean build passes.
- Migrate FastAPI startup validation from deprecated `@app.on_event("startup")` to `lifespan` context manager
- Add unit test for `Settings` validation that asserts `ValidationError` when env vars are missing
- Require non-empty values for the remaining required secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DISPATCHER_ALERT_PHONE`, `EMERGENCY_TRANSFER_PHONE`) with the same `Field(min_length=1)` guard already applied to `DEMO_PHONE_HMAC_SECRET`. Acceptance: an empty-string value for any required secret fails startup validation with a field-specific error, while a missing variable still fails as it does today. Verification: unit tests assert `ValidationError` on empty values for each required secret; `python -m pytest tests/ -v` passes.
- Add unit tests for `AssistanceRequestArgs` Pydantic validation and `UPDATE_ASSISTANCE_REQUEST_TOOL` schema shape
- Add unit tests for `CallState` and `CallStateManager` (create/get/remove/isolation) and integration tests verifying tool handlers update state correctly
- Suppress `response.create` for spurious post-greeting input (transcription-based filtering): enable input audio transcription and gate `caller_turn_complete` responses on the committed turn's transcript so empty/filler-only commits (call-setup noise or greeting echo) do not trigger an assistant response. Acceptance: a commit with no speech does not create a response; a commit with real speech creates exactly one. Verification: unit tests feed `conversation.item.input_audio_transcription.completed` with empty vs real transcripts and assert `response.create` counts
- Re-sync the `response.create` attribution deque on a server-side create failure: `_response_create_reasons` pops a reason only at `response.created`, so a create rejected with an `error` event (which never yields a `response.created`) leaves its stale reason queued and shifts attribution for every later response — worst case an ordinary `caller_turn_complete` response is tagged `post_intake_closing` and becomes `closing_response_id`, letting `_handle_closing_response_done` drive the hangup off the wrong response. Acceptance: when a create fails server-side, its queued reason is dropped (or the deque is otherwise re-synced) so the next `response.created` attributes to the correct reason and `closing_response_id` is only ever set by the closing create; a normal greeting/tool/closing call is unaffected. Verification: unit tests drive create → `error` → a later non-closing `response.created` and assert `closing_response_id` stays unset, plus the existing closing-attribution and interruption suites still pass
- Live end-to-end regression check: place a real call and verify the agent speaks exactly one fixed greeting and then stays silent until the caller speaks (no immediate "OK, let's get some information..."). Acceptance: for N test calls, no unsolicited second response before caller speech. Verification: manual telephony test against prod/staging using the new `response_create_sent` + `input_audio_buffer.*` structured logs
- Add a handler-level test for `twilio_media_stream` that drives the `start` event and asserts `process_events` is started exactly once on the early-success, early-failure-fallback, and no-early-connection paths. Acceptance: no path starts two concurrent `process_events` readers and no task is orphaned when early setup fails after task creation. Verification: unit test with a mocked `RealtimeSession` counting `asyncio.create_task(session.process_events)` calls per path
- Pass OpenAI `session_id` to `create_ticket()` for troubleshooting correlation
- Harden tests for partial-safe persistence: assert the completion-path SMS payload is read from the merged ticket row rather than the raw tool arguments, and cover the duplicate-insert race where the post-23505 re-select unexpectedly returns no row. Acceptance: a handler test with partial-but-completing args asserts `notify_dispatcher` receives the merged row's location/vehicle/issue; a persistence test forces an insert unique violation followed by an empty re-select and asserts the original API error propagates. Verification: `python -m pytest tests/test_tool_call_handling.py tests/test_ticket_persistence.py -v` passes with the new tests alongside the existing suites.
- Recover from a timed-out early assistance-request insert so no row is stuck open: `start_assistance_request` is bounded by the voice-webhook timeout, but the underlying insert thread can still commit `in_progress` after the terminal status callback (or teardown) has already run its no-op finalization, leaving an open row with no further Twilio events coming. Acceptance: a row whose insert commits after its call reached a terminal status still ends non-open — either the resolved insert re-checks a recorded terminal-status receipt for that `call_sid`, or the status-callback path retries briefly when the row does not exist yet. Verification: unit tests drive a stalled insert resolving after `abandon_if_open` ran and assert the row is finalized; the early-connection, webhook, and status-callback suites still pass.
- Lock the status no-overwrite guards against Supabase client regressions: `complete_intake` and `abandon_if_open` rely on `.update().eq().in_()` serialization, but today that is only asserted through mocked call chains, so a client-library change to filter encoding would go unnoticed. Acceptance: a test exercises the real postgrest query construction used by both finalizers (generated request path/filters asserted, or executed against an available Supabase/PostgREST instance) and proves `completed`/`escalated` rows are excluded while open rows match. Verification: `python -m pytest tests/ -v` passes with the new test; it skips cleanly when no live database is available, matching the migration-test convention.
- Dispatcher assistance-request dashboard
- Supabase Realtime assistance-request updates
- `/api/v1/assistance-requests` endpoint if a dedicated backend API becomes necessary
- Add unit tests for the dispatcher dashboard's pure display logic using a frontend test runner (e.g. Vitest): placeholder resolution (`Collecting…` for missing fields on `in_progress` requests, `Not collected` on terminal requests, real values for non-empty strings, null/undefined/empty/whitespace-only inputs handled) and the Active/Past partition (`in_progress` → Active; `completed`/`abandoned`/`escalated` → Past), plus the public demo's active-screen display logic (`stepperState` and `fieldBadge` in `frontend/src/components/DemoLivePanel.tsx`, `formatCountdown`/`formatDemoPhone` in `DemoActiveView.tsx`) across every intake status and field combination. Acceptance: a documented `npm test` script in `frontend/` runs the tests offline with no Supabase network dependency. Verification: `cd frontend && npm test` passes with coverage of the placeholder, partition, stepper, and badge cases alongside any extracted helpers.
- Execute and record a manual browser smoke checklist for the dispatcher dashboard once the dispatcher Auth account exists: unauthenticated visit to `/admin` shows only the auth screen (the home route `/` serves the public demo); sign-in opens the dashboard; refresh restores the session; Active/Past sections, `Collecting…`/`Not collected` placeholders, and escalated styling render correctly; sign-out returns to the auth screen; layout works at desktop and tablet widths. Acceptance: each checklist item performed against a running build with real dispatcher credentials and recorded (notes or screenshots linked from the follow-up). Verification: checklist completed after hosted dispatcher-user setup (see the dispatcher-auth post-deploy smoke item above).
- Backfill the dispatcher dashboard when the realtime subscription reconnects: after a `CHANNEL_ERROR` / `TIMED_OUT` / `CLOSED` state later returns to `SUBSCRIBED`, re-run the initial `assistance_requests` query so INSERT/UPDATE events committed during the outage are recovered instead of silently missing while the indicator shows "Live". Acceptance: a simulated disconnect/reconnect cycle ends with dashboard state matching a fresh page load. Verification: frontend unit test drives the subscribe callback through error → `SUBSCRIBED` and asserts the load query runs again; manual smoke with a dropped network confirms missed rows appear.
- Maintain `updated_at` on `assistance_requests` writes (e.g. a `BEFORE UPDATE` trigger setting `updated_at = now()`, or explicit sets in backend update paths) so the stale-update guard in `frontend/src/lib/requestList.ts` `preferNewer` is meaningful rather than relying on commit-ordered delivery. Acceptance: an UPDATE write advances `updated_at`; realtime payloads carry the new value. Verification: migration test asserts the trigger fires on update and existing suites still pass.
- Rate limiting
- Advanced retry/recovery workflows
- Better notification delivery tracking
- Analytics for call completion and escalation rates
- Call/transcript audit tooling
- Authentication for dispatcher-facing interfaces
- Cost and usage monitoring per call
- Live telephony regression check for caller barge-in audibility: unit tests prove `response.cancel` + Twilio `clear` + delta suppression at the message layer, but not audible silence through Twilio's real media buffer. Acceptance: for N test calls, talking over the assistant mid-sentence silences it immediately, the caller can finish a correction without being talked over, and exactly one assistant reply follows the completed turn. Verification: manual call correlating `caller_interruption_detected` → `twilio_playback_cleared` → a single `response_create_sent` in the structured logs, recorded on this item.
- Add automated coverage for the deploy workflows' configuration checks: parse `.github/workflows/deploy-production.yml` and `deploy-frontend.yml` with PyYAML, extract each `Verify required configuration` `run:` script exactly as GitHub de-indents it, and execute it against valid and invalid fixture values (multi-origin `FRONTEND_ORIGINS` lists with spaces and ports; missing scheme, path, trailing slash, credentials, `@`/`?`/`#`, blank and comma-only values; scheme-less, host-less, and empty `VITE_API_BASE_URL`; the existing `sb_secret_` and `service_role` key rejections). Acceptance: the suite passes on current main and fails when a check regresses (e.g. a comma-only origin list accepted, or `VITE_API_BASE_URL` dropped from the build-step env); PyYAML is available to the test run (declare it in `requirements-dev.txt` if it is only a transitive dependency). Verification: `python -m pytest tests/test_workflow_config_checks.py -v` passes, a deliberately weakened check makes it fail, and the existing suites still pass.
- Enforce at-most-one claimed demo session per Twilio call with a partial unique index on `demo_sessions.call_id`, and treat a duplicate-claim race as claim-lost: `call_id` has no unique constraint today (only the both-null/both-non-null check), so two truly concurrent duplicate voice webhooks for the same `CallSid` can each win a different session's guarded claim when the phone has 2+ active sessions, orphaning one claimed session. Acceptance: a partial unique index (`where call_id is not null`) applies cleanly over existing data; concurrent claim attempts for one `call_id` yield exactly one claimed session with the loser handled as claim-lost (no unhandled error); sequential duplicate webhooks remain idempotent; assistance-request linking behavior is unchanged. Verification: migration test asserts the partial unique index exists and a second row with the same non-null `call_id` is rejected; unit test forces SQLSTATE 23505 from the claim path and asserts the service returns claim-lost/None without raising; `python -m pytest tests/test_demo_session_matching.py tests/test_demo_sessions.py tests/test_migration_create_demo_sessions.py -v` passes.
- Recover a claimed demo session left unlinked when the assistance-request row is missing at claim time: if `start_assistance_request` failed or timed out (including a late-committing insert), `link_demo_session` matches zero rows — logged as a warning only — and the session stays unlinked unless a later duplicate webhook happens to re-run matching. Acceptance: a session claimed for a call eventually links to that call's assistance request once the row exists (e.g. a guarded link retry from media-stream start or the Twilio status-callback path); an already-set `demo_session_id` is never overwritten; non-demo calls are unaffected. Verification: unit test drives claim → missing-row link → row appears → retry and asserts exactly one link lands; the matching, webhook, status-callback, and closing-flow suites still pass.
- Document the public demo and dispatcher routes in the README: cover how to open the home route (`/`), start a session, and call the Twilio number, plus that the dispatcher login/dashboard lives at `/admin`. Acceptance: README's demo instructions match the implemented waiting/live/expired flow and reflect the `/` (demo) vs `/admin` (dispatcher) split. Verification: README section reviewed against the UI; links/paths in the docs open the right surface on a local build.
- Backfill the public demo view when the realtime subscription reconnects: after `CHANNEL_ERROR` / `TIMED_OUT` / `CLOSED` returns to `SUBSCRIBED`, re-run the linked `assistance_requests` query so INSERT/UPDATE events committed during the outage are recovered instead of a stale view while the indicator shows "Live". Acceptance: a simulated disconnect/reconnect ends with the demo view matching a fresh page load. Verification: frontend unit test drives the subscribe callback through error → `SUBSCRIBED` and asserts the linked-request refetch runs again (requires the frontend test-runner TODO); manual smoke with a dropped network confirms missed field/status updates appear.
- Execute and record a manual telephony smoke checklist for the public demo UI: start a session from the home route (`/`), confirm the waiting screen (demo number, masked last-four, countdown), place the inbound call, confirm the live card appears and location/vehicle/issue fill progressively, confirm completed/abandoned/escalated transitions appear without refresh, confirm refresh restores the session and view, and confirm expiry shows the expired state. Acceptance: each checklist item performed against a running build with real Twilio/Supabase and recorded (notes or screenshots linked from this follow-up). Verification: checklist completed after the `VITE_API_BASE_URL` / `FRONTEND_ORIGINS` production wiring item above is done.
- Manual browser verification for signed-in demo start (code fix landed in `feat/allow-any-auth-demo-sessions`): `POST /api/v1/demo-sessions` now accepts any authenticated Supabase token and the `demo_sessions` owner-read policy no longer requires `is_anonymous`, so a dispatcher signed in at `/admin` who opens the home demo starts a session directly instead of getting a raw 403 — no forced sign-out, and the dashboard session is never destroyed by demo start. Acceptance: manual check from a signed-in `/admin` session covers the chosen behavior (start works, refresh/restore reads the row back through RLS, expiry shows the expired state, dashboard session intact), and the anonymous (signed-out) start path still passes the demo-session tests. Verification: recorded manual browser check on this item; automated demo-session API + RLS migration suites already pass.
- Link the public demo from the `/admin` auth screen: add a small "Try the public demo" link (href `/`) so the dispatcher login surface points back to the home demo. Acceptance: an unauthenticated visitor on `/admin` can reach the demo with one click. Verification: manual navigation from `/admin` to `/` on a local build.
- Surface intake status and a visually distinct emergency state on the public demo's active screen: the redesigned live panel shows no `intake_status` label anywhere, and `escalated` (hazard) gets no danger treatment — only the stepper position changes — so the emergency/escalated treatment the public-demo UI item required is missing. Acceptance: the active screen displays the request's `intake_status`, and `escalated` renders with a danger treatment distinguishable at a glance from `in_progress`, `completed`, and `abandoned`. Verification: `cd frontend && npm run lint && npm run build` pass, and a browser check against a request in each of the four statuses shows four distinct presentations.
- Extract the presentation duplicated between the demo start and active screens into shared modules: the landing hero copy and footer (`DemoStartForm` vs `DemoActiveView`), the preview status/waveform row and the Location/Vehicle/Issue field-card icon list (`DemoStartForm` vs `DemoLivePanel`), and consider merging the near-identical `.start-card`/`.call-card` styles so copy or styling changes cannot drift between the start and active screens. Acceptance: each duplicated block exists in a single shared module used by both occurrences. Verification: `cd frontend && npm run lint && npm run build` pass, and a side-by-side visual compare of the start and active screens before/after shows no rendering differences.
- Two-phase demo-session expiry: the 15-minute TTL should bound only *starting* the call, with browser access then following the call instead of the creation clock. On a successful claim, extend the session (`expires_at = greatest(expires_at, now() + DEMO_CLAIMED_SESSION_TTL_SECONDS)`, new setting defaulting to `1800`) inside the existing guarded claim update, so the pre-update WHERE still enforces the original claim deadline, claimed rows still cannot be re-claimed, and the existing RLS policies — already gated on `expires_at > now()` — pick up the longer window with no migration or policy edit. The demo countdown must stop being creation-anchored: today it flips to expired at the original deadline even mid-call, so when it reaches zero (and when the linked request first appears) the page re-reads its `demo_sessions` row — a returned row means access was extended (adopt the new `expires_at` and keep counting), a null row means genuinely expired. Acceptance: an unclaimed session still expires at `created_at + DEMO_SESSION_TTL_SECONDS` and cannot be claimed afterwards; a session claimed by a real inbound call keeps owner read access to its session row, linked request, and Realtime events until the extended deadline, then returns to the normal expired state; claims landing after the original deadline and duplicate claims still fail; the extension never shortens an existing deadline; no schema or RLS changes, and the keep-forever retention policy is untouched. Verification: unit tests assert the claim writes the `greatest(...)` value while post-deadline and duplicate claims still return no match; a browser check runs the waiting countdown through a claimed call to the final result without an early expired screen; `python -m pytest tests/ -v` passes and README/.env.example document `DEMO_CLAIMED_SESSION_TTL_SECONDS`.
- Status: implemented in `feat/two-phase-demo-session-expiry` PR — claim-time `greatest(...)` deadline extension inside the guarded claim, startup `gt=0` validation for `DEMO_CLAIMED_SESSION_TTL_SECONDS`, demo countdown recheck on zero and on first linked request, README/.env.example documentation; reviewer APPROVE, automated verification (pytest, ruff, mypy, frontend lint/build) passes. Remaining on this item: the manual browser check of the waiting countdown through a claimed call to the final result without an early expired screen.
- Cover the demo screen's two-phase expiry recheck (`recheckExpiry` in `frontend/src/components/DemoScreen.tsx`) with frontend unit tests: a null `demo_sessions` read shows the expired state and clears storage, a returned row adopts the new `expires_at` and keeps counting, a read error leaves the session active for the next retry, and a stale session id never expires a newer session. Depends on the frontend test-runner TODO above (no `npm test` exists today). Acceptance: `cd frontend && npm test` runs these cases offline with no Supabase network dependency, alongside the display-logic coverage from that TODO. Verification: the new tests pass with the existing suite; deliberately breaking the null-row branch makes them fail.
- Recover a dispatcher-notification claim stranded by a process crash, and guard terminal writes with a per-claim token: a crash between `claim_notification_status` and the terminal `sent`/`failed` write leaves `notification_status = 'sending'` forever (every in-process path now resolves or alarms, but a hard crash does not), and an ambiguous terminal-write commit followed by `_release_claim`'s write retry can overwrite a competing holder's `sending` row, since the terminal write is guarded only on `call_id`. Acceptance: the claim stamps a per-claim token and a claim timestamp (`updated_at` is not maintained on UPDATE, so this needs a `claimed_at` column or a `moddatetime`-style trigger migration); `claim_notification_status` can re-claim a `sending` row only after a bounded lease expires; the terminal `sent`/`failed` write is guarded on the claim token so a late writer cannot release another holder's claim; a `sent` row is never re-sent. Verification: unit tests drive an expired-lease reclaim, a not-yet-expired `sending` row that stays unclaimable, and a token-mismatched terminal write that matches no rows; a migration test asserts the claim timestamp/token columns exist; `python -m pytest tests/ -v`, `python -m ruff check .`, and `python -m mypy .` pass.
- Prove `claim_notification_status` atomicity against real Postgres instead of mocked call chains: the exactly-one-winner guarantee rests on a conditional UPDATE's READ COMMITTED re-evaluation after the row lock and on PostgREST returning only the updated rows, neither of which any current test executes (complements the no-overwrite-guard regression item above, which covers query construction but not concurrency). Acceptance: a test issues two concurrent guarded claim updates for the same `call_id` and observes exactly one winner (one row updated/returned, the loser matching zero rows), and a claim attempted against a `sent` row matches zero rows. Verification: the test runs against an available Supabase/PostgREST instance or testcontainer and skips cleanly when none is available, matching the migration-test convention; `python -m pytest tests/ -v` passes with the new test.
- Document the dispatcher SMS delivery guarantee: the notifier's retry loop re-sends after any Twilio exception, including "message accepted but response lost", so delivery is at-least-once and a dispatcher can receive a duplicate SMS in that ambiguous case (Twilio provides no idempotency key), and the `notification_status` vocabulary (`pending` / `sending` / `sent` / `failed`) is currently undocumented. Acceptance: the README's dispatcher-notification section states the at-least-once semantics, the four `notification_status` values and what each means, and how to correlate messages on `sms_sid` when investigating a suspected duplicate. Verification: README section reviewed against the actual behavior in `app/services/notifier.py` and `app/services/tickets.py`.

## Recently Completed

- [x] End-of-call flow after successful ticket creation (`feat/end-of-call-flow-after-ticket`): exact fixed closing line after `create_breakdown_ticket` succeeds, then closing `response.done` → 750ms grace → Twilio hangup; per-call closing state (`ticket_created` → `closing_response_started` → `closing_response_completed` → `hangup_started`), duplicate-safe, barge-in-safe, emergency-transfer-safe, with structured closing/hangup logs and coverage in `tests/test_closing_flow.py`.

## P2

- Dynamic truck assignment
- ETA calculation
- Multilingual support
- Outbound callback
- GPS/location integration
- Payment processing
- Automated dispatch routing
- Full dispatcher operations dashboard
- Real-time fleet management
- More sophisticated call analytics
- Provider abstraction/fallback between realtime voice vendors

---

# Dispatcher Dashboard — Minimal P0 Sequence

Target experience:

```text
single dispatcher password
→ open dashboard
→ see active assistance requests
→ see past requests
→ receive live INSERT / UPDATE changes
```

The dashboard should remain intentionally small. It is a read-only operational view over the existing `assistance_requests` table.

Items below are listed in dependency order (A → B → C → D → E).

---

## P0 — Reconcile `intake_status` lifecycle

Before the dashboard depends on request lifecycle state, make `intake_status` trustworthy. The application currently drives lifecycle through `status`, while `intake_status` can remain `in_progress` indefinitely.

- Choose `intake_status` as the canonical call/intake lifecycle field
- Update each lifecycle transition so `intake_status` becomes:
  - `in_progress` when the request is created
  - `completed` when all required intake fields are collected
  - `abandoned` when an incomplete call terminates
  - `escalated` when emergency transfer begins
- Keep `status` reserved for future dispatcher/business workflow state
- Backfill or migrate existing rows so `intake_status` matches their actual lifecycle
- Do not change notification, closing, emergency-transfer, or Twilio behavior beyond keeping the lifecycle field accurate

### Acceptance Criteria

- No normal application path leaves a terminal request with `intake_status = 'in_progress'`
- Completed calls have `intake_status = 'completed'`
- Incomplete terminated calls have `intake_status = 'abandoned'`
- Emergency requests have `intake_status = 'escalated'`
- Existing production rows are migrated to a truthful `intake_status`
- `status` remains available for future dispatcher workflow semantics
- Full backend test suite passes

### Dependencies

- none

### Status

- [x] Completed in `feat/reconcile-intake-status-lifecycle` PR

---

## P0 — Configure single-dispatcher authentication and read access

Add the minimum authentication/security required for a private dispatcher dashboard. Use Supabase Auth with one dispatcher account; do not build multi-user management, roles, invitations, or organization support.

- Create one dispatcher Supabase Auth user for the MVP
- Allow authenticated dispatcher sessions to `SELECT` from `assistance_requests`
- Preserve backend service-role write access
- Keep anonymous/public access denied
- Do not expose `SUPABASE_SERVICE_ROLE_KEY` to the browser
- Browser configuration may contain only the public Supabase URL and publishable/anon key
- Document the one-time dispatcher-user setup

### Acceptance Criteria

- Unauthenticated clients cannot read `assistance_requests`
- The dispatcher account can authenticate and read assistance requests
- Authenticated dashboard access uses the public Supabase client key, never the service-role key
- Backend insert/update behavior is unchanged
- Existing RLS protections for anonymous users remain enforced
- Authentication setup is documented

### Dependencies

- P0 — Reconcile `intake_status` lifecycle

### Status

- [x] Completed in `feat/configure-dispatcher-auth-read-access` PR

---

## P0 — Build minimal dispatcher dashboard with initial request history

Create a small Vite + React + TypeScript dispatcher UI. The first version is read-only and should prioritize clarity over features.

- Create a minimal frontend application using:
  - Vite
  - React
  - TypeScript
  - `@supabase/supabase-js`
- Add a simple password-protected entry flow backed by the single Supabase Auth dispatcher account
- After authentication, load recent `assistance_requests` ordered newest-first
- Render two sections:
  - **Active Calls** — `intake_status = 'in_progress'`
  - **Past Requests** — `intake_status IN ('completed', 'abandoned', 'escalated')`
- Show at minimum:
  - started/created time
  - caller phone
  - location
  - vehicle
  - issue
  - intake status
- Display missing active-call fields as `Collecting…`
- Display missing fields on terminal requests as `Not collected`
- Make escalated/emergency requests visually distinct
- Keep the UI read-only
- Do not add maps, dispatch assignment, editing, search, analytics, transcripts, or driver tracking

### Acceptance Criteria

- Unauthenticated visitors see only the authentication screen
- Successful dispatcher authentication opens the dashboard
- Refreshing an authenticated dashboard restores the session
- Active calls and past requests are separated correctly using `intake_status`
- Partial requests render clearly without blank/undefined values
- Completed, abandoned, and escalated requests remain visible in history
- No service-role credentials or other backend secrets appear in the frontend bundle
- Dashboard works at normal desktop and tablet widths

### Dependencies

- P0 — Configure single-dispatcher authentication and read access

### Status

- [x] Completed in `feat/minimal-dispatcher-dashboard` PR (automated verification passed: strict `tsc` build, oxlint, backend suite; live sign-in/session smoke deferred to the recorded manual smoke-checklist TODO above)

---

## P0 — Add Supabase Realtime assistance-request updates

Make the dashboard update automatically as calls arrive and intake progresses. Use Supabase Realtime directly from the dispatcher browser rather than adding a custom FastAPI WebSocket or polling API.

- Subscribe to `INSERT` and `UPDATE` events for `assistance_requests`
- On `INSERT`, add the new request to the local dashboard state
- On `UPDATE`, replace/merge the matching request by `id`
- When `intake_status` changes from `in_progress` to a terminal state, move the request from Active Calls to Past Requests without requiring refresh
- Prevent duplicate rows when the initial query and realtime subscription overlap
- Show a small connection/live indicator so the dispatcher can tell whether realtime updates are connected
- Clean up the realtime subscription when the authenticated dashboard unmounts or signs out
- Do not add polling unless needed as a documented fallback

### Acceptance Criteria

- A newly created assistance request appears without refreshing the page
- Progressive location / vehicle / issue updates appear without refreshing
- A completed request automatically moves from Active Calls to Past Requests
- Abandoned and escalated requests move to Past Requests correctly
- Realtime events do not create duplicate cards
- Refreshing the page returns to the same database-backed state
- Signing out removes the realtime subscription and returns to the authentication screen

### Dependencies

- P0 — Build minimal dispatcher dashboard with initial request history

### Status

- [x] Completed in `feat/supabase-realtime-assistance-updates` PR

---

## P1 — Deploy the dispatcher dashboard to Firebase Hosting

Deploy the Vite + React dispatcher dashboard to Firebase Hosting with a stable production URL. Keep the frontend deployment separate from the Cloud Run voice-service runtime.

- Create/configure a Firebase project or Hosting site for the dispatcher UI
- Build the Vite app as a static production bundle
- Configure Firebase Hosting to serve the generated `dist/` output
- Configure production frontend environment variables for:
  - Supabase URL
  - Supabase public/publishable key
- Never inject backend/service-role secrets into the frontend build
- Add GitHub Actions deployment from `main` so a merged PR automatically deploys the latest dashboard
- Use Firebase's supported GitHub authentication/deployment approach without committing long-lived credentials where avoidable
- Configure SPA fallback routing so client-side navigation still works on refresh
- Verify the production dashboard can authenticate, load request history, and receive Supabase Realtime updates
- Document the Firebase Hosting URL and deployment/setup steps

### Acceptance Criteria

- Merging dashboard deployment changes to `main` automatically deploys the latest frontend
- Production dashboard has a stable Firebase Hosting HTTPS URL
- Dispatcher authentication works in production
- Existing assistance requests load successfully
- A live phone call appears in **Active Calls** without manual refresh
- Progressive location / vehicle / issue changes appear during the call
- Completed, abandoned, and escalated requests move to **Past Requests** automatically
- Refreshing the dashboard does not produce a Firebase 404
- No `SUPABASE_SERVICE_ROLE_KEY`, Twilio credentials, OpenAI credentials, or other backend secrets are present in the frontend bundle
- Existing Cloud Run voice-agent deployment remains unaffected

### Dependencies

- P0 — Add Supabase Realtime assistance-request updates

### Status

- [x] Completed in `feat/firebase-hosting-deploy` PR (Hosting config, deploy workflow, and docs; automated verification passed: frontend strict `tsc` build, oxlint, backend suite, workflow/config validation — live production deploy smoke deferred to the "Post-deploy smoke check for the Firebase Hosting dashboard deployment" TODO in Post-MVP → P1 until the one-time Firebase project/service-account and GitHub variable/secret setup is done)

---

## Explicitly Out of Scope for This Sequence

The first dispatcher dashboard does **not** include:

- multiple dispatcher users
- roles / permissions beyond one authenticated dispatcher account
- request editing
- acknowledge / dispatch / resolve actions
- maps
- truck assignment
- ETA calculation
- search / filtering
- analytics
- transcripts
- fleet management
- a dedicated `/api/v1/assistance-requests` backend endpoint

Those should only be introduced after real dispatcher usage demonstrates a need.

---

# Public Demo Session — P0 Sequence

Target experience:

```text
visitor opens demo page
→ enters the phone number they will call from
→ starts a short-lived demo session
→ page waits for their call
→ caller phones the Twilio demo number
→ backend matches the inbound caller to that demo session
→ assistance request is linked to the session
→ only that browser can see the live request
→ session expires automatically
```

The phone number is used only to correlate the inbound Twilio call with the browser session. It must not, by itself, authorize access to assistance-request data.

Items below are listed in dependency order (A → B → C → D → E → F).

---

## P0 — Add short-lived demo-session model

Add the minimum persistence needed to associate one browser demo session with one inbound phone call.

- Add a `demo_sessions` table containing at minimum:
  - `id`
  - `auth_user_id`
  - `phone_hmac`
  - `phone_last4`
  - `expires_at`
  - `claimed_at`
  - `call_id`
  - `created_at`
- Add optional `demo_session_id` to `assistance_requests`
- Normalize entered phone numbers to E.164 before matching
- Do not store the entered phone number as a new plaintext field in `demo_sessions`
- Store a keyed HMAC of the normalized phone number using a server-side secret rather than a plain hash
- Default demo-session lifetime to a short configurable window, e.g. 15 minutes
- A demo session may be claimed by at most one Twilio call
- Existing non-demo assistance requests remain unchanged

### Acceptance Criteria

- A valid demo session can be created with an expiry timestamp
- Raw phone number is not persisted in `demo_sessions`
- The same normalized phone number always produces the same server-side HMAC
- An expired session cannot be claimed
- A claimed session cannot be claimed by a second call
- Existing production assistance-request behavior is unchanged
- Migration and backend tests pass

### Dependencies

- none

### Status

- [x] Completed in `feat/demo-session-model` PR

---

## P0 — Create secure demo-session start flow

Allow a visitor to start a demo without creating a permanent account.

- Enable Supabase anonymous authentication for the public demo
- On **Start Demo**, create/reuse an anonymous Supabase Auth session in the browser
- Add a FastAPI endpoint such as:

  `POST /api/v1/demo-sessions`

- Require the Supabase access token on the request
- Validate the authenticated anonymous user server-side
- Accept the phone number the visitor intends to call from
- Normalize it to E.164
- Compute the server-side phone HMAC
- Create a short-lived `demo_sessions` row owned by that authenticated user
- Return only safe session metadata:
  - session id
  - phone last four digits
  - expiry time
  - demo phone number
- Do not return or expose the phone HMAC or HMAC secret

### Acceptance Criteria

- Visitor can begin a demo without creating a named account
- Invalid phone numbers are rejected
- Missing/invalid Supabase auth tokens are rejected
- Demo session is associated with the correct anonymous `auth_user_id`
- Response contains no sensitive phone-matching data
- Refreshing the browser retains the anonymous Supabase session
- Backend secrets never enter the frontend bundle

### Dependencies

- P0 — Add short-lived demo-session model

### Status

- [x] Completed in `feat/secure-demo-session-start-flow` PR (automated verification passed: endpoint/auth/phone/response-safety tests, full backend suite, ruff, mypy, frontend oxlint + strict `tsc` build, bundle secret scan — live browser smoke deferred to the public demo UI TODO and the deployed-flow wiring TODO in `# Demo Ready`)

---

## P0 — Match inbound Twilio calls to demo sessions

When an inbound call begins, associate it with the active browser session created for that caller.

- After validating the Twilio voice webhook, normalize the inbound `From` number to E.164
- Compute the same server-side phone HMAC used by demo-session creation
- Look for an unexpired, unclaimed demo session matching that HMAC
- Atomically claim at most one matching session
- Store:
  - `claimed_at`
  - Twilio `call_id`
- Link the call's `assistance_request` to the claimed `demo_session_id`
- Continue normal voice behavior if no demo session exists
- Never delay or fail the voice call because demo-session matching failed
- Do not expose matching details in logs

### Acceptance Criteria

- Calling from the number entered on the demo page claims the correct active session
- The resulting assistance request is linked to that demo session
- Calling from another number does not attach to the session
- Expired sessions are ignored
- Duplicate Twilio webhooks do not claim multiple sessions
- Two simultaneous callers remain isolated
- Calls made outside the public demo continue functioning normally

### Dependencies

- P0 — Create secure demo-session start flow

### Status

- [x] Completed in `feat/match-twilio-demo-sessions` PR

---

## P0 — Restrict demo request access with RLS

Ensure a public demo visitor can read only the assistance request associated with their own authenticated demo session.

- Add RLS policies allowing an authenticated anonymous user to read:
  - their own `demo_sessions` row
  - an `assistance_request` whose `demo_session_id` belongs to that user
- Require that the demo session is still valid for public-demo access
- Do not permit anonymous/demo users to:
  - read other assistance requests
  - insert assistance requests
  - update assistance requests
  - delete assistance requests
- Preserve dispatcher access to all assistance requests
- Preserve backend service-role access
- Ensure Supabase Realtime respects the same SELECT policy
- Do not authorize access based solely on `caller_phone`

### Acceptance Criteria

- Demo user A can read request A
- Demo user A cannot read request B
- Changing query parameters or guessing another request UUID does not expose it
- Entering another person's phone number does not grant access to historical requests for that number
- Demo users cannot modify assistance-request data
- Dispatcher dashboard access remains unchanged
- Realtime events are delivered only for rows the authenticated demo user may select

### Dependencies

- P0 — Match inbound Twilio calls to demo sessions

### Status

- [x] Completed in `feat/restrict-demo-request-access-rls` PR

---

## P0 — Build the public demo waiting and live-call UI

Add a simple public demo experience to the Firebase-hosted frontend.

### Before the call

Show:

```text
Try the Roadside AI Demo

Enter the phone number you'll call from

[ +1 604 555 1234 ]

[ Start Demo ]
```

After creating the session, show:

```text
Ready for your call

Call:
(XXX) XXX-XXXX

Waiting for a call from:
••• ••• 1234

Session expires in 14:32
```

### During the call

Once an assistance request is linked, replace the waiting state with the live request:

- call status
- location
- vehicle
- issue
- intake status
- `Collecting…` for fields not yet captured
- visually distinct emergency/escalated state

Subscribe using Supabase Realtime so progressive request updates appear without refresh.

- Never display the full caller phone number
- Do not expose other requests or request history
- Handle natural call completion, abandonment, and escalation
- Display an expired-session state when the demo window ends

### Acceptance Criteria

- User can start the demo from the public page
- Waiting screen clearly identifies the last four digits being matched
- Correct inbound call automatically transitions the page to the live request
- Location, vehicle, and issue appear progressively during the conversation
- Another demo user's call never appears
- Completed/abandoned/escalated state appears without refreshing
- Full caller phone number is never shown
- Refresh during an active demo restores the correct session and request
- Expired demo sessions can no longer view request data

### Dependencies

- P0 — Restrict demo request access with RLS

### Status

- [x] Completed in `feat/public-demo-waiting-live-call-ui` PR (automated verification passed: frontend oxlint + strict `tsc` build, backend suite, ruff, mypy; live telephony/realtime smoke deferred to the manual demo smoke-checklist TODO below)

---

## P0 — Add demo-session expiry and cleanup

Ensure public demo sessions and demo data do not accumulate indefinitely.

- Reject or hide expired demo sessions automatically
- Add cleanup for expired `demo_sessions`
- Define a short retention period for public-demo assistance requests, e.g. 48 hours
- Delete or anonymize expired demo request data according to the chosen retention policy
- Do not delete non-demo assistance requests
- Ensure cleanup cannot affect an active call
- Document the demo retention behavior

### Acceptance Criteria

- Expired sessions cannot access assistance requests
- Old demo sessions are removed automatically
- Demo assistance requests older than the configured retention period are removed/anonymized
- Production/non-demo requests are never affected
- Active demo calls are never removed by cleanup
- Cleanup failure does not affect the live voice application

### Dependencies

- P0 — Build the public demo waiting and live-call UI

### Status

- [x] Skipped — superseded by the product decision to keep all demo data indefinitely: no cleanup, deletion, or anonymization implemented; expiry enforcement verified against the existing claim/RLS tests and the retention policy documented in README in the `docs/demo-data-retention` PR

---

# Live Call Transcript — Feature Sequence

Target experience:

```text
visitor on live demo call
→ sees caller ("You") and assistant ("AI Agent") turns with timestamps appear in real time
→ refresh restores prior turns (backfill)
→ empty/dead-air states render clearly instead of the static sample
```

Design decisions (agreed 2026-09-26):

- Storage: new Supabase table (queryable, RLS-isolated), kept indefinitely for debugging/history — no expiry/cleanup, consistent with the demo-data-retention decision above.
- Delivery: Supabase Realtime (same pattern as `assistance_requests`), no custom backend WS/SSE.
- Scope: demo UI only (`DemoLivePanel`); dispatcher dashboard stays transcript-free.
- Presentation: speaker labels `You` / `AI Agent` + timestamps + autoscroll + backfill on restore/reconnect.
- Transcription model: `gpt-4o-mini-transcribe` for caller input (cheapest); caller transcription adds separate billed usage, assistant `response.output_audio_transcript` is free side-effect text.

Items below are listed in dependency order (A → B → C → D).

---

## P1 — Enable caller + assistant transcript events in the realtime session

Capture transcript text in `app/realtime/session.py` without affecting the voice loop.

- Enable caller transcription in `_configure_session` via `audio.input.transcription: {model: "gpt-4o-mini-transcribe"}` so OpenAI emits `conversation.item.input_audio_transcription.completed` (currently never arrives).
- Keep assistant `response.output_audio_transcript.delta/done` handling (currently debug-log-only at `session.py:574-579`) and expose the completed text with its `response_id` for attribution (avoid mis-tagging greeting/closing/interrupted responses).
- Capture must be non-blocking: never delay audio forwarding or tool dispatch; failures to transcribe must not break the call.
- Coordinate with Post-MVP P1 transcription-based filtering (commit-gating on the same `input_audio_transcription` events): land the `audio.input.transcription` model config once to satisfy both.

### Acceptance Criteria

- A caller turn produces an `input_audio_transcription.completed` event with text.
- An assistant turn produces an `output_audio_transcript.done` event with text and `response_id`.
- A failed/empty transcription does not break turn-taking, greeting, or closing flows.
- No audio payloads logged, only text + ids.

### Dependencies

- none

### Status

- [x] Completed in `feat/enable-transcript-events` PR

---

## P1 — Add `call_transcripts` table with RLS + Realtime, keep-forever

Add the minimum persistence for transcript turns, isolated per demo session.

- Add a `call_transcripts` table containing at minimum:
  - `id`
  - `demo_session_id` (nullable for non-demo calls)
  - `call_id`
  - `speaker` (`caller` / `assistant`)
  - `text`
  - `seq` (per-call ordering)
  - `created_at`
- Add the table to the `supabase_realtime` publication.
- Add RLS mirroring the demo-request isolation: owner-only reads on unexpired own session, dispatcher reads all, no anon cross-session reads, no client inserts/updates.
- Keep rows indefinitely (no cleanup/deletion job), matching demo-data retention.

### Acceptance Criteria

- Migration applies cleanly; table + publication + RLS policies exist.
- An anonymous demo session reads only its own rows; a foreign session id returns zero rows.
- Dispatcher account reads all rows.
- Realtime `INSERT` events flow only to the owning session subscriber.
- README retention section updated to explicitly include `call_transcripts` (keep-forever).
- Existing `assistance_requests` behavior unchanged.

### Dependencies

- P1 — Enable caller + assistant transcript events in the realtime session

### Status

- [x] Completed in `feat/call-transcripts-table-rls-realtime` PR

---

## P1 — Persist transcript turns from session events

Write each completed turn to `call_transcripts` from the realtime event handler.

- On `conversation.item.input_audio_transcription.completed`, insert `speaker = caller` with the committed text.
- On `response.output_audio_transcript.done`, insert `speaker = assistant` with the response text + `response_id` attribution (skip drops for cancelled/interrupted responses to avoid partial-text spam, per chosen policy).
- Resolve `demo_session_id` via `CallState.assistance_request_id → assistance_requests.demo_session_id` (or carry it through `twilio.py` → `session.py`).
- Assign `seq` per `call_id` for ordering/dedup; writes run off the audio hot path (background task, same pattern as notification claim).

### Acceptance Criteria

- One caller turn yields exactly one `caller` row; one assistant turn yields exactly one `assistant` row.
- Interrupted/cancelled assistant responses do not produce duplicate or truncated rows.
- Rows carry the correct `demo_session_id` / `call_id` and monotonically increasing `seq`.
- A DB write failure is logged loudly and never breaks the live call.

### Dependencies

- P1 — Add `call_transcripts` table with RLS + Realtime, keep-forever

### Status

- [x] Completed in `feat/persist-transcript-turns` PR

---

## P1 — Render the live transcript in the demo UI via Supabase Realtime + backfill

Replace the static sample in `frontend/src/components/DemoLivePanel.tsx:14-39,194-214` with live data.

- Add `TranscriptTurn{id, speaker: 'You' | 'AI Agent', text, seq, created_at}` to `frontend/src/types.ts`.
- Subscribe in `DemoScreen.tsx` (same channel pattern as `assistance_requests`) to `INSERT` on `call_transcripts` filtered to the active `demo_session_id`; order/dedup by `seq`/`id`.
- Backfill on restore and on realtime reconnect (re-run the linked query after `SUBSCRIBED`, same gap pattern as the request backfill TODO).
- Replace `SAMPLE TRANSCRIPT` header + disclaimer with live header/indicator; render empty state (waiting for speech), autoscroll on new turns, relative timestamps from call start.
- Dispatcher dashboard unchanged (explicitly out of scope).

### Acceptance Criteria

- Live caller + assistant turns appear without refresh during a real call.
- Refresh restores the same turns in order with no duplicates.
- Reconnect after a dropped network recovers missed turns.
- Empty/dead-air call shows the empty state, never the old sample text.
- `cd frontend && npm run lint && npm run build` pass.

### Dependencies

- P1 — Persist transcript turns from session events

### Status

- [x] Completed in `feat/render-live-transcript-demo-ui` PR
