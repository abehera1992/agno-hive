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
    assert out == {"__tool_event__": "start", "name": "search_files", "args": {"pattern": "voucher"}}


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
