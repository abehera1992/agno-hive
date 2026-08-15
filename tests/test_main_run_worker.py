"""Tests for main.py's _run_worker() -- the receiving side of api/server.py's
_run_worker_subprocess() (Phase 1 of process-boundary cancellation, see DOCS.md
"Process-Boundary Cancellation"). Monkeypatches sys.stdin and run_task_async so
these run without a real subprocess, the agno/MCP stack, or network I/O --
tests/test_run_worker_subprocess.py covers the actual subprocess boundary.
"""
import io
import json

import pytest

import main
from api.models import AgentSpec


def _set_stdin(monkeypatch, payload: dict):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


@pytest.mark.asyncio
async def test_success_returns_content_tokens_and_clarification(monkeypatch):
    _set_stdin(monkeypatch, {"task": "do the thing"})

    async def fake_run_task_async(**kwargs):
        return "the answer", {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}, None

    monkeypatch.setattr(main, "run_task_async", fake_run_task_async)

    result = await main._run_worker()

    assert result == {
        "content": "the answer",
        "tokens": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        "clarification": None,
    }


@pytest.mark.asyncio
async def test_run_task_async_exception_becomes_an_error_result_not_a_crash(monkeypatch):
    _set_stdin(monkeypatch, {"task": "do the thing"})

    async def failing_run_task_async(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_task_async", failing_run_task_async)

    result = await main._run_worker()

    assert "error" in result
    assert "RuntimeError" in result["error"]
    assert "boom" in result["error"]


@pytest.mark.asyncio
async def test_payload_fields_pass_through_to_run_task_async(monkeypatch):
    payload = {
        "task": "add a field",
        "coordinator_model": "qwen3-coder:30b",
        "coordinator_tools": ["get_file_content"],
        "mcp_url": "http://host:9000/mcp",
        "mcp_urls": ["http://host:9003/mcp"],
        "project_id": "ekam",
        "session_id": "sess-123",
        "mode": "coordinate",
        "read_only": True,
    }
    _set_stdin(monkeypatch, payload)

    captured = {}

    async def fake_run_task_async(**kwargs):
        captured.update(kwargs)
        return "ok", {}, None

    monkeypatch.setattr(main, "run_task_async", fake_run_task_async)

    await main._run_worker()

    assert captured["task"] == "add a field"
    assert captured["coordinator_model"] == "qwen3-coder:30b"
    assert captured["coordinator_tools"] == ["get_file_content"]
    assert captured["mcp_url"] == "http://host:9000/mcp"
    assert captured["mcp_urls"] == ["http://host:9003/mcp"]
    assert captured["project_id"] == "ekam"
    assert captured["session_id"] == "sess-123"
    assert captured["mode"] == "coordinate"
    assert captured["read_only"] is True
    assert captured["agent_specs"] is None


@pytest.mark.asyncio
async def test_agent_specs_are_reconstructed_as_real_agentspec_objects(monkeypatch):
    """The parent (api/server.py's /run handler) sends agent_specs as plain
    dicts (AgentSpec.model_dump()) -- confirms this side rebuilds real
    AgentSpec instances rather than passing raw dicts through, matching what
    run_task_async's own callers expect elsewhere in the codebase."""
    payload = {
        "task": "x",
        "agent_specs": [
            {"name": "Coder", "role": "engineer", "model": "qwen2.5-coder:32b", "instructions": ["a"]},
            {"name": "Reviewer", "role": "reviewer", "model": "qwen2.5-coder:32b", "instructions": ["b"]},
        ],
    }
    _set_stdin(monkeypatch, payload)

    captured = {}

    async def fake_run_task_async(**kwargs):
        captured.update(kwargs)
        return "ok", {}, None

    monkeypatch.setattr(main, "run_task_async", fake_run_task_async)

    await main._run_worker()

    specs = captured["agent_specs"]
    assert len(specs) == 2
    assert all(isinstance(s, AgentSpec) for s in specs)
    assert specs[0].name == "Coder"
    assert specs[1].name == "Reviewer"


@pytest.mark.asyncio
async def test_team_name_passes_through_to_run_task_async(monkeypatch):
    """2026-08-15 gate-scope extension -- api/server.py's /run handler now sends
    team_name in the worker payload so _build_team's per-team gate policy
    (see tests/test_gate_team_scoping.py) reaches the actual run."""
    _set_stdin(monkeypatch, {"task": "plan the sprint", "team_name": "sprint-master"})

    captured = {}

    async def fake_run_task_async(**kwargs):
        captured.update(kwargs)
        return "ok", {}, None

    monkeypatch.setattr(main, "run_task_async", fake_run_task_async)

    await main._run_worker()

    assert captured["team_name"] == "sprint-master"


@pytest.mark.asyncio
async def test_missing_team_name_passes_through_as_none(monkeypatch):
    """A payload with no team_name key (e.g. an older client, or main.py's own
    __main__ one-shot CLI path) must not break -- run_task_async's own
    team_name=None default preserves exact prior behavior."""
    _set_stdin(monkeypatch, {"task": "x"})

    captured = {}

    async def fake_run_task_async(**kwargs):
        captured.update(kwargs)
        return "ok", {}, None

    monkeypatch.setattr(main, "run_task_async", fake_run_task_async)

    await main._run_worker()

    assert captured["team_name"] is None


@pytest.mark.asyncio
async def test_missing_optional_fields_use_run_task_asyncs_own_defaults(monkeypatch):
    """A minimal payload (just task) must not pass None where run_task_async
    expects its own default (project_id="default", mode="coordinate",
    read_only=False) -- only agent_specs/coordinator_model/etc are genuinely
    optional-as-None."""
    _set_stdin(monkeypatch, {"task": "x"})

    captured = {}

    async def fake_run_task_async(**kwargs):
        captured.update(kwargs)
        return "ok", {}, None

    monkeypatch.setattr(main, "run_task_async", fake_run_task_async)

    await main._run_worker()

    assert captured["project_id"] == "default"
    assert captured["mode"] == "coordinate"
    assert captured["read_only"] is False
