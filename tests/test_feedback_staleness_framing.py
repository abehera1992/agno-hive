"""Tests: injected feedback is framed as a hypothesis, and absence claims are flagged.

Live failure, 2026-08-21. A stored correction read:

    "Do not mention useGetParty, useGetParties, useCreateParty, useUpdateParty,
     useDeleteParty, useAddRegistration — they do not exist in this codebase."

It was injected verbatim into a delegation and the model asserted the nonexistence
it had been handed, concluding "the entire frontend layer for the Parties feature
is missing". It is not missing: the hooks exist and simply carry Query/Mutation
suffixes (useGetPartiesQuery / useCreatePartyMutation, inventoryApi.ts:933-935),
alongside a full route directory.

Two things made that possible: the injected block was headed "read every point
before writing code" — pure assertion, no hedge — and it landed UPSTREAM of every
groundedness guard, so nothing downstream could catch it.

Absence is the claim class that rots. "X does not exist" stays true only until
someone adds X, and nothing here notices when they do.
"""
import pytest

from swarm.feedback import _ABSENCE_CLAIM_RE


# ── the absence detector ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "they do not exist in this codebase",
    "useGetParties doesn't exist",
    "these hooks don't exist anywhere",
    "there is no such function in the router",
    "no such column on that model",
    "that helper never existed",
    "the field is not defined in the schema",
    "those routes are not present",
])
def test_absence_claims_are_detected(text):
    assert _ABSENCE_CLAIM_RE.search(text), f"missed an absence claim: {text!r}"


@pytest.mark.parametrize("text", [
    "use ekamBaseQuery from @/lib/store/baseQuery, not a custom one",
    "SCSS modules only — no Tailwind and no bare className strings",
    "the correct path is Client/EcommClient-Web/ekamweb/src, not ekamweb/src",
    "always pass reducerPath when calling createApi",
])
def test_ordinary_corrections_are_not_flagged(text):
    """Convention corrections stay valid for months and must not be undermined —
    flagging everything would train the model to discount all feedback equally."""
    assert not _ABSENCE_CLAIM_RE.search(text), f"false positive on: {text!r}"


def test_the_exact_live_correction_is_caught():
    live = ("Do not mention useGetParty, useGetParties, useCreateParty, "
            "useUpdateParty, useDeleteParty, useAddRegistration — they do not "
            "exist in this codebase.")
    assert _ABSENCE_CLAIM_RE.search(live)


# ── the injected framing ──────────────────────────────────────────────────────

def _source() -> str:
    """The real injected wording, read from `load_failure_context` itself.

    Asserting on source rather than calling the function: it is DB-backed and its
    formatting is inline, so a fake would be a parallel reimplementation — the exact
    thing that made an earlier test in this session agree only with itself.

    Adjacent string literals are joined first. Python concatenates them at compile
    time, so a sentence wrapped across two source lines is contiguous at runtime but
    NOT in the raw text — a substring assertion against unjoined source fails on
    wrapping alone. (Same trap that made a grep for verify.py's MISMATCH message find
    nothing earlier today: it is built from three f-string fragments.)"""
    import inspect
    import re as _re

    from swarm import feedback

    src = inspect.getsource(feedback.load_failure_context)
    return _re.sub(r'"\s*\n\s*"', "", src)


def test_the_header_frames_feedback_as_unverified():
    src = _source()

    assert "PRIOR OBSERVATIONS, not current facts" in src
    assert "hypothesis" in src
    assert "never as evidence in its own right" in src


def test_absence_claims_get_a_staleness_warning_with_an_action():
    """A warning that only says "this might be stale" leaves the model no next step.
    This one names the tools and says what to do when the thing turns out to exist."""
    src = _source()

    assert "STALENESS RISK" in src
    assert "search_files" in src
    assert "ignore this correction entirely" in src


def test_full_symbol_resolution_is_deliberately_deferred():
    """Recorded in source, not just in a commit message: load_failure_context runs on
    the hot path at run start, so resolving every cited symbol would add a tool
    round-trip per entry to EVERY run. Tagging is free and addresses the observed
    failure; resolution-based expiry remains the follow-on."""
    import inspect

    from swarm import feedback

    src = inspect.getsource(feedback)
    assert "hot path" in src
    assert "Resolution-based expiry stays open" in src
