"""Regression test: every member agent (ContextRouter, Researcher, Coder,
Executor, Reviewer) must get the same repetition-loop protection the coordinator has
had since 2026-08-10 -- pinned temperature, a bounded max_tokens, and a repetition
penalty.

Confirmed live 2026-08-12: a phase-1 retest stalled for 4+ minutes inside a Researcher
turn with the exact signature config.coordinator_temperature's own docstring
describes for the coordinator (steady real generation throughput and a steadily
growing GPU KV cache per vLLM's own metrics, zero surfaced content) -- but
Researcher, like every other member agent, was still on get_model()'s raw defaults
(temperature=1.0, unbounded max_tokens, no frequency_penalty) because every
member-agent-building get_model() call in swarm/agents.py passed only (model_id,
host). The 2026-08-10 fix was deliberately scoped to the coordinator only, on the
assumption "Researcher/Coder plausibly benefit from more sampling variance" -- this
live incident is the direct counter-evidence to that assumption.

Uses the vLLM/OpenAILike backend throughout (not Ollama) -- get_model()'s Ollama
branch intentionally drops temperature/max_tokens/frequency_penalty entirely (agno's
Ollama model class takes sampling params via a different, nested shape), so only the
vllm branch actually surfaces these on the returned model object to assert against.
"""
import pytest

from api.models import AgentSpec
from config.config import config
from swarm.agents import (
    make_agent_from_spec, make_coder, make_context_router, make_executor,
    make_researcher, make_reviewer,
)


@pytest.fixture(autouse=True)
def _vllm_backend(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr(config, "member_temperature", 0.2)
    monkeypatch.setattr(config, "member_frequency_penalty", 0.15)
    monkeypatch.setattr(config, "member_max_tokens", 4096)
    monkeypatch.setattr(config, "coder_max_tokens", 8192)


def test_make_agent_from_spec_non_coder_gets_member_sampling_params():
    spec = AgentSpec(name="Researcher", role="engineer", model="qwen2.5-coder:32b", instructions=["x"])

    agent = make_agent_from_spec(spec)

    assert agent.model.temperature == 0.2
    assert agent.model.frequency_penalty == 0.15
    assert agent.model.max_tokens == 4096


def test_make_agent_from_spec_named_coder_no_longer_auto_gets_the_larger_budget():
    """Recommendation #4 (2026-08-13, see DOCS.md "Declarative Per-Role Policy")
    deliberately removed make_agent_from_spec's old `if spec.name == "Coder"`
    name-based special-case -- a raw AgentSpec named "Coder" with no explicit
    max_tokens (e.g. a direct RunRequest.agents POST, which never goes through
    _load_team()'s DB-fallback resolution) now gets the same member_max_tokens
    every other role gets, not a silent magic bump by name. The next test shows
    how a caller gets the larger budget now: set it explicitly."""
    spec = AgentSpec(name="Coder", role="engineer", model="qwen2.5-coder:32b", instructions=["x"])

    agent = make_agent_from_spec(spec)

    assert agent.model.max_tokens == 4096  # member_max_tokens, NOT coder_max_tokens


def test_make_agent_from_spec_honors_an_explicit_max_tokens_on_the_spec():
    """The replacement mechanism for Coder's larger budget: _load_team()
    (api/server.py) now sets AgentSpec.max_tokens explicitly from a team YAML
    field or a team_role_models DB row (Coder's max_tokens=8192 lives there --
    see swarm/model_routing.py's _TEAM_ROLE_POLICY_OVERRIDES) before
    make_agent_from_spec ever sees the spec. Same end result for the shipped
    engineering.yaml, achieved declaratively instead of by name-matching in code."""
    spec = AgentSpec(
        name="Coder", role="engineer", model="qwen2.5-coder:32b", instructions=["x"],
        max_tokens=8192,
    )

    agent = make_agent_from_spec(spec)

    assert agent.model.temperature == 0.2
    assert agent.model.frequency_penalty == 0.15
    assert agent.model.max_tokens == 8192


def test_make_agent_from_spec_honors_an_explicit_temperature_and_tool_call_limit():
    """The other two declarative fields, independent of max_tokens -- any role
    can now override any one of the three without needing a name-matched
    special-case in code for it."""
    spec = AgentSpec(
        name="Researcher", role="engineer", model="qwen2.5-coder:32b", instructions=["x"],
        temperature=0.05, tool_call_limit=3,
    )

    agent = make_agent_from_spec(spec)

    assert agent.model.temperature == 0.05
    assert agent.tool_call_limit == 3
    assert agent.model.max_tokens == 4096  # untouched -- falls back to member_max_tokens


def test_make_coder_gets_coder_max_tokens():
    agent = make_coder()

    assert agent.model.temperature == 0.2
    assert agent.model.frequency_penalty == 0.15
    assert agent.model.max_tokens == 8192


@pytest.mark.parametrize(
    "make_fn", [make_reviewer, make_researcher, make_executor, make_context_router]
)
def test_other_member_builders_get_member_max_tokens(make_fn):
    agent = make_fn()

    assert agent.model.temperature == 0.2
    assert agent.model.frequency_penalty == 0.15
    assert agent.model.max_tokens == 4096


def test_member_sampling_params_are_independent_of_coordinator_values(monkeypatch):
    """Changing the coordinator's own tuned values must not affect member agents --
    they are separate config fields, not aliases, so a future coordinator-specific
    retune can't silently also retune every member agent."""
    monkeypatch.setattr(config, "coordinator_temperature", 0.9)
    monkeypatch.setattr(config, "coordinator_max_tokens", 999)
    monkeypatch.setattr(config, "coordinator_frequency_penalty", 0.99)

    agent = make_researcher()

    assert agent.model.temperature == 0.2
    assert agent.model.max_tokens == 4096
    assert agent.model.frequency_penalty == 0.15
