"""Regression tests: _count_read_calls (swarm/team.py) must count reads made by a
DELEGATED member agent, not just the coordinator's own direct tool calls.

Confirmed live 2026-08-18: `result.messages` (agno's TeamRunOutput) only ever
holds the COORDINATOR's own direct tool calls -- a delegated member's reads
happen inside a separate, nested run, visible to the coordinator's own message
list only as one opaque `delegate_task_to_member` call (not a read tool) plus
the delegate's final text. A gap-analysis task handled entirely through
delegation (Researcher enumerates both sides, Reviewer independently
cross-checks -- a correct, encouraged pattern) triggered a false "answer
asserts code facts with ZERO read calls" retry despite both agents having read
extensively and correctly. The retry's fresh, un-grounded re-run then produced
a WRONG answer, overwriting a correct, doubly-verified one.

session_state["read_log"] (_record_read, written by _make_read_cache_tool_hook
on EVERY team member, coordinator or delegated) already tracks every real
fresh read across the whole run regardless of delegation depth -- these tests
confirm _count_read_calls now also consults it.
"""
from types import SimpleNamespace

from swarm.team import _count_read_calls


def _result(messages=None, session_state=None):
    return SimpleNamespace(messages=messages, session_state=session_state)


def _tool_msg(name: str):
    return SimpleNamespace(role="tool", tool_name=name, content="some result")


def _read_log_entry(tool: str, read_by: str = "Researcher"):
    return {"tool": tool, "args": {}, "read_by": read_by, "result_chars": 500}


def test_messages_only_read_is_still_counted():
    """Existing behavior, unaffected: the coordinator's own direct read call."""
    result = _result(messages=[_tool_msg("get_file_content")])
    assert _count_read_calls(result) == 1


def test_delegated_reads_are_counted_via_read_log_even_with_empty_messages():
    """The exact live incident shape: the coordinator's own .messages has no
    read-tool calls (it only delegated), but session_state.read_log shows the
    delegated agents' real reads."""
    result = _result(
        messages=[],
        session_state={"read_log": [
            _read_log_entry("get_file_content", "Researcher"),
            _read_log_entry("search_files", "Researcher"),
            _read_log_entry("get_file_content", "Reviewer"),
        ]},
    )
    assert _count_read_calls(result) == 3


def test_delegated_reads_are_counted_even_when_messages_is_none():
    """Some agno message shapes leave .messages as None/missing entirely --
    must not be treated as "undeterminable" when read_log has real data."""
    result = _result(messages=None, session_state={"read_log": [_read_log_entry("get_file_content")]})
    assert _count_read_calls(result) == 1


def test_messages_and_read_log_reads_are_combined_not_deduplicated_away():
    result = _result(
        messages=[_tool_msg("get_file_content")],
        session_state={"read_log": [_read_log_entry("search_files", "Researcher")]},
    )
    assert _count_read_calls(result) == 2


def test_read_log_ignores_non_read_tools():
    """delegate_task_to_member, apply_diff, etc. in the log must not inflate
    the read count -- only entries whose tool is in _READ_TOOLS count."""
    result = _result(
        messages=[],
        session_state={"read_log": [
            {"tool": "delegate_task_to_member", "args": {}, "read_by": "coordinator", "result_chars": 10},
            {"tool": "apply_diff", "args": {}, "read_by": "Coder", "result_chars": 10},
            _read_log_entry("get_file_content", "Researcher"),
        ]},
    )
    assert _count_read_calls(result) == 1


def test_no_messages_and_no_read_log_is_undeterminable():
    """Neither source has any recognizable data -- must stay -1, not 0, so a
    conversational reply or an "I could not determine" answer is never
    force-retried."""
    result = _result(messages=None, session_state=None)
    assert _count_read_calls(result) == -1


def test_no_messages_and_empty_read_log_is_undeterminable():
    result = _result(messages=[], session_state={"read_log": []})
    assert _count_read_calls(result) == -1


def test_session_state_missing_read_log_key_does_not_crash():
    result = _result(messages=[], session_state={"delegations_made": []})
    assert _count_read_calls(result) == -1


def test_session_state_that_is_not_a_dict_does_not_crash():
    """Defensive against an older agno version or a test double where
    session_state is some other falsy-but-not-dict shape."""
    result = _result(messages=[], session_state=None)
    assert _count_read_calls(result) == -1
