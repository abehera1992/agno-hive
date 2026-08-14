"""Tests for _looks_like_repetition_loop (swarm/team.py) -- a durable fix for a
4th, distinct failure mode confirmed live 2026-08-14: unlike the empty-completion
stall the same day's liveness fix targets, this run produced REAL, continuously
GROWING content (60,000+ chars and climbing) that was nonetheless useless -- the
coordinator repeating the exact sentence "I'll check the UoM conversion service
implementation to ensure it's properly implemented for Phase 1 requirements:"
verbatim, padded with large runs of blank newlines between each repeat, for 17+
minutes with no sign of stopping. Because content genuinely grew, the existing
last_progress_at signal (which only distinguishes "some new bytes" from "zero new
bytes") saw this as continuous progress and never triggered the auto-kill --
config/config.py's existing coordinator_max_tokens/coordinator_frequency_penalty
mitigations (added 2026-08-10 for the SAME failure class) bound one completion,
not the outer per-turn retry loop that just calls the model again once one capped
completion ends, so the pattern kept recurring across many separate completions.

_looks_like_repetition_loop distinguishes "genuinely new text" from "a repeat of
something already generated" so the SAME liveness/last_progress_at machinery can
catch this too, regardless of how many separate completions it spans.
"""
from swarm.team import _looks_like_repetition_loop


def test_a_verbatim_repeated_sentence_is_detected():
    prior = "Some earlier analysis text. " + "I'll check the UoM conversion service implementation to ensure it's properly implemented for Phase 1 requirements:" + " and here is what I found."
    new_segment = "\n\n\n\n\nI'll check the UoM conversion service implementation to ensure it's properly implemented for Phase 1 requirements:\n\n\n\n\n\n"

    assert _looks_like_repetition_loop(new_segment, prior) is True


def test_whitespace_padding_does_not_defeat_detection():
    """The real incident padded each repeat with large, VARIABLE runs of blank
    newlines -- a literal (non-normalized) substring check would miss this."""
    sentence = "I'll check the UoM conversion service implementation to ensure it's properly implemented for Phase 1 requirements:"
    prior = sentence + "\n" * 40
    new_segment = "\n" * 87 + sentence + "\n" * 12

    assert _looks_like_repetition_loop(new_segment, prior) is True


def test_short_segments_are_never_flagged():
    """Avoid false positives on short, legitimately-repeated fragments (a file
    path, a short phrase) -- only a substantial verbatim repeat is real evidence
    of a loop."""
    prior = "The function exists. " * 10  # short phrase, repeated many times
    new_segment = "The function exists."

    assert _looks_like_repetition_loop(new_segment, prior) is False


def test_genuinely_new_content_is_not_flagged():
    prior = "Here is the analysis of the parties module and its GSTIN handling."
    new_segment = "Now let's look at the inventory service's SKU generation logic in detail."

    assert _looks_like_repetition_loop(new_segment, prior) is False


def test_empty_segment_is_not_flagged():
    assert _looks_like_repetition_loop("", "some prior content here") is False


def test_repetition_only_outside_the_lookback_window_is_not_flagged():
    """Deliberately bounded: a phrase repeated from VERY early in a long answer
    (well outside the lookback window) is not treated as a live loop -- this
    detector targets recent, sustained repetition, not any-time-ever reuse."""
    sentence = "I'll check the UoM conversion service implementation to ensure it's properly implemented for Phase 1 requirements:"
    far_prior = sentence + ("x" * 5000)  # sentence is now far outside the lookback window
    new_segment = sentence

    assert _looks_like_repetition_loop(new_segment, far_prior) is False


def test_internal_whitespace_variations_still_match_when_normalized():
    prior = "I'll check the   UoM conversion service implementation to ensure it's properly implemented for Phase 1 requirements:"
    new_segment = "I'll check the UoM conversion service implementation to ensure it's properly implemented for Phase 1 requirements:"

    assert _looks_like_repetition_loop(new_segment, prior) is True


# ── near-repetition: escalating rewording, not verbatim (2026-08-14, 2nd incident) ──
#
# A DIFFERENT run the same day never repeated anything verbatim, so the checks
# above correctly stayed silent -- but the coordinator still never produced a
# real answer, spiraling instead through slightly REWORDED self-corrections
# about its own citation precision each time:
#   "I need to be more specific with my file:line citations. Let me try again
#    with exact verbatim citations from the codebase:"
#   "I need to be even more specific with my file:line citations. Let me try
#    again with exact verbatim citations from the codebase, using the exact
#    text from the files:"
#   "I need to be more specific with my file:line citations. Let me try again
#    with exact verbatim citations from the codebase, using the exact text
#    from the files, and make sure to include the actual file content..."
# Each version inserts an intensifier ("even") or appends a clause, so no
# version is a literal substring of another -- but they all share the same
# opening. Two changes close this: (1) strip a small set of common hedging/
# intensifier filler words before comparing, so "more specific" and "even
# more specific" normalize to the same text; (2) also check whether just the
# new segment's OPENING (not the whole segment) recurs, so a segment that
# starts the same way but keeps growing a new tail is still caught.

def test_intensifier_reworded_near_repeat_is_detected():
    prior = (
        "I need to be more specific with my file:line citations. Let me try again "
        "with exact verbatim citations from the codebase and check every claim "
        "carefully before finalizing the comparison table."
    )
    new_segment = (
        "I need to be even more specific with my file:line citations. Let me "
        "try again with exact verbatim citations from the codebase, using the "
        "exact text from the files this time to be certain."
    )

    assert _looks_like_repetition_loop(new_segment, prior) is True


def test_growing_tail_after_a_shared_opening_is_still_detected():
    """The real incident's defining shape: each version shares the same
    opening but keeps APPENDING new clauses, so no version is ever a full
    substring of an earlier one -- only the shared prefix repeats."""
    prior = (
        "I need to be more specific with my file:line citations. Let me try "
        "again with exact verbatim citations from the codebase:"
    )
    new_segment = (
        "I need to be more specific with my file:line citations. Let me try "
        "again with exact verbatim citations from the codebase, using the "
        "exact text from the files, and make sure to include the actual file "
        "content in my verification claims this time around."
    )

    assert _looks_like_repetition_loop(new_segment, prior) is True


def test_short_shared_opening_alone_is_not_enough():
    """A short, generic shared opening ('I need to check') is not strong
    evidence of a loop on its own -- the prefix check still respects the same
    minimum-length floor as the whole-segment check."""
    prior = "I need to check the migration file for the exact schema changes applied in Phase 1."
    new_segment = "I need to check whether the frontend form actually renders every required field correctly."

    assert _looks_like_repetition_loop(new_segment, prior) is False


def test_genuinely_different_sentences_sharing_a_few_words_are_not_flagged():
    """Filler-word stripping must not make two REAL, substantively different
    sentences collapse into a false match just because they share a few
    common words."""
    prior = (
        "The migration file confirms the sku_prefix column was added to the "
        "item_categories table as part of the Phase 1 schema changes."
    )
    new_segment = (
        "The frontend form component now renders every weight and dimension "
        "field with proper validation, matching the backend schema exactly."
    )

    assert _looks_like_repetition_loop(new_segment, prior) is False
