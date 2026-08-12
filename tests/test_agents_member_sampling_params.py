"""Regression test: every member agent (ContextRouter, Researcher, Planner, Coder,
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
    make_planner, make_researcher, make_reviewer,
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


def test_make_agent_from_spec_coder_gets_the_larger_coder_max_tokens():
    spec = AgentSpec(name="Coder", role="engineer", model="qwen2.5-coder:32b", instructions=["x"])

    agent = make_agent_from_spec(spec)

    assert agent.model.temperature == 0.2
    assert agent.model.frequency_penalty == 0.15
    assert agent.model.max_tokens == 8192


def test_make_coder_gets_coder_max_tokens():
    agent = make_coder()

    assert agent.model.temperature == 0.2
    assert agent.model.frequency_penalty == 0.15
    assert agent.model.max_tokens == 8192


@pytest.mark.parametrize(
    "make_fn", [make_reviewer, make_planner, make_researcher, make_executor, make_context_router]
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
