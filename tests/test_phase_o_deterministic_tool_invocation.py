"""Tests for Phase O (2026-10-04) -- generic, role-agnostic deterministic
required-tool invocation: swarm/team.py's _arm_required_tool,
_clear_required_tool, _arm_required_tool_if_available, and
_make_required_tool_clearing_hook.

Generalizes Phase P's forward_member_answer-specific forcing into shared
primitives reusable by any role for any policy-required tool. Mirrors
tests/test_tool_budget_guard.py's fixture pattern (a tiny fake agent with
.name/.tool_choice/.model._tool_choice) since these primitives operate on
exactly that same shape, regardless of which real role constructs it.

Case B (ambiguous: >1 required tool, no sequencing mechanism exists) is
deliberately NOT unit-tested here as a standalone function: the decision
("log and treat as empty") lives inline inside _build_team's local
_hooks_for() closure, a 6-line block, and extracting a redundant testable
wrapper for it would create a second source of truth for a decision this
small. It is covered by reading swarm/team.py's _hooks_for directly (the
`if len(_policy_required) > 1:` block) -- an explained gap, not a silent one.
"""
import pytest

from swarm.team import (
    _arm_required_tool,
    _arm_required_tool_if_available,
    _clear_required_tool,
    _make_required_tool_clearing_hook,
)


class _Agent:
    def __init__(self, name, tools=None):
        self.name = name
        self.tool_choice = None
        self.model = type("M", (), {"_tool_choice": None})()
        self.tools = tools or []


class _Tool:
    def __init__(self, name):
        self.name = name


async def _fn(**kwargs):
    return "REAL RESULT"


def _forced(tool_name: str) -> dict:
    return {"type": "function", "function": {"name": tool_name}}


# ── Case A: exactly one required tool ───────────────────────────────────────

def test_case_a_one_required_tool_is_armed():
    agent = _Agent("Coordinator")
    state: dict = {"active": False, "tool_name": None, "target": None}

    _arm_required_tool(agent, "tool_x", state)

    assert agent.tool_choice == _forced("tool_x")
    assert agent.model._tool_choice == _forced("tool_x")
    assert state["active"] is True
    assert state["tool_name"] == "tool_x"
    assert state["target"] is agent


# ── Restoration (the clearing hook, the common execution boundary) ─────────

@pytest.mark.asyncio
async def test_clearing_hook_restores_auto_after_matching_call():
    agent = _Agent("Coordinator")
    state: dict = {"active": False, "tool_name": None, "target": None}
    _arm_required_tool(agent, "tool_x", state)
    required_tool_states = {"Coordinator": state}

    hook = _make_required_tool_clearing_hook(required_tool_states)
    result = await hook("tool_x", _fn, {}, agent=agent, team=None)

    assert result == "REAL RESULT"
    assert agent.tool_choice is None
    assert agent.model._tool_choice is None
    assert state["active"] is False


@pytest.mark.asyncio
async def test_clearing_hook_restores_auto_even_on_mismatched_call_name():
    """Defensive backstop (documented in _make_required_tool_clearing_hook's
    own docstring): tool_choice forcing a specific function contractually
    means the model cannot call anything else, so observing a DIFFERENT
    function_name while active is itself a violation worth surfacing --
    but the force must still clear rather than get stuck."""
    agent = _Agent("Coordinator")
    state: dict = {"active": False, "tool_name": None, "target": None}
    _arm_required_tool(agent, "tool_x", state)
    required_tool_states = {"Coordinator": state}

    hook = _make_required_tool_clearing_hook(required_tool_states)
    result = await hook("tool_y", _fn, {}, agent=agent, team=None)

    assert result == "REAL RESULT"
    assert agent.tool_choice is None
    assert state["active"] is False


# ── Case D: required tool unavailable -- fail closed ────────────────────────

def test_case_d_required_tool_unavailable_fails_closed():
    target = _Agent("Researcher", tools=[_Tool("other_tool")])
    state: dict = {"active": False, "tool_name": None, "target": None}

    armed = _arm_required_tool_if_available(
        target, "missing_tool", state, role="Researcher", team_name="engineering")

    assert armed is False
    assert target.tool_choice is None
    assert state["active"] is False


def test_case_d_required_tool_available_succeeds():
    target = _Agent("Researcher", tools=[_Tool("other_tool"), _Tool("required_tool")])
    state: dict = {"active": False, "tool_name": None, "target": None}

    armed = _arm_required_tool_if_available(
        target, "required_tool", state, role="Researcher", team_name="engineering")

    assert armed is True
    assert target.tool_choice == _forced("required_tool")
    assert state["active"] is True


# ── Cross-run isolation ──────────────────────────────────────────────────────

def test_cross_run_isolation_two_states_never_interfere():
    agent_a = _Agent("Coordinator")
    agent_b = _Agent("Coordinator")  # same role name, DIFFERENT run/instance
    state_a: dict = {"active": False, "tool_name": None, "target": None}
    state_b: dict = {"active": False, "tool_name": None, "target": None}

    _arm_required_tool(agent_a, "tool_a", state_a)
    _arm_required_tool(agent_b, "tool_b", state_b)

    assert agent_a.tool_choice == _forced("tool_a")
    assert agent_b.tool_choice == _forced("tool_b")
    assert state_a["tool_name"] == "tool_a"
    assert state_b["tool_name"] == "tool_b"


@pytest.mark.asyncio
async def test_cross_run_isolation_clearing_one_does_not_affect_the_other():
    """In production, _required_tool_states is a closure-local dict created
    FRESH inside _build_team() for every single run -- isolation comes from
    there being two separate dicts (one per run), not from distinguishing
    names within one shared dict, even when both runs use the SAME role name
    ("Coordinator" in Run A is never the same dict entry as "Coordinator" in
    Run B, because they live in two different _required_tool_states dicts
    built by two different _build_team() calls)."""
    agent_a = _Agent("Coordinator")
    agent_b = _Agent("Coordinator")
    state_a: dict = {"active": False, "tool_name": None, "target": None}
    state_b: dict = {"active": False, "tool_name": None, "target": None}
    _arm_required_tool(agent_a, "tool_a", state_a)
    _arm_required_tool(agent_b, "tool_b", state_b)

    required_tool_states_run_a = {"Coordinator": state_a}
    hook_a = _make_required_tool_clearing_hook(required_tool_states_run_a)
    await hook_a("tool_a", _fn, {}, agent=agent_a, team=None)

    assert agent_a.tool_choice is None
    assert state_a["active"] is False
    # Run B's state and target are completely untouched by Run A's clearing --
    # state_b was never reachable from required_tool_states_run_a at all.
    assert agent_b.tool_choice == _forced("tool_b")
    assert state_b["active"] is True


# ── Failure cleanup (section 16, mandatory) ─────────────────────────────────

@pytest.mark.asyncio
async def test_failure_cleanup_state_cleared_even_when_tool_raises():
    agent = _Agent("Coordinator")
    state: dict = {"active": False, "tool_name": None, "target": None}
    _arm_required_tool(agent, "tool_x", state)

    async def _raising_fn(**kwargs):
        raise RuntimeError("boom")

    hook = _make_required_tool_clearing_hook({"Coordinator": state})
    with pytest.raises(RuntimeError, match="boom"):
        await hook("tool_x", _raising_fn, {}, agent=agent, team=None)

    # The exception propagated (not swallowed) AND cleanup still happened.
    assert state["active"] is False
    assert agent.tool_choice is None


# ── Role-agnosticism (section 2/9/14: one mechanism, every role) ───────────

@pytest.mark.parametrize("role_name", ["Coordinator", "Researcher", "Executor"])
@pytest.mark.asyncio
async def test_role_agnostic_primitives_work_identically_for_any_role_shaped_target(role_name):
    """No production agent_capability_policy rows exist for Researcher/Executor
    (Phase O section 14 explicitly says not to seed artificial production rows
    just to pass a test) -- this proves the MECHANISM itself is role-agnostic
    by construction, independent of which real role eventually gets a required-
    tool policy row, without needing one to exist yet."""
    target = _Agent(role_name, tools=[_Tool("required_tool")])
    state: dict = {"active": False, "tool_name": None, "target": None}

    armed = _arm_required_tool_if_available(
        target, "required_tool", state, role=role_name, team_name="engineering")
    assert armed is True
    assert target.tool_choice == _forced("required_tool")

    hook = _make_required_tool_clearing_hook({role_name: state})
    result = await hook("required_tool", _fn, {}, agent=target, team=None)
    assert result == "REAL RESULT"
    assert target.tool_choice is None
    assert state["active"] is False
