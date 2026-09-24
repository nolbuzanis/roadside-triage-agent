# Roadside Assistance Triage AI Voice Agent (MVP)

An autonomous AI voice agent system built for towing companies to handle inbound non-emergency roadside assistance calls. Powered by OpenAI Realtime and a FastAPI backend, this agent seamlessly gathers critical breakdown details from stranded drivers, persists structured assistance requests into a Supabase database, and instantly notifies human dispatchers via SMS.

## Features

- **Instant Intake**: Zero hold times for stranded callers, ensuring immediate and empathetic response.
- **Voice-to-Database Pipeline**: Converts natural spoken conversations into structured, validated Supabase records using OpenAI Realtime tool-calling.
- **Emergency Escalation**: Automatically detects hazard situations (fire, injury, trapped occupants, etc.) and transfers the call to a live human.
- **Dispatcher Alerts**: Sends instant, structured SMS notifications to dispatchers via Twilio the second an assistance request is logged.
- **Structured Logging**: JSON-structured logs with call/session correlation IDs for traceability across services.

---

## Prerequisites

Before you begin, ensure you have the following installed and configured:

- **Python 3.13+**
- **Supabase Account** (for database and persistence)
- **Supabase CLI** (for local development and migrations)
- **OpenAI Account** (for Realtime API access)
- **Twilio Account** (for PSTN inbound calling and outbound SMS alerts)
- **ngrok** (for local development — exposes local server to Twilio)

---

## Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/roadside-triage-agent.git
cd roadside-triage-agent
```

### 2. Create Virtual Environment & Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### 3. Configure Environment Variables

Copy the example environment file and fill in your actual API keys and credentials.

```bash
cp .env.example .env
```

#### Environment Variables

| Variable                     | Required | Description                                                                                     |
| ---------------------------- | -------- | ----------------------------------------------------------------------------------------------- |
| `SUPABASE_URL`               | Yes      | Supabase project URL (e.g., `https://xyz.supabase.co`)                                         |
| `SUPABASE_SERVICE_ROLE_KEY`  | Yes      | Supabase service-role key for server-side database access                                       |
| `OPENAI_API_KEY`             | Yes      | OpenAI API key for Realtime API                                                                 |
| `OPENAI_REALTIME_MODEL`      | Yes      | Realtime model name (e.g., `gpt-4o-realtime-preview-2024-10-01`)                               |
| `TWILIO_ACCOUNT_SID`         | Yes      | Twilio Account SID                                                                              |
| `TWILIO_AUTH_TOKEN`          | Yes      | Twilio Auth Token (used for request signature validation and SMS)                               |
| `TWILIO_PHONE_NUMBER`        | Yes      | Your Twilio-provisioned phone number (e.g., `+16045550199`)                                     |
| `DISPATCHER_ALERT_PHONE`     | Yes      | Cell phone number of the human dispatcher receiving SMS alerts (e.g., `+16045550100`)           |
| `EMERGENCY_TRANSFER_PHONE`   | Yes      | Emergency transfer destination (911 or local emergency number)                                  |
| `DEMO_PHONE_HMAC_SECRET`     | Yes      | Server-side keyed HMAC secret for demo phone matching (generate with `openssl rand -hex 32`)     |
| `DEMO_SESSION_TTL_SECONDS`   | No       | Demo session lifetime in seconds (default `900` = 15 minutes)                                   |

### 4. Set Up Supabase

#### Create Supabase Project

1. Go to [supabase.com](https://supabase.com) and create a new project
2. Note your **Project URL** and **Service Role Key** (Settings → API)
3. Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in your `.env`

#### Run Database Migrations

The project includes Supabase CLI migrations. With the Supabase CLI installed:

```bash
# Link to your remote project (first time only)
supabase link --project-ref <your-project-ref>

# Push migrations to create the assistance_requests table, RLS policies, and webhooks
supabase db push
```

This applies the migrations in `supabase/migrations/`:
- `assistance_requests` table with all required columns
- Row Level Security policies
- Assistance-request insert webhook for dispatcher notifications
- `assistance_requests` membership in the `supabase_realtime` publication (Supabase Realtime events for the dashboard)
- `demo_sessions` table for short-lived public demo sessions (keyed phone HMAC, never plaintext) plus the optional `assistance_requests.demo_session_id` link

Alternatively, you can run the SQL directly in the Supabase SQL Editor (Dashboard → SQL Editor).

#### Create the Dispatcher Account (One-Time)

The dispatcher dashboard authenticates with Supabase Auth using a single dispatcher account. Row Level Security grants that authenticated session read-only access to `assistance_requests` (via the `Dispatcher can read assistance requests` policy applied by the migrations); anonymous/public clients stay denied, and the backend keeps writing with the service-role key, which bypasses RLS.

1. In the Supabase Dashboard, open **Authentication → Users** and click **Add user**. Create exactly one confirmed dispatcher account (email + password). This is the only account the MVP dashboard uses.
2. Disable public sign-ups so no additional accounts can be created: under **Authentication → Sign In / Providers → Email**, turn off **Enable sign ups**. Local development mirrors this in `supabase/config.toml` (`enable_signup = false`).
3. From **Settings → API**, note the **Project URL** and the **anon/publishable key**. These two values are the only Supabase credentials a browser dashboard may contain.
4. Never place `SUPABASE_SERVICE_ROLE_KEY` (or any other backend secret) in frontend code or frontend environment variables. The service-role key stays server-side only: `.env` locally, Secret Manager in production.

### 5. Configure Twilio Phone Number

1. Purchase a phone number in the [Twilio Console](https://console.twilio.com/)
2. Under the phone number's **Voice Configuration**, set the **A call comes in** webhook to:

```
POST https://<your-domain>/api/v1/twilio/voice
```

3. Set the **Call status changes** webhook (status callback) to:

```
POST https://<your-domain>/api/v1/twilio/status
```

The status callback finalizes the assistance request when a call ends before the Media Stream connects (caller hangs up, busy, failed, no-answer, canceled). It uses the same Twilio signature validation as the voice webhook, so only still-open rows are marked abandoned — completed and escalated rows are never overwritten.

For local development, use ngrok (see [Running Locally](#running-locally) below).

**Important**: The voice webhook returns TwiML that initiates a bidirectional Media Stream to `/api/v1/twilio/media-stream`. Twilio must be able to reach both the voice webhook URL and the WebSocket endpoint.

---

## Running Locally

### Quick Start (with ngrok)

The `dev.sh` script starts both the backend and an ngrok tunnel:

```bash
chmod +x dev.sh
./dev.sh
```

This will:
1. Start the FastAPI server on `http://localhost:8000`
2. Start an ngrok tunnel
3. Print the Twilio webhook URL to configure

### Manual Start

**Terminal 1 — Start the backend:**

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — Start ngrok:**

```bash
ngrok http 8000
```

Then set your Twilio voice webhook to the ngrok HTTPS URL:

```
POST https://<your-ngrok-id>.ngrok.io/api/v1/twilio/voice
```

### Verifying the Setup

1. **Health check**: `GET /health` returns `{"status": "ok"}`
2. **Local WebSocket test**: Connect to `ws://localhost:8000/api/v1/twilio/media-stream` to verify the WebSocket endpoint accepts connections
3. **End-to-end test**: Call your Twilio phone number — the call should connect and the voice assistant should begin speaking

### Run the Dispatcher Dashboard

The read-only dispatcher UI lives in `frontend/` (Vite + React + TypeScript). It authenticates with the single dispatcher Supabase Auth account and reads `assistance_requests` using only the public Supabase URL and anon/publishable key, with live INSERT/UPDATE updates via Supabase Realtime (the `assistance_requests` table is added to the `supabase_realtime` publication by a migration).

```bash
cd frontend
npm install
cp .env.example .env.local
# Fill VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY in .env.local
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) and sign in with the dispatcher account created in [Create the Dispatcher Account (One-Time)](#create-the-dispatcher-account-one-time). Never put `SUPABASE_SERVICE_ROLE_KEY` in the frontend environment. Production deployments of the dashboard go to Firebase Hosting — see [Production Deployment (Firebase Hosting)](#production-deployment-firebase-hosting).

---

## Project Structure

```text
app/
  __init__.py
  main.py                          # FastAPI app, logging config, health endpoint
  api/
    __init__.py
    twilio.py                      # Twilio voice webhook, Media Stream WebSocket, tool handlers
    webhooks.py                    # Supabase INSERT webhook, dispatcher notification dispatch
  services/
    __init__.py
    tickets.py                     # Assistance-request CRUD in Supabase (create, update hazard, update notification)
    notifier.py                    # Dispatcher SMS via Twilio
    calls.py                       # Per-call session state (CallState, CallStateManager)
    emergency.py                   # Emergency call transfer via Twilio call control
    demo_sessions.py               # Short-lived demo sessions: phone HMAC matching, at-most-once claims
  realtime/
    __init__.py
    session.py                     # OpenAI Realtime WebSocket session manager
    tools.py                       # Tool schemas (update_assistance_request, transfer_to_emergency)
    instructions.py                # System prompt and opening greeting
    latency.py                     # Structured latency instrumentation
  core/
    __init__.py
    config.py                      # Pydantic Settings (env var validation)
tests/
  test_app.py                      # App import smoke test
  test_twilio_voice_webhook.py     # TwiML response, signature validation
  test_realtime_session.py         # Session setup, audio forwarding, tool calls, errors
  test_ticket_persistence.py       # Assistance-request CRUD, idempotency, hazard updates
  test_emergency_transfer.py       # Transfer tool, Twilio call control, escalation recording
  test_webhooks.py                 # Supabase webhook endpoint
  test_tool_call_handling.py       # Pydantic models, tool handler logic
  test_notifier.py                 # Dispatcher SMS sending
  test_instructions.py             # Instruction content validation
  test_latency.py                  # Latency tracking and metrics
  test_early_connection.py         # Early OpenAI connection lifecycle
  test_structured_logging.py       # Structured logging configuration and output
  test_demo_sessions.py            # Demo-session phone HMAC, TTL, claim guards
supabase/
  config.toml                      # Supabase CLI configuration
  migrations/                      # SQL migrations (table, RLS, webhooks)
frontend/
  index.html                       # Dispatcher dashboard entry point
  src/
    App.tsx                        # Session gate (auth screen vs dashboard)
    components/                    # AuthScreen, Dashboard, RequestCard
    lib/supabaseClient.ts          # Public Supabase client (anon key only)
```

---

## Architecture

```
Inbound PSTN Call
        |
        v
     Twilio
        |
   Media Streams (WebSocket)
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
   |                       |
   v                       v
Supabase              Twilio Call Transfer
   |
   v
Supabase INSERT Webhook
   |
   v
Dispatcher SMS (Twilio)
```

### Key Components

| Component | Role |
|-----------|------|
| **Twilio** | PSTN inbound calling, Media Stream audio, dispatcher SMS, emergency call transfer |
| **FastAPI** | Voice webhook handler, WebSocket bridge, tool dispatch, session state |
| **OpenAI Realtime** | Live conversational audio/model loop, tool invocation |
| **Supabase** | Assistance-request persistence, INSERT webhook for notification dispatch |
| **structlog** | Structured JSON logging with call/session correlation |

---

## Testing

### Run All Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

### Run Specific Test Files

```bash
python -m pytest tests/test_twilio_voice_webhook.py -v
python -m pytest tests/test_realtime_session.py -v
python -m pytest tests/test_emergency_transfer.py -v
```

### Lint & Type Check

```bash
ruff check .
mypy .
```

### CI

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs tests, linting, and type checking on every push and PR to `main`.

---

## End-to-End Phone Testing

Once your local setup is running and Twilio is configured:

### Normal Call Flow

1. Call your Twilio phone number
2. The AI assistant greets you and asks for your location
3. Provide location, vehicle details, and issue
4. The assistant saves the assistance request and confirms
5. Check Supabase for the new `assistance_requests` row
6. Check your dispatcher phone for the SMS alert

### Emergency Call Flow

1. Call your Twilio phone number
2. Say something like "My car is on fire" or "I'm bleeding"
3. The assistant detects the emergency and transfers the call
4. Verify the call transfers to `EMERGENCY_TRANSFER_PHONE`
5. If a request existed, verify it's marked as `escalated` in Supabase

---

## Production Deployment (Google Cloud Run)

This section covers deploying the application to production on Google Cloud Run with GitHub Actions for continuous deployment.

### Prerequisites for Production

- **Google Cloud Platform account** with billing enabled
- **GitHub repository** with admin access
- **Terraform or gcloud CLI** for initial GCP resource setup

### Architecture

```
GitHub main
    ↓
GitHub Actions
    ↓
Google Cloud authentication via Workload Identity Federation
    ↓
Container build
    ↓
Artifact Registry
    ↓
Cloud Run
    ↓
FastAPI
    ├── /api/v1/twilio/voice
    └── /api/v1/twilio/media-stream
            ↓
       OpenAI Realtime

Cloud Run
    ↓
Secret Manager
    ↓
OpenAI / Twilio / Supabase credentials
```

### 1. GCP Project Setup

#### Enable Required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  cloudscheduler.googleapis.com
```

#### Create Artifact Registry Repository

```bash
gcloud artifacts repositories create roadside-agent \
  --repository-format=docker \
  --location=us-central1 \
  --description="Roadside Triage Agent container images"
```

### 2. Secret Manager Setup

Create secrets for all required environment variables:

```bash
SECRETS=(
  "SUPABASE_URL"
  "SUPABASE_SERVICE_ROLE_KEY"
  "OPENAI_API_KEY"
  "OPENAI_REALTIME_MODEL"
  "TWILIO_ACCOUNT_SID"
  "TWILIO_AUTH_TOKEN"
  "TWILIO_PHONE_NUMBER"
  "DISPATCHER_ALERT_PHONE"
  "EMERGENCY_TRANSFER_PHONE"
)

for SECRET in "${SECRETS[@]}"; do
  gcloud secrets create $SECRET --replication-policy="automatic"
done
```

#### Add Secret Values

```bash
# Example for SUPABASE_URL
echo -n "https://your-project.supabase.co" | \
  gcloud secrets versions add SUPABASE_URL --data-file=-

# Repeat for all secrets
```

### 3. Service Accounts

#### Runtime Service Account (Cloud Run)

```bash
gcloud iam service-accounts create roadside-agent-runtime \
  --display-name="Roadside Agent Runtime Service Account"
```

Grant Secret Manager access:

```bash
SECRETS=(
  "SUPABASE_URL"
  "SUPABASE_SERVICE_ROLE_KEY"
  "OPENAI_API_KEY"
  "OPENAI_REALTIME_MODEL"
  "TWILIO_ACCOUNT_SID"
  "TWILIO_AUTH_TOKEN"
  "TWILIO_PHONE_NUMBER"
  "DISPATCHER_ALERT_PHONE"
  "EMERGENCY_TRANSFER_PHONE"
)

PROJECT_ID=$(gcloud config get-value project)

for SECRET in "${SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --member="serviceAccount:roadside-agent-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

#### Deployment Service Account (GitHub Actions)

```bash
gcloud iam service-accounts create github-cloud-run-deployer \
  --display-name="GitHub Actions Cloud Run Deployer"
```

Grant required roles:

```bash
PROJECT_ID=$(gcloud config get-value project)
DEPLOYER_SA="github-cloud-run-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# Cloud Run Admin
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$DEPLOYER_SA" \
  --role="roles/run.admin"

# Service Account User (to use the runtime SA)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$DEPLOYER_SA" \
  --role="roles/iam.serviceAccountUser"

# Artifact Registry Writer
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$DEPLOYER_SA" \
  --role="roles/artifactregistry.writer"
```

### 4. Workload Identity Federation

#### Create Workload Identity Pool

```bash
gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Actions Pool"
```

#### Create Workload Identity Provider

```bash
gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --display-name="GitHub Actions Provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref" \
  --issuer-uri="https://token.actions.githubusercontent.com"
```

#### Configure Provider Attributes

For the repository to authenticate, configure the provider to accept claims from your specific repository:

```bash
gcloud iam workload-identity-pools providers describe "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --format="value(name)"
```

#### Grant GitHub SA Access

```bash
PROJECT_ID=$(gcloud config get-value project)
DEPLOYER_SA="github-cloud-run-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
POOL_NUMBER=$(gcloud iam workload-identity-pools describe "github-pool" --location="global" --format="value(name)")

gcloud iam service-accounts add-iam-policy-binding $DEPLOYER_SA \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL_NUMBER}/attribute.repository/YOUR_ORG/YOUR_REPO"
```

### 5. GitHub Repository Configuration

#### Required Repository Variables

Set these in **Settings → Secrets and variables → Actions → Variables**:

| Variable | Description | Example |
|----------|-------------|---------|
| `GCP_PROJECT_ID` | Google Cloud project ID | `my-project-id` |
| `GCP_REGION` | Cloud Run region | `us-central1` |
| `RUNTIME_SA` | Runtime service account email | `roadside-agent-runtime@my-project.iam.gserviceaccount.com` |

#### Required Repository Secrets

Set these in **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Description |
|--------|-------------|
| `WIF_PROVIDER` | Workload Identity Federation provider resource name |
| `WIF_SERVICE_ACCOUNT` | Deployment service account email |

Example values:
```
WIF_PROVIDER: projects/123456789/locations/global/workloadIdentityPools/github-pool/providers/github-provider
WIF_SERVICE_ACCOUNT: github-cloud-run-deployer@my-project.iam.gserviceaccount.com
```

### 6. Deployment Workflow

The GitHub Actions workflow (`.github/workflows/deploy-production.yml`) automatically:

1. Runs tests on every push to `main`
2. Builds the Docker container
3. Pushes to Artifact Registry
4. Deploys to Cloud Run
5. Verifies the deployment with a health check
6. Outputs the production URL

#### Triggering a Deployment

```bash
# Deploy by merging a PR to main
git checkout main
git merge feature/your-feature
git push origin main

# Or deploy directly (if on main)
git push origin main
```

### 7. Cloud Run Configuration

The service is deployed with:

| Setting | Value |
|---------|-------|
| Platform | Managed |
| Region | Configurable (default: us-central1) |
| Authentication | Required (no unauthenticated access) |
| Concurrency | 8 requests per instance |
| Max instances | 10 |
| Min instances | 0 (scales to zero) |
| Request timeout | 300 seconds (5 minutes) |

### 8. Production URLs

After deployment, your service will be available at:

```
https://roadside-agent-<hash>-<region>.a.run.app
```

#### Configure Twilio Webhooks

In the Twilio Console, configure your phone number:

1. **Voice webhook** (POST):
   ```
   https://roadside-agent-<hash>-<region>.a.run.app/api/v1/twilio/voice
   ```

2. **Call status changes** (status callback, POST):
   ```
   https://roadside-agent-<hash>-<region>.a.run.app/api/v1/twilio/status
   ```
   Finalizes still-open assistance requests on terminal call statuses
   (`completed`, `busy`, `failed`, `no-answer`, `canceled`) for calls that end
   before the Media Stream connects.

3. **Media Stream** (WebSocket - configured in TwiML):
   ```
   wss://roadside-agent-<hash>-<region>.a.run.app/api/v1/twilio/media-stream
   ```

### 9. Health Check

Verify the deployment is healthy:

```bash
curl https://roadside-agent-<hash>-<region>.a.run.app/health
# Expected: {"status":"ok"}
```

### 10. Manual Redeployment

To manually redeploy without a code change:

```bash
# Using gcloud CLI
gcloud run deploy roadside-agent \
  --image us-central1-docker.pkg.dev/PROJECT_ID/roadside-agent/roadside-agent:COMMIT_SHA \
  --region us-central1 \
  --platform managed

# Or re-run the latest image
gcloud run deploy roadside-agent \
  --image us-central1-docker.pkg.dev/PROJECT_ID/roadside-agent/roadside-agent:latest \
  --region us-central1 \
  --platform managed
```

### 11. Important Limitations

#### In-Memory Call State

The application currently maintains per-call state in memory (`CallStateManager`). This means:

- Each Cloud Run instance handles calls independently
- Call state is not shared between instances
- If an instance is restarted, active calls on that instance will lose state
- Multiple concurrent calls will be distributed across instances

For the initial production deployment, this is acceptable. If you need multi-instance call state sharing, consider adding Redis or another external state store.

#### WebSocket Connections

Cloud Run supports WebSockets, but:

- WebSocket connections are subject to the request timeout (configured to 300 seconds)
- Long-running calls may be disconnected if they exceed the timeout
- Reconnection logic should be handled by the client (Twilio)

### 12. Monitoring

#### View Logs

```bash
gcloud logs read "resource.type=cloud_run_revision AND resource.labels.service_name=roadside-agent" \
  --limit=50 \
  --format="json"
```

All application logs are emitted as structured JSON (via `structlog`) with correlation fields:
- `call_sid` — Twilio CallSid for correlating across services
- `openai_session_id` — OpenAI session ID for Realtime API correlation
- `assistance_request_id` — Supabase assistance-request UUID
- `sms_sid` — Twilio SMS SID for notification tracking

#### View Metrics

In the Google Cloud Console, navigate to:
- **Cloud Run → roadside-agent → Metrics**

Key metrics to monitor:
- Request count
- Request latency
- Instance count
- Error rate

### 13. Cost Optimization

- **Scale to zero**: The service scales to zero when not in use
- **Concurrency**: Set to 8 to handle multiple requests per instance
- **Max instances**: Set to 10 to prevent excessive scaling

For production workloads, adjust these values based on your traffic patterns.

---

## Production Deployment (Firebase Hosting)

The dispatcher dashboard deploys to Firebase Hosting as a static bundle, separate from the Cloud Run voice service. Merging a PR that touches the frontend to `main` automatically builds and deploys the latest dashboard.

### Architecture

```
GitHub main (frontend/** or firebase.json change)
    ↓
GitHub Actions (.github/workflows/deploy-frontend.yml)
    ↓
npm ci → oxlint → tsc + vite build
    (VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY inlined)
    ↓
FirebaseExtended/action-hosting-deploy → live channel
    ↓
Firebase Hosting CDN
    ↓
https://<firebase-project-id>.web.app
    ↓
Browser → Supabase (public URL + anon/publishable key only)
```

The Cloud Run workflow (`.github/workflows/deploy-production.yml`) is unaffected: it still runs on every `main` push for the backend, while the Hosting workflow only triggers on `frontend/**`, `firebase.json`, or its own workflow file.

### 1. Firebase Project and Hosting Site (One-Time)

1. Create a Firebase project in the [Firebase console](https://console.firebase.google.com/) — either a new project or by adding Firebase to an existing Google Cloud project.
2. Add **Hosting** to the project and create the default Hosting site (**Build → Hosting → Get started**).
3. Install and log in with the Firebase CLI:

```bash
npm install -g firebase-tools
firebase login
```

The repository already contains `firebase.json` (serves `frontend/dist/` with SPA fallback and cache headers), so no `firebase init` is required for the deploy configuration itself.

### 2. Deploy Service Account (One-Time)

Create a dedicated service account for GitHub Actions deploys. Alternatively, run `firebase init hosting:github` from the repository root to have the Firebase CLI create the service account, store the secret, and wire up GitHub for you (it may propose its own workflow file — keep `.github/workflows/deploy-frontend.yml` as the single deploy workflow).

```bash
PROJECT_ID=<your-firebase-project-id>

gcloud iam service-accounts create github-action-dispatcher-dashboard \
  --display-name="GitHub Actions Firebase Hosting Deployer"

DEPLOYER_SA="github-action-dispatcher-dashboard@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/firebasehosting.admin"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/serviceusage.apiKeysViewer"

gcloud iam service-accounts keys create firebase-service-account.json \
  --iam-account="${DEPLOYER_SA}"
```

- `roles/firebasehosting.admin` deploys releases to the live channel; `roles/serviceusage.apiKeysViewer` is required by the Firebase CLI the action runs under the hood (see the [action's service-account documentation](https://github.com/FirebaseExtended/action-hosting-deploy/blob/master/docs/service-account.md)).
- The additional roles from that documentation are only needed for features this workflow does not use: `roles/firebaseauth.admin` for PR preview channels, `roles/run.viewer` for Hosting rewrites to Cloud Run/Functions.
- Paste the entire contents of `firebase-service-account.json` into the repository secret `FIREBASE_SERVICE_ACCOUNT` (step 3), then delete the local file. **Never commit the JSON key.** GitHub encrypts the secret at rest and exposes it only to the deploy job.

### 3. GitHub Repository Configuration

#### Required Repository Variables

Set these in **Settings → Secrets and variables → Actions → Variables**:

| Variable | Description | Example |
|----------|-------------|---------|
| `FIREBASE_PROJECT_ID` | Firebase/Google Cloud project ID that owns the Hosting site | `my-roadside-project` |
| `VITE_SUPABASE_URL` | Public Supabase project URL (inlined into the browser bundle) | `https://xyzcompany.supabase.co` |
| `VITE_SUPABASE_ANON_KEY` | Public Supabase anon/publishable key (inlined into the browser bundle) | `eyJhbGciOi...` or `sb_publishable_...` |

#### Required Repository Secrets

Set these in **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Description |
|--------|-------------|
| `FIREBASE_SERVICE_ACCOUNT` | Service account JSON key for Hosting deploys (see step 2) |

> **Security:** Only the public Supabase URL and anon/publishable key may be set as `VITE_*` variables — they ship in the browser bundle by design. Never place `SUPABASE_SERVICE_ROLE_KEY`, Twilio, or OpenAI credentials in these variables or anywhere under `frontend/`. The deploy workflow fails before building if `VITE_SUPABASE_ANON_KEY` is an `sb_secret_…` key or decodes to a `service_role` JWT.

### 4. Deployment Workflow

The GitHub Actions workflow (`.github/workflows/deploy-frontend.yml`) automatically:

1. Verifies the required repository variables and secret exist, and that `VITE_SUPABASE_ANON_KEY` is not a service-role key
2. Installs dependencies (`npm ci`) and lints (oxlint)
3. Type-checks and builds the production bundle (`tsc -b && vite build`)
4. Deploys `frontend/dist/` to the Hosting **live** channel
5. Verifies the deployed site responds with the app shell
6. Prints the production dashboard URL

#### Triggering a Deployment

```bash
# Automatic: merge a PR that touches frontend/** or firebase.json to main
git checkout main
git merge feat/your-frontend-change
git push origin main
```

Or manually: **Actions → Deploy Dispatcher Dashboard → Run workflow**.

Until the variables and secret from step 3 are configured, the workflow fails fast at the configuration check — by design, so a missing configuration is loud rather than a silent broken deploy.

### 5. Production URL

After the first successful deploy the dashboard is served at:

```
https://<firebase-project-id>.web.app
```

The workflow verifies and prints this URL on every deploy; record the concrete URL here once the first deploy has run.

### 6. SPA Routing and Caching

`firebase.json` configures:

- **SPA fallback:** every path rewrites to `/index.html`, so refreshing or deep-linking the dashboard never returns a Firebase 404.
- **`/` and `**/*.html` → `no-cache`:** Firebase matches custom-header rules against the request path *before* rewrites apply, so the root request is matched explicitly; this guarantees each refresh revalidates the HTML instead of serving a stale copy that points at removed asset hashes after a redeploy.
- **`/assets/**` → `immutable`, 1-year cache:** Vite content-hashes these files, so they can be cached indefinitely.

### 7. Security

- The build step receives only `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`; no other workflow secrets are passed to `npm run build`.
- No `SUPABASE_SERVICE_ROLE_KEY`, Twilio, or OpenAI credentials appear in the frontend repository, the workflow configuration, or the deployed bundle.
- The Cloud Run voice deployment and its Secret Manager credentials are entirely separate.

### 8. Verify the Production Dashboard

After the first deploy (tracked by the post-deploy smoke-check TODO in `TODO.md`):

1. Open `https://<firebase-project-id>.web.app` — the auth screen loads (no Firebase 404)
2. Sign in with the dispatcher account
3. Active/Past sections load, and a live assistance request appears without a manual refresh
4. Refreshing restores the same database-backed state
5. Inspect the served bundle (view source → search `service_role`, `TWILIO_`, `OPENAI_`) — no backend secrets are present
6. The Cloud Run deploy workflow still runs unchanged on the same push

---

## Environment Variables Reference

### Local Development

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Supabase service-role key |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `OPENAI_REALTIME_MODEL` | Yes | Realtime model name |
| `TWILIO_ACCOUNT_SID` | Yes | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Yes | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | Yes | Twilio phone number |
| `DISPATCHER_ALERT_PHONE` | Yes | Dispatcher SMS destination |
| `EMERGENCY_TRANSFER_PHONE` | Yes | Emergency transfer number |
| `DEMO_PHONE_HMAC_SECRET` | Yes | Keyed HMAC secret for demo phone matching (`openssl rand -hex 32`) |
| `DEMO_SESSION_TTL_SECONDS` | No | Demo session lifetime in seconds (default `900` = 15 minutes) |

### Production (Secret Manager)

Same variables as above, stored in Google Secret Manager and injected into Cloud Run at runtime. Create the `DEMO_PHONE_HMAC_SECRET` secret (with a generated value) before deploying — the service refuses to start without it.
