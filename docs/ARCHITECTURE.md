# System Architecture: Roadside Assistance Triage AI Voice Agent

## Overview

## Data Flow

```
┌─────────────────────┐
│   External Source    │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│   Processing Layer  │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│    Data Store       │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│    API Layer        │
└─────────────────────┘
```

## Component Responsibilities

### 1. Webhook Controller (`app/api/webhooks.py`)

**Responsibilities:**

- Receive and validate inbound `tool-calls` webhook payloads from Vapi.ai using the `VAPI_WEBHOOK_SECRET` header.
- Parse the structured LLM arguments (location, vehicle, issue) using Pydantic schemas.
- Return a synchronous HTTP 200 JSON response mapped to the `toolCallId` so the voice agent can confirm success to the caller.



### 2. Notification Service (`app/services/notifier.py`)

**Responsibilities:**

- Initialize the Twilio REST client using environment credentials.
- Format the raw assistance request data into a concise, readable SMS template.
- Dispatch the SMS alert to the predefined on-duty dispatcher phone number and handle API delivery failures gracefully.



### 3. Database ORM Models (`app/models/ticket.py`)

**Responsibilities:**

- Define the `AssistanceRequest` schema and state enums using SQLAlchemy 2.0 `Mapped` classes.
- Manage automatic timestamp generation for `created_at` and `updated_at` fields.
- Ensure database-level constraints (e.g., unique `call_id` indexes to prevent duplicate request creation from webhook retries).



## Data Models



### AssistanceRequest

```python
cclass AssistanceRequest(Base):
    id: int              # Primary key, autoincrement
    call_id: str         # Unique Vapi call session identifier (indexed)
    caller_phone: str    # Inbound caller phone number from Twilio SIP
    location: str        # Verbal cross-streets or landmark extracted by LLM
    vehicle: str         # Vehicle make, model, and color extracted by LLM
    issue: str           # Description of breakdown or mechanical problem
    status: str          # Free-text dispatcher/business workflow state (reserved for future use)
    intake_status: str   # Canonical intake lifecycle: in_progress, completed, abandoned, escalated
    created_at: datetime # Record creation time (UTC)
    updated_at: datetime # Record last updated time (UTC)
```



## Environment Variables


|                          |              |             |                                                       |
| ------------------------ | ------------ | ----------- | ----------------------------------------------------- |
| **Variable**             | **Required** | **Default** | **Description**                                       |
| `DATABASE_URL`           | Yes          | None        | PostgreSQL connection string                          |
| `VAPI_API_KEY`           | Yes          | None        | Secret API key for Vapi platform configuration        |
| `VAPI_WEBHOOK_SECRET`    | Yes          | None        | Secret header key for verifying inbound webhooks      |
| `TWILIO_ACCOUNT_SID`     | Yes          | None        | Account SID for Twilio REST API                       |
| `TWILIO_AUTH_TOKEN`      | Yes          | None        | Auth Token for Twilio REST API                        |
| `TWILIO_PHONE_NUMBER`    | Yes          | None        | Outbound Twilio SMS sender number                     |
| `DISPATCHER_ALERT_PHONE` | Yes          | None        | The human dispatcher's phone number to receive alerts |




## External Integrations

### [Vapi.ai](http://Vapi.ai) (Voice Orchestration)

- **Library:** Standard Python `httpx` (for outbound config) / FastAPI (for inbound webhooks)
- **Authentication:** Bearer Token (outbound) / `x-vapi-secret` Header (inbound)
- **Rate Limits:** Depends on billing tier (default is typically 10 concurrent active calls).
- **Data Retrieved:** Live call metadata, structured JSON tool-call arguments, and call transcripts.

### Twilio (SMS & Telephony)

- **Library:** `twilio` (Twilio Python Helper Library)
- **Authentication:** Account SID and Auth Token
- **Rate Limits:** 1 outbound SMS message per second (standard long code).
- **Data Retrieved:** Delivery status receipts for outbound dispatcher alerts.

## Known Limitations

1. **Cold-Start Latency** - If the FastAPI layer is hosted on a serverless platform (e.g., AWS Lambda, Vercel) that scales to zero, cold starts may cause the webhook to exceed Vapi's strict 5-second response timeout, causing the voice agent to hang or fail.
2. **No Geolocation Verification** - The system relies entirely on the LLM parsing the user's verbal description of their location; it cannot verify accuracy against a physical GPS ping.

