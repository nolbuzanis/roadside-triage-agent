"""System instructions for the roadside assistance realtime voice agent."""

from pathlib import Path

OPENING_GREETING = "This is roadside assistance. How can I help?"

# Fixed closing line spoken after update_assistance_request succeeds. The call is
# hung up only after Twilio acknowledges that this response's audio finished playing.
CLOSING_MESSAGE = (
    "You're all set. I've logged your roadside assistance request, and a dispatcher will "
    "follow up with you shortly. Please stay somewhere safe. Goodbye."
)

_INSTRUCTIONS_PATH = Path(__file__).with_name("system_prompt.md")

ROADSIDE_ASSISTANT_INSTRUCTIONS = (
    _INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    .rstrip("\n")
    .replace("{OPENING_GREETING}", OPENING_GREETING)
)
