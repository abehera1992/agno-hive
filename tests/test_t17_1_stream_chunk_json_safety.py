"""Phase T17.1 -- /stream's NDJSON worker boundary must not crash on a
tool-end chunk whose `result_tokens` (frozenset, swarm.team._salient_tokens)
or `names` (plain set, swarm.team._filenames_in) fields are still set-typed.

Root cause, PROVEN at the source level (not inferred): `main.py`'s
`_run_stream_worker` does `json.dumps({"ok": True, "v": chunk})` on the RAW
dict `run_task_stream` yields -- exactly what `swarm.team._stream_event_to_chunk`
returns for a tool-end event, which has included `result_tokens`
(`frozenset[str]`) and `names` (`set[str]`) since commit 524f261 (2026-09-01,
the thin-answer-guard fix) as legitimate same-process internal state for
team.py's own evidence-accumulation (`team._evidence_tokens |= toks`).
`/run`'s worker (`_run_worker`) never hits this -- it only ever prints the
final accumulated string result, never a raw per-chunk event dict. `/stream`
is the only path that serializes these chunks for inter-process transport,
and until this fix did so with the raw, unconverted dict.

Live-reproduced: a hive CLI chat's first tool call with a string result
(observed with `lightrag_query`, but the bug is generic to EVERY string-
returning tool -- get_file_content, search_files, etc. all build the
identical chunk shape) crashed with `TypeError: Object of type frozenset is
not JSON serializable`, caught by `_run_stream_worker`'s own except-Exception
and surfaced to the CLI as a bare error instead of the tool's real result.
"""
import io
import json

import pytest

import main


# ---------------------------------------------------------------------------
# Unit tests: _json_safe_stream_chunk
# ---------------------------------------------------------------------------

def test_str_chunk_passes_through_unchanged():
    assert main._json_safe_stream_chunk("plain content delta") == "plain content delta"


def test_frozenset_value_becomes_a_sorted_list():
    chunk = {"__tool_event__": "end", "name": "lightrag_query", "result_tokens": frozenset({"b", "a"})}
    safe = main._json_safe_stream_chunk(chunk)
    assert safe["result_tokens"] == ["a", "b"]
    json.dumps(safe)  # must not raise


def test_plain_set_value_becomes_a_sorted_list():
    chunk = {"__tool_event__": "end", "name": "search_files", "names": {"b.py", "a.py"}}
    safe = main._json_safe_stream_chunk(chunk)
    assert safe["names"] == ["a.py", "b.py"]
    json.dumps(safe)  # must not raise


def test_the_exact_failing_tool_end_shape_is_now_json_safe():
    """Reproduces the real dict shape swarm.team._stream_event_to_chunk builds
    for a lightrag_query-style string result (the exact live failure)."""
    chunk = {
        "__tool_event__": "end",
        "name": "lightrag_query",
        "result_preview": "agno-hive is present at /home/abehera1992/agno-hive...",
        "result_chars": 4821,
        "result_tokens": frozenset({"agno-hive", "lightrag", "project"}),
        "agent_name": "Researcher",
        "listing": None,
        "names": {"README.md", "team.py"},
    }
    safe = main._json_safe_stream_chunk(chunk)
    encoded = json.dumps(safe)  # the exact call that previously raised
    decoded = json.loads(encoded)
    assert decoded["name"] == "lightrag_query"
    assert sorted(decoded["result_tokens"]) == ["agno-hive", "lightrag", "project"]
    assert sorted(decoded["names"]) == ["README.md", "team.py"]


def test_none_result_tokens_passes_through_unchanged():
    """A non-string tool result leaves result_tokens as None (see
    _stream_event_to_chunk) -- must not be touched or raise."""
    chunk = {"__tool_event__": "end", "name": "x", "result_tokens": None}
    assert main._json_safe_stream_chunk(chunk) == {"__tool_event__": "end", "name": "x", "result_tokens": None}


def test_done_sentinel_with_no_sets_passes_through_unchanged():
    chunk = {"__done__": True, "content": "final answer", "tokens": {"input": 10, "output": 5}}
    assert main._json_safe_stream_chunk(chunk) == chunk


def test_tool_start_sentinel_passes_through_unchanged():
    """tool-start events (name/args/agent_name) never contained a set in the
    first place -- confirms the fix doesn't alter the already-safe shape."""
    chunk = {"__tool_event__": "start", "name": "lightrag_query",
             "args": {"query": "issue", "project_id": "agno-hive"}, "agent_name": "Researcher"}
    assert main._json_safe_stream_chunk(chunk) == chunk


# ---------------------------------------------------------------------------
# Integration tests: _run_stream_worker end-to-end, via the real NDJSON path
# ---------------------------------------------------------------------------

def _set_stdin(monkeypatch, payload: dict):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_lightrag_query_shaped_chunk_no_longer_crashes_the_worker(monkeypatch):
    """The exact regression: before this fix, this test's second line was
    {"ok": False, "error": "TypeError: Object of type frozenset is not JSON
    serializable"} instead of the real tool-end payload."""
    _set_stdin(monkeypatch, {"task": "do you have access to the agnohive project in this pc"})

    async def fake_run_task_stream(**kwargs):
        yield "I'll check the agnohive project directory."
        yield {
            "__tool_event__": "end",
            "name": "lightrag_query",
            "result_preview": "agno-hive is present at /home/abehera1992/agno-hive",
            "result_chars": 512,
            "result_tokens": frozenset({"agno-hive", "project", "directory"}),
            "agent_name": "Researcher",
            "listing": None,
            "names": {"team.py"},
        }
        yield {"__done__": True, "content": "Yes, agno-hive is present.", "tokens": {}, "clarification": None}

    monkeypatch.setattr(main, "run_task_stream", fake_run_task_stream)

    out = io.StringIO()
    await main._run_stream_worker(out)

    lines = _lines(out)
    assert len(lines) == 3
    assert all(line["ok"] is True for line in lines), f"a chunk still failed to serialize: {lines}"
    assert lines[1]["v"]["name"] == "lightrag_query"
    assert sorted(lines[1]["v"]["result_tokens"]) == ["agno-hive", "directory", "project"]
    assert lines[2]["v"]["content"] == "Yes, agno-hive is present."


@pytest.mark.asyncio
async def test_multiple_string_returning_tools_in_one_stream_all_survive(monkeypatch):
    """Not lightrag-specific -- get_file_content and search_files build the
    identical chunk shape and would have hit the same crash."""
    _set_stdin(monkeypatch, {"task": "x"})

    async def fake_run_task_stream(**kwargs):
        for tool_name, tokens, names in [
            ("get_file_content", frozenset({"line", "129"}), {"models.py"}),
            ("search_files", frozenset({"seller", "document"}), {"business_api.py", "business_admin_api.py"}),
        ]:
            yield {
                "__tool_event__": "end", "name": tool_name, "result_preview": "...",
                "result_chars": 100, "result_tokens": tokens, "agent_name": "Researcher",
                "listing": None, "names": names,
            }

    monkeypatch.setattr(main, "run_task_stream", fake_run_task_stream)

    out = io.StringIO()
    await main._run_stream_worker(out)

    lines = _lines(out)
    assert len(lines) == 2
    assert all(line["ok"] is True for line in lines)


@pytest.mark.asyncio
async def test_genuine_mid_stream_error_still_reported_correctly(monkeypatch):
    """Guard against the fix accidentally swallowing a REAL error -- the
    existing error-path test's exact shape, confirmed unaffected."""
    _set_stdin(monkeypatch, {"task": "x"})

    async def failing_run_task_stream(**kwargs):
        yield {"__tool_event__": "end", "name": "x", "result_tokens": frozenset({"a"}), "names": set()}
        raise RuntimeError("real backend failure")

    monkeypatch.setattr(main, "run_task_stream", failing_run_task_stream)

    out = io.StringIO()
    await main._run_stream_worker(out)

    lines = _lines(out)
    assert lines[0]["ok"] is True
    assert lines[1]["ok"] is False
    assert "RuntimeError" in lines[1]["error"]
    assert "real backend failure" in lines[1]["error"]
