"""Tests for swarm/model_routing.py -- the DB-backed in-process routing cache
(AGNOHive 2.3.2 addendum, 2026-08-08). get_model() (swarm/agents.py) is covered
separately in tests/test_get_model_cloud.py; this file covers the cache/seed/
reload mechanics get_model() relies on."""
import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db, model_routing as mr


@pytest.fixture(autouse=True)
async def _fresh_state(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    await db.reset_engine_for_tests()
    await mr.reset_cache_for_tests()
    yield


# ── seeding ───────────────────────────────────────────────────────────────────

async def test_load_cache_seeds_an_empty_model_catalog():
    await mr.load_cache()
    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.model_catalog))).mappings().all()
    assert len(rows) == len(mr._LOCAL_MODELS) + len(mr._CLOUD_MODELS)


async def test_load_cache_does_not_reseed_a_non_empty_catalog():
    await mr.load_cache()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.model_catalog.insert().values(
                model_id="custom-model", kind="local", provider="local",
                vllm_served_as=None, requires_cloud_gate=False, active=True,
            )
        )
    await mr.load_cache()  # must NOT re-run the seed (would duplicate-key error if it tried)
    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.model_catalog))).mappings().all()
    assert len(rows) == len(mr._LOCAL_MODELS) + len(mr._CLOUD_MODELS) + 1


async def test_seeded_local_models_carry_the_all_moe_consolidation_mapping():
    await mr.load_cache()
    route = mr.get_route("qwen2.5-coder:32b")
    assert route.kind == "local"
    assert route.vllm_served_as == "qwen3-coder-30b"
    assert route.requires_cloud_gate is False


async def test_seeded_cloud_models_require_the_gate():
    await mr.load_cache()
    for model_id, provider in mr._CLOUD_MODELS:
        route = mr.get_route(model_id)
        assert route.kind == "cloud"
        assert route.provider == provider
        assert route.requires_cloud_gate is True


async def test_seeded_team_role_defaults_cover_every_shipped_team():
    await mr.load_cache()
    for team_name, role_name, model_id in mr._TEAM_ROLE_DEFAULTS:
        assert mr.get_default_model(team_name, role_name) == model_id


async def test_seeded_coder_override_carries_the_larger_max_tokens():
    """Recommendation #4 (2026-08-13, see DOCS.md "Declarative Per-Role Policy"):
    the seed data's _TEAM_ROLE_POLICY_OVERRIDES entry for (engineering, Coder)
    replaces the old hardcoded `if spec.name == "Coder"` special-case that used
    to live in swarm/agents.py -- a fresh deployment's Coder role must still get
    max_tokens=8192, just from a data row now instead of code."""
    await mr.load_cache()
    policy = mr.get_role_policy("engineering", "Coder")
    assert policy.max_tokens == 8192


async def test_seeded_roles_without_an_override_have_no_policy_set():
    await mr.load_cache()
    policy = mr.get_role_policy("engineering", "Researcher")
    assert policy.temperature is None
    assert policy.max_tokens is None
    assert policy.tool_call_limit is None


# ── cache lookups ─────────────────────────────────────────────────────────────

async def test_get_route_returns_none_before_cache_loaded():
    assert mr.get_route("qwen3-coder:30b") is None


async def test_get_route_returns_none_for_unregistered_id():
    await mr.load_cache()
    assert mr.get_route("never-heard-of-this-model") is None


async def test_get_default_model_returns_none_for_unknown_team_role():
    await mr.load_cache()
    assert mr.get_default_model("engineering", "NoSuchRole") is None


async def test_get_role_policy_returns_none_for_unknown_team_role():
    await mr.load_cache()
    assert mr.get_role_policy("engineering", "NoSuchRole") is None


async def test_get_default_model_stays_correct_via_the_role_policy_cache():
    """get_default_model() is now a thin wrapper over the same cache
    get_role_policy() reads -- must still return exactly the model_id, not the
    whole RolePolicy object or something else."""
    await mr.load_cache()
    assert mr.get_default_model("engineering", "Coder") == mr.get_role_policy("engineering", "Coder").model_id


# ── ensure_cache_loaded idempotency ──────────────────────────────────────────

async def test_ensure_cache_loaded_only_loads_once(monkeypatch):
    calls = []
    original_load_cache = mr.load_cache

    async def _counting_load_cache():
        calls.append(1)
        await original_load_cache()

    monkeypatch.setattr(mr, "load_cache", _counting_load_cache)

    await mr.ensure_cache_loaded()
    await mr.ensure_cache_loaded()
    await mr.ensure_cache_loaded()

    assert len(calls) == 1


# ── reload() diffing ──────────────────────────────────────────────────────────

async def test_reload_with_no_changes_returns_empty_diff():
    await mr.ensure_cache_loaded()
    diff = await mr.reload()
    assert diff == {
        "model_catalog": {"added": [], "removed": [], "changed": []},
        "team_role_models": {"added": [], "removed": [], "changed": []},
    }


async def test_reload_detects_a_newly_added_model():
    await mr.ensure_cache_loaded()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.model_catalog.insert().values(
                model_id="brand-new-model", kind="local", provider="local",
                vllm_served_as=None, requires_cloud_gate=False, active=True,
            )
        )

    diff = await mr.reload()

    assert diff["model_catalog"]["added"] == ["brand-new-model"]
    assert mr.get_route("brand-new-model") is not None


async def test_reload_detects_a_removed_model():
    await mr.ensure_cache_loaded()
    async with db.get_engine().begin() as conn:
        # Every seeded model is referenced by some team_role_models row (FK-
        # protected, correctly), so insert a throwaway, unreferenced row to
        # delete instead of removing a real one.
        await conn.execute(
            db.model_catalog.insert().values(
                model_id="throwaway-model", kind="local", provider="local",
                vllm_served_as=None, requires_cloud_gate=False, active=True,
            )
        )
    await mr.load_cache()

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.delete(db.model_catalog).where(db.model_catalog.c.model_id == "throwaway-model"))

    diff = await mr.reload()

    assert diff["model_catalog"]["removed"] == ["throwaway-model"]
    assert mr.get_route("throwaway-model") is None


async def test_reload_detects_a_changed_model_field():
    await mr.ensure_cache_loaded()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.model_catalog)
            .where(db.model_catalog.c.model_id == "qwen2.5-coder:32b")
            .values(vllm_served_as="some-other-served-name")
        )

    diff = await mr.reload()

    assert diff["model_catalog"]["changed"] == ["qwen2.5-coder:32b"]
    assert mr.get_route("qwen2.5-coder:32b").vllm_served_as == "some-other-served-name"


async def test_reload_detects_team_role_model_changes():
    await mr.ensure_cache_loaded()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.team_role_models)
            .where(db.team_role_models.c.team_name == "engineering", db.team_role_models.c.role_name == "Coder")
            .values(model_id="qwen3-coder:30b")
        )

    diff = await mr.reload()

    assert diff["team_role_models"]["changed"] == ["engineering/Coder"]
    assert mr.get_default_model("engineering", "Coder") == "qwen3-coder:30b"


async def test_reload_detects_a_policy_only_change_not_just_model_id():
    """RolePolicy is a frozen dataclass, so the diff's value-equality check
    (after_roles[k] != before_roles[k]) must catch a change to temperature/
    max_tokens/tool_call_limit alone, with model_id untouched -- not just a
    model_id swap. Confirms role_changed isn't secretly still comparing bare
    model_id strings."""
    await mr.ensure_cache_loaded()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.team_role_models)
            .where(db.team_role_models.c.team_name == "engineering", db.team_role_models.c.role_name == "Researcher")
            .values(temperature=0.05)  # model_id left exactly as seeded
        )

    diff = await mr.reload()

    assert diff["team_role_models"]["changed"] == ["engineering/Researcher"]
    assert mr.get_role_policy("engineering", "Researcher").temperature == 0.05


# ── inactive rows ─────────────────────────────────────────────────────────────

async def test_inactive_row_is_invisible_to_get_route():
    await mr.ensure_cache_loaded()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.model_catalog)
            .where(db.model_catalog.c.model_id == "gpt-4o-cloud")
            .values(active=False)
        )
    await mr.load_cache()

    assert mr.get_route("gpt-4o-cloud") is None
