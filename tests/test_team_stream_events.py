"""Unit tests for _stream_event_to_chunk — the pure classifier that decides what
run_task_stream yields for each raw agno event. No agno Team/MCP dependency:
built from bare objects with just the attributes the function reads."""
from types import SimpleNamespace

from swarm.team import _is_cancelled_event, _stream_event_to_chunk


def _content_event(content):
    return SimpleNamespace(event="TeamRunContent", content=content)


def _tool_started(name, args):
    tool = SimpleNamespace(tool_name=name, tool_args=args, result=None)
    return SimpleNamespace(event="TeamToolCallStarted", tool=tool)


def _tool_completed(name, result):
    tool = SimpleNamespace(tool_name=name, tool_args=None, result=result)
    return SimpleNamespace(event="TeamToolCallCompleted", tool=tool)


def test_content_event_returns_the_text_chunk():
    assert _stream_event_to_chunk(_content_event("hello")) == "hello"


def test_content_event_with_empty_string_returns_none():
    assert _stream_event_to_chunk(_content_event("")) is None


def test_content_event_with_non_string_content_returns_none():
    assert _stream_event_to_chunk(_content_event(None)) is None


def test_tool_started_returns_start_sentinel():
    event = _tool_started("search_files", {"pattern": "voucher"})
    out = _stream_event_to_chunk(event)
    assert out == {
        "__tool_event__": "start",
        "name": "search_files",
        "args": {"pattern": "voucher"},
        "agent_name": "",
    }


def test_tool_started_with_no_args_defaults_to_empty_dict():
    event = _tool_started("list_directory", None)
    out = _stream_event_to_chunk(event)
    assert out["args"] == {}


def test_tool_completed_returns_end_sentinel_with_truncated_preview():
    event = _tool_completed("get_file_content", "x" * 500)
    out = _stream_event_to_chunk(event)
    assert out["__tool_event__"] == "end"
    assert out["name"] == "get_file_content"
    assert len(out["result_preview"]) == 200


def test_tool_completed_with_non_string_result_has_no_preview():
    event = _tool_completed("run_command", {"exit_code": 0})
    out = _stream_event_to_chunk(event)
    assert out["result_preview"] is None


def test_tool_event_with_no_tool_attribute_returns_none():
    event = SimpleNamespace(event="TeamToolCallStarted", tool=None)
    assert _stream_event_to_chunk(event) is None


def test_unrecognized_event_type_returns_none():
    event = SimpleNamespace(event="TeamRunStarted", content=None)
    assert _stream_event_to_chunk(event) is None


def test_event_missing_event_attribute_returns_none():
    event = SimpleNamespace(content="stray text")  # no .event at all
    assert _stream_event_to_chunk(event) is None


# ── member-agent (non-Team-prefixed) event types (2026-08-10) ──────────────────
# A delegated member agent's own events use agno's Agent-level names (RunContent,
# ToolCallStarted, ToolCallCompleted -- no "Team" prefix), confirmed via
# agno.run.agent.RunEvent. In mode="coordinate" the coordinator mostly delegates,
# so these are the COMMON case, not an edge case -- see _stream_event_to_chunk's
# docstring for the live investigation that found this gap.

def test_member_agent_content_event_returns_the_text_chunk():
    event = SimpleNamespace(event="RunContent", content="researching...")
    assert _stream_event_to_chunk(event) == "researching..."


def test_member_agent_tool_started_returns_start_sentinel_with_agent_name():
    tool = SimpleNamespace(tool_name="get_file_content", tool_args={"relative_path": "foo.py"}, result=None)
    event = SimpleNamespace(event="ToolCallStarted", tool=tool, agent_name="Researcher")

    out = _stream_event_to_chunk(event)

    assert out == {
        "__tool_event__": "start",
        "name": "get_file_content",
        "args": {"relative_path": "foo.py"},
        "agent_name": "Researcher",
    }


def test_member_agent_tool_completed_returns_end_sentinel_with_agent_name():
    tool = SimpleNamespace(tool_name="get_file_content", tool_args=None, result="file contents")
    event = SimpleNamespace(event="ToolCallCompleted", tool=tool, agent_name="Coder")

    out = _stream_event_to_chunk(event)

    assert out["__tool_event__"] == "end"
    assert out["agent_name"] == "Coder"


def test_team_level_tool_event_defaults_agent_name_to_empty_string():
    """The coordinator's OWN tool calls have no agent_name attribute on the fake
    event (matching BaseAgentRunEvent's own default of "") -- must not crash."""
    event = _tool_started("search_files", {"pattern": "x"})
    out = _stream_event_to_chunk(event)
    assert out["agent_name"] == ""


# ── _is_cancelled_event ──────────────────────────────────────────────────────────
# Confirmed live 2026-08-11: agno's own _arun_tasks_stream (agno/team/_run.py, the
# function backing team.arun(stream=True, ...)) catches a real asyncio.CancelledError
# internally and yields one of these events instead of re-raising -- breaking
# cooperative cancellation at the library boundary. _is_cancelled_event() is what lets
# every consumer of the stream detect this and raise its own CancelledError to
# restore propagation (see the call sites in run_task_async / run_task_stream /
# _stream_team_run).

def test_is_cancelled_event_recognizes_team_run_cancelled():
    event = SimpleNamespace(event="TeamRunCancelled", content=None)
    assert _is_cancelled_event(event) is True


def test_is_cancelled_event_recognizes_bare_run_cancelled():
    """The member-agent-level equivalent, same naming convention as
    RunContent/ToolCallStarted having no 'Team' prefix."""
    event = SimpleNamespace(event="RunCancelled", content=None)
    assert _is_cancelled_event(event) is True


def test_is_cancelled_event_false_for_ordinary_content_event():
    assert _is_cancelled_event(_content_event("hello")) is False


def test_is_cancelled_event_false_for_the_final_run_output_object():
    """The real TeamRunOutput object (yielded when yield_run_output=True) has no
    .event attribute at all -- must not be misclassified as a cancellation."""
    run_output = SimpleNamespace(content="done", messages=[], tools=[])
    assert _is_cancelled_event(run_output) is False
