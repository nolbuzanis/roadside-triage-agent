# Roadside Assistance Triage AI Voice Agent (MVP)

An autonomous AI voice agent system built for towing companies to handle inbound non-emergency roadside assistance calls. Powered by Vapi.ai (Deepgram/OpenAI/ElevenLabs) and a FastAPI backend, this agent seamlessly gathers critical breakdown details from stranded drivers, persists structured tickets into a PostgreSQL database, and instantly notifies human dispatchers via SMS.

## Features

- **Instant Intake**: Zero hold times for stranded callers, ensuring immediate and empathetic response.
- **Voice-to-Database Pipeline**: Converts natural spoken conversations into structured, validated PostgreSQL records using LLM tool-calling.
- **Emergency Escalation**: Automatically detects hazard keywords (e.g., "fire", "traffic") and transfers the call to a live human.
- **Dispatcher Alerts**: Sends instant, structured SMS notifications to dispatchers via Twilio the second a ticket is logged.

---

## Prerequisites

Before you begin, ensure you have the following installed and configured:

- **Python 3.13+**
- **PostgreSQL 15+** (running locally or via Docker)
- **Vapi.ai Account** (for voice orchestration)
- **Twilio Account** (for SIP trunking and outbound SMS alerts)

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

### 3. Configure Git Hooks

We use `pre-commit` to ensure code formatting (Black, isort) and linting (Flake8) standards are met before every commit.

```bash
pip install pre-commit
pre-commit install
```

### 4. Configure Environment Variables

Copy the example environment file and fill in your actual API keys and database credentials.

```bash
cp .env.example .env
```

#### Environment Variables Table

| Variable               | Required | Description                                                                                                  |
| ---------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| `DATABASE_URL`         | Yes      | PostgreSQL connection string (e.g., `postgresql+psycopg://user:pass@localhost:5432/roadside_db`)             |
| `VAPI_API_KEY`         | Yes      | Secret API key for configuring your Vapi.ai assistant                                                        |
| `VAPI_WEBHOOK_SECRET`  | Yes      | Custom secret header used to securely verify inbound webhooks from Vapi                                      |
| `TWILIO_ACCOUNT_SID`   | Yes      | Twilio Account SID for sending outbound SMS                                                                  |
| `TWILIO_AUTH_TOKEN`    | Yes      | Twilio Auth Token                                                                                            |
| `TWILIO_PHONE_NUMBER`  | Yes      | Your Twilio-provisioned phone number (e.g., `+16045550199`)                                                  |
| `DISPATCHER_ALERT_PHONE` | Yes    | The cell phone number of the human dispatcher receiving alerts (e.g., `+16045550100`)                        |

### 5. Run Database Migrations

Initialize your database schema using Alembic:

```bash
alembic upgrade head
```

## Running the Application

Start the FastAPI development server:

```bash
uvicorn app.main:app --reload --port 8000
```

*Note: To receive webhooks from Vapi.ai during local development, you will need to expose your local port `8000` to the internet using a tool like [ngrok](https://ngrok.com/) (`ngrok http 8000`).*

## Usage Example (Testing the Webhook)

You don't have to place a real phone call to test the backend logic. You can simulate the exact JSON payload that Vapi sends when the LLM successfully triggers the `log_breakdown_ticket` function using `curl`:

```bash
curl -X POST http://localhost:8000/api/v1/webhooks/vapi \
  -H "Content-Type: application/json" \
  -H "x-vapi-secret: your_webhook_secret_here" \
  -d '{
    "message": {
      "type": "tool-calls",
      "call": {
        "id": "call_test_890123",
        "customer": {
          "number": "+16045550111"
        }
      },
      "toolCallList": [
        {
          "id": "tool_call_xyz",
          "type": "function",
          "function": {
            "name": "log_breakdown_ticket",
            "arguments": {
              "location": "Main St and 4th Ave, Vancouver",
              "vehicle": "Blue 2020 Hyundai Kona",
              "issue": "Flat rear left tire, pulled over safely"
            }
          }
        }
      ]
    }
  }'
```

**Expected Result:**

1. The FastAPI server returns a HTTP 200 success response.
2. A new `BreakdownTicket` record is saved in your local PostgreSQL database.
3. The specified `DISPATCHER_ALERT_PHONE` receives a Twilio SMS with the formatted ticket details.
