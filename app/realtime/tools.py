"""Realtime tool definitions and typed models for the roadside triage assistant."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator


class AssistanceRequestArgs(BaseModel):
    """Validated arguments for the update_assistance_request tool.

    All intake fields are optional so partial saves are accepted; at least
    one non-empty field is required (there must be something to save).
    """

    location: str | None = None
    vehicle: str | None = None
    issue: str | None = None

    @model_validator(mode="after")
    def _require_something_to_save(self) -> AssistanceRequestArgs:
        if not (self.location or self.vehicle or self.issue):
            raise ValueError("at least one of location, vehicle, or issue is required")
        return self


class AssistanceRequestToolResult(BaseModel):
    """Result returned to the OpenAI Realtime model after a tool call."""

    status: str
    assistance_request_id: str | None = None
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


UPDATE_ASSISTANCE_REQUEST_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "update_assistance_request",
    "description": (
        "Save the caller's roadside assistance details progressively. Call with "
        "whatever fields are available (location, vehicle, issue) as soon as the "
        "caller provides them, and call again to update or correct previously "
        "saved details. Do not wait until all three fields are collected before "
        "the first call, and never invent missing values. Saving all three fields "
        "returns status 'ready_for_confirmation' — the request is saved but NOT "
        "complete; the caller must still confirm the summary via "
        "confirm_assistance_request."
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
        "required": [],
    },
}

CONFIRM_ASSISTANCE_REQUEST_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "confirm_assistance_request",
    "description": (
        "Mark the roadside assistance request as complete once the caller has "
        "verbally confirmed the summarized location, vehicle, and issue. Takes no "
        "arguments: the backend reads the saved request for this call. Call it "
        "only after the caller clearly confirms the full summary — never before, "
        "and never to save or correct details (use update_assistance_request for "
        "that). On success the system delivers the fixed closing line."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TRANSFER_TO_EMERGENCY_TOOL = {
    "type": "function",
    "name": "transfer_to_emergency",
    "description": (
        "Immediately transfer the caller to emergency services. Use this when the "
        "caller describes an immediate physical danger: active fire, collision with "
        "injuries or entrapment, bleeding or injury, trapped occupants, or a vehicle "
        "stopped in active traffic lanes. Do NOT transfer for benign mentions of words "
        "like 'traffic' or 'smoke' alone — assess whether the caller is actually in "
        "danger. Do NOT collect intake information before transferring. If in doubt, "
        "transfer."
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

REALTIME_TOOLS = [
    UPDATE_ASSISTANCE_REQUEST_TOOL,
    CONFIRM_ASSISTANCE_REQUEST_TOOL,
    TRANSFER_TO_EMERGENCY_TOOL,
]
