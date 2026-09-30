# System Architecture: Roadside Assistance Triage AI Voice Agent

## Overview

Towbie is an autonomous AI voice triage agent for roadside towing assistance.
A stranded caller dials a Twilio phone number; the backend answers with TwiML,
bridges the call's audio to the OpenAI Realtime API over a WebSocket, and the
model conducts a structured 3-field intake (location, vehicle, issue). Completed
intakes are persisted to Supabase Postgres and the on-duty dispatcher is alerted
by SMS. A React frontend serves two surfaces from one bundle: a public demo
(`/`) where visitors start a demo session and watch their call progress live,
and a dispatcher dashboard (`/admin`) with sign-in, Active/Past request lists,
and live updates via Supabase Realtime.

There is no Vapi dependency. An earlier Vapi-based webhook design
(`app/api/webhooks.py`, `VAPI_WEBHOOK_SECRET`, SQLAlchemy models) was removed;
voice orchestration is now Twilio Media Streams + OpenAI Realtime, persistence
goes through the Supabase client with the service-role key, and the only
database webhook trigger that ever existed was dropped in favor of direct
application notification (`supabase/migrations/20260918000000_drop_ticket_webhook_trigger.sql`).

## System Context

```
Caller (PSTN)
  │ voice
  ▼
Twilio (inbound number, Media Streams, call control, SMS)
  │ HTTPS webhooks / WSS audio / REST
  ▼
Backend — FastAPI on Cloud Run (`app/`)
  ├──► OpenAI Realtime API (WSS: audio + transcripts + function tools)
  ├──► Supabase Postgres (service-role: read/write, no RLS bypass issues)
  └──► Twilio REST (SMS alerts, transfer <Dial>, hangup)
Supabase Auth + Realtime ──► Browser frontend on Firebase Hosting (`frontend/`)
  ├── `/`      public demo (anonymous auth, own session only)
  └── `/admin` dispatcher dashboard (password account, all rows read-only)
```

## Call Data Flow

```
1. Twilio receives inbound call
     → POST /api/v1/twilio/voice (signature-checked)
2. Voice webhook (≤5s budgets per step):
     → open early OpenAI Realtime connection (hide setup latency)
     → create open assistance_requests row (idempotent per call)
     → match inbound From-number to a claimed demo session (HMAC lookup)
     → return TwiML <Connect><Stream> to WS /api/v1/twilio/media-stream
3. Media-stream bridge (G.711 μ-law audio end-to-end — OpenAI `pcmu`
   matches Twilio's native format, no transcoding):
     → deterministic opening greeting (single response.create, gated)
     → progressive intake via update_assistance_request tool calls
     → caller confirmation via confirm_assistance_request (clear-affirmative only)
       → fixed closing line → wait for play-out mark → hangup_call
     → emergency via transfer_to_emergency (silent tool call)
       → fixed transfer line → <Dial> redirect (or demo-mode hangup)
     → caller interruption cancels assistant speech + clears Twilio buffer
     → terminal Twilio status callback → abandon_if_open for open rows
4. Persistence + notification:
     → partial saves merged per call; completion guarded
       (`in_progress`/`abandoned` → `completed`, abandoned self-heals)
     → dispatcher SMS claimed atomically (pending/failed → sending) with retry,
       terminal state sent/failed
     → transcript turns persisted best-effort with (call_id, seq) dedup
5. Live UI fan-out via Supabase Realtime INSERT/UPDATE to demo + dashboard
```

## Backend (`app/`)

### Entrypoint (`app/main.py`)

- `FastAPI(title="Roadside Triage Agent")` with two routers under `/api/v1`
  (`twilio`, `demo_sessions`).
- CORS allows only `GET`/`POST` from comma-separated `FRONTEND_ORIGINS`
  (`Authorization`, `Content-Type` headers); empty setting denies all
  cross-origin browser requests.
- Startup validates `Settings` via `@app.on_event("startup")` and exits(1) when
  required variables are missing. This uses the deprecated `on_event` API; a
  migration to the `lifespan` context manager is tracked in `docs/TODO.md`.
- `GET /health` returns `{status: ok, response_time_ms}` plus opt-in downstream
  checks (each 5s timeout): `?check_db` (Supabase row read),
  `?check_twilio` (non-billable account fetch), `?check_openai` (models list).
  Any failure reports `degraded` (or `error` when configuration is invalid).
- Structured JSON logging via `structlog` (ISO timestamps, log levels).

### HTTP + WebSocket Routes

| Method + Path                    | File                  | Behavior                                                        |
| -------------------------------- | --------------------- | --------------------------------------------------------------- |
| `POST /api/v1/twilio/voice`      | `app/api/twilio.py`   | Signature check, early OpenAI connect, idempotent intake row, demo-session match+link, returns TwiML Media Stream |
| `POST /api/v1/twilio/status`     | `app/api/twilio.py`   | Signature check; terminal statuses (`completed/busy/failed/no-answer/canceled`) abandon open rows |
| `WS /api/v1/twilio/media-stream` | `app/api/twilio.py`   | Twilio↔OpenAI audio bridge (`connected/start/media/mark/stop`), early-connection reuse, teardown abandons open rows |
| `POST /api/v1/demo-sessions`     | `app/api/demo_sessions.py` | Creates a demo session → `201 {id, phone_last4, expires_at, demo_phone}` |
| `GET /health`                    | `app/main.py`         | Liveness + optional dependency checks (see above)               |

There is no backend dispatcher-read proxy (the dashboard reads Supabase
directly), no ticket-insert webhook endpoint, and no feedback endpoint (the
demo feedback link is a frontend-only external URL).

### Realtime Voice Session (`app/realtime/`)

- `session.py` — per-call `RealtimeSession` over
  `wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}` with
  `audio/pcmu` in/out, `marin` voice, and server VAD
  (`create_response: false`, `interrupt_response: false` so the app drives
  responses explicitly).
- `instructions.py` + `system_prompt.md` — safety-first instructions: silent
  emergency transfer, progressive 3-field intake, summarize →
  `confirm_assistance_request` on clear affirmation only, fixed closing and
  transfer lines. The opening greeting (`"This is roadside assistance. How can
  I help?"`) is played via a single gated `response.create`; a 10s fallback
  covers delivery failures.
- `tools.py` — the model's three function tools:
  - `update_assistance_request` (partial saves; all fields optional but at
    least one non-empty required; all three saved → `ready_for_confirmation`,
    still requiring verbal confirmation),
  - `confirm_assistance_request` (no arguments; backend marks the saved request
    complete only after the caller clearly confirms the full summary),
  - `transfer_to_emergency` (`reason` required; called silently, system speaks
    the fixed transfer message; benign word mentions alone must not trigger it).
- Caller interruption (`speech_started`) cancels the in-flight model response
  and clears Twilio's audio buffer; cancelled-response deltas are dropped.
- Transcripts come from model events (caller `input_audio_transcription`,
  assistant `output_audio_transcript`); interrupted partials are dropped and
  empty commits are filtered with a fail-open timeout.
- Closing: after confirmation the model speaks the fixed closing line; the call
  hangs up only after Twilio acknowledges play-out (`mark` events with timeout
  fallbacks). Emergency transfer uses speak-then-redirect: the fixed transfer
  message plays fully before the `<Dial>` redirect (demo mode hangs up instead
  of dialing; see configuration).
- `latency.py` — monotonic-clock `CallLatencyTracker` logging setup milestones
  and tool durations on session close.

### Services (`app/services/`)

- `tickets.py` — assistance-request lifecycle: idempotent open-row creation per
  call, non-empty-field merges (unique-violation race handling), guarded
  completion (`in_progress`/`abandoned` → `completed`), guarded abandonment
  (`in_progress` → `abandoned`), atomic notification claiming
  (`pending`/`failed` → `sending`), guarded demo-session linking, hazard flag
  updates.
- `notifier.py` — dispatcher SMS (`New assistance request / Call / From /
  Location / Vehicle / Issue`) with 3 attempts, re-claim between retries, and
  terminal `sent`/`failed` states.
- `demo_sessions.py` — E.164 phone normalization, keyed HMAC phone matching
  (`DEMO_PHONE_HMAC_SECRET`, never plaintext) plus last-4 for display, creation
  TTL (default 900s), guarded claim (`claimed_at IS NULL`, unexpired) that
  extends the deadline to `greatest(expires_at, now + claimed_TTL)`, newest-first
  HMAC candidate lookup for inbound-call matching.
- `transcripts.py` — best-effort transcript inserts with `(call_id, seq)`
  dedup treated as no-op.
- `calls.py` — single-loop `CallStateManager` (`twilio_call_id`, caller phone,
  stream SID, transfer state) plus early-connection tasks shared with the voice
  webhook to hide Realtime setup latency and to reuse/cancel superseded
  connections on duplicate webhooks.
- `emergency.py` / `hangup.py` — Twilio call control (`<Dial>` redirect and
  `status=completed` hangup).

### Configuration (`app/core/config.py`, `.env.example`)

Required (no defaults): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL`, `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DISPATCHER_ALERT_PHONE`,
`EMERGENCY_TRANSFER_PHONE`, `DEMO_PHONE_HMAC_SECRET` (min length 1).

Optional: `EMERGENCY_HANGUP_INSTEAD_OF_TRANSFER` (default `true` — demo mode
hangs up after the emergency message instead of dialing the transfer number),
`DEMO_SESSION_TTL_SECONDS` (default 900), `DEMO_CLAIMED_SESSION_TTL_SECONDS`
(default 1800, positive), `FRONTEND_ORIGINS` (default `""` = deny all browser
cross-origin traffic).

## Data Layer (Supabase Postgres)

The backend uses the Supabase Python client with the service-role key (bypasses
RLS). Browsers use the anon key and are read-only: no INSERT/UPDATE/DELETE
policies exist for `authenticated` on any table.

### Tables

**`assistance_requests`** (renamed from `breakdown_tickets`;
`supabase/migrations/20260923000000_rename_breakdown_tickets_to_assistance_requests.sql`):
`id` (uuid), `call_id` (text, unique — webhook-retry dedup), `caller_phone`,
nullable `location`/`vehicle`/`issue` (partial intake), `status` (mirrored
workflow state), `intake_status` (`in_progress`/`completed`/`abandoned`/
`escalated`), `hazard_detected`/`hazard_reason`, `notification_status`
(`pending`/`sending`/`sent`/`failed`), `demo_session_id` (nullable FK, null on
delete), timestamps. Intake nullability and the status lifecycle arrived via
`20260922000000_allow_nullable_intake_add_intake_status.sql` with one-time
backfills (`20260922150000`, `20260923120000`).

**`demo_sessions`** (`20260923180000_create_demo_sessions.sql`): `id` (uuid),
`auth_user_id` (uuid, no FK — plain-Postgres migration-test compatibility),
`phone_hmac` (text, indexed), `phone_last4` (`^[0-9]{4}$`), `expires_at`,
paired `claimed_at`/`call_id` (check constraint: both null or both set),
`created_at`.

**`call_transcripts`** (`20260926000000_create_call_transcripts.sql`): `id`
(uuid), `demo_session_id` (nullable FK, null on delete — non-demo calls have
none), `call_id`, `speaker` (`caller`/`assistant`), non-empty `text`, `seq`
(`>= 0`, unique per `(call_id, seq)` for ordering/dedup). Retention is
indefinite (no cleanup job).

### Row-Level Security + Realtime

- Base convention is deny-all (`using (false)`), with a permissive dispatcher
  read policy (`using (true)` for `authenticated`) and RESTRICTIVE demo-scoping
  policies that AND-limit demo users to rows linked to their own unexpired
  session (`auth.uid()`, `expires_at > now()`).
- `assistance_requests` and `call_transcripts` are both in the
  `supabase_realtime` publication (guarded, idempotent adds), so the frontend
  subscribes to live INSERT/UPDATE (requests) and INSERT (transcripts).

### Demo Session Lifecycle (two-phase expiry)

Creation TTL bounds *starting* the demo; a successful claim extends the
deadline to `greatest(expires_at, now + claimed_TTL)` so browser access follows
the claimed call. The frontend re-reads the row on deadline: a returned row
means extended, an RLS-null means expired. Phone numbers are stored only as
HMAC + last-4, and an inbound call claims at most one session through the
guarded update.

## Frontend (`frontend/`)

Vite + React 19 + TypeScript (`tsc -b && vite build`, `oxlint`), no router.
`App.tsx` routes by pathname: `/admin` requires a Supabase session (else
`AuthScreen` password sign-in) and renders `Dashboard`; everything else renders
`DemoScreen`. Titles switch between `Towbie Demo` and `Towbie · Dispatcher
Dashboard`.

### Library Helpers (`frontend/src/lib/`)

- `supabaseClient.ts` — throws when `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY`
  are missing; never touches the service-role key.
- `demoSession.ts` — reuses anonymous auth, `POST {VITE_API_BASE_URL}/api/v1/
  demo-sessions` to start.
- `demoStorage.ts` — sessionStorage `roadside-demo-session`
  (`{id, phone_last4, expires_at, demo_phone}`); storage alone never authorizes.
- `requestList.ts` — `upsertRequest` dedup-by-`id`, `preferNewer` `updated_at`
  guard, `mergeLoadedRequests` for load-vs-realtime races.
- `transcripts.ts` — normalizes rows (caller→You, assistant→AI Agent; drops
  empty/unknown), ordered upsert/merge by `seq`.
- `callStatus.ts` — maps intake status to demo call phase (null→waiting,
  `in_progress`→active, terminal→ended).
- `requestFields.ts` / `realtimeStatus.ts` / `waveform.ts` — display
  placeholders (`Collecting…` vs `Not collected`), connection indicator state,
  and the speaking waveform bars.
- `feedback.ts` — post-call feedback URL from `VITE_FEEDBACK_URL` with a
  same-page placeholder fallback.

### Components (`frontend/src/components/`)

- `DemoScreen.tsx` — `start`/`active`/`expired` phases, session restore with
  expiry recheck, per-session `demo-request-{id}` (INSERT/UPDATE) and
  `demo-transcript-{id}` (INSERT) subscriptions, countdown timer.
- `DemoStartForm.tsx` / `DemoActiveView.tsx` / `DemoLivePanel.tsx` — phone
  entry + hero, call card (masked `••• ••• {last4}`, copy button, countdown,
  feedback CTA when ended), and the live Location/Vehicle/Issue cards with a
  4-step stepper plus auto-scrolling transcript.
- `Dashboard.tsx` — latest 100 `assistance_requests`, single
  `assistance-requests` channel (INSERT/UPDATE merged), Active (`in_progress`)
  vs Past (terminal) split, sign-out; `RequestCard.tsx` renders rows.
- `AuthScreen.tsx`, `LandingHeader.tsx`, `DemoFooter.tsx`, `TowbieLogo.tsx`,
  `icons.tsx`, `DemoFeedbackCta.tsx` — supporting UI.

### Realtime + Env

Subscribe state drives the connection indicator (`SUBSCRIBED` → live, else
disconnected). Required env: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`,
`VITE_API_BASE_URL`; optional: `VITE_FEEDBACK_URL` (see `frontend/.env.example`
and `frontend/src/vite-env.d.ts`). The bundle carries no product analytics
today. Hosting is Firebase (`firebase.json` serves `frontend/dist` with an SPA
rewrite; immutable caching for `/assets/**`, no-cache for `/`, `/admin`, HTML).

## CI / CD (`.github/workflows/`, `Dockerfile`)

- `ci.yml` (push + PR to `main`): parallel `test` (`pip install`
  `requirements.txt` + `requirements-dev.txt`, `pytest`), `lint`
  (`ruff check .`), `typecheck` (`mypy .`).
- `deploy-production.yml` (push to `main` on any path, or manual dispatch):
  the `migrate` job runs conditionally (only when `supabase/migrations/**`
  changed, via `dorny/paths-filter`, or on manual dispatch) with Supabase CLI
  (`2.117.0`) `db push` → `pytest` → Cloud Run deploy (Workload Identity
  Federation; validates `FRONTEND_ORIGINS`; builds/pushes to Artifact Registry;
  deploys with Secret Manager secrets, `--timeout 300 --concurrency 8
  --max 10 --min 0`; health-checks `/health` repeatedly; prints the Twilio
  voice and media-stream URLs). Migrations always run before code deploy.
- `deploy-frontend.yml` (push to `main` touching `frontend/**`,
  `firebase.json`, or the workflow; or dispatch): verifies required repo
  variables (`FIREBASE_PROJECT_ID`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`,
  `VITE_API_BASE_URL`) + `FIREBASE_SERVICE_ACCOUNT` secret, validates the API
  URL and rejects service-role/`sb_secret_` anon keys → `npm ci` → `npm run
  lint` → `npm run build` with inlined `VITE_*` → live-channel Firebase
  Hosting deploy → post-deploy HTML verification of `/` and `/admin`.
- `Dockerfile` (`python:3.13-slim`): installs `requirements.txt`, copies only
  `app/`, serves `uvicorn app.main:app` on `${PORT:-8080}`. `.dockerignore`
  keeps `app/**/*.md` (the system prompt ships in the image) while excluding
  tests, supabase files, and local env.
- `supabase/config.toml`: project `roadside-triage-agent`, Postgres 17,
  Realtime enabled, signups disabled, anonymous sign-ins enabled (demo flow).
  Supabase Storage is configured by default but unused — no buckets, no
  `storage.*` calls. Local voice development tunnels via `dev.sh` through a
  fixed ngrok domain.

## External Integrations

- **OpenAI Realtime API** — voice, caller transcription, and the three function
  tools over one per-call WebSocket; `/health?check_openai` probes the models
  endpoint.
- **Twilio** — inbound PSTN voice (signature-validated webhooks with
  forwarded-proto/host reconstruction behind proxies), Media Streams (mulaw
  audio bridge), call control (transfer `<Dial>`, status callbacks, hangup),
  and dispatcher SMS alerts (atomic claim + retry).
- **Supabase** — Postgres (3 tables), Auth (anonymous demo sessions +
  dispatcher password account, signups disabled), Realtime (2 publications).
- **Firebase Hosting** — static hosting for the demo + dashboard.
- **Feedback form** — external URL only; no backend endpoint or table.

## Known Limitations

1. **No geolocation verification** — location comes entirely from the caller's
   verbal description parsed by the model; nothing checks it against GPS.
2. **Cold starts** — Cloud Run scales to zero, so an idle instance can add
   latency to the first webhook response (mitigated by 5s per-step budgets and
   the early Realtime connection, not eliminated).
3. **Demo-mode emergency default** — `EMERGENCY_HANGUP_INSTEAD_OF_TRANSFER`
   defaults to `true`; production transfer dials `EMERGENCY_TRANSFER_PHONE`.
4. **Product non-goals (per `docs/PRODUCT.md`)** — no payments, no truck
   dispatch/ETA routing, no outbound callbacks, English only.
5. **Reconnect hardening is in progress** — dashboard/demo recovery after a
   Realtime outage and intake-event coverage across restore paths are tracked
   follow-ups in `docs/TODO.md`, not current behavior.
