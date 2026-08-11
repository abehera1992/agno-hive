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


# ── read_only-scoped max_iterations (2026-08-11) ────────────────────────────────
# Confirmed live: a read-only research task reached a fully correct, complete answer
# by roughly its 6th delegate_task_to_member round, then kept delegating for 8+ MORE
# rounds and 40,000+ characters -- reading an entirely unrelated API's full CRUD
# implementation nobody asked about. The default max_iterations=25 never came close
# to catching this. A read-only run structurally can't need the full pipeline's
# iteration budget (writes are stripped from every agent, so there's no
# Coder/Executor implementation phase to budget rounds for), so it gets its own,
# tighter ceiling instead of lowering the global default (which DOES need headroom
# for a real multi-file write pipeline: Coordinator -> ContextRouter -> Researcher ->
# Planner -> Coder -> Executor -> Reviewer).

def test_build_team_uses_the_default_max_iterations_when_not_read_only(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.max_iterations", 25)
    monkeypatch.setattr("swarm.team.config.read_only_max_iterations", 10)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
        read_only=False,
    )

    assert result.max_iterations == 25


def test_build_team_uses_the_tighter_ceiling_when_read_only(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.max_iterations", 25)
    monkeypatch.setattr("swarm.team.config.read_only_max_iterations", 10)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
        read_only=True,
    )

    assert result.max_iterations == 10


def test_build_team_read_only_max_iterations_follows_config_override(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.team.config.max_iterations", 25)
    monkeypatch.setattr("swarm.team.config.read_only_max_iterations", 6)

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
        read_only=True,
    )

    assert result.max_iterations == 6
