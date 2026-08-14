"""Tests for _ends_with_unfinished_intent (swarm/team.py) -- a durable fix for a
6th, distinct failure mode confirmed live 2026-08-14: on a genuinely FRESH,
un-chained session (all prior sessions had just been explicitly deleted), a run
returned a clean 200 OK in only 33s -- notion_search succeeded, notion_get_page
succeeded three separate times with escalating max_lines (default -> 1000 ->
2000 -> 5000, each call genuinely succeeding with no error), then the answer's
own narration stated an intent to pivot approaches ("Let me search for relevant
files and code patterns related to parties and inventory implementation.") and
the response simply ENDED there -- no search tool was ever actually called, no
real comparison was ever produced. Not a hang, not a crash, not a token-cap
truncation (the returned content was only ~1,400 chars) -- the coordinator
appears to have decided its turn was complete despite the task being nowhere
near done.

_ends_with_unfinished_intent distinguishes "the answer's own final words
describe a next action" from "the answer actually reached a conclusion," so
_verified_answer can catch this the same deterministic way it already catches
a false write claim or an unverified search claim.
"""
from types import SimpleNamespace

import pytest

from swarm import team
from swarm.team import _ends_with_unfinished_intent


# ---- pure function: _ends_with_unfinished_intent ----------------------------

def test_the_real_incidents_exact_ending_is_detected():
    content = (
        "I'm having trouble retrieving the full content of the Phase 1 "
        "requirements page. Let me try a different approach by searching for "
        "specific keywords related to the parties/inventory module "
        "implementation in the codebase to understand what's already covered "
        "and what's missing. Let me search for relevant files and code "
        "patterns related to parties and inventory implementation."
    )

    assert _ends_with_unfinished_intent(content) is True


def test_a_short_stated_next_step_with_nothing_after_it_is_detected():
    content = "Here is what I found so far. Let me check the migration file for the exact schema:"

    assert _ends_with_unfinished_intent(content) is True


def test_a_finished_answer_ending_on_a_conclusion_is_not_flagged():
    content = (
        "The sku_prefix column exists in models.py at line 129. The migration "
        "is complete and all Phase 1 requirements are covered by the current "
        "implementation."
    )

    assert _ends_with_unfinished_intent(content) is False


def test_a_completed_verification_using_past_tense_is_not_flagged():
    """A real risk case: 'I need to verify X' can appear even in a genuinely
    FINISHED answer if the rest of that same clause says it already happened.
    Must not false-positive just because the trigger words appear somewhere
    near the end."""
    content = (
        "I need to verify this against the actual schema, which I have "
        "already done above."
    )

    assert _ends_with_unfinished_intent(content) is False


def test_a_benign_closing_offer_is_not_flagged():
    content = (
        "All Phase 1 requirements are implemented and verified. Let me know "
        "if you'd like me to look into any other module next."
    )

    assert _ends_with_unfinished_intent(content) is False


def test_a_mid_answer_stated_step_that_is_actually_followed_through_is_not_flagged():
    content = (
        "Let me check the migration file: it confirms sku_prefix was added "
        "at line 20. All requirements are covered."
    )

    assert _ends_with_unfinished_intent(content) is False


def test_empty_content_is_not_flagged():
    assert _ends_with_unfinished_intent("") is False
    assert _ends_with_unfinished_intent(None) is False


# ---- _verified_answer's unfinished-intent guard -----------------------------

def _msgs(*items):
    return SimpleNamespace(messages=list(items))


def _tool_msg(name: str, content: str):
    return SimpleNamespace(role="tool", tool_name=name, content=content)


class _FakeTeam:
    """Same fixture shape as test_team_write_claims.py's own _FakeTeam."""

    def __init__(self, retry_result):
        self._retry_result = retry_result
        self.prompts = []

    def arun(self, prompt, stream=False, yield_run_output=False):
        self.prompts.append(prompt)
        if stream:
            return self._stream()
        return self._direct()

    async def _direct(self):
        return self._retry_result

    async def _stream(self):
        if self._retry_result is not None:
            yield self._retry_result


@pytest.mark.asyncio
async def test_verified_answer_retries_when_content_ends_mid_task():
    original_result = _msgs(_tool_msg("notion_get_page", "some partial page content"))
    content = (
        "Let me retrieve the Phase 1 requirements page. Now let me search for "
        "relevant files and code patterns related to parties and inventory "
        "implementation."
    )
    retry_result = SimpleNamespace(
        content=(
            "The sku_prefix column exists in models.py at line 129. All Phase "
            "1 requirements are covered by the current implementation."
        ),
        messages=[_tool_msg("get_file_content", "sku_prefix = Column(...)")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "compare phase 1 requirements", fake_team, None, result=original_result)

    assert len(fake_team.prompts) == 1  # it actually retried
    assert "never actually took it" in fake_team.prompts[0]
    assert out.startswith("The sku_prefix column exists")
    assert "INCOMPLETE" not in out  # retry finished cleanly, no disclaimer needed


@pytest.mark.asyncio
async def test_verified_answer_surfaces_disclaimer_when_retry_also_ends_mid_task():
    original_result = _msgs(_tool_msg("notion_get_page", "some partial page content"))
    content = "Let me search for relevant files related to parties and inventory implementation."
    retry_result = SimpleNamespace(
        content="Let me try a different approach and check the models file instead.",
        messages=[_tool_msg("notion_get_page", "some partial page content")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "compare phase 1 requirements", fake_team, None, result=original_result)

    assert len(fake_team.prompts) == 1  # bounded at one retry
    assert "INCOMPLETE" in out


@pytest.mark.asyncio
async def test_verified_answer_does_not_retry_a_genuinely_finished_answer():
    original_result = _msgs(_tool_msg("get_file_content", "sku_prefix = Column(...)"))
    content = "The sku_prefix column exists in models.py at line 129. All Phase 1 requirements are covered."
    fake_team = _FakeTeam(retry_result=None)

    out = await team._verified_answer(content, "compare phase 1 requirements", fake_team, None, result=original_result)

    assert fake_team.prompts == []  # nothing to retry
    assert out == content


@pytest.mark.asyncio
async def test_unfinished_intent_guard_runs_before_the_write_claim_guard():
    """Both guards could theoretically fire on the same draft -- the
    unfinished-intent check runs first (a task that never finished is more
    fundamental than a specific wrong claim within it) and consumes the shared
    retry budget, so the write-claim guard must not ALSO attempt a SECOND
    retry -- but, matching the same aggregate-budget pattern the existing
    guards already establish (see test_team_write_claims.py's own
    test_verified_answer_does_not_stack_a_second_retry_after_write_claim_guard_used_the_budget),
    it still surfaces ITS OWN disclaimer on the (still-wrong) retried content,
    just without spending a second retry to do it."""
    original_result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    content = (
        "The change has been applied via apply_diff to x.scss. Let me verify "
        "this looks correct by checking the file again."
    )
    retry_result = SimpleNamespace(
        content="The change has been applied via apply_diff to x.scss.",  # still a false write claim
        messages=[_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss.")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add a badge class", fake_team, None, result=original_result)

    assert len(fake_team.prompts) == 1  # only ONE retry ran, from the unfinished-intent guard
    assert "never actually took it" in fake_team.prompts[0]
    assert "already used by an earlier check" in out  # write-claim guard didn't retry again...
    assert "NOT applied" in out  # ...but still surfaced its own disclaimer, not silently dropped
