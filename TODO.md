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
create_ticket()       transfer call
   |
   v
Supabase
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

# Progressive `assistance_request` Lifecycle — P0 Sequence

Target lifecycle:

```text
valid inbound call
→ create one assistance_request immediately
→ progressively persist location / vehicle / issue
→ complete normally, abandon on disconnect, or escalate on emergency
```

Items below are listed in dependency order (A → B → C → D → E). The domain rename was deliberately staged last, after the behavioral changes were stable, to minimize production risk.

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

- Gate the post-closing hangup on the caller's follow-up turn: after `closing_response_completed`, a caller question still creates a `caller_turn_complete` response, and the re-armed grace task can fire while the assistant's reply is mid-generation/mid-playback, cutting it off. Acceptance: when the caller speaks after the closing response completes, the hangup waits until that follow-up assistant response reaches a terminal state (or the call ends naturally); no disconnect occurs while a post-closing assistant response is in progress. Verification: unit tests drive closing completion → speech_started/stopped → committed → follow-up `response.created`/`response.done` and assert `on_closing_finished` is not called until the follow-up `response.done` arrives, plus a test asserting the grace task does not fire while a response is outstanding.
- Verify the spoken closing delivery the same way the greeting is verified: compare the closing response's delivered transcript against `CLOSING_MESSAGE` on completion. Acceptance: matching deliveries log `closing_delivery_verified` and mismatches log `closing_delivery_mismatch` at error level, both with delivered/expected text and `call_sid`; no audio payloads logged. Verification: unit tests feed `response.done` with matching and mismatching transcripts and assert the two log events.
- Extract a shared Twilio client factory (e.g. `get_twilio_client()`) used by `app/services/emergency.py` and `app/services/hangup.py` so call-control operations do not each construct `TwilioClient(settings...)` independently. Acceptance: a single construction site builds the client from settings; transfer and hangup behavior unchanged. Verification: existing emergency and hangup unit tests pass (patch points updated to the factory); grep shows one `TwilioClient(` construction in `app/`.
- Live end-to-end regression check for the closing flow: place a real call, complete intake, and confirm the agent speaks exactly the fixed closing line and Twilio hangs up after the audio finishes with no extra questions. Acceptance: for N test calls, the spoken closing matches `CLOSING_MESSAGE` and the call terminates after `closing_response_completed` + grace. Verification: manual telephony test correlating the `intake_completed` → `closing_response_started` → `closing_response_completed` → `call_hangup_started` → `call_hangup_completed` structured log sequence.
- Post-deploy smoke check for the `breakdown_tickets` → `assistance_requests` rename: apply the rename migration in the deployed environment, then place one real call that completes intake. Acceptance: exactly one row lands in `assistance_requests`, the `assistance_requests_pkey` and `assistance_requests_call_id_key` constraints exist, the `assistance_requests` RLS policy is attached and enforced, dispatcher SMS arrives with the "New assistance request" copy, and no query or write touches a `breakdown_tickets` table. Verification: live Twilio call plus SQL inspection of table name, constraints, row contents, and policy attachment in the deployed database.
- Post-deploy smoke check for single-dispatcher authentication: after pushing the dispatcher read-access migration to the hosted Supabase project, create the one dispatcher Auth user per the README setup, confirm public sign-ups are disabled in the hosted dashboard (local `config.toml` only affects local development), and verify the account can sign in and `SELECT` from `assistance_requests` using only the public Supabase URL plus the anon/publishable key. Acceptance: anon/unauthenticated queries return no rows, the dispatcher account authenticates and reads requests, and no service-role key is used client-side. Verification: hosted Supabase dashboard steps plus a PostgREST/curl check with the anon key (zero rows) and with the dispatcher's session token (rows returned).
- Migrate FastAPI startup validation from deprecated `@app.on_event("startup")` to `lifespan` context manager
- Add unit test for `Settings` validation that asserts `ValidationError` when env vars are missing
- Add unit tests for `AssistanceRequestArgs` Pydantic validation and `UPDATE_ASSISTANCE_REQUEST_TOOL` schema shape
- Add unit tests for `CallState` and `CallStateManager` (create/get/remove/isolation) and integration tests verifying tool handlers update state correctly
- Suppress `response.create` for spurious post-greeting input (transcription-based filtering): enable input audio transcription and gate `caller_turn_complete` responses on the committed turn's transcript so empty/filler-only commits (call-setup noise or greeting echo) do not trigger an assistant response. Acceptance: a commit with no speech does not create a response; a commit with real speech creates exactly one. Verification: unit tests feed `conversation.item.input_audio_transcription.completed` with empty vs real transcripts and assert `response.create` counts
- Live end-to-end regression check: place a real call and verify the agent speaks exactly one fixed greeting and then stays silent until the caller speaks (no immediate "OK, let's get some information..."). Acceptance: for N test calls, no unsolicited second response before caller speech. Verification: manual telephony test against prod/staging using the new `response_create_sent` + `input_audio_buffer.*` structured logs
- Add a handler-level test for `twilio_media_stream` that drives the `start` event and asserts `process_events` is started exactly once on the early-success, early-failure-fallback, and no-early-connection paths. Acceptance: no path starts two concurrent `process_events` readers and no task is orphaned when early setup fails after task creation. Verification: unit test with a mocked `RealtimeSession` counting `asyncio.create_task(session.process_events)` calls per path
- Open the greeting gate when no greeting response will ever complete: with `create_response: False`, `_greeting_response_done` only flips on a first `response.done`, so `greeting=""` (or a silently failed greeting `response.create`) leaves the session permanently deaf to caller turns. Acceptance: with no greeting configured the first caller commit creates a response; if the greeting response never arrives within a bounded time, later caller turns still get responses. Verification: unit tests for the empty-greeting first turn and a greeting-timeout/fallback path
- Pass OpenAI `session_id` to `create_ticket()` for troubleshooting correlation
- Claim the dispatcher-SMS notification atomically before sending, and add a retry path for failed sends: the current `notification_status == "pending"` guard is check-then-act, so a duplicate completing tool call arriving while the SMS task is still in flight (or after a swallowed `update_notification_status` failure) can double-send, and a `failed` status is permanently suppressed with no recovery. Acceptance: the completing handler claims the row with a conditional update (only a row still in its pre-send state wins the claim); exactly one claim winner fires `notify_dispatcher`; a losing concurrent duplicate still returns `status = "created"` without sending; a defined retry can re-attempt `failed` sends while a `sent` row is never re-sent. Verification: unit tests drive two concurrent completing handler calls against a mocked claim and assert exactly one SMS, plus claim-lost and failed-then-retry path tests; the existing completion-gating and retry tests still pass.
- Harden tests for partial-safe persistence: assert the completion-path SMS payload is read from the merged ticket row rather than the raw tool arguments, and cover the duplicate-insert race where the post-23505 re-select unexpectedly returns no row. Acceptance: a handler test with partial-but-completing args asserts `notify_dispatcher` receives the merged row's location/vehicle/issue; a persistence test forces an insert unique violation followed by an empty re-select and asserts the original API error propagates. Verification: `python -m pytest tests/test_tool_call_handling.py tests/test_ticket_persistence.py -v` passes with the new tests alongside the existing suites.
- Make duplicate voice webhooks for the same `CallSid` reuse or safely cancel-and-replace the pending early OpenAI connection: a second webhook currently overwrites `_pending_connections[call_sid]`, orphaning the first `connection_task` (and the realtime session/WebSocket it creates), which is never awaited, cancelled, or closed. Acceptance: a retried/duplicate voice webhook for a call with a pending or live early connection does not leak the superseded task or session; the media stream still consumes exactly one live connection. Verification: unit test issues two webhook posts for the same `CallSid` and asserts the first connection task is cancelled/closed (or reused) while exactly one entry remains in `_pending_connections`; existing early-connection and webhook suites still pass.
- Recover from a timed-out early assistance-request insert so no row is stuck open: `start_assistance_request` is bounded by the voice-webhook timeout, but the underlying insert thread can still commit `in_progress` after the terminal status callback (or teardown) has already run its no-op finalization, leaving an open row with no further Twilio events coming. Acceptance: a row whose insert commits after its call reached a terminal status still ends non-open — either the resolved insert re-checks a recorded terminal-status receipt for that `call_sid`, or the status-callback path retries briefly when the row does not exist yet. Verification: unit tests drive a stalled insert resolving after `abandon_if_open` ran and assert the row is finalized; the early-connection, webhook, and status-callback suites still pass.
- Lock the status no-overwrite guards against Supabase client regressions: `complete_intake` and `abandon_if_open` rely on `.update().eq().in_()` serialization, but today that is only asserted through mocked call chains, so a client-library change to filter encoding would go unnoticed. Acceptance: a test exercises the real postgrest query construction used by both finalizers (generated request path/filters asserted, or executed against an available Supabase/PostgREST instance) and proves `completed`/`escalated` rows are excluded while open rows match. Verification: `python -m pytest tests/ -v` passes with the new test; it skips cleanly when no live database is available, matching the migration-test convention.
- Dispatcher assistance-request dashboard
- Supabase Realtime assistance-request updates
- `/api/v1/assistance-requests` endpoint if a dedicated backend API becomes necessary
- Add unit tests for the dispatcher dashboard's pure display logic using a frontend test runner (e.g. Vitest): placeholder resolution (`Collecting…` for missing fields on `in_progress` requests, `Not collected` on terminal requests, real values for non-empty strings, null/undefined/empty/whitespace-only inputs handled) and the Active/Past partition (`in_progress` → Active; `completed`/`abandoned`/`escalated` → Past). Acceptance: a documented `npm test` script in `frontend/` runs the tests offline with no Supabase network dependency. Verification: `cd frontend && npm test` passes with coverage of the placeholder and partition cases alongside any extracted helpers.
- Execute and record a manual browser smoke checklist for the dispatcher dashboard once the dispatcher Auth account exists: unauthenticated visit shows only the auth screen; sign-in opens the dashboard; refresh restores the session; Active/Past sections, `Collecting…`/`Not collected` placeholders, and escalated styling render correctly; sign-out returns to the auth screen; layout works at desktop and tablet widths. Acceptance: each checklist item performed against a running build with real dispatcher credentials and recorded (notes or screenshots linked from the follow-up). Verification: checklist completed after hosted dispatcher-user setup (see the dispatcher-auth post-deploy smoke item above).
- Backfill the dispatcher dashboard when the realtime subscription reconnects: after a `CHANNEL_ERROR` / `TIMED_OUT` / `CLOSED` state later returns to `SUBSCRIBED`, re-run the initial `assistance_requests` query so INSERT/UPDATE events committed during the outage are recovered instead of silently missing while the indicator shows "Live". Acceptance: a simulated disconnect/reconnect cycle ends with dashboard state matching a fresh page load. Verification: frontend unit test drives the subscribe callback through error → `SUBSCRIBED` and asserts the load query runs again; manual smoke with a dropped network confirms missed rows appear.
- Maintain `updated_at` on `assistance_requests` writes (e.g. a `BEFORE UPDATE` trigger setting `updated_at = now()`, or explicit sets in backend update paths) so the stale-update guard in `frontend/src/lib/requestList.ts` `preferNewer` is meaningful rather than relying on commit-ordered delivery. Acceptance: an UPDATE write advances `updated_at`; realtime payloads carry the new value. Verification: migration test asserts the trigger fires on update and existing suites still pass.
- Rate limiting
- Advanced retry/recovery workflows
- Better notification delivery tracking
- Analytics for call completion and escalation rates
- Call/transcript audit tooling
- Authentication for dispatcher-facing interfaces
- Cost and usage monitoring per call

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

- [ ] Not started

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
