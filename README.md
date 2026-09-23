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
supabase/
  config.toml                      # Supabase CLI configuration
  migrations/                      # SQL migrations (table, RLS, webhooks)
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

### Production (Secret Manager)

Same variables as above, stored in Google Secret Manager and injected into Cloud Run at runtime.
