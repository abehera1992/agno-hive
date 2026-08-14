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
