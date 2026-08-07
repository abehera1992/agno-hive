"""Regression tests for the read-only tool-call cache hook.

Confirmed live 2026-08-07: get_files_batch was called 21-29 times for the SAME
2 files across one 6-agent coordinate-mode run, because agno's
share_member_interactions only forwards a teammate's final TEXT answer, never
the raw tool result. A companion prompt-level fix (telling agents to forward
and trust citations) did not measurably reduce this in a live re-test, since
it depends on model instruction-following rather than a mechanical guarantee.

Two mechanical facts, verified by reading agno's actual source (not assumed),
that this hook and its wiring depend on:
1. Every MCP-server-backed tool call is async on the client side unconditionally
   (agno.utils.mcp.get_entrypoint_for_tool's call_tool is `async def`, no sync
   variant) -- a sync hook doing `function(**args)` would get an unawaited
   coroutine, not the real result. The hook must be async and must await.
2. In mode="coordinate" the coordinator mostly delegates rather than calling
   tools itself -- a hook registered only on Team(tool_hooks=[...]) never sees
   the member agents' own tool calls, which is where the measured redundant
   reads actually happen. Confirmed live: a coordinator-only hook logged
   nothing across live runs that made dozens of get_files_batch calls.
"""
import asyncio

import pytest

from swarm.team import _make_read_cache_tool_hook, _build_team, _CACHEABLE_READ_TOOLS


# ── _make_read_cache_tool_hook: caching behavior ────────────────────────────────

@pytest.mark.asyncio
async def test_cache_hook_calls_through_on_first_call():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "file content here"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert result == "file content here"
    assert calls == [{"relative_path": "x.py"}]


@pytest.mark.asyncio
async def test_cache_hook_serves_cached_result_on_second_identical_call():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return f"content #{len(calls)}"

    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert first == "content #1"
    assert second == "content #1"  # cached, NOT "content #2" -- the function was not called again
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_hook_treats_different_args_as_different_cache_entries():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return kwargs["relative_path"]

    await hook("get_file_content", fake_get_file_content, {"relative_path": "a.py"})
    await hook("get_file_content", fake_get_file_content, {"relative_path": "b.py"})

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_hook_treats_argument_order_as_the_same_cache_entry():
    """A cache key built from insertion-order dict items would treat
    {"a": 1, "b": 2} and {"b": 2, "a": 1} as different calls -- json.dumps with
    sort_keys=True is what prevents that."""
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_search_files(**kwargs):
        calls.append(kwargs)
        return "match"

    await hook("search_files", fake_search_files, {"pattern": "x", "glob_filter": "**/*.py"})
    await hook("search_files", fake_search_files, {"glob_filter": "**/*.py", "pattern": "x"})

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_hook_does_not_cache_a_non_cacheable_tool():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_apply_diff(**kwargs):
        calls.append(kwargs)
        return "review_pending: x.py"

    await hook("apply_diff", fake_apply_diff, {"relative_path": "x.py", "old_string": "a", "new_string": "b"})
    await hook("apply_diff", fake_apply_diff, {"relative_path": "x.py", "old_string": "a", "new_string": "b"})

    assert len(calls) == 2  # never cached -- a write tool must always call through


@pytest.mark.asyncio
async def test_cache_hook_falls_back_to_call_through_on_unserializable_args():
    hook = _make_read_cache_tool_hook()
    calls = []

    class _NotJsonSerializable:
        pass

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "content"

    bad_args = {"relative_path": "x.py", "weird": _NotJsonSerializable()}
    await hook("get_file_content", fake_get_file_content, bad_args)
    await hook("get_file_content", fake_get_file_content, bad_args)

    assert len(calls) == 2  # can't build a cache key -- always calls through, never crashes


@pytest.mark.asyncio
async def test_cache_hook_two_independent_hooks_have_independent_caches():
    """Confirms the cache is scoped to ONE _make_read_cache_tool_hook() call (one
    run), not a module-level shared cache that would leak across sessions."""
    hook_a = _make_read_cache_tool_hook()
    hook_b = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "content"

    await hook_a("get_file_content", fake_get_file_content, {"relative_path": "x.py"})
    await hook_b("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert len(calls) == 2  # hook_b's cache is empty -- it never saw hook_a's call


def test_cacheable_read_tools_excludes_every_mutating_tool():
    """The cache must never intercept a write/mutating tool -- overlap here would
    mean a second identical write call gets silently skipped instead of actually
    running, which is a correctness bug, not an efficiency win."""
    from swarm.team import _MUTATING_TOOLS
    assert _CACHEABLE_READ_TOOLS.isdisjoint(_MUTATING_TOOLS)


# ── _build_team wiring: shared hook across coordinator AND every member ────────

def test_build_team_registers_a_tool_hook_on_the_coordinator(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    # read-cache hook (Detour fix) + interception hook (Phase 9a) -- both shared
    assert result.tool_hooks is not None
    assert len(result.tool_hooks) == 2


def test_build_team_shares_the_same_hook_instance_between_coordinator_and_fallback_members(monkeypatch):
    """Fallback path (agent_specs=None -> make_coder + make_reviewer). The SAME
    hook object (not just an equivalent one) must reach every member -- that's
    what makes the cache actually shared rather than one-per-agent."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    for member in result.members:
        assert member.tool_hooks is not None
        assert member.tool_hooks == result.tool_hooks  # same objects, not just equal


def test_build_team_shares_the_same_hook_instance_with_spec_based_members(monkeypatch):
    """agent_specs path (make_agent_from_spec) -- the primary path used by the
    real engineering.yaml team, not just the make_coder/make_reviewer fallback."""
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

    assert len(result.members) == 1
    assert result.members[0].tool_hooks == result.tool_hooks
