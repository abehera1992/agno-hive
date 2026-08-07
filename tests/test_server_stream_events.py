"""Unit tests for _tool_event_to_sse — the pure formatter that turns a
run_task_stream tool-event dict into an SSE data line. No FastAPI app
dependency: this is a plain string-in, string-out function."""
import json

from api.server import _tool_event_to_sse


def test_start_event_produces_tool_start_sse_line():
    chunk = {"__tool_event__": "start", "name": "search_files", "args": {"pattern": "x"}}
    line = _tool_event_to_sse(chunk)
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    payload = json.loads(line[len("data: "):].strip())
    assert payload == {"type": "tool_start", "name": "search_files", "args": {"pattern": "x"}}


def test_end_event_produces_tool_end_sse_line():
    chunk = {"__tool_event__": "end", "name": "get_file_content", "result_preview": "abc"}
    line = _tool_event_to_sse(chunk)
    payload = json.loads(line[len("data: "):].strip())
    assert payload == {"type": "tool_end", "name": "get_file_content", "result_preview": "abc"}


def test_unrecognized_dict_shape_returns_none():
    assert _tool_event_to_sse({"__done__": True, "content": "x"}) is None


def test_dict_with_no_tool_event_key_returns_none():
    assert _tool_event_to_sse({"name": "x"}) is None
