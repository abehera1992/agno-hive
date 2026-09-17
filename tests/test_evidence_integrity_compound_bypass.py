"""Phase AC -- compound guard / multi-rewrite integrity.

Phases Y and Z each closed a SINGLE-guard verification bypass (Y:
_evidence_integrity_check's own "resolved" adoption path; Z: six
_adopt_retry sites inside _verified_answer reusing a stale verdict). Phase
AC's own forensic audit found a THIRD, narrower instance in the SAME
function Y already touched -- proving that fixing each guard in isolation
does not by itself prove the composition is safe, exactly the risk this
phase was built to check.

_evidence_integrity_check has three distinct _force_uncertainty_answer call
sites. Two of them (the reconciliation retry raising, or coming back empty)
wrap `content` -- the ORIGINAL text, already verify_claims-checked by
_verified_answer before this function was ever called. The third
(`recheck`, i.e. "the reconciliation retry STILL contradicts the SAME
evidence category it was re-asked about") wraps `retried` instead -- a
FRESH, never-verified candidate -- and _force_uncertainty_answer's own
docstring says it ships that candidate's FULL TEXT verbatim ("the reader
gets the full original text... Everything else below is the original
answer, unmodified"). Before this phase's fix, ANY fabricated symbol
`retried` introduced while failing to fix the ORIGINAL contradiction
shipped completely unchecked -- worse than the sibling "resolved" branch
just below it, which Phase Y already made call verify_claims before
adopting.

The fix merges a verify_claims check into the SAME disclosure this branch
already builds, exactly mirroring Phase Y's own shape (a
{claimed, category, real} finding dict appended to `recheck`) -- not a new
mechanism, not a new retry, not a second verifier.

Every test here is SIMULATED (monkeypatched _stream_team_run/_verify_claims
standing in for a live model and hive-mcp), matching this file's own
established convention (test_evidence_integrity.py).
"""
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import _evidence_integrity_check


def _team_for_check(**kw):
    kw.setdefault("_read_state", {})
    kw.setdefault("_tool_evidence", [])
    return SimpleNamespace(**kw)


# ── 1. The exact compound bypass, reproduced and fixed -----------------------

@pytest.mark.asyncio
async def test_a_retry_that_still_contradicts_AND_fabricates_is_flagged_for_both(monkeypatch):
    """Composition: verify(content) [upstream, already done] -> guard A
    (evidence-integrity) fires, retries -> retry STILL contradicts the DB
    count it was re-asked about AND introduces a fabricated symbol. Before
    the fix, only the DB-count contradiction was ever disclosed; the
    fabrication shipped as confident, unchecked fact inside the "original
    answer, unmodified" the UNRESOLVED banner claimed was safe."""
    async def fake_stream(*a, **k):
        # Still wrong on the SAME finding (500 rows, not 0) AND now also
        # names a symbol that does not exist anywhere in the repo.
        return ("The table has 500 rows. See `FooBarNonExistentClass` for "
                "the schema."), object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    verify_calls = {"n": 0, "seen": []}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        verify_calls["n"] += 1
        verify_calls["seen"].append(content)
        if "FooBarNonExistentClass" in content:
            return ("verify_claims — 1 claim(s) could NOT be found: "
                     "FooBarNonExistentClass"), True, False
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(
        content, "task", team, "http://fake-hive-mcp", object())

    # The ORIGINAL DB-count contradiction is still disclosed (unchanged behavior).
    assert "UNRESOLVED" in result
    assert "500" in result
    assert "0 rows" in result
    # The NEW fabrication is now ALSO disclosed, not silently shipped.
    assert "FooBarNonExistentClass" in result
    assert "could NOT be found" in result
    # verify_claims was actually invoked against the RETRIED candidate, not the
    # original draft -- proving the composition, not just the isolated guard.
    assert any("FooBarNonExistentClass" in c for c in verify_calls["seen"])


@pytest.mark.asyncio
async def test_a_retry_that_still_contradicts_but_is_otherwise_clean_is_unchanged(monkeypatch):
    """No false positive: when the retry's own text has nothing verify_claims
    would flag, the disclosure is exactly what it was before this phase --
    only the original contradiction, nothing invented."""
    async def fake_stream(*a, **k):
        return "The table has 500 rows.", object()  # still wrong, but not fabricated
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(
        content, "task", team, "http://fake-hive-mcp", object())

    assert "UNRESOLVED" in result
    assert "500" in result
    assert "could NOT be found" not in result  # no fabricated finding invented


# ── 2. Boundedness: no extra retries, exactly one extra verify_claims call ---

@pytest.mark.asyncio
async def test_still_contradicts_branch_costs_exactly_one_extra_verify_claims_call(monkeypatch):
    stream_calls = {"n": 0}

    async def fake_stream(*a, **k):
        stream_calls["n"] += 1
        return "The table has 500 rows.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    verify_calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        verify_calls["n"] += 1
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    await _evidence_integrity_check(
        content, "task", team, "http://fake-hive-mcp", object())

    assert stream_calls["n"] == 1  # ONE reconciliation attempt, unchanged (Rule 4)
    assert verify_calls["n"] == 1  # ONE extra check on the retried candidate, no loop


# ── 3. A different order: reconciliation triggered by a DIFFERENT category --
#      (the real run_id=fd66e6422564 incident shape -- comparison
#      completeness/gap, not DB row count) -- proving the fix is not
#      specific to one trigger's own code path.

@pytest.mark.asyncio
async def test_the_fix_generalizes_to_the_comparison_completeness_trigger(monkeypatch):
    """Same composition, different entry finding category -- the real
    incident's own shape (T13b, comparison completeness/gap), reproduced
    with a still-contradicting AND fabricating retry."""
    async def fake_computed_comparison(task, enumerations, hive_mcp_url,
                                        hive_mcp_tools, content, team=None):
        # Real _computed_comparison footnote shape -- _comparison_gap_counts
        # parses the literal "TOTALS: ..." line, not free prose.
        return ("**THE COMPARISON, COMPUTED**\n```\n"
                "TOTALS: left 9, right 48, matched 3, left-only 6, right-only 45.\n```")
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_computed_comparison)

    async def fake_stream(*a, **k):
        return ("All backend endpoints are covered. See "
                "`VoucherSeriesConfigHistory` for the schema."), object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        if "VoucherSeriesConfigHistory" in content:
            return ("verify_claims — 1 claim(s) could NOT be found: "
                     "VoucherSeriesConfigHistory"), True, False
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check()
    content = "All 9 backend endpoints are covered by frontend hooks."
    result = await _evidence_integrity_check(
        content, "task", team, "http://fake-hive-mcp", object())

    assert "UNRESOLVED" in result
    assert "comparison completeness" in result.lower() or "left-only" in result
    assert "VoucherSeriesConfigHistory" in result
    assert "could NOT be found" in result
