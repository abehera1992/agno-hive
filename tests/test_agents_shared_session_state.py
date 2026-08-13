"""Regression test: every member agent builder (make_agent_from_spec, make_coder,
make_reviewer, make_planner, make_researcher, make_executor, make_context_router)
must enable agno's own session_state mechanism -- enable_agentic_state (adds the
real update_session_state tool) and add_session_state_to_context (renders the
current state into the agent's own prompt automatically).

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
    make_agent_from_spec, make_coder, make_context_router, make_executor,
    make_planner, make_researcher, make_reviewer,
)


@pytest.fixture(autouse=True)
def _vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")


def test_make_agent_from_spec_enables_agentic_state():
    spec = AgentSpec(name="Researcher", role="engineer", model="qwen2.5-coder:32b", instructions=["x"])

    agent = make_agent_from_spec(spec)

    assert agent.enable_agentic_state is True
    assert agent.add_session_state_to_context is True


@pytest.mark.parametrize(
    "make_fn",
    [make_coder, make_reviewer, make_planner, make_researcher, make_executor, make_context_router],
)
def test_common_kwargs_builders_enable_agentic_state(make_fn):
    agent = make_fn()

    assert agent.enable_agentic_state is True
    assert agent.add_session_state_to_context is True


@pytest.mark.parametrize("make_fn", [make_coder, make_reviewer, make_planner, make_researcher, make_executor])
def test_base_preamble_builders_mention_shared_state_in_instructions(make_fn):
    """ContextRouter deliberately excluded -- its instructions list does not
    include _BASE_PREAMBLE (kept minimal/fast by design); it still gets the
    mechanical enable_agentic_state/add_session_state_to_context wiring above,
    just not the explicit prose pointer, since the mechanism doesn't depend on
    the model reading instructions to work."""
    agent = make_fn()

    assert any("SHARED STATE" in instr for instr in agent.instructions)
