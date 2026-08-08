"""Tests for the /admin/model-routes endpoints (AGNOHive 2.3.2 addendum,
2026-08-08). Handlers are called directly against a real in-memory SQLite DB --
consistent with this repo's existing convention (see
tests/test_server_tree_endpoints.py) of calling endpoint functions directly
rather than through a FastAPI TestClient, and with this session's broader
preference for real DB integration tests over mocks where the DB is cheap
(in-memory SQLite) to stand up."""
import pytest
from fastapi import HTTPException

from api.models import ModelCatalogEntry, ModelCatalogPatch, TeamRoleModelEntry
from config.config import config
from swarm import db, model_routing as mr


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    await db.reset_engine_for_tests()
    await mr.reset_cache_for_tests()
    await db.ensure_schema()
    yield


# ── model_catalog CRUD ────────────────────────────────────────────────────────

async def test_create_and_list_model_route():
    from api.server import create_model_route, list_model_routes

    created = await create_model_route(
        ModelCatalogEntry(model_id="test-model", kind="local", provider="local", requires_cloud_gate=False)
    )
    assert created.model_id == "test-model"

    listed = await list_model_routes()
    assert any(m["model_id"] == "test-model" for m in listed["models"])


async def test_create_duplicate_model_id_returns_409():
    from api.server import create_model_route

    entry = ModelCatalogEntry(model_id="dupe", kind="local", provider="local")
    await create_model_route(entry)

    with pytest.raises(HTTPException) as exc_info:
        await create_model_route(entry)
    assert exc_info.value.status_code == 409


async def test_patch_updates_only_supplied_fields():
    from api.server import create_model_route, update_model_route

    await create_model_route(
        ModelCatalogEntry(model_id="patchable", kind="local", provider="local", vllm_served_as="original")
    )

    updated = await update_model_route("patchable", ModelCatalogPatch(vllm_served_as="new-served-name"))

    assert updated["vllm_served_as"] == "new-served-name"
    assert updated["provider"] == "local"  # untouched


async def test_patch_unknown_model_returns_404():
    from api.server import update_model_route

    with pytest.raises(HTTPException) as exc_info:
        await update_model_route("nonexistent", ModelCatalogPatch(active=False))
    assert exc_info.value.status_code == 404


async def test_patch_with_no_fields_returns_400():
    from api.server import create_model_route, update_model_route

    await create_model_route(ModelCatalogEntry(model_id="m1", kind="local", provider="local"))

    with pytest.raises(HTTPException) as exc_info:
        await update_model_route("m1", ModelCatalogPatch())
    assert exc_info.value.status_code == 400


async def test_delete_model_route():
    from api.server import create_model_route, delete_model_route, list_model_routes

    await create_model_route(ModelCatalogEntry(model_id="deletable", kind="local", provider="local"))
    result = await delete_model_route("deletable")
    assert result == {"deleted": "deletable"}

    listed = await list_model_routes()
    assert not any(m["model_id"] == "deletable" for m in listed["models"])


async def test_delete_unknown_model_returns_404():
    from api.server import delete_model_route

    with pytest.raises(HTTPException) as exc_info:
        await delete_model_route("nonexistent")
    assert exc_info.value.status_code == 404


async def test_delete_a_model_still_referenced_by_team_role_models_returns_409():
    """The FK on team_role_models.model_id enforces this at the DB layer --
    verifying the endpoint surfaces it as a clear 409, not a raw 500."""
    from api.server import create_model_route, delete_model_route, upsert_team_role_model

    await create_model_route(ModelCatalogEntry(model_id="in-use-model", kind="local", provider="local"))
    await upsert_team_role_model(TeamRoleModelEntry(team_name="myteam", role_name="Coder", model_id="in-use-model"))

    with pytest.raises(HTTPException) as exc_info:
        await delete_model_route("in-use-model")
    assert exc_info.value.status_code == 409


# ── team_role_models CRUD ─────────────────────────────────────────────────────

async def test_upsert_team_role_model_creates_then_updates():
    from api.server import create_model_route, list_team_role_models, upsert_team_role_model

    await create_model_route(ModelCatalogEntry(model_id="model-a", kind="local", provider="local"))
    await create_model_route(ModelCatalogEntry(model_id="model-b", kind="local", provider="local"))

    await upsert_team_role_model(TeamRoleModelEntry(team_name="myteam", role_name="Coder", model_id="model-a"))
    listed = await list_team_role_models()
    assert {"team_name": "myteam", "role_name": "Coder", "model_id": "model-a"} in [dict(r) for r in listed["defaults"]]

    # Upsert again with a different model_id -- must UPDATE, not create a duplicate row
    await upsert_team_role_model(TeamRoleModelEntry(team_name="myteam", role_name="Coder", model_id="model-b"))
    listed = await list_team_role_models()
    rows = [dict(r) for r in listed["defaults"]]
    assert [r for r in rows if r["team_name"] == "myteam" and r["role_name"] == "Coder"] == [
        {"team_name": "myteam", "role_name": "Coder", "model_id": "model-b"}
    ]


async def test_upsert_team_role_model_rejects_unknown_model_id():
    from api.server import upsert_team_role_model

    with pytest.raises(HTTPException) as exc_info:
        await upsert_team_role_model(TeamRoleModelEntry(team_name="myteam", role_name="Coder", model_id="never-created"))
    assert exc_info.value.status_code == 409


async def test_delete_team_role_model():
    from api.server import create_model_route, delete_team_role_model, upsert_team_role_model

    await create_model_route(ModelCatalogEntry(model_id="model-a", kind="local", provider="local"))
    await upsert_team_role_model(TeamRoleModelEntry(team_name="myteam", role_name="Coder", model_id="model-a"))

    result = await delete_team_role_model("myteam", "Coder")
    assert result == {"deleted": "myteam/Coder"}


async def test_delete_unknown_team_role_returns_404():
    from api.server import delete_team_role_model

    with pytest.raises(HTTPException) as exc_info:
        await delete_team_role_model("no-such-team", "NoRole")
    assert exc_info.value.status_code == 404


# ── reload ────────────────────────────────────────────────────────────────────

async def test_reload_endpoint_reports_the_diff():
    from api.server import create_model_route, reload_model_routes

    await mr.ensure_cache_loaded()  # baseline, seeds the catalog
    await create_model_route(ModelCatalogEntry(model_id="added-via-api", kind="local", provider="local"))

    response = await reload_model_routes()

    assert "added-via-api" in response.model_catalog["added"]
    assert mr.get_route("added-via-api") is not None


async def test_reload_endpoint_is_the_only_thing_that_updates_the_live_cache():
    """A create/patch/delete call alone must NOT change get_model()'s routing --
    only /admin/model-routes/reload does, per the documented design."""
    from api.server import create_model_route

    await mr.ensure_cache_loaded()
    assert mr.get_route("not-yet-reloaded") is None

    await create_model_route(ModelCatalogEntry(model_id="not-yet-reloaded", kind="local", provider="local"))

    assert mr.get_route("not-yet-reloaded") is None  # still not visible until reload
