"""Regression tests: the orchestrator must not accept a "file was changed" claim
unless a write tool call in that same run's trace actually succeeded.

Confirmed live 2026-08-05: a Coder called apply_diff() twice against the same
file, both failed with "old_string not found" (a malformed old_string built by
copying get_file_content's line-number prefix), then reported "The change has
been applied via apply_diff to parties.module.scss" anyway -- no .hive_proposed
file existed anywhere on disk, confirmed directly against the container
filesystem. The existing citation/fact groundedness checks in _verified_answer
had no way to catch this: the claim contained no fabricated file path or
symbol, just a false report of a successful write.
"""
from types import SimpleNamespace

import pytest

from swarm import team


def _msgs(*items):
    return SimpleNamespace(messages=list(items))


def _tool_msg(name: str, content: str):
    return SimpleNamespace(role="tool", tool_name=name, content=content)


class _FakeTeam:
    def __init__(self, retry_result):
        self._retry_result = retry_result
        self.prompts = []

    async def arun(self, prompt):
        self.prompts.append(prompt)
        return self._retry_result


# ---- _summarize_actual_writes -----------------------------------------------

def test_summarize_actual_writes_returns_empty_for_no_write_activity():
    result = _msgs(_tool_msg("get_file_content", "some file content"))
    assert team._summarize_actual_writes(result) == ""


def test_summarize_actual_writes_returns_empty_for_a_failed_write():
    result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    assert team._summarize_actual_writes(result) == ""


def test_summarize_actual_writes_lists_a_successful_staged_change():
    result = _msgs(_tool_msg("apply_diff", "review_pending: x.scss — this change is now staged."))
    out = team._summarize_actual_writes(result)
    assert "Actual file changes this run" in out
    assert "x.scss — review_pending" in out


def test_summarize_actual_writes_lists_a_new_file_write():
    result = _msgs(_tool_msg("write_file", "written: newpage.tsx"))
    out = team._summarize_actual_writes(result)
    assert "newpage.tsx — written" in out


def test_summarize_actual_writes_deduplicates_the_same_file():
    result = _msgs(
        _tool_msg("apply_diff", "review_pending: x.scss — this change is now staged."),
        _tool_msg("apply_diff", "review_pending: x.scss — this change is now staged."),
    )
    out = team._summarize_actual_writes(result)
    assert out.count("x.scss") == 1


def test_summarize_actual_writes_surfaces_selectors_touched():
    result = _msgs(_tool_msg(
        "apply_diff",
        "review_pending: x.module.scss — this change is now staged.\n"
        "Selectors touched: .statusBadge",
    ))
    out = team._summarize_actual_writes(result)
    assert "selectors: .statusBadge" in out


def test_summarize_actual_writes_surfaces_scope_creep_across_two_calls():
    """Confirmed live 2026-08-06: a lint-fix retry, asked to correct a namespace
    mismatch on .statusBadge, ALSO injected unrelated properties into an existing,
    already-correct .badgeBoth rule the task never named -- narrated as a fix that
    never actually applied to .badgeBoth. Two separate apply_diff calls to the SAME
    file, each touching a different selector, must both surface in the aggregate --
    a human must be able to see the full scope of what changed, not just the last
    call's slice of it."""
    result = _msgs(
        _tool_msg(
            "apply_diff",
            "review_pending: parties.module.scss — this change is now staged.\n"
            "Selectors touched: .badgeBoth",
        ),
        _tool_msg(
            "apply_diff",
            "review_pending: parties.module.scss — this change is now staged.\n"
            "Selectors touched: .statusBadge",
        ),
    )
    out = team._summarize_actual_writes(result)
    assert "selectors:" in out
    assert ".badgeBoth" in out
    assert ".statusBadge" in out


# ---- _count_successful_write_calls -----------------------------------------

def test_count_successful_write_calls_returns_minus_one_with_no_messages():
    result = SimpleNamespace(messages=[])
    assert team._count_successful_write_calls(result) == -1


def test_count_successful_write_calls_returns_minus_one_when_nothing_inspectable():
    result = _msgs(SimpleNamespace(role="assistant", content="hello"))
    assert team._count_successful_write_calls(result) == -1


def test_count_successful_write_calls_returns_zero_when_write_call_failed():
    result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    assert team._count_successful_write_calls(result) == 0


def test_count_successful_write_calls_counts_a_genuine_success():
    result = _msgs(_tool_msg("apply_diff", "review_pending: x.scss — this change is now staged."))
    assert team._count_successful_write_calls(result) == 1


def test_count_successful_write_calls_ignores_unrelated_read_tool_but_stays_determinable():
    result = _msgs(
        _tool_msg("get_file_content", "# x.scss — lines 0..10 of 10\n1\tfoo"),
        _tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."),
    )
    assert team._count_successful_write_calls(result) == 0  # determinable, zero successes


# ---- _verified_answer's write-claim guard -----------------------------------

@pytest.mark.asyncio
async def test_verified_answer_appends_disclaimer_when_retry_still_falsely_claims_success():
    original_result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    content = "The change has been applied via apply_diff to x.scss."

    retry_result = SimpleNamespace(
        content="The change has been successfully applied to x.scss.",  # retried, but STILL fabricated
        messages=[_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss.")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add a badge class", fake_team, None, result=original_result)

    assert len(fake_team.prompts) == 1  # it actually retried
    assert "apply_diff() or write_file()" in fake_team.prompts[0]
    assert "NOT applied" in out  # bounded at one retry -- surfaced, not hidden


@pytest.mark.asyncio
async def test_verified_answer_accepts_retry_that_honestly_reports_failure():
    """An honest 'it failed' retry is not a fabrication and must not be flagged --
    only a retry that STILL claims success without a successful write earns the
    disclaimer."""
    original_result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    content = "The change has been applied via apply_diff to x.scss."

    retry_result = SimpleNamespace(
        content="Reported the exact apply_diff failure instead of claiming success.",
        messages=[_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss.")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add a badge class", fake_team, None, result=original_result)

    assert len(fake_team.prompts) == 1
    assert out == "Reported the exact apply_diff failure instead of claiming success."
    assert "NOT applied" not in out


@pytest.mark.asyncio
async def test_verified_answer_accepts_retry_that_actually_succeeds():
    original_result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    content = "The change has been applied via apply_diff to x.scss."

    retry_result = SimpleNamespace(
        content="The statusBadge class has been added to x.scss.",
        messages=[_tool_msg("apply_diff", "review_pending: x.scss — this change is now staged.")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add a badge class", fake_team, None, result=original_result)

    assert out.startswith("The statusBadge class has been added to x.scss.")
    assert "NOT applied" not in out
    assert "Actual file changes this run" in out  # ground-truth appendix, from the retry's own trace
    assert "x.scss — review_pending" in out


@pytest.mark.asyncio
async def test_verified_answer_does_not_retry_when_write_already_succeeded():
    original_result = _msgs(_tool_msg("apply_diff", "review_pending: x.scss — this change is now staged."))
    content = "The change has been applied via apply_diff to x.scss."
    fake_team = _FakeTeam(retry_result=None)

    out = await team._verified_answer(content, "add a badge class", fake_team, None, result=original_result)

    assert fake_team.prompts == []  # no retry needed
    assert out.startswith(content)
    assert "Actual file changes this run" in out
    assert "x.scss — review_pending" in out


@pytest.mark.asyncio
async def test_verified_answer_does_not_retry_when_writes_undeterminable():
    original_result = _msgs(SimpleNamespace(role="assistant", content="hello"))
    content = "The change has been applied via apply_diff to x.scss."
    fake_team = _FakeTeam(retry_result=None)

    out = await team._verified_answer(content, "add a badge class", fake_team, None, result=original_result)

    assert fake_team.prompts == []  # -1 (undeterminable) must not force a retry
    assert out == content


@pytest.mark.asyncio
async def test_verified_answer_ignores_content_with_no_write_claim():
    original_result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    content = "I could not find a suitable place to add this class."
    fake_team = _FakeTeam(retry_result=None)

    out = await team._verified_answer(content, "add a badge class", fake_team, None, result=original_result)

    assert fake_team.prompts == []  # nothing claimed, nothing to retry
    assert out == content


@pytest.mark.asyncio
async def test_verified_answer_surfaces_a_real_write_the_narrative_never_mentions():
    """Confirmed live 2026-08-05: a Coder staged a genuinely correct .statusBadge
    insertion early in a run, then did a SECOND research pass, reconsidered its
    approach, and reported "No new statusBadge class is needed... the existing
    .badge class is sufficient" -- never mentioning or withdrawing the still-staged
    change. _CLAIMED_WRITE_RE has nothing to match here (the narrative doesn't claim
    a write happened, so the existing fabrication guard never fires) -- only the
    unconditional ground-truth appendix catches this direction."""
    original_result = _msgs(_tool_msg("apply_diff", "review_pending: parties.module.scss — this change is now staged."))
    content = "No new statusBadge class is needed. The existing .badge class is sufficient."
    fake_team = _FakeTeam(retry_result=None)

    out = await team._verified_answer(content, "add a status badge", fake_team, None, result=original_result)

    assert fake_team.prompts == []  # no write claimed, so no retry -- this isn't that guard
    assert out.startswith(content)
    assert "Actual file changes this run" in out
    assert "parties.module.scss — review_pending" in out


@pytest.mark.asyncio
async def test_verified_answer_retries_on_a_lint_violation_report(monkeypatch):
    """Confirmed live 2026-08-06: verify_claims' CONVENTIONS section (CODE_LINT_FORBID/
    REQUIRE, and the SCSS namespace-consistency check) uses its own "VIOLATION" prefix,
    which _claim_token was never taught to recognise. A report containing ONLY a
    NAMESPACE MISMATCH violation set bad=True correctly (verify_claims' own problem
    count includes lint findings), but missing_symbols and bad_citations both came back
    empty, so the function fell through to "not missing_symbols and not bad_citations"
    and shipped the unfixed staged file with no retry and no disclaimer -- exactly the
    category of bug the 2026-08-04 AMBIGUOUS/MISMATCH fix closed for citations, just
    never extended to CONVENTIONS."""
    canned_report = (
        "CONVENTIONS (1 violation(s)):\n"
        "  VIOLATION  NAMESPACE MISMATCH in x.module.scss: bare $gray-200 used, but "
        "this file already references it as index.$gray-200 elsewhere\n\n"
        "VERDICT: 1 claim(s) could NOT be found in the project."
    )
    call_count = {"n": 0}

    async def fake_verify_claims(content, hive_mcp_url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return canned_report, True
        return "VERDICT: every checked claim exists in the project.", False

    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    original_result = _msgs(_tool_msg("apply_diff", "review_pending: x.module.scss — this change is now staged."))
    content = "Added the statusBadge class."
    retry_result = SimpleNamespace(
        content="Fixed the namespace prefix in the statusBadge class.",
        messages=[_tool_msg("apply_diff", "review_pending: x.module.scss — this change is now staged.")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add a badge class", fake_team, "http://fake/mcp", result=original_result)

    assert len(fake_team.prompts) == 1  # it actually retried instead of silently shipping
    assert "apply_diff() again" in fake_team.prompts[0]
    assert "$gray-200" in fake_team.prompts[0]
    assert out.startswith("Fixed the namespace prefix")


@pytest.mark.asyncio
async def test_verified_answer_ground_truth_survives_a_retry_that_makes_no_new_write(monkeypatch):
    """Confirmed live 2026-08-06: a Coder staged a real (if namespace-wrong)
    .statusBadge in its FIRST attempt. A lint-violation retry then gave up honestly
    ("has not been successfully modified due to repeated apply_diff failures") --
    true of THAT retry's own trace, but the ground-truth appendix at the time only
    inspected the LAST result object, so it produced an empty appendix next to a
    claim that was, in fact, also wrong: a write really had happened earlier in the
    same run. The appendix must union write evidence across every attempt, not just
    the final one."""
    canned_report = (
        "CONVENTIONS (1 violation(s)):\n"
        "  VIOLATION  NAMESPACE MISMATCH in parties.module.scss: bare $accent-deep used, "
        "but this file already references it as index.$accent-deep elsewhere\n\n"
        "VERDICT: 1 claim(s) could NOT be found in the project."
    )

    async def fake_verify_claims(content, hive_mcp_url):
        return canned_report, True  # still bad after the retry too -- it gave up

    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    original_result = _msgs(_tool_msg("apply_diff", "review_pending: parties.module.scss — this change is now staged."))
    content = "Added the statusBadge class using $accent-deep."

    # The retry's OWN trace has NO write tool calls at all -- it just gave up.
    retry_result = SimpleNamespace(
        content="This file has not been successfully modified due to repeated apply_diff failures.",
        messages=[_tool_msg("get_file_content", "# parties.module.scss\n...")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add a badge class", fake_team, "http://fake/mcp", result=original_result)

    assert "Actual file changes this run" in out  # not silently dropped
    assert "parties.module.scss — review_pending" in out


# ---- Aggregate retry budget across all four guards (2026-08-10) ------------
# Each guard was individually bounded at one retry, but until this fix that
# boundedness was never enforced IN AGGREGATE: a draft tripping the write-claim
# guard, then having ITS retry's own content trip the verify_claims guard, could
# still trigger a SECOND full team.arun() -- confirmed live as 20+ minutes of
# activity after a caching-implementation answer's apply_diff() calls had already
# succeeded. len(all_results) > 1 (something already appended beyond the
# original) is the shared signal every guard now checks before attempting its
# own retry.

@pytest.mark.asyncio
async def test_verified_answer_does_not_stack_a_second_retry_after_write_claim_guard_used_the_budget(monkeypatch):
    canned_report = (
        "CONVENTIONS (1 violation(s)):\n"
        "  VIOLATION  NAMESPACE MISMATCH in plan_limits_api.py: bare REDIS_TTL used\n\n"
        "VERDICT: 1 claim(s) could NOT be found in the project."
    )

    async def fake_verify_claims(content, hive_mcp_url):
        return canned_report, True

    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    # Original: claims success but the write failed -- trips the write-claim guard (Block 1).
    original_result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in plan_limits_api.py."))
    content = "Redis caching has been added to plan_limits_api.py."

    # The write-claim guard's retry SUCCEEDS (a real write happened) -- Block 1 accepts it
    # and moves on with this as the new content/result, rather than returning immediately.
    # But verify_claims (mocked above) reports a lint violation on THIS retried content,
    # which would normally trigger Block 4's own retry -- the aggregate budget must stop it.
    retry_result = SimpleNamespace(
        content="Redis caching has been added to plan_limits_api.py using REDIS_TTL.",
        messages=[_tool_msg("apply_diff", "review_pending: plan_limits_api.py — this change is now staged.")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add caching", fake_team, "http://fake/mcp", result=original_result)

    assert len(fake_team.prompts) == 1  # only the write-claim guard's retry ran -- no second one
    assert "already used by an earlier check" in out
    assert "NAMESPACE MISMATCH" in out  # the lint problem is still surfaced, not silently dropped
    assert "plan_limits_api.py — review_pending" in out  # ground-truth appendix still present


@pytest.mark.asyncio
async def test_verified_answer_single_guard_still_retries_normally_when_budget_available(monkeypatch):
    """Regression guard for the fix above: a run that trips exactly ONE guard must
    still retry as before -- the aggregate budget must not turn into a blanket
    no-retry-ever regression."""
    async def fake_verify_claims(content, hive_mcp_url):
        return "VERDICT: every checked claim exists in the project.", False

    monkeypatch.setattr(team, "_verify_claims", fake_verify_claims)

    original_result = _msgs(_tool_msg("apply_diff", "apply_diff failed: old_string not found in x.scss."))
    content = "The change has been applied via apply_diff to x.scss."
    retry_result = SimpleNamespace(
        content="The statusBadge class has been added to x.scss.",
        messages=[_tool_msg("apply_diff", "review_pending: x.scss — this change is now staged.")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "add a badge class", fake_team, "http://fake/mcp", result=original_result)

    assert len(fake_team.prompts) == 1  # the one guard that fired did retry
    assert out.startswith("The statusBadge class has been added to x.scss.")
