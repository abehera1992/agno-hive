"""Phase 13 (AGNOHive Reliability Program): evidence-preserving architecture.

Hypothesis under test: Hive's reliability is limited because authoritative
tool evidence is converted into probabilistic LLM prose and then transported
through member/Coordinator synthesis, instead of being preserved
independently and made available to downstream reasoning. This is the
smallest generic (not test-specific) architectural change addressing that:
when the existing thin-answer tool-evidence guard fires (see
_captured_tool_evidence, 2026-08-28), the Coordinator now gets ONE chance to
answer again FROM the run's own captured tool evidence -- quoted verbatim --
before the guard falls back to its pre-existing behaviour of merely
appending that evidence as a footnote for the reader to reconcile by hand.

These tests exercise _reconcile_thin_answer_with_tool_evidence directly, the
same way tests/test_comparison_reconciliation.py exercises Phase 2B's
_reconcile_completeness_claim_with_comparison -- this is deliberately the
same shape of mechanism (detect -> retry with evidence quoted verbatim ->
accept only if genuinely grounded -> otherwise fall back unchanged), applied
to a different guard.

Prior-art constraint this design was built to respect (see the function's
own docstring): a naive "write what your members actually reported" retry
was tried once already, for a different guard, and it fabricated a sprint
summary, seven work items, five owner names and two statistics rather than
admit it had nothing to add -- accepted only because it was longer. This
mechanism's acceptance gate is _answer_supported_by_evidence (a token-subset
check), never length or read count, specifically to not repeat that failure.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _EVIDENCE_RECONCILE_FLAG,
    _reconcile_thin_answer_with_tool_evidence,
    _tool_evidence_lines,
)

THIN_CONTENT = "OS: Ubuntu 22.04.4. Python: 3.11.6. Working dir: /home/ubuntu/ekam-app."

TOOL_EVIDENCE = [
    {"name": "get_env_info", "agent": "Executor",
     "preview": "OS: Linux 6.6.87.2-microsoft-standard-WSL2 (x86_64) Python: 3.12.14 "
                "at /usr/local/bin/python Project root: /project Current working "
                "directory: /app", "chars": 1547},
]

GROUNDED_RETRY = "OS: Linux WSL2. Python: 3.12.14. Current working directory: /app."
UNGROUNDED_RETRY = "OS: Ubuntu 24.04. Python: 3.9.0. Working dir: /srv/app."


def _run(coro):
    return asyncio.run(coro)


class _Team:
    """Bare stand-in, matching test_comparison_reconciliation.py's _Team:
    _run_read_count/_member_reads_delta degrade to -1/0 for an object with
    no _read_state, which _more_grounded then treats as 'could not tell' ->
    always adopt on the read-count axis (the evidence-support check is the
    real gate here, exercised separately)."""

    def __init__(self, evidence=None, evidence_tokens=None):
        if evidence is not None:
            self._tool_evidence = evidence
        if evidence_tokens is not None:
            self._evidence_tokens = evidence_tokens


def _grounded_team():
    # Tokens a real run's tools actually returned -- computed the same way
    # production does (team._evidence_tokens is built by _salient_tokens over
    # real tool output), so this fixture cannot drift from what the acceptance
    # gate actually checks. Matches GROUNDED_RETRY, not UNGROUNDED_RETRY,
    # exactly like _answer_supported_by_evidence's own docstring example
    # (T9's fabricated values contradicted get_env_info).
    evidence_text = " ".join(item["preview"] for item in TOOL_EVIDENCE)
    return _Team(
        evidence=TOOL_EVIDENCE,
        evidence_tokens=team_mod._salient_tokens(evidence_text),
    )


# ── _tool_evidence_lines ─────────────────────────────────────────────────


def test_tool_evidence_lines_empty_when_no_evidence_captured():
    assert _tool_evidence_lines(_Team()) == []


def test_tool_evidence_lines_renders_name_agent_chars_preview():
    lines = _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE))
    assert len(lines) == 1
    assert "get_env_info" in lines[0]
    assert "[Executor]" in lines[0]
    assert "1,547 chars" in lines[0]
    assert "Current working directory: /app" in lines[0]


# ── _reconcile_thin_answer_with_tool_evidence: trigger gating ───────────────


def test_synthesis_run_never_reconciles(monkeypatch):
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not retry on a synthesis run")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "task text", _grounded_team(), [None], None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), True))
    assert reconciled is False
    assert called["n"] == 0
    assert content == THIN_CONTENT


def test_no_tool_evidence_lines_is_a_pure_skip():
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "task text", _grounded_team(), [None], None, None, [], False))
    assert reconciled is False
    assert content == THIN_CONTENT


def test_budget_already_spent_by_an_earlier_guard_is_respected(monkeypatch):
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not spend a second retry")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None, SimpleNamespace()]  # a prior guard already retried once
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "task text", _grounded_team(), all_results, None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), False))
    assert reconciled is False
    assert called["n"] == 0
    assert content == THIN_CONTENT


def test_flag_already_set_prevents_a_second_fire_on_the_same_team(monkeypatch):
    async def fake_stream(*a, **k):
        raise AssertionError("must not retry once the flag is already set")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    setattr(team, _EVIDENCE_RECONCILE_FLAG, True)
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "task text", team, [None], None, None,
        _tool_evidence_lines(_Team(evidence=TOOL_EVIDENCE)), False))
    assert reconciled is False


# ── retry outcomes ───────────────────────────────────────────────────────


def test_grounded_retry_is_adopted_and_flag_is_set(monkeypatch):
    """The strong-support pattern Phase 13 is looking for: given the real
    captured tool evidence quoted verbatim, the retry answers correctly and
    is adopted as the final content."""
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        captured["label"] = log_label
        return GROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    all_results = [None]
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "what is the current working directory?", team, all_results,
        None, None, _tool_evidence_lines(team), False))

    assert reconciled is True
    assert content == GROUNDED_RETRY
    assert captured["label"] == "evidence-reconciliation"
    # The real tool output reaches the retry prompt verbatim, not a paraphrase.
    assert "Current working directory: /app" in captured["prompt"]
    assert "do not add any fact" in captured["prompt"].lower()
    assert getattr(team, _EVIDENCE_RECONCILE_FLAG, False) is True


def test_ungrounded_retry_is_rejected_not_adopted_for_being_longer(monkeypatch):
    """The exact failure this mechanism must not repeat: a retry that is
    different (and no shorter) but still not actually grounded in the quoted
    evidence must be rejected, not adopted merely because it changed."""
    async def fake_stream(*a, **k):
        return UNGROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "what is the current working directory?", team, [None], None,
        None, _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_CONTENT


def test_retry_returning_nothing_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        return "", SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_CONTENT


def test_retry_exception_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _grounded_team()
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_CONTENT


def test_no_evidence_tokens_captured_never_adopts(monkeypatch):
    """_answer_supported_by_evidence is conservative-by-construction: with no
    token set captured it returns False regardless of content. A team that
    has _tool_evidence (so the guard fires) but never populated
    _evidence_tokens must not adopt any retry."""
    async def fake_stream(*a, **k):
        return GROUNDED_RETRY, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _Team(evidence=TOOL_EVIDENCE)  # no evidence_tokens
    content, result, reconciled = _run(_reconcile_thin_answer_with_tool_evidence(
        THIN_CONTENT, "task text", team, [None], None, None,
        _tool_evidence_lines(team), False))
    assert reconciled is False
    assert content == THIN_CONTENT
