"""Unit tests for _stream_event_to_chunk — the pure classifier that decides what
run_task_stream yields for each raw agno event. No agno Team/MCP dependency:
built from bare objects with just the attributes the function reads."""
from types import SimpleNamespace

from swarm.team import _stream_event_to_chunk


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


# ── RunError/TeamRunError classification (2026-08-18 live incident) ─────────────
# A litellm.ContextWindowExceededError on a long, delegation-heavy run arrived as
# one of these event types and, before this fix, fell through to `return None`
# (the same bucket as any harmless unrecognized event) -- the run then idled for
# 5+ minutes until the 300s liveness auto-kill eventually caught it. See
# _BackendRunError's own docstring in swarm/team.py for the fast-fail fix this
# classification enables.

def test_run_error_event_returns_run_error_sentinel():
    event = SimpleNamespace(event="RunError", content="litellm.ContextWindowExceededError: ...")
    out = _stream_event_to_chunk(event)
    assert out == {
        "__run_error__": True,
        "message": "litellm.ContextWindowExceededError: ...",
        "agent_name": "",
    }


def test_team_run_error_event_returns_run_error_sentinel():
    event = SimpleNamespace(event="TeamRunError", content="some backend failure")
    out = _stream_event_to_chunk(event)
    assert out["__run_error__"] is True
    assert out["message"] == "some backend failure"


def test_run_error_event_with_agent_name_preserves_it():
    event = SimpleNamespace(event="RunError", content="boom", agent_name="Researcher")
    out = _stream_event_to_chunk(event)
    assert out["agent_name"] == "Researcher"


def test_run_error_event_with_no_content_gets_a_placeholder_message():
    event = SimpleNamespace(event="RunError", content=None)
    out = _stream_event_to_chunk(event)
    assert out["__run_error__"] is True
    assert "RunError" in out["message"]


def test_run_error_event_with_empty_string_content_gets_a_placeholder_message():
    event = SimpleNamespace(event="RunError", content="")
    out = _stream_event_to_chunk(event)
    assert out["message"]  # never an empty/falsy message
