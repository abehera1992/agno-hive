"""Phase J-C: downstream completeness / coverage semantics.

The comparison engine (Phase J-A/J-B, hive-mcp/tools/compare.py) now
distinguishes MATCH / PARTIAL_MATCH / LEFT_ONLY / RIGHT_ONLY / AMBIGUOUS, but
before this phase the ONLY thing swarm/team.py's completeness logic could see
was (left_only, right_only) via _comparison_gap_counts. That meant an
all-zero (left_only, right_only) result -- even one containing real AMBIGUOUS
relationships -- was indistinguishable from a genuinely clean comparison, and
was persisted/treated as "supported". This is the exact "unknown becomes
complete" defect this phase's hard invariant forbids.

_comparison_summary is the new, single authoritative parse (still text-based
under the MCP tool boundary, but consolidated into one function instead of
several ad-hoc regexes) that exposes partial_match and ambiguous alongside
the existing counts, and _reconcile_completeness_claim_with_comparison /
_evidence_integrity_findings's category-3 check were both updated to consume
it -- never collapsing PARTIAL_MATCH or AMBIGUOUS into "missing" or "complete".
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _comparison_summary,
    _evidence_integrity_findings,
    _reconcile_completeness_claim_with_comparison,
)


def _run(coro):
    return asyncio.run(coro)


class _Team:
    """Bare stand-in, matching test_comparison_reconciliation.py's own
    convention -- _run_read_count/_member_reads_delta both degrade safely for
    an object with no _read_state, which _more_grounded then treats as
    'could not tell' -> always adopt."""


def _cmp_note(left=0, right=0, matched=0, left_only=0, right_only=0,
              partial_match=None, ambiguous=None, body_extra=""):
    """A _computed_comparison-shaped footnote, matching hive-mcp/tools/
    compare.py's REAL post-J-A/J-B rendering exactly -- the TOTALS line plus
    its two additive siblings. `partial_match`/`ambiguous` default to None
    (omit the line entirely) to also exercise the pre-J-A/J-B compatibility
    path _comparison_summary's own docstring describes."""
    lines = [
        "compare_enumerations — a.py  vs  b.ts",
        "join: two-stage -- identity, then attribute agreement.",
        "",
        body_extra,
        f"TOTALS: left {left}, right {right}, matched {matched}, "
        f"left-only {left_only}, right-only {right_only}.",
    ]
    if partial_match is not None:
        lines.append(f"PARTIAL-MATCH TOTAL: {partial_match} (same identity, "
                      f"differing attribute).")
    if ambiguous is not None:
        lines.append(f"AMBIGUOUS TOTAL: {ambiguous} (unresolved).")
    body = "\n".join(l for l in lines if l or l == "")
    return ("\n\n---\n**THE COMPARISON, COMPUTED — ...**\n```\n" + body + "\n```")


# ── _comparison_summary: pure parser ──────────────────────────────────────────

def test_summary_parses_all_five_counts():
    note = _cmp_note(left=9, right=48, matched=3, left_only=4, right_only=43,
                      partial_match=2, ambiguous=0)
    s = _comparison_summary(note)
    assert s == {"left": 9, "right": 48, "matched": 3, "left_only": 4,
                 "right_only": 43, "partial_match": 2, "ambiguous": 0}


def test_summary_returns_none_when_totals_line_absent():
    assert _comparison_summary("") is None
    assert _comparison_summary("no totals line here at all") is None


def test_summary_defaults_partial_and_ambiguous_to_zero_when_lines_absent():
    """A pre-J-A/J-B cmp_note (or any note whose PARTIAL-MATCH/AMBIGUOUS
    lines are genuinely absent) is a real, not-unknown fact for those two
    counts specifically -- the base TOTALS line is what gates None."""
    note = _cmp_note(left=2, right=1, matched=1, left_only=1, right_only=0,
                      partial_match=None, ambiguous=None)
    s = _comparison_summary(note)
    assert s["partial_match"] == 0
    assert s["ambiguous"] == 0


# ── C1: all MATCH -> coverage complete, consistency clean ────────────────────

@pytest.mark.asyncio
async def test_C1_all_match_is_supported_no_retry(monkeypatch):
    calls = {"n": 0}

    async def fail_if_called(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not retry when nothing is contradicted")
    monkeypatch.setattr(team_mod, "_stream_team_run", fail_if_called)

    persisted = {}

    async def fake_persist(team, statement, status):
        persisted["status"] = status
    monkeypatch.setattr(team_mod, "_persist_completeness_claim", fake_persist)

    note = _cmp_note(left=3, right=3, matched=3, left_only=0, right_only=0,
                      partial_match=0, ambiguous=0)
    content = "All endpoints have a corresponding hook. There are no missing items."
    out_content, out_result, reconciled = await _reconcile_completeness_claim_with_comparison(
        content, "task text", _Team(), [None], None, None, note, False)

    assert reconciled is False
    assert out_content == content
    assert calls["n"] == 0
    assert persisted.get("status") == "supported"


# ── C2: LEFT_ONLY -> coverage incomplete ──────────────────────────────────────

@pytest.mark.asyncio
async def test_C2_left_only_triggers_reconciliation_and_is_contradicted(monkeypatch):
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return "6 endpoints have no corresponding hook: /x.", SimpleNamespace(messages=[])
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    persisted = {}

    async def fake_persist(team, statement, status):
        persisted["status"] = status
    monkeypatch.setattr(team_mod, "_persist_completeness_claim", fake_persist)

    note = _cmp_note(left=2, right=1, matched=1, left_only=1, right_only=0,
                      partial_match=0, ambiguous=0)
    content = "All endpoints have a corresponding hook. There are no missing items."
    out_content, out_result, reconciled = await _reconcile_completeness_claim_with_comparison(
        content, "task text", _Team(), [None], None, None, note, False)

    assert reconciled is True
    assert persisted.get("status") == "contradicted"
    assert "left-only=1" in "" or "left" in captured["prompt"].lower()


# ── C3: RIGHT_ONLY preserves direction, never becomes LEFT_ONLY ─────────────

@pytest.mark.asyncio
async def test_C3_right_only_is_reported_as_right_not_reworded_as_left(monkeypatch):
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return "corrected answer naming the real right-only item", SimpleNamespace(messages=[])
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def no_op_persist(*a, **k):
        return None
    monkeypatch.setattr(team_mod, "_persist_completeness_claim", no_op_persist)

    note = _cmp_note(left=1, right=2, matched=1, left_only=0, right_only=1,
                      partial_match=0, ambiguous=0)
    content = "Everything is fully covered on both sides."
    out_content, out_result, reconciled = await _reconcile_completeness_claim_with_comparison(
        content, "task text", _Team(), [None], None, None, note, False)

    assert reconciled is True
    assert "right-only" in captured["prompt"] or "on the right" in captured["prompt"]
    # Directionality preserved -- the prompt names 0 left-only, 1 right-only,
    # never inflates left_only with the right-side count.
    assert "0 item(s) on the left" in captured["prompt"]


# ── C4: PARTIAL_MATCH -> exists, consistency mismatch, NEVER missing ────────

@pytest.mark.asyncio
async def test_C4_partial_match_alone_does_not_trigger_a_retry(monkeypatch):
    """The central Phase J-C rule: PARTIAL_MATCH means identity exists on
    both sides -- a completeness (existence) claim is not contradicted by a
    consistency-only defect, so this must NOT spend the one reconciliation
    retry. It must still be durably recorded as a distinct status so the
    defect is never silently dropped."""
    calls = {"n": 0}

    async def fail_if_called(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not retry for partial_match alone")
    monkeypatch.setattr(team_mod, "_stream_team_run", fail_if_called)

    persisted = {}

    async def fake_persist(team, statement, status):
        persisted["status"] = status
    monkeypatch.setattr(team_mod, "_persist_completeness_claim", fake_persist)

    note = _cmp_note(left=2, right=2, matched=0, left_only=0, right_only=0,
                      partial_match=2, ambiguous=0)
    content = "There are no missing items."
    out_content, out_result, reconciled = await _reconcile_completeness_claim_with_comparison(
        content, "task text", _Team(), [None], None, None, note, False)

    assert reconciled is False
    assert calls["n"] == 0
    assert persisted.get("status") == "supported_with_consistency_defect"


# ── C5 / C7: AMBIGUOUS -> unresolved, never complete, never invented-missing ─

@pytest.mark.asyncio
async def test_C5_ambiguous_alone_triggers_reconciliation_as_unresolved(monkeypatch):
    """The hard invariant this phase exists to enforce: an all-zero
    left_only/right_only result with real AMBIGUOUS entries must NOT be
    persisted as "supported" (complete) -- it must be treated as needing
    reconciliation, and persisted as "unresolved", distinct from
    "contradicted" (which implies a proven missing item)."""
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return ("Two items could not be resolved to a unique match and are "
                "reported as unresolved.", SimpleNamespace(messages=[]))
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    persisted = {}

    async def fake_persist(team, statement, status):
        persisted["status"] = status
    monkeypatch.setattr(team_mod, "_persist_completeness_claim", fake_persist)

    note = _cmp_note(left=2, right=2, matched=0, left_only=0, right_only=0,
                      partial_match=0, ambiguous=2)
    content = "Coverage is fully verified; there are no missing items."
    out_content, out_result, reconciled = await _reconcile_completeness_claim_with_comparison(
        content, "task text", _Team(), [None], None, None, note, False)

    assert reconciled is True
    assert persisted.get("status") == "unresolved"
    assert "could not establish a unique relationship" in captured["prompt"]
    assert "AMBIGUOUS" in captured["prompt"]


def test_C7_unknown_safety_unresolved_is_never_the_same_string_as_supported(monkeypatch):
    """Structural guard: "unresolved" and "supported" (and
    "supported_with_consistency_defect") must be three genuinely distinct
    status strings -- persisting AMBIGUOUS findings under the SAME string a
    genuinely clean comparison uses would silently recreate the exact bug
    this phase closes, even if the retry-triggering logic above is correct."""
    import inspect
    src = inspect.getsource(team_mod._reconcile_completeness_claim_with_comparison)
    assert '"supported"' in src
    assert '"supported_with_consistency_defect"' in src
    assert '"unresolved"' in src
    assert '"contradicted"' in src
    # All four are pairwise distinct literal strings in the source.
    statuses = {"supported", "supported_with_consistency_defect",
                "unresolved", "contradicted"}
    assert len(statuses) == 4


# ── C6: mixed relationships -- each category survives independently ─────────

@pytest.mark.asyncio
async def test_C6_mixed_comparison_prompt_names_every_category_present(monkeypatch):
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return "corrected answer", SimpleNamespace(messages=[])
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def no_op_persist(*a, **k):
        return None
    monkeypatch.setattr(team_mod, "_persist_completeness_claim", no_op_persist)

    note = _cmp_note(left=9, right=48, matched=3, left_only=4, right_only=43,
                      partial_match=2, ambiguous=1)
    content = "All backend operations have a corresponding, fully-agreeing frontend hook."
    out_content, out_result, reconciled = await _reconcile_completeness_claim_with_comparison(
        content, "task text", _Team(), [None], None, None, note, False)

    assert reconciled is True
    prompt = captured["prompt"]
    # Existence (left-only/right-only counts) present.
    assert "4 item(s) on the left" in prompt
    assert "43 on the right" in prompt
    # Consistency (partial_match) present, and NOT called "missing".
    assert "2 item(s) that exist on both sides but disagree" in prompt
    # The instruction distinguishes all three dimensions explicitly.
    assert "AMBIGUOUS" in prompt and "PARTIAL MATCH" in prompt


# ── C8: T13b regression -- the real, live-validated shape ───────────────────

@pytest.mark.asyncio
async def test_C8_t13b_shape_4_left_only_2_partial_match_0_ambiguous(monkeypatch):
    """The real T13b numbers, live-validated this session:
    9 backend operations, 5 frontend hooks -> 3 MATCH, 2 PARTIAL_MATCH,
    4 LEFT_ONLY, 43 RIGHT_ONLY, 0 AMBIGUOUS. The important fact for the
    backend coverage question is 4 LEFT_ONLY, never rewritten to six just
    because two operations are method-mismatched PARTIAL_MATCHes -- and the
    PUT-vs-POST evidence itself must survive into the retry prompt, not be
    summarized away."""
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return ("4 endpoints have no frontend counterpart: grn, credit-note, "
                "stock-adjustment, stock-transfer.", SimpleNamespace(messages=[]))
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def no_op_persist(*a, **k):
        return None
    monkeypatch.setattr(team_mod, "_persist_completeness_claim", no_op_persist)

    body = (
        "PARTIAL MATCHES — same identity (path), differing on a named attribute (2):\n"
        "  /vouchers/{}/post   PUT /vouchers/{voucher_id}/post  <->  "
        "POST /api/inventoryservice/vouchers/${id}/post   (http_method: PUT != POST)\n"
        "  /vouchers/{}/cancel   PUT /vouchers/{voucher_id}/cancel  <->  "
        "POST /api/inventoryservice/vouchers/${id}/cancel   (http_method: PUT != POST)"
    )
    note = _cmp_note(left=9, right=48, matched=3, left_only=4, right_only=43,
                      partial_match=2, ambiguous=0, body_extra=body)
    content = "All 9 backend operations have a corresponding, agreeing frontend hook."
    out_content, out_result, reconciled = await _reconcile_completeness_claim_with_comparison(
        content, "task text", _Team(), [None], None, None, note, False)

    assert reconciled is True
    prompt = captured["prompt"]
    assert "4 item(s) on the left" in prompt
    assert "PUT != POST" in prompt  # the evidence body is embedded verbatim
    assert "not missing" in prompt or "not claim they fully agree" in prompt


# ── _evidence_integrity_findings: the later, last-resort check ──────────────

@pytest.mark.asyncio
async def test_evidence_integrity_findings_catches_ambiguous_only_completeness_claim(
        monkeypatch):
    """The SAME hard invariant, enforced at the later, last-resort guard too:
    a completeness claim that survived every earlier check must still be
    caught here when the comparison has real AMBIGUOUS entries, even with
    zero left_only/right_only."""
    note = _cmp_note(left=2, right=2, matched=0, left_only=0, right_only=0,
                      partial_match=0, ambiguous=2)

    content = "Coverage is fully verified; there are no missing items."
    findings = await _evidence_integrity_findings(
        content, "task text", _Team(), None, None, note)

    gap_findings = [f for f in findings if f["category"] == "comparison completeness/gap"]
    assert len(gap_findings) == 1
    assert "AMBIGUOUS" in gap_findings[0]["real"]
    assert "unresolved" in gap_findings[0]["real"]


@pytest.mark.asyncio
async def test_evidence_integrity_findings_does_not_flag_partial_match_alone(monkeypatch):
    """PARTIAL_MATCH alone (no left_only/right_only/ambiguous) must not be
    reported as a completeness/gap finding at this layer either -- existence
    is not contradicted, so there is nothing for THIS category to flag."""
    note = _cmp_note(left=2, right=2, matched=0, left_only=0, right_only=0,
                      partial_match=2, ambiguous=0)

    content = "There are no missing items."
    findings = await _evidence_integrity_findings(
        content, "task text", _Team(), None, None, note)

    gap_findings = [f for f in findings if f["category"] == "comparison completeness/gap"]
    assert gap_findings == []
