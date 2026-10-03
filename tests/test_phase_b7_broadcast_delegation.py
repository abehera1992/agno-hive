"""Phase B.7 (2026-10-02): fixes the structured-delegation wrapper's assumption
that every agno-resolved delegation entrypoint is the singular per-member
`adelegate_task_to_member(member_id, task)`. Proven false live in B.6: a
`mode: broadcast` team (parallel-review) resolves the PLURAL
`adelegate_task_to_members(task)` instead (agno/team/_default_tools.py:1153,
branching on `team.delegate_to_all_members`) -- a function with NO `member_id`
parameter at all, that resolves `team.members` itself and dispatches to every
one of them internally. The old unconditional `original_entrypoint(member_id=
member_id, task=canonical_task)` call raised:

    TypeError: ...adelegate_task_to_members() got an unexpected keyword
    argument 'member_id'

before `member_agent.run()` was ever reached -- `_start_member_objective` had
already run (it precedes this call), so `CONTROL_BOX_OBJECTIVE_STARTED` fired
correctly, but zero tool calls meant zero `CONTROL_BOX_ACTION_RECORDED` was
the CORRECT outcome, not a Control Box defect (B.5's own "defensive-only
branch" conclusion, observed under a new trigger).

The fix detects broadcast purely from `original_function.name ==
"delegate_task_to_members"` -- the same intrinsic signal agno itself sets via
`Function.from_callable(delegate_function, name=...)` -- never from a team
name/type check, so it applies to ANY current or future broadcast team.  For
broadcast, it (a) starts ONE Control Box objective per REAL resolved member
(not just the one, dispatch-irrelevant `member_id` string the model supplied)
and (b) calls `original_entrypoint(task=canonical_task)` with no `member_id`.
Coordinate-mode dispatch is byte-for-byte unchanged.

These tests follow test_phase_s_structural_delegation.py's own convention
(the `_FakeFunction` stand-in only ever needs `.entrypoint`/
`.stop_after_tool_call`/`.show_result`, now also `.name` for mode detection)
for the dispatch-logic tests, and test_member_control_box_cache_interaction.
py's convention (real agno Function/FunctionCall objects through the real
hook chain) for the Control Box telemetry tests -- no Control Box code is
touched or re-implemented here, only exercised through its real, unchanged
entry points.
"""
import asyncio

import pytest
from agno.tools.function import Function, FunctionCall

import swarm.team as team_mod
from swarm.team import (
    _build_structured_delegation_tool, _get_member_control_box,
    _make_member_control_box_hook, _start_member_objective,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeFunction:
    """Same minimal stand-in as test_phase_s_structural_delegation.py's own,
    extended with `.name` -- the ONLY new signal Phase B.7 reads to tell a
    singular entrypoint from a broadcast one."""

    def __init__(self, entrypoint, name):
        self.entrypoint = entrypoint
        self.name = name
        self.stop_after_tool_call = False
        self.show_result = True


class _Agent:
    def __init__(self, name: str):
        self.name = name


class _Team:
    """Bare stand-in, same convention as every other Control Box test file's
    own _Team -- real attribute access only (`.members`, and whatever
    _get_member_control_box lazily attaches)."""

    def __init__(self, members=None):
        self.members = members or []


def _coordinate_entrypoint(calls):
    """Matches agno's real adelegate_task_to_member's signature exactly --
    (member_id, task). Calling this with anything else raises TypeError,
    the same shape as the real production failure."""

    async def entrypoint(member_id, task):
        calls.append({"member_id": member_id, "task": task})
        yield f"[delegating to {member_id}]"
        yield f"Agent {member_id}: fake result"

    return entrypoint


def _broadcast_entrypoint(calls):
    """Matches agno's real adelegate_task_to_members's signature exactly --
    (task) ONLY. Calling this with a `member_id` kwarg raises
    `TypeError: ...got an unexpected keyword argument 'member_id'` -- the
    exact live B.6 failure -- so any regression back to the unconditional
    `member_id=member_id` call is caught immediately by this fake, not
    silently absorbed."""

    async def entrypoint(task):
        calls.append({"task": task})
        yield "[broadcasting to all members]"
        yield "Researcher: fake result"
        yield "SecurityReviewer: fake result"
        yield "PerformanceReviewer: fake result"

    return entrypoint


def _dispatch(fake_function, team, member_id="researcher"):
    new_function = _build_structured_delegation_tool(fake_function, team)
    chunks = _run(_drain(new_function.entrypoint(
        member_id=member_id, target="t", objective="o",
        evidence_required="e", completion_criteria="c",
    )))
    return chunks


async def _drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


# ── Test 1: coordinate regression -- singular path byte-identical ──────────


def test_1_coordinate_mode_dispatch_unchanged():
    calls = []
    fake = _FakeFunction(_coordinate_entrypoint(calls), name="delegate_task_to_member")
    team = _Team(members=[_Agent("Researcher")])

    chunks = _dispatch(fake, team, member_id="researcher")

    assert calls == [{"member_id": "researcher", "task": calls[0]["task"]}]
    assert chunks[-1] == "Agent researcher: fake result"


# ── Test 2: broadcast invocation uses the real (task)-only signature ───────


def test_2_broadcast_mode_calls_entrypoint_with_task_only():
    calls = []
    fake = _FakeFunction(_broadcast_entrypoint(calls), name="delegate_task_to_members")
    team = _Team(members=[_Agent("Researcher"), _Agent("SecurityReviewer"),
                           _Agent("PerformanceReviewer")])

    chunks = _dispatch(fake, team, member_id="securityreviewer")

    assert calls == [{"task": calls[0]["task"]}]  # no member_id key at all
    assert chunks[0] == "[broadcasting to all members]"


# ── Test 3: broadcast hand-off succeeds (agno's own member_agent.run() fan-out
# is agno's code, proven correct by direct source reading in Phase B.7 Phase 2
# -- this proves OUR wrapper hands off without crashing, which is what was
# broken) ────────────────────────────────────────────────────────────────────


def test_3_broadcast_dispatch_completes_without_our_wrapper_raising():
    calls = []
    fake = _FakeFunction(_broadcast_entrypoint(calls), name="delegate_task_to_members")
    team = _Team(members=[_Agent("Researcher")])

    chunks = _dispatch(fake, team)  # would raise before reaching here if broken

    assert len(chunks) == 4
    assert len(calls) == 1


# ── Test 4: the exact B.6 TypeError does not recur ──────────────────────────


def test_4_no_typeerror_for_unexpected_member_id_kwarg():
    calls = []
    fake = _FakeFunction(_broadcast_entrypoint(calls), name="delegate_task_to_members")
    team = _Team(members=[_Agent("Researcher")])

    try:
        _dispatch(fake, team, member_id="whatever-the-model-typed")
    except TypeError as e:
        pytest.fail(f"B.6 regression: broadcast dispatch raised {e!r}")


# ── Test 5: Control Box objective-start fires for EVERY real resolved member,
# not just the one (dispatch-irrelevant) member_id the model supplied ────────


def test_5_broadcast_starts_one_objective_per_real_member():
    calls = []
    fake = _FakeFunction(_broadcast_entrypoint(calls), name="delegate_task_to_members")
    team = _Team(members=[_Agent("Researcher"), _Agent("SecurityReviewer"),
                           _Agent("PerformanceReviewer")])

    _dispatch(fake, team, member_id="securityreviewer")  # irrelevant value

    started = {s.member_id for s in _get_member_control_box(team).list()}
    assert started == {"researcher", "security-reviewer", "performance-reviewer"}


def test_5b_coordinate_still_starts_exactly_one_objective():
    calls = []
    fake = _FakeFunction(_coordinate_entrypoint(calls), name="delegate_task_to_member")
    team = _Team(members=[_Agent("Researcher"), _Agent("SecurityReviewer")])

    _dispatch(fake, team, member_id="researcher")

    started = {s.member_id for s in _get_member_control_box(team).list()}
    assert started == {"researcher"}  # SecurityReviewer never started -- unaffected


# ── Test 6: once a broadcast member actually executes a tool, the real,
# unchanged Control Box hook chain records it exactly once ─────────────────


def _make_tool(entrypoint):
    return Function(name="get_file_content", entrypoint=entrypoint,
                     tool_hooks=[team_mod._make_member_control_box_hook()])


async def _call(function, agent, team, **kwargs):
    function._agent = agent
    function._team = team
    fc = FunctionCall(function=function, arguments=kwargs)
    result = await fc.aexecute()
    return result.result


def test_6_broadcast_started_member_records_exactly_one_real_tool_action():
    team = _Team()
    _start_member_objective(team, "SecurityReviewer", "dkey", "review", "ev", "done")

    calls = {"n": 0}

    async def entrypoint(relative_path=None):
        calls["n"] += 1
        return f"content-{calls['n']}"

    f = _make_tool(entrypoint)
    agent = _Agent("SecurityReviewer")  # display-cased, like real agent.name

    result = _run(_call(f, agent, team, relative_path="security.py"))

    assert result == "content-1"
    state = _get_member_control_box(team).get("security-reviewer", "dkey")
    assert state is not None
    assert state.total_actions == 1


# ── Test 7: multiple broadcast members are independently observable -- no
# cross-attribution between them ────────────────────────────────────────────


def test_7_multiple_broadcast_members_do_not_cross_attribute():
    team = _Team()
    for member_id in ("Researcher", "SecurityReviewer", "PerformanceReviewer"):
        _start_member_objective(team, member_id, "shared-dkey", "review the module",
                                 "ev", "done")

    async def entrypoint(relative_path=None):
        return "ok"

    for member_name, expected_key in (
        ("Researcher", "researcher"), ("SecurityReviewer", "security-reviewer"),
        ("PerformanceReviewer", "performance-reviewer"),
    ):
        f = _make_tool(entrypoint)
        _run(_call(f, _Agent(member_name), team, relative_path="x.py"))

    box = _get_member_control_box(team)
    for key in ("researcher", "security-reviewer", "performance-reviewer"):
        state = box.get(key, "shared-dkey")
        assert state is not None
        assert state.total_actions == 1, f"{key} must show exactly its own action"


# ── Test 8: cache-hit telemetry survives for state started via the NEW
# broadcast branch (previously only proven for the singular branch, B.4) ───


def test_8_cache_hit_recorded_for_broadcast_started_state():
    team = _Team()
    _start_member_objective(team, "SecurityReviewer", "dkey", "review", "ev", "done")

    calls = {"n": 0}

    async def entrypoint(relative_path=None):
        calls["n"] += 1
        return f"content-{calls['n']}"

    hooks = [team_mod._make_member_control_box_hook(), team_mod._make_read_cache_tool_hook()]
    f = Function(name="get_file_content", entrypoint=entrypoint, tool_hooks=hooks)
    agent = _Agent("SecurityReviewer")

    _run(_call(f, agent, team, relative_path="security.py"))       # fresh
    _run(_call(f, agent, team, relative_path="security.py"))       # cache hit

    assert calls["n"] == 1, "underlying tool must execute exactly once"
    state = _get_member_control_box(team).get("security-reviewer", "dkey")
    assert state.total_actions == 2  # both calls recorded, including the cache hit
