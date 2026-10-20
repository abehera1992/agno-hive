"""T36 Track B -- a two-sided task naming SPECIFIC gaps with no
compare_enumerations call at all must not ship unverified.

Root cause, confirmed against the live T13a answer (session 821f8a68-35d1-4913-
9772-e74f0b41ae06, 2026-10-19 battery, recovered verbatim from this conversation's
own transcript): the Researcher correctly enumerated all 9 real backend endpoints
(with decorators) but never named a specific frontend file to search for hooks --
its own forwarded text shows no hook-search trace at all -- and found only 2 of the
5 real hooks (useGetVouchersQuery, useCreateVoucherMutation). No
compare_enumerations call happened this run. The final answer still shipped:

    #### Backend Endpoints Without Frontend Counterparts
    The following backend endpoints do not have a frontend counterpart:
    1. Post Voucher
    2. Cancel Voucher
    3. Create GRN from PO
    4. Create Credit Note
    5. Create Stock Adjustment
    6. Create Stock Transfer

-- six gaps, two of them (Post, Cancel) wrong: both have real hooks
(usePostVoucherMutation, useCancelVoucherMutation) the run's own evidence gathering
never reached. No existing guard caught this:

  * `_integrity_named_item_falsely_gapped` needs a REAL compare_enumerations call
    to contradict the named items against -- none happened.
  * `_integrity_completeness_claim_without_comparison` (Phase T17, 2026-10-08)
    already catches this exact task's FLAT "zero gaps" phrasing with no
    comparison call -- but this run's answer is not that shape; it names six
    specific items instead, so that check's own zero-gap lexicon never matched.
  * `_GAP_CLAIM_LINE_RE` itself never matched "Without Frontend Counterparts" /
    "do not have a frontend counterpart" at all -- neither phrase contains any
    of its existing keywords (gap/missing/absent/uncovered/"not defined"/etc).

Fix: `_GAP_CLAIM_LINE_RE` widened for "counterpart" phrasing (mirroring
`_RECONCILE_EXTRA_COMPLETENESS_RE`'s own T17 widening for the inverse, zero-gap
phrasing of the same sentence shape), plus a new sibling check,
`_integrity_gap_list_without_comparison`, catching the NONZERO counterpart of
`_integrity_completeness_claim_without_comparison`: a two-sided task, a named gap
list, and no compare_enumerations call at all is exactly as unverifiable as a flat
"fully covered" sentence backed by nothing.
"""
import asyncio
from types import SimpleNamespace

import pytest

from swarm.team import (
    _evidence_integrity_findings,
    _first_named_gap_item,
    _gap_claim_block_lines,
    _integrity_gap_list_without_comparison,
)

_T13A_TASK = (
    "Audit the vouchers module: list its backend endpoints, its database "
    "tables, and its frontend hooks, and identify anything with no frontend "
    "counterpart."
)

_T13A_REAL_ANSWER = (
    "#### Backend Endpoints Without Frontend Counterparts\n"
    "The following backend endpoints do not have a frontend counterpart:\n\n"
    "1. **Post Voucher**\n"
    "2. **Cancel Voucher**\n"
    "3. **Create GRN from PO**\n"
    "4. **Create Credit Note**\n"
    "5. **Create Stock Adjustment**\n"
    "6. **Create Stock Transfer**\n\n"
    "These endpoints are present in the backend but are not directly exposed "
    "to the frontend through the identified hooks.\n"
)


def _run(coro):
    return asyncio.run(coro)


# ── _GAP_CLAIM_LINE_RE widening / _gap_claim_block_lines extraction ────────────

def test_counterpart_heading_is_recognized_as_a_gap_claim():
    lines = _gap_claim_block_lines(_T13A_REAL_ANSWER)
    assert any("Post Voucher" in l for l in lines)
    assert any("Cancel Voucher" in l for l in lines)


def test_first_named_gap_item_extracts_the_real_first_item():
    assert _first_named_gap_item(_T13A_REAL_ANSWER) == "Post Voucher"


def test_first_named_gap_item_is_none_without_a_list():
    assert _first_named_gap_item("There are some gaps in coverage.") is None


# ── _integrity_gap_list_without_comparison (new sibling check) ─────────────────

def test_t13a_real_answer_flagged_with_no_comparison_call():
    result = _integrity_gap_list_without_comparison(_T13A_REAL_ANSWER, _T13A_TASK, "")
    assert result is not None
    claimed, real = result
    assert claimed == "Post Voucher"
    assert "no compare_enumerations call" in real


def test_silent_when_a_real_comparison_ran():
    """Sibling check (_integrity_named_item_falsely_gapped) owns this case."""
    cmp_note = (
        "\n\n---\n**THE COMPARISON, COMPUTED**\n```\nTOTALS: left 9, right 5, "
        "matched 5, left-only 4, right-only 0.\n```")
    assert _integrity_gap_list_without_comparison(
        _T13A_REAL_ANSWER, _T13A_TASK, cmp_note) is None


def test_silent_when_task_is_not_two_sided():
    assert _integrity_gap_list_without_comparison(
        _T13A_REAL_ANSWER, "Summarize the vouchers module.", "") is None


def test_silent_when_answer_names_no_specific_gap():
    answer = "Some backend endpoints may not have frontend coverage."
    assert _integrity_gap_list_without_comparison(answer, _T13A_TASK, "") is None


def test_silent_when_answer_has_no_gap_claim_at_all():
    answer = "The vouchers module has 9 backend endpoints and 5 frontend hooks."
    assert _integrity_gap_list_without_comparison(answer, _T13A_TASK, "") is None


# ── Live replay (2026-10-20), same day as the fix: re-running the exact T13a ───
# ── task against the deployed guard reproduced its own root cause (page.tsx   ─
# ── read instead of the API slice) but with SLIGHTLY different wording than   ─
# ── the original historical answer -- "...with No Frontend Counterpart" ("with ─
# ── no", not "without") and "do not have A CORRESPONDING frontend hook" (ends  ─
# ── in "hook", not "counterpart"). Neither matched the first widening; this   ─
# ── pins the live-caught miss so it cannot regress silently.

_T13A_LIVE_REPLAY_ANSWER = (
    "### Backend Functionality with No Frontend Counterpart\n\n"
    "The following backend endpoints do not have a corresponding frontend "
    "hook:\n\n"
    "1. **Post voucher**\n"
    "2. **Cancel voucher**\n"
    "3. **Create GRN from PO**\n"
    "4. **Create credit note**\n"
    "5. **Create stock adjustment**\n"
    "6. **Create stock transfer**\n\n"
    "These endpoints are available in the backend but are not directly "
    "accessible through the frontend hooks identified in the page.tsx file.\n"
)


def test_live_replay_wording_shape_is_caught():
    result = _integrity_gap_list_without_comparison(
        _T13A_LIVE_REPLAY_ANSWER, _T13A_TASK, "")
    assert result is not None
    claimed, real = result
    assert claimed == "Post voucher"


# ── Full _evidence_integrity_findings wiring ────────────────────────────────────

@pytest.mark.asyncio
async def test_findings_catch_the_real_t13a_shape_end_to_end():
    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    findings = await _evidence_integrity_findings(
        _T13A_REAL_ANSWER, _T13A_TASK, team, None, None, "")
    gap_findings = [f for f in findings if f["category"] == "comparison completeness/gap"]
    assert gap_findings
    assert any("Post Voucher" in f["claimed"] for f in gap_findings)


@pytest.mark.asyncio
async def test_findings_do_not_duplicate_fire_the_zero_gap_sibling():
    """The named-gap-list check and the flat-zero-gap check are mutually
    exclusive by construction (a nonzero named list is not completeness-
    claim-shaped) -- confirms they do not both fire on the same answer."""
    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    findings = await _evidence_integrity_findings(
        _T13A_REAL_ANSWER, _T13A_TASK, team, None, None, "")
    gap_findings = [f for f in findings if f["category"] == "comparison completeness/gap"]
    assert len(gap_findings) == 1
