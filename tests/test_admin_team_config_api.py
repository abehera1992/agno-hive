"""Tests for the /admin/team-config/* endpoints (AGNOHive 2.3.3, 2026-08-18).
Mirrors tests/test_admin_model_routes_api.py's convention: endpoint functions
called directly against a real in-memory SQLite DB, not through a FastAPI
TestClient."""
import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from api.models import (
    TeamRoleToolEntry, TeamRoleSkillEntry, InstructionOverlayCreate,
    InstructionOverlayPatch, TeamGateFlagEntry, RegistryRefreshRequest,
)
from config.config import config
from swarm import db, team_config as tc


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await tc.reset_cache_for_tests()
    await db.ensure_routing_schema()
    yield


async def _register(tool_names=(), skill_names=()):
    async with db.get_routing_engine().begin() as conn:
        for t in tool_names:
            await conn.execute(db.tool_registry.insert().values(tool_name=t))
        for s in skill_names:
            await conn.execute(db.skill_registry.insert().values(skill_name=s))


# ── team_role_tools CRUD ──────────────────────────────────────────────────────

async def test_create_and_list_team_role_tool():
    from api.server import create_team_role_tool, list_team_role_tools

    await _register(tool_names=["apply_diff"])
    created = await create_team_role_tool(TeamRoleToolEntry(team_name="engineering", role_name="Coder", tool_name="apply_diff"))
    assert created.tool_name == "apply_diff"

    listed = await list_team_role_tools()
    assert any(g["tool_name"] == "apply_diff" for g in listed["grants"])


async def test_create_team_role_tool_rejects_unregistered_name():
    from api.server import create_team_role_tool

    with pytest.raises(HTTPException) as exc_info:
        await create_team_role_tool(TeamRoleToolEntry(team_name="engineering", role_name="Coder", tool_name="not_a_real_tool"))
    assert exc_info.value.status_code == 400
    assert "registry" in exc_info.value.detail


async def test_create_duplicate_team_role_tool_returns_409():
    from api.server import create_team_role_tool

    await _register(tool_names=["apply_diff"])
    entry = TeamRoleToolEntry(team_name="engineering", role_name="Coder", tool_name="apply_diff")
    await create_team_role_tool(entry)
    with pytest.raises(HTTPException) as exc_info:
        await create_team_role_tool(entry)
    assert exc_info.value.status_code == 409


async def test_delete_team_role_tool():
    from api.server import create_team_role_tool, delete_team_role_tool, list_team_role_tools

    await _register(tool_names=["apply_diff"])
    await create_team_role_tool(TeamRoleToolEntry(team_name="engineering", role_name="Coder", tool_name="apply_diff"))
    result = await delete_team_role_tool("engineering", "Coder", "apply_diff")
    assert result == {"deleted": "engineering/Coder/apply_diff"}
    listed = await list_team_role_tools()
    assert listed["grants"] == []


async def test_delete_unknown_team_role_tool_returns_404():
    from api.server import delete_team_role_tool

    with pytest.raises(HTTPException) as exc_info:
        await delete_team_role_tool("engineering", "Coder", "nonexistent")
    assert exc_info.value.status_code == 404


# ── team_role_skills CRUD (mirrors tools) ─────────────────────────────────────

async def test_create_team_role_skill_rejects_unregistered_name():
    from api.server import create_team_role_skill

    with pytest.raises(HTTPException) as exc_info:
        await create_team_role_skill(TeamRoleSkillEntry(team_name="engineering", role_name="Coder", skill_name="not-a-real-skill"))
    assert exc_info.value.status_code == 400


async def test_create_and_delete_team_role_skill():
    from api.server import create_team_role_skill, delete_team_role_skill, list_team_role_skills

    await _register(skill_names=["code-conventions"])
    await create_team_role_skill(TeamRoleSkillEntry(team_name="engineering", role_name="Coder", skill_name="code-conventions"))
    listed = await list_team_role_skills()
    assert any(g["skill_name"] == "code-conventions" for g in listed["grants"])

    await delete_team_role_skill("engineering", "Coder", "code-conventions")
    listed = await list_team_role_skills()
    assert listed["grants"] == []


# ── instruction overlays ──────────────────────────────────────────────────────

async def test_create_instruction_overlay():
    from api.server import create_instruction_overlay

    created = await create_instruction_overlay(
        InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text="a note", created_by="test")
    )
    assert created.instruction_text == "a note"
    assert created.active is True
    assert created.id is not None


async def test_instruction_overlay_soft_cap_enforced_at_write_time():
    from api.server import create_instruction_overlay

    for i in range(tc.INSTRUCTION_OVERLAY_SOFT_CAP):
        await create_instruction_overlay(
            InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text=f"note {i}")
        )
    with pytest.raises(HTTPException) as exc_info:
        await create_instruction_overlay(
            InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text="one too many")
        )
    assert exc_info.value.status_code == 409
    assert "soft cap" in exc_info.value.detail


async def test_deactivating_an_overlay_frees_a_cap_slot():
    from api.server import create_instruction_overlay, patch_instruction_overlay

    first = None
    for i in range(tc.INSTRUCTION_OVERLAY_SOFT_CAP):
        created = await create_instruction_overlay(
            InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text=f"note {i}")
        )
        if first is None:
            first = created

    await patch_instruction_overlay(first.id, InstructionOverlayPatch(active=False))
    # Should now succeed -- one slot freed
    created = await create_instruction_overlay(
        InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text="fits now")
    )
    assert created.instruction_text == "fits now"


async def test_patch_overlay_with_no_fields_returns_400():
    from api.server import create_instruction_overlay, patch_instruction_overlay

    created = await create_instruction_overlay(
        InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text="a note")
    )
    with pytest.raises(HTTPException) as exc_info:
        await patch_instruction_overlay(created.id, InstructionOverlayPatch())
    assert exc_info.value.status_code == 400


async def test_delete_instruction_overlay():
    from api.server import create_instruction_overlay, delete_instruction_overlay, list_instruction_overlays

    created = await create_instruction_overlay(
        InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text="a note")
    )
    await delete_instruction_overlay(created.id)
    listed = await list_instruction_overlays(team_name="engineering", role_name="Coder")
    assert listed == []


async def test_delete_unknown_overlay_returns_404():
    from api.server import delete_instruction_overlay

    with pytest.raises(HTTPException) as exc_info:
        await delete_instruction_overlay(99999)
    assert exc_info.value.status_code == 404


async def test_list_instruction_overlays_filters_by_team_and_role():
    from api.server import create_instruction_overlay, list_instruction_overlays

    await create_instruction_overlay(InstructionOverlayCreate(team_name="engineering", role_name="Coder", instruction_text="a"))
    await create_instruction_overlay(InstructionOverlayCreate(team_name="engineering", role_name="Reviewer", instruction_text="b"))
    await create_instruction_overlay(InstructionOverlayCreate(team_name="planning", role_name="Coder", instruction_text="c"))

    coder_only = await list_instruction_overlays(team_name="engineering", role_name="Coder")
    assert len(coder_only) == 1
    assert coder_only[0].instruction_text == "a"


# ── team gate flags ────────────────────────────────────────────────────────────

async def test_upsert_team_gate_flag_rejects_unknown_gate_name():
    from api.server import upsert_team_gate_flag

    with pytest.raises(HTTPException) as exc_info:
        await upsert_team_gate_flag(TeamGateFlagEntry(team_name="planning", gate_name="not_a_real_gate", enabled=True))
    assert exc_info.value.status_code == 400


async def test_upsert_and_list_team_gate_flag():
    from api.server import upsert_team_gate_flag, list_team_gate_flags

    await upsert_team_gate_flag(TeamGateFlagEntry(team_name="planning", gate_name="decompose_first", enabled=True))
    listed = await list_team_gate_flags()
    assert any(f["team_name"] == "planning" and f["enabled"] is True for f in listed["flags"])


async def test_upsert_team_gate_flag_updates_existing_row():
    from api.server import upsert_team_gate_flag, list_team_gate_flags

    await upsert_team_gate_flag(TeamGateFlagEntry(team_name="planning", gate_name="decompose_first", enabled=True))
    await upsert_team_gate_flag(TeamGateFlagEntry(team_name="planning", gate_name="decompose_first", enabled=False))
    listed = await list_team_gate_flags()
    matching = [f for f in listed["flags"] if f["team_name"] == "planning" and f["gate_name"] == "decompose_first"]
    assert len(matching) == 1
    assert matching[0]["enabled"] is False


async def test_delete_team_gate_flag():
    from api.server import upsert_team_gate_flag, delete_team_gate_flag, list_team_gate_flags

    await upsert_team_gate_flag(TeamGateFlagEntry(team_name="planning", gate_name="decompose_first", enabled=True))
    await delete_team_gate_flag("planning", "decompose_first")
    listed = await list_team_gate_flags()
    assert listed["flags"] == []


async def test_delete_unknown_gate_flag_returns_404():
    from api.server import delete_team_gate_flag

    with pytest.raises(HTTPException) as exc_info:
        await delete_team_gate_flag("planning", "decompose_first")
    assert exc_info.value.status_code == 404


# ── registry refresh + reload ─────────────────────────────────────────────────

async def test_refresh_registry_endpoint():
    from api.server import refresh_team_config_registry, create_team_role_tool

    result = await refresh_team_config_registry(RegistryRefreshRequest(tool_names=["brand_new_tool"], skill_names=["brand-new-skill"]))
    assert result == {"tool_names_refreshed": 1, "skill_names_refreshed": 1}
    # Now grantable, since it's registered
    created = await create_team_role_tool(TeamRoleToolEntry(team_name="engineering", role_name="Coder", tool_name="brand_new_tool"))
    assert created.tool_name == "brand_new_tool"


async def test_reload_endpoint_reports_diff():
    from api.server import create_team_role_tool, reload_team_config

    await _register(tool_names=["apply_diff"])
    await create_team_role_tool(TeamRoleToolEntry(team_name="engineering", role_name="Coder", tool_name="apply_diff"))
    diff = await reload_team_config()
    assert "apply_diff" in diff.tool_grants_added.get("engineering/Coder", [])
