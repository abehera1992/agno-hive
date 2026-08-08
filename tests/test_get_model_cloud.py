"""Tests for swarm/agents.py's get_model() -- DB-backed model routing (AGNOHive
2.3.2 addendum, 2026-08-08) plus regression coverage for the pre-existing vLLM/
Ollama branches.

House style: swarm/agents.py does `from config.config import config` (one shared
object import, not per-name), so tests patch attributes directly on that object --
`monkeypatch.setattr(config, "x", value)` -- matching test_agents_skills.py's existing
`monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")` pattern.

Routing itself is no longer a hardcoded dict/set in swarm/agents.py -- get_model()
reads swarm/model_routing.py's in-process cache, seeded fresh against an in-memory
SQLite DB (model_catalog/team_role_models tables) by the autouse fixture below, so
every test starts from the same known-good seed state regardless of test order.
"""
import pytest

from config.config import config
from swarm import db, model_routing
from swarm.agents import get_model


@pytest.fixture(autouse=True)
async def _fresh_model_routing_cache(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    await db.reset_engine_for_tests()
    await model_routing.reset_cache_for_tests()
    await model_routing.ensure_cache_loaded()  # triggers the seed against the fresh empty DB
    yield


# ── cloud alias routing ─────────────────────────────────────────────────────────

async def test_cloud_alias_raises_when_allow_cloud_models_is_false(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", False)

    with pytest.raises(RuntimeError, match="ALLOW_CLOUD_MODELS"):
        get_model("claude-sonnet-cloud", "http://ollama-host")


async def test_cloud_alias_error_names_the_requested_model(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", False)

    with pytest.raises(RuntimeError, match="gpt-4o-cloud"):
        get_model("gpt-4o-cloud", "http://ollama-host")


async def test_cloud_alias_succeeds_when_allow_cloud_models_is_true(monkeypatch):
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("claude-sonnet-cloud", "http://ollama-host")

    assert model.id == "claude-sonnet-cloud"
    assert model.base_url == "http://litellm-host:4000/v1"
    assert model.api_key == "EMPTY"


async def test_cloud_alias_routes_the_same_regardless_of_inference_backend(monkeypatch):
    """Cloud routing is a per-agent choice (which model_id a team YAML names), not a
    global backend switch -- it must resolve the same way whether the rest of the
    swarm is running INFERENCE_BACKEND=ollama or =vllm."""
    monkeypatch.setattr(config, "allow_cloud_models", True)
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    monkeypatch.setattr(config, "inference_backend", "ollama")
    via_ollama_backend = get_model("sonar-pro-cloud", "http://ollama-host")

    monkeypatch.setattr(config, "inference_backend", "vllm")
    via_vllm_backend = get_model("sonar-pro-cloud", "http://ollama-host")

    assert via_ollama_backend.id == via_vllm_backend.id == "sonar-pro-cloud"
    assert via_ollama_backend.base_url == via_vllm_backend.base_url == "http://litellm-host:4000/v1"


async def test_all_seeded_cloud_models_are_gated(monkeypatch):
    """Every model_catalog row seeded with kind='cloud' must actually hit the gate --
    guards against a future seed entry added without requires_cloud_gate=True."""
    monkeypatch.setattr(config, "allow_cloud_models", False)

    cloud_ids = [mid for mid, route in model_routing._route_cache.items() if route.kind == "cloud"]
    assert cloud_ids, "expected at least one seeded cloud model"
    for model_id in cloud_ids:
        with pytest.raises(RuntimeError, match="ALLOW_CLOUD_MODELS"):
            get_model(model_id, "http://ollama-host")


async def test_inactive_route_falls_back_to_unregistered_behavior(monkeypatch):
    """A model_catalog row marked active=False must behave exactly like an
    unregistered id -- a soft-disable, not a delete, but still inert for routing."""
    import sqlalchemy as sa

    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.model_catalog)
            .where(db.model_catalog.c.model_id == "qwen2.5-coder:32b")
            .values(active=False)
        )
    await model_routing.load_cache()

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")
    assert model.id == "qwen2.5-coder-32b"  # dash-mangled passthrough, NOT the consolidation target


# ── regression: pre-existing vLLM / Ollama branches, unaffected by the cloud check ──

async def test_non_cloud_model_id_with_vllm_backend_unaffected_by_allow_cloud_models(monkeypatch):
    """A model id that ISN'T a cloud alias must behave identically regardless of
    ALLOW_CLOUD_MODELS -- the gate only ever applies to requires_cloud_gate rows."""
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")
    monkeypatch.setattr(config, "allow_cloud_models", False)

    model = get_model("qwen2.5-coder:32b", "http://ollama-host")

    assert model.id == "qwen3-coder-30b"  # collapsed via the seeded ALL-MoE consolidation row
    assert model.base_url == "http://litellm-host:4000/v1"


async def test_unmapped_model_id_with_vllm_backend_falls_back_to_dash_mangling(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://litellm-host:4000/v1")

    model = get_model("some-unmapped:tag", "http://ollama-host")

    assert model.id == "some-unmapped-tag"


async def test_ollama_backend_returns_ollama_tool_fix(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "ollama")

    model = get_model("qwen2.5-coder:32b", "http://ollama-host:11434")

    assert type(model).__name__ == "OllamaToolFix"
    assert model.id == "qwen2.5-coder:32b"
