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


def test_instructions_distinguish_true_emergencies_from_false_positives() -> None:
    """Must explicitly warn against transferring for benign keyword mentions."""
    assert "false positive" in ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()


def test_instructions_warn_against_keyword_matching() -> None:
    """Must not transfer based on keyword matching alone."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "keyword matching" in lower or "keyword" in lower


def test_instructions_provide_traffic_distinction() -> None:
    """Must distinguish 'stopped in active traffic' from 'traffic is heavy'."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "stopped in active traffic" in lower or "travel lane" in lower


def test_instructions_provide_smoke_distinction() -> None:
    """Must distinguish vehicle fire from exhaust or steam."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "exhaust" in lower or "steam" in lower


def test_instructions_provide_accident_distinction() -> None:
    """Must distinguish collision with injuries from minor fender-bender."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "fender-bender" in lower or "injuries" in lower


def test_instructions_prioritize_safety_when_uncertain() -> None:
    """Must instruct erring on the side of transfer when uncertain."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "when in doubt" in lower or "err on the side" in lower


def test_instructions_require_use_of_transfer_tool() -> None:
    """Must instruct using the transfer tool, not just telling caller to call 911."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "transfer_to_emergency" in lower
