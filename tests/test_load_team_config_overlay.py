"""Tests for AGNOHive 2.3.3's read-path wiring in api/server.py's _load_team():
Tier 1 (DB-granted extra tools/skills, additive-union with a team YAML's own
tools:/skills: list) and Tier 2 (additive-only instruction overlays, appended
after the YAML's base instructions:). Mirrors
tests/test_load_team_db_fallback.py's synthetic-team-in-tmp_path pattern."""
import pytest
import yaml

from config.config import config
from swarm import db, model_routing as mr, team_config as tc


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await mr.reset_cache_for_tests()
    await tc.reset_cache_for_tests()
    await mr.ensure_cache_loaded()
    yield


def _write_team_yaml(tmp_path, data: dict):
    (tmp_path / "synthetic-team.yaml").write_text(yaml.safe_dump(data))


async def _add_tool_grant(team_name, role_name, tool_name):
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(db.team_role_tools.insert().values(team_name=team_name, role_name=role_name, tool_name=tool_name))
    await tc.reload()


async def _add_skill_grant(team_name, role_name, skill_name):
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(db.team_role_skills.insert().values(team_name=team_name, role_name=role_name, skill_name=skill_name))
    await tc.reload()


async def _add_overlay(team_name, role_name, text, active=True):
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_role_instruction_overlays.insert().values(
                team_name=team_name, role_name=role_name, instruction_text=text, active=active,
            )
        )
    await tc.reload()


def _base_yaml():
    return {
        "name": "synthetic-team",
        "coordinator_model": "qwen3-coder:30b",
        "agents": [
            {
                "name": "Coder", "role": "coder", "model": "qwen2.5-coder:32b",
                "instructions": ["base instruction one", "base instruction two"],
                "tools": ["get_file_content", "apply_diff"],
                "skills": ["verification-discipline"],
            },
        ],
    }


# ── Tier 1: tools union ───────────────────────────────────────────────────────

async def test_db_granted_extra_tool_is_unioned_with_yaml_tools(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coder", "web_search")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert set(agents[0].tools) == {"get_file_content", "apply_diff", "web_search"}


async def test_no_db_grant_leaves_yaml_tools_unchanged(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()

    agents, _, _, _ = server._load_team("synthetic-team")

    assert set(agents[0].tools) == {"get_file_content", "apply_diff"}


async def test_db_grant_never_narrows_an_unrestricted_agent(tmp_path, monkeypatch):
    """A role with NO tools: in its YAML is unrestricted (sees every connected
    tool, per make_agent_from_spec's own `if spec.tools:` truthy check) -- a DB
    grant must never turn that into a restrictive list of just the grant."""
    from api import server

    data = _base_yaml()
    del data["agents"][0]["tools"]  # unrestricted
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coder", "web_search")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert agents[0].tools is None


async def test_db_granted_extra_coordinator_tool_is_unioned(tmp_path, monkeypatch):
    from api import server

    data = _base_yaml()
    data["coordinator_tools"] = ["get_file_content"]
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coordinator", "notion_search")

    _, _, _, coordinator_tools = server._load_team("synthetic-team")

    assert set(coordinator_tools) == {"get_file_content", "notion_search"}


async def test_no_coordinator_tools_yaml_field_is_unaffected_by_db_grants(tmp_path, monkeypatch):
    """A team with NO coordinator_tools: at all (e.g. real engineering.yaml) is
    unrestricted by design -- a DB grant must not turn that into a restrictive
    allowlist either."""
    from api import server

    data = _base_yaml()  # no coordinator_tools key
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coordinator", "notion_search")

    _, _, _, coordinator_tools = server._load_team("synthetic-team")

    assert coordinator_tools is None


# ── Tier 1: skills union (mirrors tools) ──────────────────────────────────────

async def test_db_granted_extra_skill_is_unioned_with_yaml_skills(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_skill_grant("synthetic-team", "Coder", "code-conventions")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert set(agents[0].skills) == {"verification-discipline", "code-conventions"}


# ── Tier 2: instruction overlays ──────────────────────────────────────────────

async def test_active_overlay_is_appended_after_base_instructions(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_overlay("synthetic-team", "Coder", "a user-added note")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert agents[0].instructions == [
        "base instruction one", "base instruction two", tc.OVERLAY_HEADER, "a user-added note",
    ]


async def test_inactive_overlay_is_not_appended(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_overlay("synthetic-team", "Coder", "should not appear", active=False)

    agents, _, _, _ = server._load_team("synthetic-team")

    assert agents[0].instructions == ["base instruction one", "base instruction two"]


async def test_no_overlay_leaves_base_instructions_completely_unchanged(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()

    agents, _, _, _ = server._load_team("synthetic-team")

    assert agents[0].instructions == ["base instruction one", "base instruction two"]


async def test_multiple_active_overlays_appended_in_insertion_order():
    """Direct team_config test (not through _load_team) -- confirms ordering,
    since _load_team's own test above only exercises a single overlay."""
    await tc.ensure_cache_loaded()
    await _add_overlay("engineering", "Coder", "first")
    await _add_overlay("engineering", "Coder", "second")

    assert tc.get_instruction_overlays("engineering", "Coder") == ["first", "second"]
