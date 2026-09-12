"""Phase 16 (AGNOHive Reliability Program): relay-drop recovery experiment.

Phase 15's forensic trace found the relay-drop guard (severe named-item
drop: members collectively name >= _RELAY_DROP_MIN_ITEMS files, answer keeps
<= _RELAY_DROP_MAX_KEPT_RATIO of them) fires on a DIFFERENT signal than
Phase 14 screw #1's _completion_claim_instead_of_answer (a named-filename
union vs. a raw char-count ratio), so a severe drop can reach the relay-drop
guard without ever tripping screw #1 -- confirmed live on a T12-shaped run
(53 named, 4 kept) whose final answer was not short by character count.
team._tool_evidence was confirmed still fully populated at that point.

This is a THIRD trigger sharing the same _grounded_retry_from_evidence core
as Phase 13's over-delivery mechanism and Phase 14 screw #1's under-delivery
mechanism -- same acceptance discipline (grounded via
_answer_supported_by_evidence, never on length or name count alone), same
shared one-retry-per-call budget, same once-per-run flag pattern.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _RELAY_DROP_RECONCILE_FLAG,
    _reconcile_relay_drop_with_tool_evidence,
    _tool_evidence_lines,
)

DROPPED_STUB = (
    "The inventory service has 16 routers, models for items and vouchers, "
    "and integrates with the business service."
)

TOOL_EVIDENCE = [
    {"name": "get_file_content", "agent": "Researcher",
     "preview": "app.include_router(items_api.router) "
                "app.include_router(categories_api.router) "
                "app.include_router(vouchers_api.router)",
     "chars": 7725},
]

MISSING = ["items_api.py", "categories_api.py", "vouchers_api.py"]

GROUNDED_RETRY = (
    "The routers are: items_api.py, categories_api.py, vouchers_api.py."
)
UNGROUNDED_RETRY = (
    "The routers are: items_api.py, categories_api.py, vouchers_api.py, "
    "plus purchase_order_api.py and stock_transactions_api.py."
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


# ── trigger gating ───────────────────────────────────────────────────────


def test_synthesis_run_never_reconciles(monkeypatch):
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not retry on a synthesis run")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "describe the inventory service", _grounded_team(), [None],
        None, None, _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), True,
        MISSING))
    assert reconciled is False
    assert called["n"] == 0
    assert content == DROPPED_STUB


def test_no_tool_evidence_is_a_pure_skip():
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", _grounded_team(), [None], None, None, [],
        False, MISSING))
    assert reconciled is False
    assert content == DROPPED_STUB


def test_no_missing_items_is_a_pure_skip():
    """The relay-drop guard's own trigger already requires a non-empty
    `missing` list; this pins that the reconciliation function independently
    refuses to fire without one too, rather than trusting the caller."""
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", _grounded_team(), [None], None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), False, []))
    assert reconciled is False
    assert content == DROPPED_STUB


def test_budget_already_spent_by_an_earlier_guard_is_respected(monkeypatch):
    """Shares the SAME one-retry-per-call budget as its two siblings -- this
    is a third TRIGGER, not a third retry opportunity."""
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not spend a second retry")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None, SimpleNamespace()]  # a prior guard already retried once
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", _grounded_team(), all_results, None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), False, MISSING))
    assert reconciled is False
    assert called["n"] == 0
    assert content == DROPPED_STUB


def test_flag_already_set_prevents_a_second_fire_on_the_same_team(monkeypatch):
    async def fake_stream(*a, **k):
        raise AssertionError("must not retry once the flag is already set")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    setattr(team, _RELAY_DROP_RECONCILE_FLAG, True)
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), False, MISSING))
    assert reconciled is False


def test_relay_drop_flag_is_independent_of_its_two_siblings():
    from swarm.team import _EVIDENCE_RECONCILE_FLAG, _UNDER_DELIVERY_RECONCILE_FLAG
    assert len({_EVIDENCE_RECONCILE_FLAG, _UNDER_DELIVERY_RECONCILE_FLAG,
                _RELAY_DROP_RECONCILE_FLAG}) == 3


# ── retry outcomes ───────────────────────────────────────────────────────


def test_severe_relay_drop_is_recovered_with_the_missing_items_restored(monkeypatch):
    """The T12-shaped case this screw targets: a long, plausible-looking
    answer that dropped almost all of the 3+ named files; the retry, given
    the specific missing names plus the raw tool evidence, restores them and
    is adopted because it is genuinely grounded."""
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        captured["label"] = log_label
        return GROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    all_results = [None]
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "describe the inventory service's routers", team,
        all_results, None, None, _tool_evidence_lines(team), False, MISSING))

    assert reconciled is True
    assert content == GROUNDED_RETRY
    assert captured["label"] == "relay-drop-reconciliation"
    # The specific missing items reach the retry prompt, not a paraphrase.
    assert "items_api.py" in captured["prompt"]
    assert "categories_api.py" in captured["prompt"]
    assert "vouchers_api.py" in captured["prompt"]
    assert "do not add any" in captured["prompt"].lower()
    assert getattr(team, _RELAY_DROP_RECONCILE_FLAG, False) is True
    # Every one of the previously-missing items is now actually present.
    for item in MISSING:
        assert item in content


def test_retry_naming_more_missing_items_but_ungrounded_is_rejected(monkeypatch):
    """Falsification guard: a retry that mentions the missing items AND
    additional, unsupported ones must be rejected -- acceptance is gated on
    _answer_supported_by_evidence, not on how many of the requested missing
    names show up."""
    async def fake_stream(*a, **k):
        return UNGROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False, MISSING))
    assert reconciled is False
    assert content == DROPPED_STUB


def test_retry_returning_nothing_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        return "", SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False, MISSING))
    assert reconciled is False
    assert content == DROPPED_STUB


def test_retry_exception_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False, MISSING))
    assert reconciled is False
    assert content == DROPPED_STUB


def test_no_evidence_tokens_captured_never_adopts(monkeypatch):
    async def fake_stream(*a, **k):
        return GROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _Team(evidence=TOOL_EVIDENCE)  # no evidence_tokens
    content, result, reconciled = _run(_reconcile_relay_drop_with_tool_evidence(
        DROPPED_STUB, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False, MISSING))
    assert reconciled is False
    assert content == DROPPED_STUB
