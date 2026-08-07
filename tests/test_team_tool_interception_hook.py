"""Unit tests for the tool_hooks interception checkpoint (AGNOHive 2.3.1 Phase 9a).

Async + shared across the coordinator AND every member agent, for the same two
mechanical facts _make_read_cache_tool_hook depends on (verified by reading
agno's actual source, see that hook's docstring): every MCP-server-backed tool
call is async on the client side unconditionally, and in mode="coordinate" the
coordinator mostly delegates -- a coordinator-only hook misses the member
agents' own tool calls.

Deliberately NOT wired to Phase 7's client-side `_steering_queue` (cli/hive) --
that queue lives in the user's machine's CLI process, this hook runs
server-side in swarm/team.py. There is no existing mid-run client<->server
channel connecting the two; abort_event is a reusable building block a future
caller can wire up, not something already connected to steering. See
_make_tool_interception_hook's docstring for the full scoping note.
"""
import asyncio

import pytest

from swarm.team import _make_tool_interception_hook, _build_team, ToolCallAborted


# ── _make_tool_interception_hook: pass-through / audit behavior (abort_event=None) ──

@pytest.mark.asyncio
async def test_hook_calls_through_and_returns_the_result_when_no_abort_event():
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return kwargs["x"] * 2

    result = await hook("double", fake_tool, {"x": 21})

    assert result == 42


@pytest.mark.asyncio
async def test_hook_prints_a_trace_line_on_success(capsys):
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "ok"

    await hook("my_tool", fake_tool, {"x": 1})

    out = capsys.readouterr().out
    assert "my_tool" in out


@pytest.mark.asyncio
async def test_hook_still_prints_and_reraises_on_a_failing_tool(capsys):
    hook = _make_tool_interception_hook()

    async def failing_tool(**kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await hook("bad_tool", failing_tool, {})

    out = capsys.readouterr().out
    assert "bad_tool" in out


# ── _make_tool_interception_hook: abort behavior (abort_event set) ─────────────

@pytest.mark.asyncio
async def test_hook_calls_through_when_abort_event_exists_but_is_not_set():
    abort_event = asyncio.Event()
    hook = _make_tool_interception_hook(abort_event=abort_event)
    calls = []

    async def fake_tool(**kwargs):
        calls.append(kwargs)
        return "ran"

    result = await hook("some_tool", fake_tool, {"x": 1})

    assert result == "ran"
    assert calls == [{"x": 1}]


@pytest.mark.asyncio
async def test_hook_skips_the_call_and_raises_when_abort_event_is_set():
    abort_event = asyncio.Event()
    abort_event.set()
    hook = _make_tool_interception_hook(abort_event=abort_event)
    calls = []

    async def fake_tool(**kwargs):
        calls.append(kwargs)  # must never run
        return "ran"

    with pytest.raises(ToolCallAborted):
        await hook("some_tool", fake_tool, {"x": 1})

    assert calls == []  # the underlying tool was never invoked


@pytest.mark.asyncio
async def test_hook_prints_an_aborted_trace_line(capsys):
    abort_event = asyncio.Event()
    abort_event.set()
    hook = _make_tool_interception_hook(abort_event=abort_event)

    async def fake_tool(**kwargs):
        return "ran"

    with pytest.raises(ToolCallAborted):
        await hook("some_tool", fake_tool, {})

    out = capsys.readouterr().out
    assert "some_tool" in out
    assert "ABORTED" in out


# ── _build_team wiring: interception hook shared across coordinator AND every member ──

def test_build_team_registers_the_interception_hook_alongside_the_cache_hook(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert len(result.tool_hooks) == 2
    # second hook is the interception hook -- an async closure named accordingly
    assert result.tool_hooks[1].__name__ == "_tool_interception_hook"


def test_build_team_shares_the_same_interception_hook_instance_with_fallback_members(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    interception_hook = result.tool_hooks[1]
    for member in result.members:
        assert member.tool_hooks[1] is interception_hook


def test_build_team_shares_the_same_interception_hook_instance_with_spec_based_members(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    class _FakeSpec:
        name = "Researcher"
        model = "qwen2.5-coder:32b"
        tools = None
        instructions = ["Research the codebase."]
        role = "Researcher"
        description = "Research specialist."
        skills = None

    result = _build_team(
        agent_specs=[_FakeSpec()],
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    interception_hook = result.tool_hooks[1]
    assert result.members[0].tool_hooks[1] is interception_hook
