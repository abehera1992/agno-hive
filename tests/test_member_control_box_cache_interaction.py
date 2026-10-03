"""Phase B.4 regression suite for the proven Phase B.2 defect: the Control
Box hook, originally appended to the per-role tail (inner to
_read_cache_tool_hook in the real composed chain), was silently skipped on
every same-run cache hit -- read_cache_tool_hook's own cache-hit branch
(`else: result = cache[cache_key]`) returns without ever calling its own
`function` parameter, which is the closure wrapping every hook registered
after it, including the Control Box hook.

Unlike tests/test_member_control_box.py (which calls the hook's returned
closure directly, in isolation), every test here builds a REAL
agno.tools.function.Function/FunctionCall and composes the REAL
_make_member_control_box_hook()/_make_read_cache_tool_hook() factories
through agno's own (real, installed) chain-building algorithm
(Function._build_nested_execution_chain_async's
reduce(create_hook_wrapper, reversed(tool_hooks), execute_entrypoint_async)).
This is the only way to prove the fix against the actual mechanism that
caused the regression, rather than against a hand-written stand-in of it.
"""
import asyncio

import pytest
from agno.tools.function import Function, FunctionCall

import swarm.team as team_mod
from swarm.team import _get_member_control_box, _start_member_objective


def _run(coro):
    return asyncio.run(coro)


class _Agent:
    def __init__(self, name: str):
        self.name = name


class _Team:
    """Bare stand-in, same convention as test_member_control_box.py's own
    _Team -- nothing here needs any Team behavior beyond being a plain
    object _get_member_control_box can getattr/setattr on."""


def _make_tool(entrypoint, extra_hooks=None):
    """One real Function wired with the REAL control-box hook immediately
    followed by the REAL read-cache hook -- the exact order _hooks_for now
    builds (interception_hook, control_box_hook, search_before_browse_gate_hook,
    read_cache_hook, ...). interception_hook/search_before_browse_gate_hook
    are omitted here since neither one's own logic affects get_file_content
    calls (both confirmed, in the Phase B.2/B.3 investigation, to be
    delegation-tool-specific early-returns) -- the two hooks that matter for
    this exact defect are control_box_hook and read_cache_hook, adjacent in
    the real chain either way."""
    hooks = [team_mod._make_member_control_box_hook(), team_mod._make_read_cache_tool_hook()]
    if extra_hooks:
        hooks.extend(extra_hooks)
    return Function(name="get_file_content", entrypoint=entrypoint, tool_hooks=hooks)


def _wire(function: Function, agent, team) -> None:
    function._agent = agent
    function._team = team


async def _call(function: Function, **kwargs):
    fc = FunctionCall(function=function, arguments=kwargs)
    result = await fc.aexecute()
    return result.result


def _records_for(team, member_id: str, delegation_key: str):
    return _get_member_control_box(team).get(member_id, delegation_key)


# ── Test 1: fresh call ───────────────────────────────────────────────────


def test_1_fresh_call_is_recorded_exactly_once():
    team = _Team()
    agent = _Agent("Researcher")
    calls = {"n": 0}

    async def entrypoint(relative_path=None):
        calls["n"] += 1
        return f"content-{relative_path}-{calls['n']}"

    f = _make_tool(entrypoint)
    _wire(f, agent, team)
    _start_member_objective(team, "researcher", "d1", "obj", "ev", "done")

    result = _run(_call(f, relative_path="a.py"))

    assert calls["n"] == 1  # one fresh execution
    assert result == "content-a.py-1"
    state = _records_for(team, "researcher", "d1")
    assert state.total_actions == 1
    assert state.no_progress_streak == 0


# ── Test 2: same-run cache hit ───────────────────────────────────────────


def test_2_same_run_cache_hit_is_recorded_without_double_execution():
    team = _Team()
    agent = _Agent("Researcher")
    calls = {"n": 0}

    async def entrypoint(relative_path=None):
        calls["n"] += 1
        return f"content-{relative_path}-{calls['n']}"

    f = _make_tool(entrypoint)
    _wire(f, agent, team)
    _start_member_objective(team, "researcher", "d1", "obj", "ev", "done")

    _run(_call(f, relative_path="a.py"))      # fresh
    _run(_call(f, relative_path="a.py"))      # identical repeat -> cache hit

    assert calls["n"] == 1, "underlying tool must execute exactly once"
    state = _records_for(team, "researcher", "d1")
    assert state.total_actions == 2           # BOTH calls recorded
    assert state.no_progress_streak == 1       # the cache-hit repeat is no-progress


# ── Test 3 (critical): three sequential delegations, reproducing the ───────
# exact Phase C/B.2 production incident -- D1/D2 fresh reads of two
# different files, D3 re-reads both, hitting the cache both times.


def test_3_three_sequential_delegations_reproduces_and_fixes_the_incident():
    team = _Team()
    agent = _Agent("Researcher")
    calls = {"a.py": 0, "b.py": 0}

    async def entrypoint(relative_path=None):
        calls[relative_path] += 1
        return f"content-{relative_path}-{calls[relative_path]}"

    f = _make_tool(entrypoint)
    _wire(f, agent, team)

    # D1: fresh read of file A
    _start_member_objective(team, "researcher", "d1", "list A", "ev", "done")
    _run(_call(f, relative_path="a.py"))
    d1 = _records_for(team, "researcher", "d1")
    assert d1.total_actions == 1

    # D2: fresh read of file B
    _start_member_objective(team, "researcher", "d2", "list B", "ev", "done")
    _run(_call(f, relative_path="b.py"))
    d2 = _records_for(team, "researcher", "d2")
    assert d2.total_actions == 1

    # D3: re-reads BOTH already-cached files -- the exact production shape
    _start_member_objective(team, "researcher", "d3", "compare A and B", "ev", "done")
    _run(_call(f, relative_path="a.py"))   # cache hit
    _run(_call(f, relative_path="b.py"))   # cache hit
    d3 = _records_for(team, "researcher", "d3")

    assert calls["a.py"] == 1, "file A must not be re-executed on D3's cache hit"
    assert calls["b.py"] == 1, "file B must not be re-executed on D3's cache hit"
    assert d3.total_actions == 2, "D3 must show 2 recorded actions, not 0 (the proven defect)"

    total_logical_actions = d1.total_actions + d2.total_actions + d3.total_actions
    total_fresh_executions = calls["a.py"] + calls["b.py"]
    assert total_logical_actions == 4
    assert total_fresh_executions == 2


# ── Test 4: N cache hits ─────────────────────────────────────────────────


def test_4_n_cache_hits_produce_n_plus_one_records_and_one_execution():
    team = _Team()
    agent = _Agent("Researcher")
    calls = {"n": 0}

    async def entrypoint(relative_path=None):
        calls["n"] += 1
        return f"content-{calls['n']}"

    f = _make_tool(entrypoint)
    _wire(f, agent, team)
    _start_member_objective(team, "researcher", "d1", "obj", "ev", "done")

    for _ in range(5):  # 1 fresh + 4 repeats
        _run(_call(f, relative_path="a.py"))

    assert calls["n"] == 1
    state = _records_for(team, "researcher", "d1")
    assert state.total_actions == 5
    assert state.no_progress_streak == 4


# ── Test 5: mixed fresh/cache sequence ───────────────────────────────────


def test_5_mixed_fresh_and_cache_sequence_exact_accounting():
    team = _Team()
    agent = _Agent("Researcher")
    calls = {"a.py": 0, "b.py": 0, "c.py": 0}

    async def entrypoint(relative_path=None):
        calls[relative_path] += 1
        return f"content-{relative_path}-{calls[relative_path]}"

    f = _make_tool(entrypoint)
    _wire(f, agent, team)
    _start_member_objective(team, "researcher", "d1", "obj", "ev", "done")

    sequence = ["a.py", "a.py", "b.py", "a.py", "b.py", "c.py"]
    for path in sequence:
        _run(_call(f, relative_path=path))

    assert calls["a.py"] == 1
    assert calls["b.py"] == 1
    assert calls["c.py"] == 1
    total_fresh_executions = sum(calls.values())
    assert total_fresh_executions == 3

    state = _records_for(team, "researcher", "d1")
    assert state.total_actions == 6
    # logical calls (6) == fresh executions (3) + cache serves (3)
    assert state.total_actions == total_fresh_executions + 3


# ── Test 6: Phase B.1 member-id normalization survives the relocation ──────


def test_6_member_id_normalization_still_works_through_the_real_chain():
    """The hook is no longer bound to a per-role `role` argument -- `who`
    now resolves ENTIRELY from agent.name (display-cased, e.g.
    'Researcher'). Must still normalize to match the Coordinator's own
    lowercase delegation argument, for both the first AND a subsequent
    delegation."""
    team = _Team()
    agent = _Agent("Researcher")  # display-cased, like the real agent.name
    calls = {"n": 0}

    async def entrypoint(relative_path=None):
        calls["n"] += 1
        return "ok"

    f = _make_tool(entrypoint)
    _wire(f, agent, team)

    _start_member_objective(team, "researcher", "d1", "first", "ev", "done")
    _run(_call(f, relative_path="a.py"))
    assert _records_for(team, "researcher", "d1").total_actions == 1

    _start_member_objective(team, "researcher", "d2", "second", "ev", "done")
    _run(_call(f, relative_path="b.py"))
    assert _records_for(team, "researcher", "d2").total_actions == 1


# ── Test 7: Coordinator-level calls remain a correct no-op ──────────────


def test_7_coordinator_call_with_no_delegation_remains_a_noop():
    """agent=None (a coordinator's own direct tool call) must still resolve
    to the 'Coordinator' fallback and correctly find no MemberControlState
    -- unaffected by the relocation."""
    team = _Team()
    calls = {"n": 0}

    async def entrypoint(relative_path=None):
        calls["n"] += 1
        return "ok"

    f = _make_tool(entrypoint)
    _wire(f, None, team)

    _run(_call(f, relative_path="a.py"))
    assert calls["n"] == 1
    assert _get_member_control_box(team).list() == []
