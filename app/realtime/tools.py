"""Realtime tool definitions and typed models for the roadside triage assistant."""

from __future__ import annotations

from pydantic import BaseModel


class TicketArgs(BaseModel):
    """Validated arguments for the create_breakdown_ticket tool."""

    location: str
    vehicle: str
    issue: str


class TicketToolResult(BaseModel):
    """Result returned to the OpenAI Realtime model after a tool call."""

    status: str
    ticket_id: str | None = None
    message: str | None = None
    error: str | None = None


CREATE_BREAKDOWN_TICKET_TOOL = {
    "type": "function",
    "name": "create_breakdown_ticket",
    "description": (
        "Create a roadside assistance breakdown ticket after collecting the caller's "
        "location, vehicle details, and issue description. Call this only after you "
        "have all three pieces of information."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "The caller's current location or nearest intersection/address",
            },
            "vehicle": {
                "type": "string",
                "description": "Vehicle make, model, year, and color if available",
            },
            "issue": {
                "type": "string",
                "description": "Description of the breakdown or problem",
            },
        },
        "required": ["location", "vehicle", "issue"],
    },
}

REALTIME_TOOLS = [CREATE_BREAKDOWN_TICKET_TOOL]
