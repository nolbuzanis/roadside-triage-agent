"""Tests for realtime conversation instructions content."""

from app.realtime.instructions import ROADSIDE_ASSISTANT_INSTRUCTIONS


def test_instructions_contain_safety_priority() -> None:
    """Safety must be the top priority in the instructions."""
    assert "Safety First" in ROADSIDE_ASSISTANT_INSTRUCTIONS


def test_instructions_define_emergency_scenarios() -> None:
    """All critical emergency scenarios must be listed."""
    emergencies = [
        "fire",
        "collision",
        "accident",
        "injury",
        "bleeding",
        "trapped",
        "traffic",
        "explosion",
    ]
    for phrase in emergencies:
        assert phrase.lower() in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower(), (
            f"Missing emergency scenario: {phrase}"
        )


def test_instructions_require_three_fields() -> None:
    """Must collect exactly location, vehicle, and issue."""
    assert "location" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "vehicle" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "issue" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()


def test_instructions_prohibit_payment_info() -> None:
    """Must not collect payment information."""
    assert "payment" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "do not" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()


def test_instructions_prohibit_eta_promises() -> None:
    """Must not promise truck ETA."""
    assert "eta" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower() or "arrival time" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()


def test_instructions_enforce_one_question_at_a_time() -> None:
    """Must ask one question at a time."""
    assert "one question at a time" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()


def test_instructions_reference_ticket_tool() -> None:
    """Must reference the create_breakdown_ticket tool."""
    assert "create_breakdown_ticket" in ROADSIDE_ASSISTANT_INSTRUCTIONS


def test_instructions_prohibit_inventing_information() -> None:
    """Must not invent or assume information."""
    assert "invent" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower() or "assume" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
