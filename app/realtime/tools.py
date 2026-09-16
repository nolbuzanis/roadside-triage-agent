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


class EmergencyTransferArgs(BaseModel):
    """Validated arguments for the transfer_to_emergency tool."""

    reason: str


class EmergencyTransferResult(BaseModel):
    """Result returned to the OpenAI Realtime model after an emergency transfer."""

    status: str
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

TRANSFER_TO_EMERGENCY_TOOL = {
    "type": "function",
    "name": "transfer_to_emergency",
    "description": (
        "Immediately transfer the caller to emergency services. Use this when the "
        "caller is in immediate danger — fire, active collision, injury, trapped "
        "occupants, unsafe position in traffic, or any situation requiring emergency "
        "response. Do NOT collect intake information before transferring."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief description of the emergency situation",
            },
        },
        "required": ["reason"],
    },
}

REALTIME_TOOLS = [CREATE_BREAKDOWN_TICKET_TOOL, TRANSFER_TO_EMERGENCY_TOOL]
