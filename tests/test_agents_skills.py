from api.models import AgentSpec
from swarm.agents import format_skill_catalog, make_agent_from_spec, make_coder


_CATALOG = [
    {"name": "notion-grounding", "description": "Notion rules", "source": "hive-mcp"},
    {"name": "db-facts", "description": "DB rules", "source": "hive-mcp"},
]


def test_format_skill_catalog_lists_all_when_names_is_none():
    text = format_skill_catalog(_CATALOG, None)

    assert "notion-grounding: Notion rules" in text
    assert "db-facts: DB rules" in text


def test_format_skill_catalog_filters_to_given_names():
    text = format_skill_catalog(_CATALOG, ["db-facts"])

    assert "db-facts: DB rules" in text
    assert "notion-grounding" not in text


def test_format_skill_catalog_empty_catalog_returns_empty_string():
    assert format_skill_catalog([], None) == ""


def test_format_skill_catalog_no_matching_names_returns_empty_string():
    assert format_skill_catalog(_CATALOG, ["does-not-exist"]) == ""


def test_make_agent_from_spec_appends_filtered_catalog_to_instructions(monkeypatch):
    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    spec = AgentSpec(
        name="Coder", role="engineer", model="qwen2.5-coder:32b",
        instructions=["base instruction"], skills=["db-facts"],
    )

    agent = make_agent_from_spec(spec, skill_catalog=_CATALOG)

    joined = "\n".join(agent.instructions)
    assert "base instruction" in joined
    assert "db-facts: DB rules" in joined
    assert "notion-grounding" not in joined


def test_make_agent_from_spec_without_skill_catalog_is_unchanged(monkeypatch):
    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    spec = AgentSpec(
        name="Coder", role="engineer", model="qwen2.5-coder:32b",
        instructions=["base instruction"],
    )

    agent = make_agent_from_spec(spec)

    assert agent.instructions == ["base instruction"]


def test_make_agent_from_spec_sets_tool_call_limit(monkeypatch):
    """Confirmed live 2026-08-06: a Coder made 18+ consecutive apply_diff calls with
    an identical, hallucinated old_string, each refused, each refusal ignored -- 36+
    total tool calls in what the coordinator's own max_iterations never saw as more
    than one of its own iterations, because no Agent construction in this codebase
    set agno's own tool_call_limit (framework-enforced at the model-call layer, not
    just an advisory text response)."""
    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.agents.config.tool_call_limit", 25)
    spec = AgentSpec(name="Coder", role="engineer", model="qwen2.5-coder:32b", instructions=["x"])

    agent = make_agent_from_spec(spec)

    assert agent.tool_call_limit == 25


def test_make_coder_sets_tool_call_limit(monkeypatch):
    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    monkeypatch.setattr("swarm.agents.config.tool_call_limit", 25)

    agent = make_coder()

    assert agent.tool_call_limit == 25
