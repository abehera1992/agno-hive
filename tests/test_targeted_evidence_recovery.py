"""Phase 18 (AGNOHive Reliability Program): targeted evidence availability
for relay-drop recovery.

Phase 17's live validation found a genuine relay-drop fire (18 named / 4
kept) whose grounded retry was correctly REJECTED, and traced why: 2 of the
run's own large get_file_content results had already overflowed the
12-item team._tool_evidence cap before the retry ran, so the retry's
quotable evidence view was incomplete for exactly the kind of large, late
reads a long enumeration task produces.

Hypothesis under test: the retry failed because its evidence view was
incomplete, not because the retry/acceptance mechanism is defective.

Treatment: _dropped_evidence_lines_for_missing surfaces ONLY the entries in
team._tool_evidence_dropped (Phase 14, screw #3 -- already bounded at
_TOOL_EVIDENCE_DROPPED_MAX) whose salient tokens match one of the relay-drop
guard's own reported `missing` items. Phase 14's drop-telemetry record was
extended with a bounded preview (same ~200-char format as the retained
ledger) specifically so this selection has something to quote. The global
_TOOL_EVIDENCE_MAX_ITEMS cap is unchanged; acceptance is still gated solely
on _answer_supported_by_evidence, unchanged.

These tests exercise the new selector directly and confirm its integration
into _reconcile_relay_drop_with_tool_evidence, mirroring the existing
tests/test_relay_drop_reconciliation.py structure.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _RELAY_DROP_RECONCILE_FLAG,
    _dropped_evidence_lines_for_missing,
    _reconcile_relay_drop_with_tool_evidence,
    _tool_evidence_lines,
)


def _run(coro):
    return asyncio.run(coro)


class _Team:
    def __init__(self, evidence=None, evidence_tokens=None, dropped=None):
        if evidence is not None:
            self._tool_evidence = evidence
        if evidence_tokens is not None:
            self._evidence_tokens = evidence_tokens
        if dropped is not None:
            self._tool_evidence_dropped = dropped


RETAINED_EVIDENCE = [
    {"name": "get_file_content", "agent": "Researcher",
     "preview": "get_file_content returned items_api.py categories_api.py",
     "chars": 7725},
]

# Simulates the exact Phase 17 shape: a large get_file_content result for
# godowns_api.py that overflowed the 12-item cap before the retry ran.
DROPPED_EVIDENCE = [
    {"name": "get_file_content", "agent": "Reviewer",
     "preview": "get_file_content returned godowns_api.py with router tags Godowns",
     "chars": 9800,
     "salient_tokens": ["godowns_api.py", "router", "godowns"]},
    # An UNRELATED dropped item -- must never be pulled in for a `missing`
    # list that doesn't mention it.
    {"name": "search_files", "agent": "Researcher",
     "preview": "search_files found no matches for deprecated_helper.py",
     "chars": 120,
     "salient_tokens": ["deprecated_helper.py"]},
]

MISSING = ["godowns_api.py", "hsn_api.py"]  # hsn_api.py has no dropped match

GROUNDED_RETRY = "The routers are defined in items_api.py, categories_api.py, godowns_api.py."
UNGROUNDED_RETRY = "The routers are defined in items_api.py, godowns_api.py, and phantom_api.py."


def _grounded_team(dropped=DROPPED_EVIDENCE):
    evidence_text = " ".join(
        item["preview"] for item in RETAINED_EVIDENCE + (dropped or []))
    return _Team(
        evidence=RETAINED_EVIDENCE,
        evidence_tokens=team_mod._salient_tokens(evidence_text),
        dropped=dropped,
    )


# ── _dropped_evidence_lines_for_missing: the targeted selector itself ──────


def test_no_dropped_evidence_is_a_pure_skip():
    assert _dropped_evidence_lines_for_missing(_Team(), MISSING) == []


def test_no_missing_items_is_a_pure_skip():
    assert _dropped_evidence_lines_for_missing(
        _Team(dropped=DROPPED_EVIDENCE), []) == []


def test_relevant_dropped_evidence_reaches_the_selection():
    lines = _dropped_evidence_lines_for_missing(
        _Team(dropped=DROPPED_EVIDENCE), MISSING)
    assert len(lines) == 1
    assert "Godowns" in lines[0]
    assert "Reviewer" in lines[0]


def test_unrelated_dropped_evidence_is_not_indiscriminately_added():
    """The 'deprecated_helper.py' entry must never appear -- nothing in
    `missing` names it."""
    lines = _dropped_evidence_lines_for_missing(
        _Team(dropped=DROPPED_EVIDENCE), MISSING)
    assert not any("deprecated_helper" in l for l in lines)


def test_missing_item_with_no_dropped_match_yields_nothing_extra():
    """hsn_api.py is in `missing` but has no corresponding dropped entry --
    the selector must not fabricate or substitute anything for it."""
    lines = _dropped_evidence_lines_for_missing(
        _Team(dropped=DROPPED_EVIDENCE), ["hsn_api.py"])
    assert lines == []


# ── integration into _reconcile_relay_drop_with_tool_evidence ──────────────


def test_targeted_evidence_reaches_the_retry_prompt(monkeypatch):
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return GROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        "a long answer that dropped most named files", "describe the routers",
        team, [None], None, None, _tool_evidence_lines(team), False, MISSING))

    assert "Godowns" in captured["prompt"]  # the targeted, previously-capped evidence
    assert reconciled is True
    assert content == GROUNDED_RETRY


def test_acceptance_still_requires_answer_supported_by_evidence(monkeypatch):
    """Requirement: even WITH the targeted evidence supplied, a retry naming
    something ungrounded (phantom_api.router) is rejected -- acceptance
    discipline is unchanged by this screw."""
    async def fake_stream(*a, **k):
        return UNGROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        "a long answer that dropped most named files", "describe the routers",
        team, [None], None, None, _tool_evidence_lines(team), False, MISSING))
    assert reconciled is False
    assert content == "a long answer that dropped most named files"


def test_retry_remains_bounded_to_one_attempt(monkeypatch):
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not spend a second retry")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    all_results = [None, SimpleNamespace()]  # budget already spent this call
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        "draft", "task text", team, all_results, None, None,
        _tool_evidence_lines(team), False, MISSING))
    assert reconciled is False
    assert called["n"] == 0


def test_no_targeted_evidence_available_falls_back_to_retained_only(monkeypatch):
    """When nothing in team._tool_evidence_dropped matches `missing`, the
    retry still runs on the retained ledger alone -- unchanged from Phase 16
    behaviour."""
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return "items_api.router, categories_api.router.", SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team(dropped=[])
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        "draft", "task text", team, [None], None, None,
        _tool_evidence_lines(team), False, MISSING))
    assert "Godowns" not in captured["prompt"]
    assert reconciled is True
