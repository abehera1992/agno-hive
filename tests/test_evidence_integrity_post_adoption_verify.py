"""Phase Y -- post-correction verification guard.

Phase X forensics (real run, run_id=fd66e6422564, T13b rep 2): `_verify_claims`
flagged 25 bad claims on a draft; `_evidence_integrity_check` (Phase R) separately
found a comparison-completeness contradiction, ran its own one-shot reconciliation
retry, that retry made ZERO tool calls and produced an entirely different,
fabricated replacement answer, the recheck against comparison-category findings
came back clean (the new answer no longer contradicted THAT specific finding),
and the guard adopted and shipped it -- WITHOUT ever sending the new candidate
back through `_verify_claims`. The candidate never crossed the same verification
boundary every other path to a shipped answer crosses.

Phase Y closes this: after the reconciliation candidate resolves the comparison
contradiction it was re-asked about, it is now ALSO run through the existing
`_verify_claims` (the same function _verified_answer's own draft check already
uses) before being adopted. A candidate that fails this check is never shipped
clean -- it takes the SAME disposition this file already uses for "retry still
contradicts" (`_force_uncertainty_answer` + `_persist_evidence_integrity_trace`,
resolved=False), not a new policy.

Every test here is SIMULATED (constructed content/team state, monkeypatched
`_stream_team_run` and `_verify_claims` standing in for a live model and a live
hive-mcp connection) -- this project has no live model available in this
environment, matching test_evidence_integrity.py's own established convention.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import _EVIDENCE_INTEGRITY_FLAG, _evidence_integrity_check


def _run(coro):
    return asyncio.run(coro)


def _team_for_check(**kw):
    kw.setdefault("_read_state", {})
    kw.setdefault("_tool_evidence", [])
    return SimpleNamespace(**kw)


# The DB-row-count category is the simplest existing trigger for a contradiction
# (see test_evidence_integrity.py's own _team_for_check usage) -- reused here so
# these tests exercise the SAME entry path into _evidence_integrity_check, not a
# parallel one.
_CONTRADICTING_TEAM_KW = dict(_tool_evidence=[
    {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
_CONTRADICTING_CONTENT = "The table has 12,473 rows."


# ── 1. Phase X regression: the exact fabricated-content shape must now cross
#      verify_claims before adoption ----------------------------------------

@pytest.mark.asyncio
async def test_fabricated_reconciliation_candidate_is_sent_through_verify_claims(monkeypatch):
    """Reproduces the real incident's shape: a reconciliation retry that resolves
    the comparison contradiction it was asked about (the table now says '0 rows')
    but whose new text ALSO contains claims a deterministic grep cannot find
    (mirroring the real run's invented endpoint/table/hook names). verify_claims
    must actually be invoked on this exact candidate, not skipped."""
    async def fake_stream(*a, **k):
        return ("The table has 0 rows. See `class VoucherSeriesConfigHistory"
                "(BaseModel)` for the schema."), object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    seen = {"content": None}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        seen["content"] = content
        return "verify_claims — 1 claim(s) could NOT be found", True, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(**_CONTRADICTING_TEAM_KW)
    result = await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    assert seen["content"] is not None, "verify_claims was never called on the candidate"
    assert "VoucherSeriesConfigHistory" in seen["content"]
    assert result != seen["content"]  # not shipped clean


# ── 2. Bad candidate is not silently shipped ---------------------------------

@pytest.mark.asyncio
async def test_bad_candidate_is_forced_uncertain_not_shipped_clean(monkeypatch):
    async def fake_stream(*a, **k):
        return "The table has 0 rows, and also `fabricatedThing()`.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims — 1 claim(s) could NOT be found: fabricatedThing", True, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(**_CONTRADICTING_TEAM_KW)
    result = await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    assert "UNRESOLVED" in result
    assert "fabricated claim" in result.lower()
    # The original (pre-reconciliation) candidate's own wrong number must not
    # be what ships either -- the rewritten candidate is preserved, flagged.
    assert "fabricatedThing" in result or "verify_claims" in result


@pytest.mark.asyncio
async def test_bad_candidate_persists_as_contradicted(monkeypatch):
    async def fake_stream(*a, **k):
        return "The table has 0 rows, and also `fabricatedThing()`.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims — 1 claim(s) could NOT be found", True, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    persisted = []

    async def fake_persist_claim(run_context, statement, status, **kw):
        persisted.append(status)
    monkeypatch.setattr(team_mod.execution_store, "persist_claim", fake_persist_claim)

    team = _team_for_check(_run_context=object(), **_CONTRADICTING_TEAM_KW)
    await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    assert persisted == ["contradicted"]


# ── 3. Good (clean) candidate can still be adopted ---------------------------

@pytest.mark.asyncio
async def test_clean_candidate_is_still_adopted(monkeypatch):
    async def fake_stream(*a, **k):
        return "The table has 0 rows.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        calls["n"] += 1
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(**_CONTRADICTING_TEAM_KW)
    result = await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    assert result == "The table has 0 rows."
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_clean_candidate_persists_as_supported(monkeypatch):
    async def fake_stream(*a, **k):
        return "The table has 0 rows.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    persisted = []

    async def fake_persist_claim(run_context, statement, status, **kw):
        persisted.append(status)
    monkeypatch.setattr(team_mod.execution_store, "persist_claim", fake_persist_claim)

    team = _team_for_check(_run_context=object(), **_CONTRADICTING_TEAM_KW)
    await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    assert persisted == ["supported"]


# ── 4. Existing _verified_answer / no-hive-mcp-configured path unchanged -----

@pytest.mark.asyncio
async def test_no_hive_mcp_configured_skips_the_new_check_and_still_adopts(monkeypatch):
    """When hive_mcp_url/hive_mcp_tools are both None (as in
    test_evidence_integrity.py's own existing tests), _verify_claims's own
    early-return makes it a no-op (bad=False) -- the Y1 gate must not change
    behavior for this already-covered, already-passing case."""
    async def fake_stream(*a, **k):
        return "The table has 0 rows.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(**_CONTRADICTING_TEAM_KW)
    result = await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, None, None)
    assert result == "The table has 0 rows."


# ── 5. Boundedness: exactly one verify_claims call, no reconcile-verify loop --

@pytest.mark.asyncio
async def test_single_verify_claims_call_no_loop_on_bad_candidate(monkeypatch):
    stream_calls = {"n": 0}

    async def fake_stream(*a, **k):
        stream_calls["n"] += 1
        return "The table has 0 rows, and also `fabricatedThing()`.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    verify_calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        verify_calls["n"] += 1
        return "verify_claims — 1 claim(s) could NOT be found", True, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(**_CONTRADICTING_TEAM_KW)
    await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    # ONE reconciliation attempt (Rule 4's own bound, unchanged by this phase),
    # and exactly one verify_claims call on its output -- never a second retry
    # triggered by a bad verify_claims verdict.
    assert stream_calls["n"] == 1
    assert verify_calls["n"] == 1


@pytest.mark.asyncio
async def test_single_verify_claims_call_no_loop_on_clean_candidate(monkeypatch):
    stream_calls = {"n": 0}

    async def fake_stream(*a, **k):
        stream_calls["n"] += 1
        return "The table has 0 rows.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    verify_calls = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        verify_calls["n"] += 1
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(**_CONTRADICTING_TEAM_KW)
    await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    assert stream_calls["n"] == 1
    assert verify_calls["n"] == 1


# ── 6. Y2 (intentionally not implemented): a zero-tool-call reconciliation is
#       not specially blocked by tool-call count alone -- only by what the
#       candidate actually claims (verify_claims), matching the documented Y2
#       decision in swarm/team.py's own comment above the reconciliation prompt.

@pytest.mark.asyncio
async def test_zero_tool_call_reconciliation_with_a_clean_result_is_still_adopted(monkeypatch):
    """The real incident's retry made zero tool calls -- but a retry that makes
    zero tool calls and STILL produces a claim-free, grep-clean answer (e.g. it
    correctly reused evidence already in its own context) must not be rejected
    merely for having made no tool call. Y2 was evaluated and deliberately not
    implemented as a hard gate; this proves that decision holds in code, not
    only in the comment."""
    async def fake_stream(*a, **k):
        # No tool call recorded anywhere in this fake's own bookkeeping --
        # _stream_team_run's real return includes a run_output; this fake's
        # plain `object()` stands in for one with no tool-call trace, exactly
        # like the real incident.
        return "The table has 0 rows.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", fake_verify_claims)

    team = _team_for_check(**_CONTRADICTING_TEAM_KW)
    result = await _evidence_integrity_check(
        _CONTRADICTING_CONTENT, "task", team, "http://hive-mcp", object())

    assert result == "The table has 0 rows."
