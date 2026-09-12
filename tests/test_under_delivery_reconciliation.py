"""Phase 14, screw #1 (AGNOHive Reliability Program): under-delivery recovery.

Phase 13's forensic execution-path investigation found that
_reconcile_thin_answer_with_tool_evidence -- the over-delivery recovery
mechanism -- never fired across the whole treatment battery, and that the
battery's actual dominant failure shape was the OPPOSITE polarity:
_completion_claim_instead_of_answer ("the reads happened, nothing was
invented, but the answer doesn't carry it") fired first and returned before
Phase 13's mechanism was ever reached (T13a: 532 chars against 16,214
gathered).

This is the mirror mechanism: _reconcile_under_delivery_with_tool_evidence,
sharing the exact same acceptance discipline (_grounded_retry_from_evidence,
gated on _answer_supported_by_evidence -- never on length, never on how many
names a retry mentions) as Phase 13's over-delivery case, wired into the
_completion_claim_instead_of_answer call site in _verified_answer.

These tests exercise the new function directly (mirroring
tests/test_evidence_reconciliation.py's own structure for the over-delivery
case) plus a few tests confirming the shared _grounded_retry_from_evidence
core still backs both mechanisms identically.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _UNDER_DELIVERY_RECONCILE_FLAG,
    _grounded_retry_from_evidence,
    _reconcile_under_delivery_with_tool_evidence,
    _tool_evidence_lines,
)

THIN_STUB = "The vouchers module is fully implemented across frontend, backend, and models."

TOOL_EVIDENCE = [
    {"name": "get_file_content", "agent": "Researcher",
     "preview": "@router.get(\"/vouchers\") @router.get(\"/vouchers/{voucher_id}\") "
                "@router.post(\"/vouchers\") @router.post(\"/vouchers/grn/{po_id}\") "
                "@router.post(\"/vouchers/credit-note/{invoice_id}\") "
                "@router.post(\"/vouchers/stock-adjustment\") "
                "@router.post(\"/vouchers/stock-transfer\")", "chars": 16214},
]

GROUNDED_RETRY = (
    "The endpoints are: /vouchers, /vouchers/{voucher_id}, "
    "/vouchers/grn/{po_id}, /vouchers/credit-note/{invoice_id}, "
    "/vouchers/stock-adjustment, /vouchers/stock-transfer."
)
UNGROUNDED_RETRY = (
    "The vouchers module additionally exposes /vouchers/bulk-import and "
    "/vouchers/export-csv, both fully implemented and production-ready."
)


def _run(coro):
    return asyncio.run(coro)


class _Team:
    def __init__(self, evidence=None, evidence_tokens=None):
        if evidence is not None:
            self._tool_evidence = evidence
        if evidence_tokens is not None:
            self._evidence_tokens = evidence_tokens


def _grounded_team():
    evidence_text = " ".join(item["preview"] for item in TOOL_EVIDENCE)
    return _Team(
        evidence=TOOL_EVIDENCE,
        evidence_tokens=team_mod._salient_tokens(evidence_text),
    )


# ── trigger gating (mirrors test_evidence_reconciliation.py's structure) ────


def test_synthesis_run_never_reconciles(monkeypatch):
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not retry on a synthesis run")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "audit the vouchers module", _grounded_team(), [None], None,
        None, _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), True))
    assert reconciled is False
    assert called["n"] == 0
    assert content == THIN_STUB


def test_no_tool_evidence_lines_is_a_pure_skip():
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "task text", _grounded_team(), [None], None, None, [], False))
    assert reconciled is False
    assert content == THIN_STUB


def test_budget_already_spent_by_an_earlier_guard_is_respected(monkeypatch):
    """Shares the SAME one-retry-per-call budget as every other reconciliation
    in this file -- an earlier guard (e.g. _answer_outruns_evidence) having
    already retried once this call must block this one too."""
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not spend a second retry")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None, SimpleNamespace()]  # a prior guard already retried once
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "task text", _grounded_team(), all_results, None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), False))
    assert reconciled is False
    assert called["n"] == 0
    assert content == THIN_STUB


def test_flag_already_set_prevents_a_second_fire_on_the_same_team(monkeypatch):
    async def fake_stream(*a, **k):
        raise AssertionError("must not retry once the flag is already set")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    setattr(team, _UNDER_DELIVERY_RECONCILE_FLAG, True)
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), False))
    assert reconciled is False


def test_the_two_reconciliation_flags_are_independent():
    """Phase 13's own flag must not block screw #1's mechanism, and vice
    versa -- they guard two different, mutually-exclusive guards (a run can
    trip at most one of _answer_outruns_evidence-family vs
    _completion_claim_instead_of_answer per call), but nothing should
    accidentally couple them."""
    from swarm.team import _EVIDENCE_RECONCILE_FLAG
    assert _EVIDENCE_RECONCILE_FLAG != _UNDER_DELIVERY_RECONCILE_FLAG


# ── retry outcomes ───────────────────────────────────────────────────────


def test_severe_member_compression_case_is_recovered(monkeypatch):
    """The T13a shape this screw targets: severe compression (16,214 gathered
    vs. a short stub), real tool evidence available, retry answers correctly
    from it and is adopted."""
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        captured["label"] = log_label
        return GROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    all_results = [None]
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "Audit the vouchers module: list its endpoints...", team,
        all_results, None, None, _tool_evidence_lines(team), False))

    assert reconciled is True
    assert content == GROUNDED_RETRY
    assert captured["label"] == "under-delivery-reconciliation"
    assert "vouchers/grn/{po_id}" in captured["prompt"]
    assert "do not add any fact" in captured["prompt"].lower()
    assert getattr(team, _UNDER_DELIVERY_RECONCILE_FLAG, False) is True


def test_ungrounded_recovery_is_rejected_not_adopted_for_naming_more_things(monkeypatch):
    """Requirement I: a retry must not be accepted merely because it mentions
    MORE names/facts than the stub it replaces -- only because it is grounded.
    This retry invents two endpoints that were never in the evidence."""
    async def fake_stream(*a, **k):
        return UNGROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_STUB


def test_retry_returning_nothing_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        return "", SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_STUB


def test_retry_exception_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_STUB


def test_no_evidence_tokens_captured_never_adopts(monkeypatch):
    """_answer_supported_by_evidence is conservative-by-construction (empty
    token set -> False regardless of content); this mechanism inherits that
    exactly, same as Phase 13's over-delivery case."""
    async def fake_stream(*a, **k):
        return GROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _Team(evidence=TOOL_EVIDENCE)  # no evidence_tokens
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        THIN_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_STUB


# ── the shared core, exercised directly (used by both mechanisms) ──────────


def test_grounded_retry_from_evidence_rejects_a_retry_that_is_merely_longer(monkeypatch):
    """Requirement H: length alone must never carry a retry -- this is the
    exact prior-art trap documented on _reconcile_thin_answer_with_tool_
    evidence's own docstring, now pinned against the shared helper directly."""
    longer_but_ungrounded = UNGROUNDED_RETRY * 5  # much longer, still invented

    async def fake_stream(*a, **k):
        return longer_but_ungrounded, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_grounded_retry_from_evidence(
        "test-label", "retry prompt", THIN_STUB, "task text", team, [None],
        None, None))
    assert reconciled is False
    assert content == THIN_STUB


def test_grounded_retry_from_evidence_accepts_a_grounded_shorter_retry(monkeypatch):
    """The mirror check: a SHORTER but fully-grounded retry must still be
    accepted -- acceptance is about truth, not size in either direction."""
    async def fake_stream(*a, **k):
        return "vouchers/grn/{po_id}", SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_grounded_retry_from_evidence(
        "test-label", "retry prompt", THIN_STUB, "task text", team, [None],
        None, None))
    assert reconciled is True
    assert content == "vouchers/grn/{po_id}"
