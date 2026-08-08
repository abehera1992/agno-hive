"""DB-backed model routing (AGNOHive 2.3.2 addendum, 2026-08-08) — replaces the
old swarm/agents.py hardcoded `_VLLM_MODEL_MAP` dict + `_CLOUD_ALIASES` set with
two SQL tables (`model_catalog`, `team_role_models`; see swarm/db.py), read once
into an in-process cache.

get_model() (swarm/agents.py) is a synchronous hot path called on every agent
construction, so it never talks to the DB directly — it only reads this module's
in-process cache via get_route(). Call ensure_cache_loaded() once per process
before any get_model() call can rely on DB-seeded routing; this is done at
FastAPI startup (api/server.py) AND defensively right before every _build_team()
call in swarm/team.py, so both the API-server path and the plain `main.py` CLI
one-shot path (which never runs the FastAPI startup event) are covered.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from swarm import db


@dataclass(frozen=True)
class ModelRoute:
    model_id: str
    kind: str  # "local" | "cloud"
    provider: str
    vllm_served_as: str | None
    requires_cloud_gate: bool
    active: bool


_route_cache: dict[str, ModelRoute] = {}
_default_model_cache: dict[tuple[str, str], str] = {}
_cache_loaded = False
_load_lock = asyncio.Lock()


def get_route(model_id: str) -> ModelRoute | None:
    """Sync, cache-only lookup — the hot path get_model() calls on every agent
    construction. Returns None for an unregistered id OR a row marked inactive
    (both fall back to today's pre-DB-routing behavior in get_model())."""
    route = _route_cache.get(model_id)
    if route is None or not route.active:
        return None
    return route


def get_default_model(team_name: str, role_name: str) -> str | None:
    """Sync, cache-only lookup used by api/server.py's _load_team() when a team
    YAML omits a role's model: field — the team YAML's own model: field, when
    present, always takes precedence over this."""
    return _default_model_cache.get((team_name, role_name))


async def ensure_cache_loaded() -> None:
    """Idempotent — safe to call from every request path. Loads the cache at
    most once per process unless reload() is called explicitly."""
    global _cache_loaded
    if _cache_loaded:
        return
    async with _load_lock:
        if _cache_loaded:  # another coroutine may have just finished loading
            return
        await load_cache()
        _cache_loaded = True


async def load_cache() -> None:
    """(Re)populate the in-process cache from the DB, seeding defaults into a
    brand-new/empty model_catalog first so a fresh deployment (SQLite by
    default) gets working local-model routing without a manual seed step."""
    await db.ensure_schema()
    async with db.get_engine().begin() as conn:
        existing = (await conn.execute(select(db.model_catalog.c.model_id).limit(1))).first()
        if existing is None:
            await _seed_defaults(conn)

        catalog_rows = (await conn.execute(select(db.model_catalog))).mappings().all()
        role_rows = (await conn.execute(select(db.team_role_models))).mappings().all()

    _route_cache.clear()
    for r in catalog_rows:
        _route_cache[r["model_id"]] = ModelRoute(
            model_id=r["model_id"], kind=r["kind"], provider=r["provider"],
            vllm_served_as=r["vllm_served_as"], requires_cloud_gate=r["requires_cloud_gate"],
            active=r["active"],
        )
    _default_model_cache.clear()
    for r in role_rows:
        _default_model_cache[(r["team_name"], r["role_name"])] = r["model_id"]


async def reload() -> dict:
    """Re-read the DB into the cache and return a diff — rows added/changed/
    removed since the previous cache snapshot — so an admin edit
    (POST /admin/model-routes/reload) is visible immediately instead of a bare
    200 that leaves the caller guessing whether anything actually changed."""
    before_catalog = dict(_route_cache)
    before_roles = dict(_default_model_cache)
    await load_cache()
    after_catalog = dict(_route_cache)
    after_roles = dict(_default_model_cache)

    added = [mid for mid in after_catalog if mid not in before_catalog]
    removed = [mid for mid in before_catalog if mid not in after_catalog]
    changed = [
        mid for mid in after_catalog
        if mid in before_catalog and after_catalog[mid] != before_catalog[mid]
    ]
    role_added = [f"{t}/{r}" for (t, r) in after_roles if (t, r) not in before_roles]
    role_removed = [f"{t}/{r}" for (t, r) in before_roles if (t, r) not in after_roles]
    role_changed = [
        f"{t}/{r}" for (t, r) in after_roles
        if (t, r) in before_roles and after_roles[(t, r)] != before_roles[(t, r)]
    ]
    return {
        "model_catalog": {"added": added, "removed": removed, "changed": changed},
        "team_role_models": {"added": role_added, "removed": role_removed, "changed": role_changed},
    }


async def reset_cache_for_tests() -> None:
    """Test-only: clears the in-process cache and its loaded flag so a test can
    force a fresh load against a freshly monkeypatched config.database_url."""
    global _cache_loaded
    _route_cache.clear()
    _default_model_cache.clear()
    _cache_loaded = False


# ── Seed data ─────────────────────────────────────────────────────────────────
# ZGX local models (was swarm/agents.py's _VLLM_MODEL_MAP) + the 5 cloud aliases
# (was _CLOUD_ALIASES) + each shipped team YAML's current per-agent model, so a
# fresh deployment's routing/gating behavior matches the pre-DB code on first
# run. Runs once, only when model_catalog is empty (see load_cache above) —
# never overwrites rows an admin has since edited.

_LOCAL_MODELS = [
    # (model_id, vllm_served_as) — the vLLM ALL-MoE consolidation mapping (see
    # the historical note formerly in swarm/agents.py): every local id collapses
    # onto the one resident coordinator model under INFERENCE_BACKEND=vllm.
    # provider is "local" (not "ollama"/"vllm" specifically) because a single
    # row serves BOTH backends — which one actually runs is
    # config.inference_backend's call at request time, not a per-model fact.
    ("qwen3-coder:30b", "qwen3-coder-30b"),
    ("qwen2.5-coder:32b", "qwen3-coder-30b"),
    ("qwen2.5-coder:7b", "qwen3-coder-30b"),
    ("llama3.1:8b", "qwen3-coder-30b"),
]

_CLOUD_MODELS = [
    # (model_id, provider) — see zgx-ai-setup/litellm-config.yaml's "Cloud providers" section.
    ("claude-sonnet-cloud", "anthropic"),
    ("gpt-4o-cloud", "openai"),
    ("gemini-flash-cloud", "google"),
    ("sonar-pro-cloud", "perplexity"),
    ("hf-inference-cloud", "huggingface"),
]

# (team_name, role_name, model_id) — current per-agent model: values from
# teams/*.yaml at the time this addendum landed (2026-08-08). Coordinator uses
# the fixed role_name "Coordinator". This seed only ever runs once against an
# empty model_catalog, so it does not need to stay in lockstep with the YAMLs
# afterward — a team YAML's own model: field always wins over this DB default
# (see get_default_model()'s docstring / api/server.py's _load_team()).
_TEAM_ROLE_DEFAULTS = [
    ("engineering", "Coordinator", "qwen3-coder:30b"),
    ("engineering", "ContextRouter", "llama3.1:8b"),
    ("engineering", "Researcher", "qwen2.5-coder:32b"),
    ("engineering", "Planner", "qwen2.5-coder:32b"),
    ("engineering", "Coder", "qwen2.5-coder:32b"),
    ("engineering", "Executor", "llama3.1:8b"),
    ("engineering", "Reviewer", "qwen2.5-coder:32b"),
    ("parallel-review", "Coordinator", "qwen2.5-coder:7b"),
    ("parallel-review", "Researcher", "qwen2.5-coder:32b"),
    ("parallel-review", "SecurityReviewer", "qwen2.5-coder:32b"),
    ("parallel-review", "PerformanceReviewer", "qwen2.5-coder:32b"),
    ("planning", "Coordinator", "qwen2.5-coder:7b"),
    ("planning", "ContextRouter", "llama3.1:8b"),
    ("planning", "Researcher", "qwen2.5-coder:32b"),
    ("planning", "Planner", "qwen2.5-coder:32b"),
    ("sprint-master", "Coordinator", "qwen3-coder:30b"),
    ("sprint-master", "BacklogResearcher", "qwen2.5-coder:32b"),
    ("sprint-master", "StoryWriter", "qwen2.5-coder:32b"),
]


async def _seed_defaults(conn) -> None:
    await conn.execute(
        db.model_catalog.insert(),
        [
            {
                "model_id": model_id, "kind": "local", "provider": "local",
                "vllm_served_as": served_as, "requires_cloud_gate": False, "active": True,
            }
            for model_id, served_as in _LOCAL_MODELS
        ] + [
            {
                "model_id": model_id, "kind": "cloud", "provider": provider,
                "vllm_served_as": None, "requires_cloud_gate": True, "active": True,
            }
            for model_id, provider in _CLOUD_MODELS
        ],
    )
    await conn.execute(
        db.team_role_models.insert(),
        [
            {"team_name": team_name, "role_name": role_name, "model_id": model_id}
            for team_name, role_name, model_id in _TEAM_ROLE_DEFAULTS
        ],
    )
