from api.models import AgentSpec
from swarm.agents import format_skill_catalog, make_agent_from_spec


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
