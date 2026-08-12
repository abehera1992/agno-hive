"""Tests for main.py's _run_stream_worker() -- the receiving side of
api/server.py's _stream_worker_subprocess() (Phase 3 of process-boundary
cancellation, see DOCS.md "Process-Boundary Cancellation"). Monkeypatches
sys.stdin and run_task_stream so these run without a real subprocess, the
agno/MCP stack, or network I/O -- tests/test_stream_worker_subprocess.py
covers the actual subprocess boundary. Mirrors
tests/test_main_run_worker.py's structure.
"""
import io
import json

import pytest

import main
from api.models import AgentSpec


def _set_stdin(monkeypatch, payload: dict):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_success_writes_one_line_per_chunk_in_order(monkeypatch):
    _set_stdin(monkeypatch, {"task": "do the thing"})

    async def fake_run_task_stream(**kwargs):
        yield "chunk one"
        yield {"__tool_event__": "start", "name": "search_files"}
        yield {"__done__": True, "content": "final", "tokens": {}, "clarification": None}

    monkeypatch.setattr(main, "run_task_stream", fake_run_task_stream)

    out = io.StringIO()
    await main._run_stream_worker(out)

    lines = _lines(out)
    assert lines == [
        {"ok": True, "v": "chunk one"},
        {"ok": True, "v": {"__tool_event__": "start", "name": "search_files"}},
        {"ok": True, "v": {"__done__": True, "content": "final", "tokens": {}, "clarification": None}},
    ]


@pytest.mark.asyncio
async def test_mid_stream_exception_becomes_a_trailing_error_line(monkeypatch):
    _set_stdin(monkeypatch, {"task": "do the thing"})

    async def failing_run_task_stream(**kwargs):
        yield "partial chunk"
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_task_stream", failing_run_task_stream)

    out = io.StringIO()
    await main._run_stream_worker(out)

    lines = _lines(out)
    assert lines[0] == {"ok": True, "v": "partial chunk"}
    assert lines[1]["ok"] is False
    assert "RuntimeError" in lines[1]["error"]
    assert "boom" in lines[1]["error"]


@pytest.mark.asyncio
async def test_payload_fields_pass_through_to_run_task_stream(monkeypatch):
    payload = {
        "task": "add a field",
        "coordinator_model": "qwen3-coder:30b",
        "mcp_urls": ["http://host:9003/mcp"],
        "project_id": "ekam",
        "session_id": "sess-123",
        "mode": "coordinate",
        "read_only": True,
    }
    _set_stdin(monkeypatch, payload)

    captured = {}

    async def fake_run_task_stream(**kwargs):
        captured.update(kwargs)
        return
        yield  # pragma: no cover -- makes this an async generator

    monkeypatch.setattr(main, "run_task_stream", fake_run_task_stream)

    await main._run_stream_worker(io.StringIO())

    assert captured["task"] == "add a field"
    assert captured["coordinator_model"] == "qwen3-coder:30b"
    assert captured["mcp_urls"] == ["http://host:9003/mcp"]
    assert captured["project_id"] == "ekam"
    assert captured["session_id"] == "sess-123"
    assert captured["read_only"] is True
    assert captured["agent_specs"] is None


@pytest.mark.asyncio
async def test_agent_specs_are_reconstructed_as_real_agentspec_objects(monkeypatch):
    payload = {
        "task": "x",
        "agent_specs": [
            {"name": "Coder", "role": "engineer", "model": "qwen2.5-coder:32b", "instructions": ["a"]},
        ],
    }
    _set_stdin(monkeypatch, payload)

    captured = {}

    async def fake_run_task_stream(**kwargs):
        captured.update(kwargs)
        return
        yield  # pragma: no cover

    monkeypatch.setattr(main, "run_task_stream", fake_run_task_stream)

    await main._run_stream_worker(io.StringIO())

    specs = captured["agent_specs"]
    assert len(specs) == 1
    assert isinstance(specs[0], AgentSpec)
    assert specs[0].name == "Coder"
