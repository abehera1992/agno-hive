"""Tests: a run that hit tool_call_limit must say so instead of answering as if clean.

This is the one failure in swarm/team.py that no tool hook can ever observe. Reading the
installed agno package directly (agno/models/base.py): once current_function_call_count
exceeds function_call_limit, agno appends create_tool_call_limit_error_result(fc) and
`continue`s, so the refused call never enters function_calls_to_run and NO tool event is
yielded for it. Every reinforcement this codebase has -- the duplicate-read stub, the
forced-answer nudge, the tool_choice="none" escalation, stub collapsing -- hangs off tool
hooks, which only fire for calls that actually run. Hence the standing note that this
"bypasses every one of this file's reinforcement hooks entirely".

What IS reachable is the refusal itself: agno records it as an ordinary tool-role message
with tool_call_error=True, readable after the fact even though it was never announced.

Production evidence (30-day log review, 2026-08-20): one run spent its remaining turns
narrating "I'm encountering a persistent tool call limit that's preventing me from
retrieving the utility_ai_client file..." repeatedly -- agno had already said "Don't try
to execute it again" -- until the repetition detector killed it. Another ended with
RunCompleted content "No context retrieved. Tool call limits exceeded." Both answered
from evidence they were refused, and nothing said so.
"""
from types import SimpleNamespace

from swarm.team import _tools_refused_for_limit


def _limit_msg(tool_name: str):
    """Exactly agno's own shape for a refused call."""
    return SimpleNamespace(
        role="tool",
        tool_name=tool_name,
        tool_call_error=True,
        content=(f"Tool call limit reached. Tool call {tool_name} not executed. "
                 f"Don't try to execute it again."),
    )


def _ok_tool_msg(tool_name: str):
    return SimpleNamespace(role="tool", tool_name=tool_name,
                           tool_call_error=False, content="file contents here")


def test_refused_tool_is_detected():
    result = SimpleNamespace(messages=[
        _ok_tool_msg("search_files"),
        _limit_msg("get_file_content"),
    ])
    assert _tools_refused_for_limit(result) == ["get_file_content"]


def test_multiple_distinct_refusals_are_all_reported_once_each():
    result = SimpleNamespace(messages=[
        _limit_msg("get_file_content"),
        _limit_msg("search_files"),
        _limit_msg("get_file_content"),      # same tool refused twice
    ])
    assert _tools_refused_for_limit(result) == ["get_file_content", "search_files"]


def test_a_healthy_run_reports_nothing():
    """No false positives -- the overwhelming majority of runs never hit the ceiling."""
    result = SimpleNamespace(messages=[
        _ok_tool_msg("get_file_content"),
        _ok_tool_msg("db_query"),
    ])
    assert _tools_refused_for_limit(result) == []


def test_an_ordinary_tool_error_is_not_mistaken_for_the_limit():
    """tool_call_error=True is set for any failed call. Only agno's limit wording counts;
    a genuine tool failure is a different problem with a different remedy."""
    result = SimpleNamespace(messages=[
        SimpleNamespace(role="tool", tool_name="db_query", tool_call_error=True,
                        content="relation \"items\" does not exist"),
    ])
    assert _tools_refused_for_limit(result) == []


def test_non_tool_messages_are_ignored():
    """An assistant merely TALKING about the limit -- which is exactly what the observed
    runs did, at length -- must not be read as evidence of a refusal."""
    result = SimpleNamespace(messages=[
        SimpleNamespace(role="assistant", tool_name=None, tool_call_error=False,
                        content="I'm encountering a persistent tool call limit reached "
                                "that's preventing me from retrieving the file"),
    ])
    assert _tools_refused_for_limit(result) == []


def test_unrecognised_result_shapes_do_not_raise():
    """A disclosure helper must never be the thing that breaks a failing run."""
    assert _tools_refused_for_limit(SimpleNamespace()) == []
    assert _tools_refused_for_limit(SimpleNamespace(messages=None)) == []
    assert _tools_refused_for_limit(SimpleNamespace(messages=[SimpleNamespace()])) == []


def test_refusal_without_a_tool_name_still_reports():
    """Better a vague disclosure than a silent one."""
    msg = _limit_msg("x")
    msg.tool_name = None
    assert _tools_refused_for_limit(SimpleNamespace(messages=[msg])) == ["<unknown tool>"]
