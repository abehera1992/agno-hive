"""Phase Z32 (2026-09-29) -- Z16's repetition-loop guard correctly truncates a
runaway generation, but the fact that it did so (`repetition_stopped`, a purely
local variable in run_task_async's own inline copy of the Z16 logic) was computed
and then thrown away -- nothing downstream ever learned the surviving content was
cut off mid-answer rather than completed normally.

Live-confirmed by Z31: a T13b run correctly delegated all three research targets,
correctly synthesized every finding, and was cut off by the repetition guard while
writing the frontend-hooks section -- 9,049 chars in, before the task's own
"identify anything present in the backend with no frontend counterpart" conclusion
was ever written. The up-front `_verify_claims` call inside `_verified_answer` then
submitted the exact same (truncated) text hive-mcp's own dedup cache had already
seen once this run (the model's own voluntary verify_claims tool call during
generation, before the cutoff) and got back a prose "STOPPED: already checked"
result with none of the structured NOT FOUND/BAD lines the citation-correction
guard's extractors look for -- no retry ever fired, and the incomplete draft
shipped with only a generic disclaimer.

Fix: `team._repetition_truncated` now carries the signal (set at the exact point
run_task_async's own Z16 copy already decided to truncate), and a new guard,
`_complete_repetition_truncated_answer`, runs FIRST in `_verified_answer` -- before
the up-front verify_claims call -- giving the team exactly ONE bounded chance to
finish the existing draft using what it already gathered, not a re-run of the
research. Uses the SAME `_stream_team_run`/`_adopt_retry`/aggregate-one-retry-
budget machinery every other guard in this function already uses; Z16's own
truncation behavior and _stream_team_run's separate copy of it are both completely
unchanged.
"""
from types import SimpleNamespace

import pytest

from swarm import team


def _msgs(*items):
    return SimpleNamespace(messages=list(items))


def _tool_msg(name: str, content: str):
    return SimpleNamespace(role="tool", tool_name=name, content=content)


class _FakeTeam:
    """Same convention as test_team_write_claims.py's _FakeTeam."""

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


# ---- Case A -- ordinary (non-truncated) answer: guard is a complete no-op -------

@pytest.mark.asyncio
async def test_no_truncation_flag_is_a_pure_passthrough():
    fake_team = _FakeTeam(SimpleNamespace(content="irrelevant", messages=[]))
    content = "A complete, ordinary answer with a real conclusion."
    result = _msgs(_tool_msg("get_file_content", "some file"))

    out_content, out_result = await team._complete_repetition_truncated_answer(
        content, "describe the module", fake_team, [result], result, None)

    assert out_content == content
    assert out_result is result
    assert fake_team.prompts == []  # never even attempted a completion call


# ---- Case B -- repetition-truncated answer is recognized ------------------------

@pytest.mark.asyncio
async def test_truncation_flag_triggers_a_completion_attempt():
    fake_team = _FakeTeam(SimpleNamespace(
        content="...finished with the real conclusion.",
        messages=[_tool_msg("get_file_content", "x")],
    ))
    fake_team._repetition_truncated = True
    content = "Endpoints: A, B. Tables: C. Frontend hooks: D, E, and then it just..."
    result = _msgs()

    await team._complete_repetition_truncated_answer(
        content, "audit the module", fake_team, [result], result, None)

    assert len(fake_team.prompts) == 1
    prompt = fake_team.prompts[0]
    assert content in prompt  # the truncated draft is quoted, not discarded
    assert "do not re-research or re-delegate" in prompt.lower() or \
           "do not re-research" in prompt.lower()
    assert fake_team._repetition_truncated is False  # cleared immediately on entry


# ---- Case C -- bounded: at most one completion attempt --------------------------

@pytest.mark.asyncio
async def test_completion_is_attempted_at_most_once_per_call():
    fake_team = _FakeTeam(SimpleNamespace(content="finished.", messages=[]))
    fake_team._repetition_truncated = True
    content = "Truncated answer..."
    result = _msgs()

    # First call: fires normally.
    await team._complete_repetition_truncated_answer(
        content, "task", fake_team, [result], result, None)
    assert len(fake_team.prompts) == 1

    # Re-set the trigger flag (simulating some later, unrelated code re-setting it
    # within the SAME call) -- the one-shot flag must still block a second attempt.
    fake_team._repetition_truncated = True
    await team._complete_repetition_truncated_answer(
        content, "task", fake_team, [result], result, None)
    assert len(fake_team.prompts) == 1  # still just the one -- no second completion call


# ---- Case D -- successful completion: the conclusion reaches the final answer ---

@pytest.mark.asyncio
async def test_successful_completion_is_adopted_and_contains_the_conclusion():
    completed_text = (
        "Endpoints: A, B. Tables: C. Frontend hooks: D, E, F, G, H. "
        "Gap analysis: B and C have no frontend counterpart."
    )
    fake_team = _FakeTeam(SimpleNamespace(
        content=completed_text,
        messages=[_tool_msg("get_file_content", "x")],  # at least as grounded as the draft
    ))
    fake_team._repetition_truncated = True
    content = "Endpoints: A, B. Tables: C. Frontend hooks: D, E, and then it just..."
    result = _msgs()  # original coordinator result: zero direct reads (all delegated)

    out_content, out_result = await team._complete_repetition_truncated_answer(
        content, "audit the module", fake_team, [result], result, None)

    assert out_content == completed_text
    assert "Gap analysis" in out_content
    assert "INCOMPLETE ANSWER" not in out_content  # no disclosure needed -- it completed


# ---- Case E -- completion cannot safely occur: honest disclosure ----------------

@pytest.mark.asyncio
async def test_completion_returning_nothing_keeps_the_draft_with_a_disclosure():
    """_FakeTeam(None) reproduces _stream_team_run's own "(no response)" sentinel
    (used throughout this file whenever a generation yields zero content) -- this
    guard must recognise that literal, established sentinel as "nothing usable",
    the same way it already recognises a falsy/empty completion."""
    fake_team = _FakeTeam(None)  # the completion attempt produces nothing at all
    fake_team._repetition_truncated = True
    content = "Truncated answer with no conclusion..."
    result = _msgs()

    out_content, out_result = await team._complete_repetition_truncated_answer(
        content, "audit the module", fake_team, [result], result, None)

    assert out_content.startswith(content)
    assert "INCOMPLETE ANSWER" in out_content
    assert out_result is result


@pytest.mark.asyncio
async def test_less_grounded_completion_is_rejected_with_a_disclosure():
    """The completion attempt itself came back with LESS evidence than the
    original draft (e.g. it hallucinated a short "finished" answer without ever
    having the delegated findings) -- _adopt_retry/_more_grounded (unmodified)
    must still reject it, same as any other guard's retry. The retry's own result
    carries a real, recognised-but-non-read tool call (not an empty message list,
    which _count_read_calls treats as "undeterminable" and therefore always
    adopts) so the comparison is genuinely determinable on both sides."""
    fake_team = _FakeTeam(SimpleNamespace(
        content="Done.", messages=[_tool_msg("update_session_state", "noted")]))
    fake_team._repetition_truncated = True
    content = "Truncated answer..."
    # Original draft's own result shows real evidence gathered (read calls present).
    result = _msgs(
        _tool_msg("get_file_content", "a"),
        _tool_msg("get_file_content", "b"),
        _tool_msg("get_file_content", "c"),
    )

    out_content, out_result = await team._complete_repetition_truncated_answer(
        content, "audit the module", fake_team, [result], result, None)

    assert out_content.startswith(content)
    assert "INCOMPLETE ANSWER" in out_content


# ---- Case F -- ordinary already-verified duplicate is untouched by this guard ---

@pytest.mark.asyncio
async def test_ordinary_verify_claims_dedup_path_is_unaffected_when_not_truncated():
    """No _repetition_truncated flag at all (the overwhelmingly common case) --
    _verified_answer's existing verify_claims/dedup behavior must be byte-for-byte
    unchanged, proving this guard does not globally weaken deduplication."""
    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools=None):
        return "VERDICT: every checked claim exists in the project.", False, False

    import unittest.mock as mock
    with mock.patch.object(team, "_verify_claims", fake_verify_claims):
        fake_team = _FakeTeam(SimpleNamespace(content="unused", messages=[]))
        content = "A complete, already-verified answer."

        out = await team._verified_answer(
            content, "describe the module", fake_team, "http://fake/mcp")

    assert out == content  # untouched -- no completion attempted, no retry consumed
    assert fake_team.prompts == []


# ---- Step 8 -- the full Z31 shape, end-to-end through _verified_answer ----------

@pytest.mark.asyncio
async def test_z31_shape_end_to_end_completion_reaches_verify_claims_before_dedup():
    """Reproduces the exact structure of the live Z31 incident: a truncated draft
    whose text was ALREADY submitted to verify_claims once this run (the dedup-
    collision precondition) is completed FIRST -- so the up-front verify_claims
    call inside _verified_answer only ever examines the finished text, never the
    truncated one, and the dedup collision never has anything to collide with.

    Demonstrates the OLD implementation's failure mode directly: WITHOUT the
    completion guard (simulated by never setting _repetition_truncated), the
    truncated text reaches verify_claims as-is."""
    checked_texts = []

    async def fake_verify_claims(content, hive_mcp_url, hive_mcp_tools=None):
        checked_texts.append(content)
        return "VERDICT: every checked claim exists in the project.", False, False

    completed_text = (
        "Endpoints: 9. Tables: 3. Frontend hooks: 5. "
        "Gap analysis: grn, credit-note, stock-adjustment, stock-transfer have no "
        "frontend counterpart."
    )
    truncated_text = "Endpoints: 9. Tables: 3. Frontend hooks: 5. Gap analysis: g"

    import unittest.mock as mock
    with mock.patch.object(team, "_verify_claims", fake_verify_claims):
        # NEW behavior: completion runs first, verify_claims only sees the finished text.
        fake_team = _FakeTeam(SimpleNamespace(
            content=completed_text, messages=[_tool_msg("get_file_content", "x")]))
        fake_team._repetition_truncated = True

        out = await team._verified_answer(
            truncated_text, "audit the vouchers module", fake_team, "http://fake/mcp")

    assert out == completed_text
    assert checked_texts == [completed_text]  # verify_claims never saw the truncated text
    assert truncated_text not in checked_texts

    # OLD behavior (no completion guard engaged, e.g. a normal completed answer):
    # verify_claims examines exactly what was handed to it, unchanged.
    checked_texts.clear()
    with mock.patch.object(team, "_verify_claims", fake_verify_claims):
        fake_team2 = _FakeTeam(SimpleNamespace(content="unused", messages=[]))
        out2 = await team._verified_answer(
            truncated_text, "audit the vouchers module", fake_team2, "http://fake/mcp")

    assert out2 == truncated_text
    assert checked_texts == [truncated_text]  # no flag set -- reaches verify_claims as-is
