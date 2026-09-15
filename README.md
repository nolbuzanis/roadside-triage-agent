# Roadside Assistance Triage AI Voice Agent (MVP)

An autonomous AI voice agent system built for towing companies to handle inbound non-emergency roadside assistance calls. Powered by OpenAI Realtime and a FastAPI backend, this agent seamlessly gathers critical breakdown details from stranded drivers, persists structured tickets into a Supabase database, and instantly notifies human dispatchers via SMS.

## Features

- **Instant Intake**: Zero hold times for stranded callers, ensuring immediate and empathetic response.
- **Voice-to-Database Pipeline**: Converts natural spoken conversations into structured, validated Supabase records using OpenAI Realtime tool-calling.
- **Emergency Escalation**: Automatically detects hazard situations (fire, injury, trapped occupants, etc.) and transfers the call to a live human.
- **Dispatcher Alerts**: Sends instant, structured SMS notifications to dispatchers via Twilio the second a ticket is logged.

---

## Prerequisites

Before you begin, ensure you have the following installed and configured:

- **Python 3.13+**
- **Supabase Account** (for database and persistence)
- **OpenAI Account** (for Realtime API access)
- **Twilio Account** (for PSTN inbound calling and outbound SMS alerts)

---

## Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/roadside-triage-agent.git
cd roadside-triage-agent
```

### 2. Create Virtual Environment & Install Dependencies

```bash
python3.13 -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy the example environment file and fill in your actual API keys and credentials.

```bash
cp .env.example .env
```

#### Environment Variables

| Variable                  | Required | Description                                                                                     |
| ------------------------- | -------- | ----------------------------------------------------------------------------------------------- |
| `SUPABASE_URL`            | Yes      | Supabase project URL (e.g., `https://xyz.supabase.co`)                                         |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes    | Supabase service-role key for server-side database access                                       |
| `OPENAI_API_KEY`          | Yes      | OpenAI API key for Realtime API                                                                 |
| `OPENAI_REALTIME_MODEL`   | Yes      | Realtime model name (e.g., `gpt-4o-realtime-preview-2024-10-01`)                               |
| `TWILIO_ACCOUNT_SID`      | Yes      | Twilio Account SID                                                                              |
| `TWILIO_AUTH_TOKEN`       | Yes      | Twilio Auth Token (used for request signature validation and SMS)                               |
| `TWILIO_PHONE_NUMBER`     | Yes      | Your Twilio-provisioned phone number (e.g., `+16045550199`)                                     |
| `DISPATCHER_ALERT_PHONE`  | Yes      | Cell phone number of the human dispatcher receiving SMS alerts (e.g., `+16045550100`)           |
| `EMERGENCY_TRANSFER_PHONE`| Yes      | Emergency transfer destination (911 or local emergency number)                                  |

### 4. Configure Twilio Phone Number

1. Purchase a phone number in the [Twilio Console](https://console.twilio.com/)
2. Under the phone number's **Voice Configuration**, set the **A call comes in** webhook to:

```
https://<your-domain>/api/v1/twilio/voice
```

For local development, use [ngrok](https://ngrok.com/) to expose your local server:

```bash
ngrok http 8000
```

Then set the webhook URL to the ngrok HTTPS URL (e.g., `https://abc123.ngrok.io/api/v1/twilio/voice`).

**Important**: The voice webhook returns TwiML that initiates a bidirectional Media Stream to `/api/v1/twilio/media-stream`. Twilio must be able to reach both the voice webhook and the WebSocket endpoint.

### 5. Set Up Supabase

1. Create a Supabase project
2. Run the migration to create the `breakdown_tickets` table (see `supabase/migrations/`)
3. Ensure Row Level Security is enabled on `breakdown_tickets`

## Running the Application

Start the FastAPI development server:

```bash
uvicorn app.main:app --reload --port 8000
```

### Verifying the Setup

1. **Health check**: `GET /health` returns `{"status": "ok"}`
2. **Local WebSocket test**: Connect to `ws://localhost:8000/api/v1/twilio/media-stream` to verify the WebSocket endpoint accepts connections
3. **End-to-end test**: Call your Twilio phone number — the call should connect and the voice assistant should begin speaking

---

## Architecture

```
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
