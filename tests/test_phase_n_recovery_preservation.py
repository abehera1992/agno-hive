"""Phase N / Track A (2026-10-03): recovery adoption must be additive, never
destructive.

Phase M proved, via direct source reading and live T12/T13a reproduction, that
_complete_repetition_truncated_answer's "adopted" branches returned `completed`
(the recovery pass's own output) ALONE -- discarding `content` (the original,
already-produced pre-repeat draft Z16 specifically keeps rather than throwing
away) whenever `_adopt_retry` judged `completed` at least as grounded. Live cost
(T12): the original draft's own pre-repeat 8,784 chars were silently discarded
this way, and the final answer named 0 of 16 real routers and 0 of 31 real model
classes the member had already found and the draft may already have named.

The fix makes the "adopted" path concatenate `content` + `completed` instead of
replacing. These tests exercise that change directly against the real function
(no mocking of the concatenation logic itself) with a stubbed _stream_team_run,
matching this file's established pattern for testing async guard functions
without a live model call.
"""
import asyncio

import pytest

import swarm.team as team_mod
from swarm.team import _complete_repetition_truncated_answer, _TRUNCATED_ANSWER_NOTE


class _FakeTeam:
    def __init__(self, repetition_truncated=True, member_results=None):
        self._repetition_truncated = repetition_truncated
        self._member_results = member_results or {}


def _stub_stream_team_run(completed_text, *, nested_truncation=False, raises=None):
    async def _fake(team, prompt, log_label=None, liveness_path=None):
        if raises is not None:
            raise raises
        if nested_truncation:
            team._repetition_truncated = True
        return completed_text, {"fake_retry_result": True}
    return _fake


@pytest.fixture(autouse=True)
def _restore_stream_team_run():
    original = team_mod._stream_team_run
    yield
    team_mod._stream_team_run = original


def _run(coro):
    return asyncio.run(coro)


# ── 1. Original draft preserved after a clean, adopted recovery ────────────

def test_original_draft_preserved_after_recovery():
    team_mod._stream_team_run = _stub_stream_team_run(
        "the continuation, finishing the conclusion.")
    team = _FakeTeam()
    content, result = _run(_complete_repetition_truncated_answer(
        "the original pre-repeat draft, naming 16 real routers.",
        "task", team, [], {"orig": True}, None))
    assert "the original pre-repeat draft, naming 16 real routers." in content
    assert "the continuation, finishing the conclusion." in content
    assert _TRUNCATED_ANSWER_NOTE not in content


# ── 2. Empty original draft -- recovery result alone, no leading blank junk ─

def test_empty_original_draft():
    team_mod._stream_team_run = _stub_stream_team_run("recovery-only content.")
    team = _FakeTeam()
    content, result = _run(_complete_repetition_truncated_answer(
        "", "task", team, [], {"orig": True}, None))
    assert content == "recovery-only content."


# ── 3. Empty recovery result -- original draft kept, disclosed incomplete ──

def test_empty_recovery_keeps_original_draft():
    team_mod._stream_team_run = _stub_stream_team_run("")
    team = _FakeTeam()
    content, result = _run(_complete_repetition_truncated_answer(
        "original draft text.", "task", team, [], {"orig": True}, None))
    assert "original draft text." in content
    assert _TRUNCATED_ANSWER_NOTE in content


# ── 4. Recovery raises an exception -- original draft kept, never lost ─────

def test_recovery_exception_keeps_original_draft():
    team_mod._stream_team_run = _stub_stream_team_run(
        "unused", raises=RuntimeError("boom"))
    team = _FakeTeam()
    content, result = _run(_complete_repetition_truncated_answer(
        "original draft text, must survive.", "task", team, [], {"orig": True}, None))
    assert "original draft text, must survive." in content
    assert _TRUNCATED_ANSWER_NOTE in content


# ── 5. Overlapping/restated content: original still present (no destructive
#       loss), without this test asserting zero duplication is impossible ──

def test_overlapping_content_does_not_lose_original():
    restated = "original draft text, naming routers A, B, C."
    team_mod._stream_team_run = _stub_stream_team_run(restated)
    team = _FakeTeam()
    content, result = _run(_complete_repetition_truncated_answer(
        "original draft text, naming routers A, B, C.",
        "task", team, [], {"orig": True}, None))
    # The original is never silently dropped, even when the recovery restates it --
    # this is the one-call-bounded duplication the fix explicitly tolerates in
    # exchange for never losing content outright.
    assert content.count("original draft text, naming routers A, B, C.") >= 1
    assert "original draft text, naming routers A, B, C." in content


# ── 6. Recovery itself truncates (nested repetition) -- disclosed, original +
#       partial recovery both preserved ─────────────────────────────────────

def test_nested_recovery_truncation_preserves_both_and_discloses():
    team_mod._stream_team_run = _stub_stream_team_run(
        "partial recovery text that itself got cut off mid-",
        nested_truncation=True)
    team = _FakeTeam()
    content, result = _run(_complete_repetition_truncated_answer(
        "original draft text with real findings.",
        "task", team, [], {"orig": True}, None))
    assert "original draft text with real findings." in content
    assert "partial recovery text that itself got cut off mid-" in content
    assert _TRUNCATED_ANSWER_NOTE in content


# ── 7. Original evidence survives nested truncation even when original draft
#       is itself the ONLY place the evidence appears (recovery contributes
#       nothing new) ───────────────────────────────────────────────────────

def test_original_evidence_survives_when_recovery_contributes_nothing_new():
    team_mod._stream_team_run = _stub_stream_team_run(
        "I attempted to continue but found nothing further to add",
        nested_truncation=True)
    team = _FakeTeam()
    content, result = _run(_complete_repetition_truncated_answer(
        "16 real routers: admin_gst_api.py, categories_api.py, ...",
        "task", team, [], {"orig": True}, None))
    assert "16 real routers: admin_gst_api.py, categories_api.py, ..." in content


# ── 8. Incomplete disclosure remains visible in every truncated-outcome path ─

@pytest.mark.parametrize("scenario", ["empty_recovery", "exception", "nested"])
def test_incomplete_disclosure_always_visible(scenario):
    team = _FakeTeam()
    if scenario == "empty_recovery":
        team_mod._stream_team_run = _stub_stream_team_run("")
    elif scenario == "exception":
        team_mod._stream_team_run = _stub_stream_team_run("x", raises=RuntimeError("boom"))
    else:
        team_mod._stream_team_run = _stub_stream_team_run("partial", nested_truncation=True)
    content, result = _run(_complete_repetition_truncated_answer(
        "draft", "task", team, [], {"orig": True}, None))
    assert _TRUNCATED_ANSWER_NOTE in content


# ── 9. No regression to normal (non-repetition-truncated) execution ────────

def test_no_regression_when_not_repetition_truncated():
    """team._repetition_truncated is False at entry -- the function must be a
    pure no-op, exactly as before this change (this path is untouched by the
    fix: the early-return at the top of the function is unmodified)."""
    team = _FakeTeam(repetition_truncated=False)

    async def _should_not_be_called(*a, **kw):
        raise AssertionError("_stream_team_run must not be called when "
                              "team._repetition_truncated is False")
    team_mod._stream_team_run = _should_not_be_called

    content, result = _run(_complete_repetition_truncated_answer(
        "unchanged content", "task", team, [], {"orig": "unchanged"}, None))
    assert content == "unchanged content"
    assert result == {"orig": "unchanged"}
