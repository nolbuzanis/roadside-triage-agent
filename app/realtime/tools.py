"""Realtime tool definitions for the roadside triage assistant."""

from __future__ import annotations

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
