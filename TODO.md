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

The backend owns Twilio call/webhook handling, the realtime audio WebSocket bridge, conversation/session state, ticket persistence, emergency transfer control, and dispatcher notification integration.

OpenAI Realtime owns the live conversational audio/model loop.

Supabase owns persistence.

Twilio owns PSTN calling and dispatcher SMS.

---

## Priority Legend

- `[ ] P0` = required for MVP
- `[ ] P1` = important, not blocking MVP
- `[ ] P2` = post-MVP

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

---

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

---

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

---

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

---

## P0 — Health Check

- Keep `/health`
- Verify application process is running
- Optionally verify Supabase connectivity separately
- Optionally verify OpenAI/Twilio configuration without making billable API calls

### Acceptance Criteria

- Health endpoint responds quickly
- Deployment platform can use it for health monitoring

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

---

## P1 — Add Voice Configuration Documentation

Document:

- Realtime system prompt
- Realtime model configuration
- Voice/audio settings
- Tool schema for `create_breakdown_ticket`
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
- Normal calls invoke `create_breakdown_ticket`
- Exactly one ticket is created in Supabase
- Duplicate ticket creation is idempotent
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

- Migrate FastAPI startup validation from deprecated `@app.on_event("startup")` to `lifespan` context manager
- Add unit test for `Settings` validation that asserts `ValidationError` when env vars are missing
- Handle `IntegrityError` in `create_ticket()` for concurrent duplicate `call_id` inserts (atomic idempotent insert)
- Add unit tests for `TicketArgs` Pydantic validation and `CREATE_BREAKDOWN_TICKET_TOOL` schema shape
- Pass OpenAI `session_id` to `create_ticket()` for troubleshooting correlation
- Dispatcher ticket dashboard
- Supabase Realtime ticket updates
- `/api/v1/tickets` endpoint if a dedicated backend API becomes necessary
- Rate limiting
- Advanced retry/recovery workflows
- Better notification delivery tracking
- Analytics for call completion and escalation rates
- Call/transcript audit tooling
- Authentication for dispatcher-facing interfaces
- Cost and usage monitoring per call

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
