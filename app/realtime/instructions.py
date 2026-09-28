"""System instructions for the roadside assistance realtime voice agent."""

from pathlib import Path

OPENING_GREETING = "This is roadside assistance. How can I help?"

# Fixed closing line spoken after confirm_assistance_request succeeds (the caller
# confirmed the summarized intake). The call is hung up only after Twilio
# acknowledges that this response's audio finished playing.
CLOSING_MESSAGE = (
    "You're all set. I've logged your roadside assistance request, and a dispatcher will "
    "follow up with you shortly. Please stay somewhere safe. Goodbye."
)

# Fixed transfer line spoken after transfer_to_emergency succeeds. Delivered
# via per-response instructions (like the greeting and closing lines) so the
# model never improvises a second message on top of the tool-call turn's
# speech. The Twilio redirect starts only after this response finishes
# playing.
TRANSFER_MESSAGE = (
    "This sounds like a serious emergency, so I'm transferring you to emergency "
    "services now. Move away from the vehicle if you can, stay safe, and stay on "
    "the line."
)

_INSTRUCTIONS_PATH = Path(__file__).with_name("system_prompt.md")

ROADSIDE_ASSISTANT_INSTRUCTIONS = (
    _INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    .rstrip("\n")
    .replace("{OPENING_GREETING}", OPENING_GREETING)
)
