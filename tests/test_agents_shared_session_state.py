"""Regression test: every member agent builder (make_agent_from_spec, make_coder,
make_reviewer, make_planner, make_researcher, make_executor, make_context_router)
must have BOTH halves of agno-hive's own session_state mechanism wired in:
add_session_state_to_context=True (agno's own, working correctly -- renders the
current state into the agent's own prompt automatically) and update_session_state
present in its tools list (agno-hive's OWN replacement for agno's broken
Agent.enable_agentic_state=True -- see swarm/agents.py's own docstring on
update_session_state for the confirmed-live agno 2.5.17 bug this avoids:
typing.get_type_hints() does not support the functools.partial-wrapped tool agno's
own enable_agentic_state machinery builds).

Mirrors test_agents_member_sampling_params.py's structure -- see that file's
docstring for why the coordinator-only-fix mistake matters here too: turning this
on for the Team but not every member would mean a delegated member never sees
what a sibling already established, which is exactly the gap this exists to
close (see swarm/team.py's _record_read / _make_delegation_log_hook and
tests/test_team_shared_session_state.py for the mechanical writers this makes
visible).

No initial session_state=... value is asserted here -- agno hands each delegated
member a deepcopy of the TEAM's current session_state at dispatch time regardless
of what the member itself was constructed with (team/_task_tools.py); the one
real seed dict lives on the Team object, tested in
test_team_shared_session_state.py.
"""
import pytest

from api.models import AgentSpec
from config.config import config
from swarm.agents import (
    _update_session_state_impl, make_agent_from_spec, make_coder, make_context_router,
    make_executor, make_planner, make_researcher, make_reviewer, update_session_state,
)


@pytest.fixture(autouse=True)
def _vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")


# ── _update_session_state_impl: the real logic behind the update_session_state tool ──
# Tested directly rather than through the @agno_tool-decorated update_session_state
# wrapper (a thin pass-through -- see its own body) -- the same split
# request_clarification's own tests don't need, since that tool's body is a single
# literal return with no logic to isolate.


class _FakeRunContext:
    def __init__(self, session_state):
        self.session_state = session_state


def test_merges_updates_into_existing_session_state():
    run_context = _FakeRunContext({"existing_key": "existing_value"})

    result = _update_session_state_impl({"new_key": "new_value"}, run_context)

    assert run_context.session_state == {"existing_key": "existing_value", "new_key": "new_value"}
    assert "new_key" in result


def test_overwrites_an_existing_key_with_the_same_name():
    run_context = _FakeRunContext({"fact": "old"})

    _update_session_state_impl({"fact": "new"}, run_context)

    assert run_context.session_state["fact"] == "new"


def test_none_run_context_returns_a_message_instead_of_crashing():
    result = _update_session_state_impl({"key": "value"}, None)

    assert "No shared state" in result


def test_none_session_state_returns_a_message_instead_of_crashing():
    run_context = _FakeRunContext(None)

    result = _update_session_state_impl({"key": "value"}, run_context)

    assert "No shared state" in result
    assert run_context.session_state is None  # untouched, not silently replaced


def test_missing_run_context_argument_does_not_crash():
    """Defensive default (run_context: RunContext = None) -- a caller that omits
    it entirely must not raise a TypeError."""
    result = _update_session_state_impl({"key": "value"})

    assert "No shared state" in result


# ── update_session_state: the real @agno_tool-wrapped Function object ──────────────
# Shape-only, matching how test_team_request_clarification_tool.py tests
# request_clarification -- a decorated function becomes an agno Function object,
# not a plain callable.

def test_update_session_state_is_a_function_named_correctly():
    assert update_session_state.name == "update_session_state"


def test_update_session_state_does_not_stop_the_run():
    """Unlike request_clarification, this is routine bookkeeping mid-run -- the
    agent must continue working after calling it, not end its turn."""
    assert not getattr(update_session_state, "stop_after_tool_call", False)


def test_make_agent_from_spec_has_shared_state_wired_in():
    spec = AgentSpec(name="Researcher", role="engineer", model="qwen2.5-coder:32b", instructions=["x"])

    agent = make_agent_from_spec(spec)

    assert agent.add_session_state_to_context is True
    assert update_session_state in agent.tools


def test_make_agent_from_spec_never_sets_agnos_own_broken_flag():
    """enable_agentic_state=True must never be set anywhere -- it would silently
    add a SECOND, broken, identically-named update_session_state tool alongside
    agno-hive's own working one (see swarm/agents.py's docstring)."""
    spec = AgentSpec(name="Researcher", role="engineer", model="qwen2.5-coder:32b", instructions=["x"])

    agent = make_agent_from_spec(spec)

    assert agent.enable_agentic_state is False


@pytest.mark.parametrize(
    "make_fn",
    [make_coder, make_reviewer, make_planner, make_researcher, make_executor, make_context_router],
)
def test_common_kwargs_builders_have_shared_state_wired_in(make_fn):
    agent = make_fn()

    assert agent.add_session_state_to_context is True
    assert update_session_state in agent.tools
    assert agent.enable_agentic_state is False


@pytest.mark.parametrize("make_fn", [make_coder, make_reviewer, make_planner, make_researcher, make_executor])
def test_base_preamble_builders_mention_shared_state_in_instructions(make_fn):
    """ContextRouter deliberately excluded -- its instructions list does not
    include _BASE_PREAMBLE (kept minimal/fast by design); it still gets the
    mechanical wiring above, just not the explicit prose pointer, since the
    mechanism doesn't depend on the model reading instructions to work."""
    agent = make_fn()

    assert any("SHARED STATE" in instr for instr in agent.instructions)
