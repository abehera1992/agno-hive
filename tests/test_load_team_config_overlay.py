"""Tests for AGNOHive 2.3.3's read-path wiring in api/server.py's _load_team():
Tier 1 (DB-backed tools/skills, override-with-fallback -- same precedence as
model:/policy: the YAML's own tools:/skills:, when present, always wins
outright; when the YAML omits the field, the DB supplies the role's full
list) and Tier 2 (additive-only instruction overlays, appended after the
YAML's base instructions:). Mirrors tests/test_load_team_db_fallback.py's
synthetic-team-in-tmp_path pattern."""
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


# ── Tier 1: tools, override-with-DB-fallback ──────────────────────────────────

async def test_yaml_tools_win_outright_over_a_db_grant(tmp_path, monkeypatch):
    """The YAML's own tools:, when present, always wins -- the same 'pin it
    back here to take it out of DB control' escape hatch model: already has.
    A DB row for this role is NOT unioned in; it's simply ignored."""
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coder", "web_search")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert set(agents[0].tools) == {"get_file_content", "apply_diff"}


async def test_yaml_omits_tools_falls_back_to_full_db_list(tmp_path, monkeypatch):
    """Once the YAML omits tools: entirely, the DB is the sole source -- the
    role's tool list is exactly what's granted, not a union with anything."""
    from api import server

    data = _base_yaml()
    del data["agents"][0]["tools"]
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coder", "web_search")
    await _add_tool_grant("synthetic-team", "Coder", "get_file_content")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert set(agents[0].tools) == {"web_search", "get_file_content"}


async def test_no_yaml_tools_and_no_db_grant_stays_unrestricted(tmp_path, monkeypatch):
    """A role with NO tools: in its YAML AND no DB rows either is unrestricted
    (sees every connected tool, per make_agent_from_spec's own
    `if spec.tools:` truthy check) -- the historical, pre-DB-migration
    behavior for a role nobody has migrated yet."""
    from api import server

    data = _base_yaml()
    del data["agents"][0]["tools"]
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()

    agents, _, _, _ = server._load_team("synthetic-team")

    assert agents[0].tools is None


async def test_yaml_omits_coordinator_tools_falls_back_to_db(tmp_path, monkeypatch):
    from api import server

    data = _base_yaml()  # no coordinator_tools key
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coordinator", "notion_search")
    await _add_tool_grant("synthetic-team", "Coordinator", "get_file_content")

    _, _, _, coordinator_tools = server._load_team("synthetic-team")

    assert set(coordinator_tools) == {"notion_search", "get_file_content"}


async def test_no_coordinator_tools_yaml_field_and_no_db_grant_stays_unrestricted(tmp_path, monkeypatch):
    """A team with NO coordinator_tools: at all (e.g. real engineering.yaml)
    AND no DB rows either is unrestricted by design."""
    from api import server

    data = _base_yaml()  # no coordinator_tools key
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()

    _, _, _, coordinator_tools = server._load_team("synthetic-team")

    assert coordinator_tools is None


async def test_yaml_coordinator_tools_win_outright_over_a_db_grant(tmp_path, monkeypatch):
    from api import server

    data = _base_yaml()
    data["coordinator_tools"] = ["get_file_content"]
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_tool_grant("synthetic-team", "Coordinator", "notion_search")

    _, _, _, coordinator_tools = server._load_team("synthetic-team")

    assert coordinator_tools == ["get_file_content"]


# ── Tier 1: skills, override-with-DB-fallback (mirrors tools) ────────────────

async def test_yaml_skills_win_outright_over_a_db_grant(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, _base_yaml())
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_skill_grant("synthetic-team", "Coder", "code-conventions")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert set(agents[0].skills) == {"verification-discipline"}


async def test_yaml_omits_skills_falls_back_to_full_db_list(tmp_path, monkeypatch):
    from api import server

    data = _base_yaml()
    del data["agents"][0]["skills"]
    _write_team_yaml(tmp_path, data)
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await tc.ensure_cache_loaded()
    await _add_skill_grant("synthetic-team", "Coder", "code-conventions")

    agents, _, _, _ = server._load_team("synthetic-team")

    assert set(agents[0].skills) == {"code-conventions"}


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
