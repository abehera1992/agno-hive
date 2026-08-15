"""Tests for api/server.py's _load_team() DB-backed model default fallback
(AGNOHive 2.3.2 addendum, 2026-08-08): a team YAML MAY omit an agent's model:
field (or coordinator_model:) and let model_routing.get_default_model() fill it
in from team_role_models. Every shipped teams/*.yaml (engineering, planning,
parallel-review, sprint-master) had its model:/coordinator_model: fields removed
2026-08-08 specifically to make them DB-managed, so the tail of this file
exercises the fallback against the real files; the earlier tests use a synthetic
team in an isolated temp directory to cover edge cases (override, missing
default) that don't exist in the real, now fully DB-managed teams."""
import pytest
import yaml
from fastapi import HTTPException

from config.config import config
from swarm import db, model_routing as mr


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    await db.reset_engine_for_tests()
    await mr.reset_cache_for_tests()
    yield


def _write_team_yaml(tmp_path, data: dict):
    (tmp_path / "synthetic-team.yaml").write_text(yaml.safe_dump(data))


async def test_load_team_falls_back_to_db_default_when_model_omitted(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, {
        "name": "synthetic-team",
        "coordinator_model": "qwen3-coder:30b",
        "agents": [
            {"name": "Coder", "role": "coder", "instructions": ["do stuff"]},  # model: omitted
        ],
    })
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)

    await mr.ensure_cache_loaded()  # seeds model_catalog + team_role_models, including engineering/Coder
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.team_role_models.insert().values(
                team_name="synthetic-team", role_name="Coder", model_id="qwen2.5-coder:32b",
            )
        )
    await mr.reload()

    agents, coordinator, mode, _ = server._load_team("synthetic-team")

    assert agents[0].model == "qwen2.5-coder:32b"
    assert coordinator == "qwen3-coder:30b"


async def test_load_team_yaml_model_field_overrides_db_default(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, {
        "name": "synthetic-team",
        "coordinator_model": "qwen3-coder:30b",
        "agents": [
            {"name": "Coder", "role": "coder", "model": "llama3.1:8b", "instructions": ["do stuff"]},
        ],
    })
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)

    await mr.ensure_cache_loaded()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.team_role_models.insert().values(
                team_name="synthetic-team", role_name="Coder", model_id="qwen2.5-coder:32b",
            )
        )
    await mr.reload()

    agents, _, _, _ = server._load_team("synthetic-team")

    assert agents[0].model == "llama3.1:8b"  # YAML wins over the DB default


async def test_load_team_raises_clearly_when_neither_yaml_nor_db_has_a_model(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, {
        "name": "synthetic-team",
        "agents": [
            {"name": "Ghost", "role": "nobody", "instructions": ["do stuff"]},  # no model:, no DB row either
        ],
    })
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    await mr.ensure_cache_loaded()

    with pytest.raises(HTTPException) as exc_info:
        server._load_team("synthetic-team")
    assert exc_info.value.status_code == 500
    assert "Ghost" in exc_info.value.detail


async def test_load_team_coordinator_falls_back_to_config_leader_model_as_last_resort(tmp_path, monkeypatch):
    from api import server

    _write_team_yaml(tmp_path, {
        "name": "synthetic-team",
        "agents": [
            {"name": "Coder", "role": "coder", "model": "llama3.1:8b", "instructions": ["do stuff"]},
        ],
    })
    monkeypatch.setattr(server, "_TEAMS_DIR", tmp_path)
    monkeypatch.setattr(config, "leader_model", "fallback-leader-model")
    await mr.ensure_cache_loaded()

    _, coordinator, _, _ = server._load_team("synthetic-team")

    assert coordinator == "fallback-leader-model"


# ── Real shipped teams — now fully DB-managed (2026-08-08) ───────────────────
# teams/engineering.yaml, planning.yaml, parallel-review.yaml, sprint-master.yaml
# had every model:/coordinator_model: field removed so the values below (which
# match the shipped model_routing.py seed exactly) resolve entirely from
# team_role_models — this locks that end-to-end behavior in against a real
# regression, not just a synthetic YAML.

@pytest.mark.parametrize("team_name,expected_coordinator,expected_models", [
    ("engineering", "qwen3-coder:30b", {
        "ContextRouter": "llama3.1:8b",
        "Researcher": "qwen2.5-coder:32b",
        "Coder": "qwen2.5-coder:32b",
        "Executor": "llama3.1:8b",
        "Reviewer": "qwen2.5-coder:32b",
    }),
    ("planning", "qwen2.5-coder:7b", {
        "ContextRouter": "llama3.1:8b",
        "Researcher": "qwen2.5-coder:32b",
        "Planner": "qwen2.5-coder:32b",
    }),
    ("parallel-review", "qwen2.5-coder:7b", {
        "Researcher": "qwen2.5-coder:32b",
        "SecurityReviewer": "qwen2.5-coder:32b",
        "PerformanceReviewer": "qwen2.5-coder:32b",
    }),
    ("sprint-master", "qwen3-coder:30b", {
        "BacklogResearcher": "qwen2.5-coder:32b",
        "StoryWriter": "qwen2.5-coder:32b",
    }),
])
async def test_real_shipped_team_resolves_every_model_from_the_db(
    team_name, expected_coordinator, expected_models,
):
    from api import server

    await mr.ensure_cache_loaded()

    agents, coordinator, _, _ = server._load_team(team_name)

    assert coordinator == expected_coordinator
    resolved = {a.name: a.model for a in agents}
    assert resolved == expected_models


async def test_real_shipped_teams_no_longer_declare_model_in_yaml():
    """Guards against someone re-adding a model: field to a shipped team without
    updating this file's expectations above -- if this fails, either the YAML
    regained a pinned model (intentional or not) or the parametrized test above
    needs updating to match."""
    import yaml

    for team_name in ["engineering", "planning", "parallel-review", "sprint-master"]:
        data = yaml.safe_load(open(f"teams/{team_name}.yaml"))
        assert "coordinator_model" not in data
        for agent in data["agents"]:
            assert "model" not in agent, f"{team_name}/{agent['name']} still pins a model: in YAML"
