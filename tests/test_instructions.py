"""Tests for realtime conversation instructions content."""

from app.realtime.instructions import OPENING_GREETING, ROADSIDE_ASSISTANT_INSTRUCTIONS


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
    """Must reference the update_assistance_request tool."""
    assert "update_assistance_request" in ROADSIDE_ASSISTANT_INSTRUCTIONS


def test_instructions_reference_confirmation_tool() -> None:
    """Must reference the confirm_assistance_request completion tool."""
    assert "confirm_assistance_request" in ROADSIDE_ASSISTANT_INSTRUCTIONS


def test_instructions_direct_progressive_saving() -> None:
    """Must instruct saving each field as it is collected, not only at the end."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "as soon as" in lower
    assert "call the tool again" in lower or "call again" in lower


def test_instructions_separate_saving_from_completion() -> None:
    """Saving all three fields must not be treated as completion."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "saving all three fields does not mean the intake is complete" in lower
    assert "only complete after the caller verbally confirms" in lower


def test_instructions_require_summary_before_completion() -> None:
    """Must summarize location, vehicle, and issue and ask for confirmation."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "summarize all three" in lower
    assert "is that all correct?" in lower
    assert "all three details" in lower


def test_instructions_require_caller_confirmation_before_completing() -> None:
    """The completion tool may only run after the caller explicitly confirms."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "only after the caller explicitly confirms" in lower
    assert "do not call confirm_assistance_request before the caller confirms" in lower


def test_instructions_require_reconfirmation_after_correction() -> None:
    """A corrected field re-opens the summary, not the intake."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "corrects any detail" in lower
    assert "ask for confirmation again" in lower


def test_instructions_define_clear_affirmative_as_only_completion_path() -> None:
    """Only an unambiguous agreement may call the confirmation tool."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "clear affirmative" in lower
    assert "unambiguous agreement" in lower
    assert "only reply that completes the intake" in lower
    assert "call confirm_assistance_request, exactly once" in lower
    assert "that's right" in lower


def test_instructions_define_declined_confirmation_fallback() -> None:
    """A clear rejection must never confirm; the model asks what to correct."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "clear rejection" in lower
    assert "not quite" in lower
    assert "that's wrong" in lower
    assert "ask briefly what needs correcting" in lower
    assert "do not call confirm_assistance_request" in lower


def test_instructions_reject_ambiguous_confirmation_responses() -> None:
    """Hedged answers are not confirmation and require an explicit yes/no."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "ambiguous or hedged" in lower
    for hedge in ("i think so", "probably", "maybe", "i guess"):
        assert hedge in lower
    assert "this is not a confirmation" in lower
    assert "clear yes or no" in lower


def test_instructions_reject_silence_and_dead_air_confirmation() -> None:
    """Unanswered confirmation must never be treated as agreement."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "silence or no answer" in lower
    assert "dead air is not a confirmation" in lower
    assert "do not assume silence means confirmation" in lower
    # The defined fallback turn for dead air.
    assert "ask the same confirmation question once more" in lower


def test_instructions_never_complete_on_declined_ambiguous_or_unanswered_summary() -> None:
    """The prompt must state the no-completion rule for non-answers outright."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert (
        "declined, ambiguous, or unanswered confirmation never completes the intake"
        in lower
    )
    # Restated where completion itself is defined.
    assert "summary never completes the intake" in lower


def test_instructions_persist_correction_before_re_summarizing() -> None:
    """A correction is saved first, then all three fields are re-confirmed."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "only the corrected field or fields" in lower
    assert "summarize the complete current location, vehicle, and issue again" in lower


def test_instructions_keep_emergency_priority_over_confirmation_fallback() -> None:
    """The decline/ambiguity fallbacks must not outrank an emergency transfer."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "transfer immediately instead of asking for confirmation" in lower
    assert "including while clarifying" in lower


def test_instructions_add_no_closing_language_of_their_own() -> None:
    """The closing line stays system-owned; no model-written closing."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "never add a closing of your own" in lower


def test_instructions_do_not_claim_saving_closes_the_call() -> None:
    """The prompt must not promise an automatic closing on the third save."""
    instructions = ROADSIDE_ASSISTANT_INSTRUCTIONS
    assert '"created"' not in instructions
    assert "returns status" not in instructions


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


def test_greeting_constant_value() -> None:
    """The greeting constant must be the exact expected string."""
    assert OPENING_GREETING == "This is roadside assistance. How can I help?"


def test_instructions_include_greeting_section() -> None:
    """Instructions must contain an Opening Greeting section."""
    assert "Opening Greeting" in ROADSIDE_ASSISTANT_INSTRUCTIONS


def test_instructions_reference_greeting_constant() -> None:
    """Instructions must include the exact greeting text."""
    assert OPENING_GREETING in ROADSIDE_ASSISTANT_INSTRUCTIONS


def test_instructions_forbid_repeating_greeting() -> None:
    """Instructions must tell the model not to repeat or substitute the greeting."""
    lower = ROADSIDE_ASSISTANT_INSTRUCTIONS.lower()
    assert "never repeat it" in lower
    assert "never use a greeting of your own" in lower
