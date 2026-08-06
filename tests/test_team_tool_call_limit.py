"""Regression test: the coordinator Team must set agno's own tool_call_limit, not
just max_iterations.

Confirmed live 2026-08-06: a Coder made 18+ consecutive apply_diff calls with an
identical, hallucinated old_string, each one correctly refused by hive-mcp, each
refusal ignored -- 36+ total tool calls, all inside what the coordinator's own
max_iterations counted as a SINGLE one of its own iterations (max_iterations bounds
the coordinator's delegate/decide loop, not tool calls made INSIDE one delegation).
tool_call_limit is agno's own framework-enforced cap, checked at the model-call
layer -- a real circuit breaker, not an advisory text response a model can ignore.
"""
from swarm.team import _build_team


def test_build_team_sets_tool_call_limit(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.tool_call_limit", 25)
    monkeypatch.setattr("swarm.team.config.max_iterations", 25)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.tool_call_limit == 25
    assert result.max_iterations == 25


def test_build_team_tool_call_limit_follows_config_override(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.tool_call_limit", 40)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.tool_call_limit == 40
