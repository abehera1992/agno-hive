"""B: forward_member_answer is a runtime-recorded handoff.

Before, the tool returned the member's text and relied on the Coordinator to copy it into
its final answer -- the "game of telephone" it exists to prevent. Now the tool records the
exact text at forward time (team._forwarded_members), returns a short receipt, and the
runtime appends whatever the final answer does not already carry. Nothing here needs the
model to follow a syntax: a coordinator that retypes, paraphrases or forgets the forwarded
text still ships it.
"""
import inspect
from types import SimpleNamespace

import pytest

from swarm import team as team_mod
from swarm.team import (
    _BUDGET_EXHAUSTED_ANSWER, _looks_like_repetition_loop, _make_forward_member_answer,
    _strip_leaked_tool_tags, _with_forwarded_evidence, render_member_findings,
)

LIST_ANSWER = "\n".join(f"- API/svc/router/file_{i}.py" for i in range(24))


def _tool(answers, forwarded):
    return _make_forward_member_answer(answers, forwarded)


async def _forward(tool, member_id):
    return await tool.entrypoint(member_id=member_id)


# ── recording ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_forward_records_exact_text_and_returns_a_short_receipt():
    answers, forwarded = {"researcher": LIST_ANSWER}, {}
    receipt = await _forward(_tool(answers, forwarded), "Researcher")

    assert forwarded == {"researcher": LIST_ANSWER}
    assert LIST_ANSWER not in receipt and len(receipt) < 400
    assert "FORWARDED" in receipt and "researcher" in receipt


@pytest.mark.asyncio
async def test_text_is_captured_at_forward_time_not_at_answer_time():
    answers, forwarded = {"researcher": "first version"}, {}
    tool = _tool(answers, forwarded)
    await _forward(tool, "researcher")
    answers["researcher"] = "a later, different answer"
    assert forwarded["researcher"] == "first version"


@pytest.mark.asyncio
async def test_nothing_stored_records_nothing_and_says_so():
    # PHASE R (2026-09-24): reworded from "NOTHING TO FORWARD ... Delegate first, then
    # forward" to an explicit terminal-state message -- the empty-store case is a dead
    # end for THIS call, not an invitation to retry the same tool (see Q13/Q14's
    # 56-call forward_member_answer retry loop this rewording targets).
    forwarded = {}
    out = await _forward(_tool({}, forwarded), "researcher")
    assert forwarded == {} and out.startswith("NO RESULT FOR")
    assert "terminal state" in out


@pytest.mark.asyncio
async def test_without_a_forwarded_map_the_tool_behaves_as_before():
    out = await _forward(_make_forward_member_answer({"researcher": "text"}), "researcher")
    assert out == "text"


# ── runtime inclusion ────────────────────────────────────────────────────────

def _team(forwarded):
    return SimpleNamespace(_forwarded_members=forwarded)


def test_dropped_forwarded_text_is_appended_verbatim():
    out = _with_forwarded_evidence("Summary only.", _team({"researcher": LIST_ANSWER}))
    assert out.startswith("Summary only.")
    assert LIST_ANSWER in out


def test_paraphrase_does_not_hide_the_original():
    out = _with_forwarded_evidence("There are about two dozen router files.",
                                   _team({"researcher": LIST_ANSWER}))
    assert LIST_ANSWER in out and out.startswith("There are about two dozen")


def test_text_already_carried_is_never_duplicated():
    content = "Findings:\n" + LIST_ANSWER + "\n\nMy ordering."
    assert _with_forwarded_evidence(content, _team({"researcher": LIST_ANSWER})) == content


def test_whitespace_differences_do_not_cause_a_duplicate():
    reflowed = LIST_ANSWER.replace("\n", "\n\n").replace("- ", "-   ")
    content = "Findings:\n" + reflowed
    assert _with_forwarded_evidence(content, _team({"researcher": LIST_ANSWER})) == content


def test_only_the_missing_member_is_appended():
    content = "A:\n" + LIST_ANSWER
    out = _with_forwarded_evidence(
        content, _team({"researcher": LIST_ANSWER, "reviewer": "REVIEWER FINDINGS"}))
    assert out.count(LIST_ANSWER) == 1
    assert "REVIEWER FINDINGS" in out


def test_forwarded_members_appear_in_forward_order():
    out = _with_forwarded_evidence("x", _team({"second": "S2 text", "first": "F1 text"}))
    assert out.index("S2 text") < out.index("F1 text")


def test_nothing_forwarded_leaves_content_untouched():
    for team in (_team({}), _team(None), SimpleNamespace(), None):
        assert _with_forwarded_evidence("The answer.", team) == "The answer."


def test_unforwarded_member_answers_are_never_appended():
    team = SimpleNamespace(_forwarded_members={},
                           _member_results={"researcher": LIST_ANSWER})
    assert _with_forwarded_evidence("The answer.", team) == "The answer."


def test_empty_content_stays_empty_even_with_forwarded_evidence():
    """Appending to an empty answer would make a failed run look valid to the
    empty-answer banner and every `if not retried`; _recovered_member_findings covers it."""
    assert _with_forwarded_evidence("", _team({"researcher": LIST_ANSWER})) == ""
    assert _with_forwarded_evidence(None, _team({"researcher": LIST_ANSWER})) is None


def test_canned_budget_exhausted_answer_is_returned_exactly():
    out = _with_forwarded_evidence(_BUDGET_EXHAUSTED_ANSWER, _team({"researcher": LIST_ANSWER}))
    assert out == _BUDGET_EXHAUSTED_ANSWER
    assert out.strip() == _BUDGET_EXHAUSTED_ANSWER          # what _adopt_retry compares
    padded = "\n " + _BUDGET_EXHAUSTED_ANSWER + " \n"
    assert _with_forwarded_evidence(padded, _team({"researcher": LIST_ANSWER})) == padded


def test_content_that_is_only_leaked_tool_call_syntax_is_returned_unchanged():
    leaked = '<tool_call>{"name": "get_file_content", "arguments": {"path": "a.py"}}</tool_call>'
    assert not _strip_leaked_tool_tags(leaked).strip()       # precondition: really syntax-only
    assert _with_forwarded_evidence(leaked, _team({"researcher": LIST_ANSWER})) == leaked


def test_whitespace_only_content_is_returned_unchanged():
    blank = "  \n\t "
    assert _with_forwarded_evidence(blank, _team({"researcher": LIST_ANSWER})) == blank


def test_a_quoted_tool_call_tag_that_is_not_a_call_is_still_a_real_answer():
    """_strip_leaked_tool_tags keeps a quotation of the tags, so this is real content and
    still receives the forwarded evidence."""
    content = "The parser looks for a <tool_call> tag in the reply."
    assert _strip_leaked_tool_tags(content).strip()
    assert LIST_ANSWER in _with_forwarded_evidence(content, _team({"researcher": LIST_ANSWER}))


def test_normal_content_with_one_forwarded_member_is_appended_as_before():
    out = _with_forwarded_evidence("Summary only.", _team({"researcher": LIST_ANSWER}))
    assert out.startswith("Summary only.\n\n---\n**FORWARDED FROM THE MEMBERS")
    assert out.endswith("### From researcher\n" + LIST_ANSWER.strip())


def test_normal_content_with_multiple_forwarded_members_is_appended_as_before():
    out = _with_forwarded_evidence(
        "Summary only.", _team({"researcher": LIST_ANSWER, "reviewer": "REVIEWER FINDINGS"}))
    assert out.startswith("Summary only.")
    assert out.count("**FORWARDED FROM THE MEMBERS") == 1
    assert out.index("### From researcher") < out.index("### From reviewer")
    assert LIST_ANSWER in out and out.endswith("REVIEWER FINDINGS")


# ── all three answer paths, and the build ────────────────────────────────────

def test_all_three_first_surviving_answer_sites_apply_the_forwarded_evidence():
    src = inspect.getsource(team_mod)
    assert src.count("_with_forwarded_evidence(_first_surviving_answer(") == 3
    assert src.count("_first_surviving_answer(") == src.count(
        "_with_forwarded_evidence(_first_surviving_answer(") + 1  # + the def itself


def test_evidence_is_appended_before_the_guards_run():
    """The relay-drop guard appends member findings when the answer lacks them; the
    forwarded text must already be in the answer by then or it would be added twice."""
    stream = inspect.getsource(team_mod.run_task_stream)
    assert stream.index("_with_forwarded_evidence(") < stream.index("_fill_count_markers(")


def test_build_team_binds_the_map_the_forward_tool_writes(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr(team_mod.team_config, "get_extra_tools",
                        lambda *a, **k: ["forward_member_answer"])
    t = team_mod._build_team(
        agent_specs=None, coordinator_model="m", coordinator_tools=[], mode="coordinate",
        mcp_list=[], instructions=[])
    assert t._forwarded_members == {}
    tool = next(x for x in t.tools if getattr(x, "name", "") == "forward_member_answer")
    t._member_results["researcher"] = LIST_ANSWER
    import asyncio
    asyncio.run(tool.entrypoint(member_id="researcher"))
    assert t._forwarded_members == {"researcher": LIST_ANSWER}
    assert LIST_ANSWER in _with_forwarded_evidence("summary", t)


def test_build_team_without_the_grant_never_appends_anything(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr(team_mod.team_config, "get_extra_tools", lambda *a, **k: [])
    t = team_mod._build_team(
        agent_specs=None, coordinator_model="m", coordinator_tools=[], mode="coordinate",
        mcp_list=[], instructions=[])
    t._member_results["researcher"] = LIST_ANSWER
    assert _with_forwarded_evidence("summary", t) == "summary"


# ── preserved behaviour ──────────────────────────────────────────────────────

def test_kill_recovery_still_renders_member_results_unchanged():
    out = render_member_findings({"researcher": LIST_ANSWER})
    assert LIST_ANSWER in out and "WHAT THE MEMBERS ACTUALLY REPORTED" in out


def test_repetition_detector_still_catches_the_coordinators_own_repetition():
    para = ("The inventory service exposes routers for items, categories, hsn codes and "
            "parties, each mounted under its own prefix in main.py. ") * 3
    assert _looks_like_repetition_loop(para, para) is True
    assert _looks_like_repetition_loop(
        "A completely different closing paragraph about ordering.", para) is False


def test_forwarded_evidence_is_added_after_streaming_not_fed_to_the_detector():
    stream = inspect.getsource(team_mod.run_task_stream)
    loop_end = stream.index('accumulated = "".join(full_content)')
    assert "_looks_like_repetition_loop" not in stream[loop_end:stream.index("_fill_count_markers(")]
