"""Tests for swarm/team_config.py -- AGNOHive 2.3.3's DB-backed team config
additions (per-role tool/skill allowlist grants, additive-only instruction
overlays, per-team gate flags, tool/skill registry). Mirrors
tests/test_model_routing.py's fixture pattern and split-engine test isolation."""
import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db, team_config as tc


@pytest.fixture(autouse=True)
async def _fresh_state(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await tc.reset_cache_for_tests()
    yield


# ── seeding ───────────────────────────────────────────────────────────────────

async def test_load_cache_seeds_tool_and_skill_grants_from_static_snapshot():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        tool_rows = (await conn.execute(sa.select(db.team_role_tools))).mappings().all()
        skill_rows = (await conn.execute(sa.select(db.team_role_skills))).mappings().all()
    assert len(tool_rows) > 0
    assert len(skill_rows) > 0
    # Spot-check a real, known grant from _DEFAULT_TOOL_GRANTS (the former
    # teams/engineering.yaml content, captured 2026-08-18 when tools:/skills:
    # were removed from the YAML in favor of the DB as the runtime source)
    assert any(
        r["team_name"] == "engineering" and r["role_name"] == "Coder" and r["tool_name"] == "apply_diff"
        for r in tool_rows
    )


async def test_load_cache_does_not_reseed_a_non_empty_team_role_tools():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        before = (await conn.execute(sa.select(db.team_role_tools))).mappings().all()
        await conn.execute(
            db.team_role_tools.insert().values(team_name="engineering", role_name="Coder", tool_name="a_custom_tool")
        )
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        after = (await conn.execute(sa.select(db.team_role_tools))).mappings().all()
    assert len(after) == len(before) + 1


async def test_instruction_overlays_and_gate_flags_start_empty():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        overlay_rows = (await conn.execute(sa.select(db.team_role_instruction_overlays))).mappings().all()
        gate_rows = (await conn.execute(sa.select(db.team_gate_flags))).mappings().all()
    assert overlay_rows == []
    assert gate_rows == []


async def test_registry_seeded_from_default_tool_and_skill_grants():
    await tc.load_cache()
    assert tc.is_tool_registered("apply_diff")
    assert tc.is_tool_registered("get_file_content")
    assert not tc.is_tool_registered("definitely_not_a_real_tool")
    assert tc.is_skill_registered("verification-discipline")
    assert not tc.is_skill_registered("definitely_not_a_real_skill")


# ── get_extra_tools / get_extra_skills ───────────────────────────────────────

async def test_get_extra_tools_returns_empty_list_for_unknown_role():
    await tc.load_cache()
    assert tc.get_extra_tools("engineering", "NoSuchRole") == []


async def test_get_extra_tools_reflects_a_new_db_grant_after_reload():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_role_tools.insert().values(team_name="engineering", role_name="Reviewer", tool_name="web_search")
        )
    await tc.reload()
    assert "web_search" in tc.get_extra_tools("engineering", "Reviewer")


async def test_get_extra_skills_reflects_a_new_db_grant_after_reload():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_role_skills.insert().values(team_name="engineering", role_name="Executor", skill_name="code-conventions")
        )
    await tc.reload()
    assert "code-conventions" in tc.get_extra_skills("engineering", "Executor")


# ── get_instruction_overlays ─────────────────────────────────────────────────

async def test_get_instruction_overlays_empty_when_none_exist():
    await tc.load_cache()
    assert tc.get_instruction_overlays("engineering", "Coder") == []


async def test_get_instruction_overlays_returns_only_active_rows_in_order():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_role_instruction_overlays.insert(),
            [
                {"team_name": "engineering", "role_name": "Coder", "instruction_text": "first note", "active": True},
                {"team_name": "engineering", "role_name": "Coder", "instruction_text": "inactive note", "active": False},
                {"team_name": "engineering", "role_name": "Coder", "instruction_text": "second note", "active": True},
            ],
        )
    await tc.reload()
    assert tc.get_instruction_overlays("engineering", "Coder") == ["first note", "second note"]


# ── get_gate_enabled ──────────────────────────────────────────────────────────

async def test_get_gate_enabled_falls_back_to_default_with_no_db_row():
    await tc.load_cache()
    assert tc.get_gate_enabled("engineering", "decompose_first", default=True) is True
    assert tc.get_gate_enabled("planning", "decompose_first", default=False) is False


async def test_get_gate_enabled_none_team_name_always_uses_default():
    await tc.load_cache()
    assert tc.get_gate_enabled(None, "decompose_first", default=True) is True
    assert tc.get_gate_enabled(None, "decompose_first", default=False) is False


async def test_get_gate_enabled_db_row_overrides_default():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_gate_flags.insert().values(team_name="planning", gate_name="decompose_first", enabled=True)
        )
    await tc.reload()
    # default=False (planning is not in _GATE_ENABLED_TEAMS) -- DB row flips it to True
    assert tc.get_gate_enabled("planning", "decompose_first", default=False) is True


async def test_get_gate_enabled_db_row_can_disable_a_default_enabled_team():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_gate_flags.insert().values(team_name="engineering", gate_name="search_before_browse", enabled=False)
        )
    await tc.reload()
    assert tc.get_gate_enabled("engineering", "search_before_browse", default=True) is False


# ── refresh_registry ──────────────────────────────────────────────────────────

async def test_refresh_registry_adds_new_names():
    await tc.load_cache()
    assert not tc.is_tool_registered("brand_new_tool")
    await tc.refresh_registry(["brand_new_tool"], ["brand-new-skill"])
    await tc.reload()
    assert tc.is_tool_registered("brand_new_tool")
    assert tc.is_skill_registered("brand-new-skill")


async def test_refresh_registry_is_idempotent_on_an_already_known_name():
    await tc.load_cache()
    await tc.refresh_registry(["apply_diff"], [])  # already seeded from YAML
    await tc.refresh_registry(["apply_diff"], [])  # must not raise a duplicate-key error
    await tc.reload()
    assert tc.is_tool_registered("apply_diff")


# ── reload() diff shape ───────────────────────────────────────────────────────

async def test_reload_reports_added_tool_grant():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_role_tools.insert().values(team_name="engineering", role_name="Reviewer", tool_name="web_fetch")
        )
    diff = await tc.reload()
    assert "web_fetch" in diff["tool_grants_added"]["engineering/Reviewer"]


async def test_reload_reports_overlay_count_delta():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_role_instruction_overlays.insert().values(
                team_name="engineering", role_name="Coder", instruction_text="a note", active=True,
            )
        )
    diff = await tc.reload()
    assert diff["overlay_count_delta"] == 1


async def test_reload_reports_changed_gate():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.team_gate_flags.insert().values(team_name="planning", gate_name="decompose_first", enabled=True)
        )
    diff = await tc.reload()
    assert "planning/decompose_first" in diff["gates_changed"]
