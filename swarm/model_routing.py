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

import httpx
from sqlalchemy import select

from config.config import config
from swarm import db


@dataclass(frozen=True)
class ModelRoute:
    model_id: str
    kind: str  # "local" | "cloud"
    provider: str
    vllm_served_as: str | None
    requires_cloud_gate: bool
    active: bool


@dataclass(frozen=True)
class RolePolicy:
    """A (team, role)'s DB-backed default -- default model plus, since
    Recommendation #4 (2026-08-13, see DOCS.md "Declarative Per-Role Policy"),
    default sampling params. temperature/max_tokens/tool_call_limit are each
    independently Optional: None means "no DB override for this field, use
    config.py's existing global default" -- adding this dataclass and its three
    new fields changes nothing for a (team, role) row that never sets them."""
    model_id: str
    temperature: float | None
    max_tokens: int | None
    tool_call_limit: int | None


_route_cache: dict[str, ModelRoute] = {}
_role_policy_cache: dict[tuple[str, str], RolePolicy] = {}
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
    present, always takes precedence over this. Thin wrapper over
    get_role_policy() for callers that only ever needed the model id."""
    policy = _role_policy_cache.get((team_name, role_name))
    return policy.model_id if policy else None


def get_role_policy(team_name: str, role_name: str) -> RolePolicy | None:
    """Sync, cache-only lookup of a role's full DB-backed default -- model plus
    sampling params. Used by api/server.py's _load_team() the same way
    get_default_model() already was: a team YAML's own explicit field always
    wins; this only fills a gap the YAML left unset."""
    return _role_policy_cache.get((team_name, role_name))


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
    await db.ensure_routing_schema()
    async with db.get_routing_engine().begin() as conn:
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
    _role_policy_cache.clear()
    for r in role_rows:
        _role_policy_cache[(r["team_name"], r["role_name"])] = RolePolicy(
            model_id=r["model_id"], temperature=r["temperature"],
            max_tokens=r["max_tokens"], tool_call_limit=r["tool_call_limit"],
        )


async def reload() -> dict:
    """Re-read the DB into the cache and return a diff — rows added/changed/
    removed since the previous cache snapshot — so an admin edit
    (POST /admin/model-routes/reload) is visible immediately instead of a bare
    200 that leaves the caller guessing whether anything actually changed.

    role_changed compares whole RolePolicy objects (value equality, since it's a
    frozen dataclass), not just model_id — an admin edit that only changes
    max_tokens for an existing (team, role) row now correctly shows up as
    "changed" too, not just a model_id swap."""
    before_catalog = dict(_route_cache)
    before_roles = dict(_role_policy_cache)
    await load_cache()
    after_catalog = dict(_route_cache)
    after_roles = dict(_role_policy_cache)

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


async def check_coordinator_readiness(team_name: str = "engineering") -> str | None:
    """Best-effort startup diagnostic, added 2026-08-16 after a real gap was
    found: nothing in this codebase ever verified that a resolved model_id's
    backend (Ollama or the vLLM/LiteLLM gateway) is actually reachable, or that
    the model is actually pulled/served. A brand-new user cloning the repo gets
    a silently, unconditionally auto-seeded model_catalog (see load_cache()
    above) regardless of whether Ollama/vLLM exist on their machine at all --
    /health only confirms the FastAPI process itself is alive, and the first
    real signal of a missing backend was previously a raw connection error or
    404 surfacing deep inside agno's model client mid-task, nowhere near this
    project's own "fail loudly and specifically" convention (e.g.
    ALLOW_CLOUD_MODELS).

    Checks ONLY the Coordinator of one representative team (default
    "engineering") -- a full per-role audit of every row in team_role_models
    belongs in a dedicated admin endpoint, not something that runs on every
    process start. Returns None if it looks healthy, OR if the check itself
    can't reach a conclusion (no DB row yet, cloud-routed, request shape
    unexpected) -- this is diagnostic only and must never be the reason
    startup fails or a task is blocked; a wrong warning is a nuisance, a
    false-negative silence is acceptable, but blocking on this check is not.
    Returns a human-actionable message naming the exact command to run
    otherwise.
    """
    try:
        model_id = get_default_model(team_name, "Coordinator")
        if not model_id:
            return None
        route = get_route(model_id)
        if route is None or route.kind == "cloud":
            # Unregistered/inactive falls back to get_model()'s own existing
            # behavior, not a backend-readiness concern. Cloud readiness is a
            # credentials/network question already covered by the
            # ALLOW_CLOUD_MODELS gate, not a local-setup one.
            return None

        async with httpx.AsyncClient(timeout=5.0) as client:
            if config.inference_backend == "ollama":
                host = config.ollama_host or "http://localhost:11434"
                try:
                    resp = await client.get(f"{host.rstrip('/')}/api/tags")
                    resp.raise_for_status()
                except httpx.HTTPError:
                    return (
                        f"Ollama isn't reachable at {host} — the Coordinator's model "
                        f"({model_id!r}) can't be served. Install/start Ollama, then run "
                        f"`ollama pull {model_id}`. See docs/guide/setup.md."
                    )
                # Ollama model names in `ollama list`/`/api/tags` carry a tag suffix
                # (e.g. ":latest") even for what was pulled as a bare name — match on
                # a prefix so a real pull of `model_id` is recognized regardless of
                # the exact suffix Ollama reports it under.
                pulled = {m.get("name", "") for m in resp.json().get("models", [])}
                if not any(n == model_id or n.startswith(f"{model_id}") for n in pulled):
                    return (
                        f"Ollama is running but doesn't have `{model_id}` pulled yet "
                        f"(the Coordinator's model for team {team_name!r}). Run "
                        f"`ollama pull {model_id}`."
                    )
            elif config.inference_backend == "vllm":
                served_as = route.vllm_served_as or model_id.replace(":", "-")
                try:
                    resp = await client.get(f"{config.vllm_gateway_url.rstrip('/')}/models")
                    resp.raise_for_status()
                except httpx.HTTPError:
                    return (
                        f"The vLLM/LiteLLM gateway isn't reachable at "
                        f"{config.vllm_gateway_url} — the Coordinator's model "
                        f"({model_id!r} -> {served_as!r}) can't be served. Start the "
                        f"stack: `docker compose -f zgx-ai-setup/docker-compose.yml up "
                        f"-d`. See docs/guide/setup.md."
                    )
                served = {m.get("id", "") for m in resp.json().get("data", [])}
                if served_as not in served:
                    return (
                        f"The LiteLLM gateway is up but doesn't know about "
                        f"`{served_as}` (the Coordinator's served alias for team "
                        f"{team_name!r}). Check zgx-ai-setup/litellm-config.yaml and "
                        f"that the matching vLLM container is running."
                    )
    except Exception:
        # Never let a diagnostic check crash startup or a task -- anything
        # unexpected (malformed response, unknown backend value, etc.) is
        # treated the same as "couldn't reach a conclusion."
        return None
    return None


async def reset_cache_for_tests() -> None:
    """Test-only: clears the in-process cache and its loaded flag so a test can
    force a fresh load against a freshly monkeypatched config.database_url."""
    global _cache_loaded
    _route_cache.clear()
    _role_policy_cache.clear()
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
    #
    # served alias is "local-shared", deliberately NOT named after any model family
    # (renamed 2026-08-16 from "qwen3-coder-30b"). That name was kept "stable" through
    # three real served-model swaps (granite4.1:30b -> qwen3-coder:30b -> Qwen3-30B-
    # A3B-Instruct-2507-FP8 -> now the fine-tuned qwen3-30b-hive-v2-fp8) and stopped
    # describing the actual model after the FIRST of those swaps -- a vendor-committed
    # "stable" alias is not actually stable, it just delays the day the name goes
    # stale. "local-shared" never claims a model family, so it can't drift again.
    ("qwen3-coder:30b", "local-shared"),
    ("qwen2.5-coder:32b", "local-shared"),
    ("qwen2.5-coder:7b", "local-shared"),
    ("llama3.1:8b", "local-shared"),
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

# Explicit sampling-param overrides for specific (team, role) pairs (Recommendation
# #4, 2026-08-13, see DOCS.md "Declarative Per-Role Policy"). Omitted fields stay
# NULL -- "use config.py's global default," today's existing behavior for every
# role not listed here. Coder's max_tokens=8192 replaces swarm/agents.py's old
# hardcoded `if spec.name == "Coder"` special-case with a data row carrying the
# SAME value -- a fresh deployment's seeded behavior is unchanged, only the
# mechanism moved from code to data. Like _TEAM_ROLE_DEFAULTS above, this only
# ever runs against an empty model_catalog; an already-populated database (ZGX's)
# needs the identical value applied once via a real /admin/model-routes/teams
# call instead, since create_all()'s "missing table" check doesn't re-run seeding.
#
# engineering/Reviewer's tool_call_limit=45 (2026-08-18, T6 empty-completion-loop
# root cause): confirmed live (task k9732nnic) that Reviewer's default 25-call
# budget (config.tool_call_limit) is not enough headroom for its gap-analysis
# cross-check role -- re-deriving both of Researcher's enumerated lists plus
# independently verifying the conclusion legitimately needs ~16-18 reads, and
# with zero margin for any repeated/paginated call, Reviewer hit exactly 25 real
# tool calls (confirmed via journalctl: search_knowledge_graph + 8 distinct
# get_file_content/search_files calls, THEN an unexplained full second pass
# repeating 8 of those same calls verbatim -- see the empty-completion note
# below for why it likely lost its place and restarted). Once agno's own
# arun_function_calls/run_function_calls (agno/models/base.py) sees
# current_function_call_count > tool_call_limit, it stops executing calls
# entirely and instead appends `create_tool_call_limit_error_result` -- a
# generic "Tool call limit reached... don't try again" tool-result message --
# for every further attempt. This bypasses team.py's own tool_hooks chain
# completely (the rejection happens inside agno's model layer, before any hook
# in swarm/team.py ever runs), so none of this file's own escalating-stub
# reinforcement (_duplicate_read_stub, _forced_answer_nudge) ever gets a chance
# to redirect the model. Confirmed via vllm-coord's own engine logs (sustained
# non-zero generation throughput, "Running: 1 reqs" almost continuously) plus
# _log_unclassified_stream_event's own throttled counter (~20 raw
# ModelRequestStarted/RunContent/ModelRequestCompleted cycles logged as one
# print, each individual cycle only ~1-2s wall time): once past the ceiling,
# the model kept re-attempting tool calls in a rapid (~1.5s/cycle), silent loop
# -- each attempt voided by agno's own limit check with content='' that turn --
# for the full 300s+ until the outer liveness watchdog killed the run. This
# entry raises the ceiling so the legitimate cross-check completes with real
# margin to spare, which is the correct fix for THIS incident's proximate
# cause; the deeper gap (agno's own rejection path has no way to reach team.py's
# reinforcement machinery, and a model that starts attempting tools it can't use
# doesn't reliably self-correct into a text-only final answer) remains open --
# see DOCS.md "Reviewer Empty-Completion Loop Root-Caused" for the full
# investigation and the raw evidence this entry summarizes.
_TEAM_ROLE_POLICY_OVERRIDES: dict[tuple[str, str], dict] = {
    ("engineering", "Coder"): {"max_tokens": 8192},
    ("engineering", "Reviewer"): {"tool_call_limit": 45},
}


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
            {
                "team_name": team_name, "role_name": role_name, "model_id": model_id,
                "temperature": None, "max_tokens": None, "tool_call_limit": None,
                **_TEAM_ROLE_POLICY_OVERRIDES.get((team_name, role_name), {}),
            }
            for team_name, role_name, model_id in _TEAM_ROLE_DEFAULTS
        ],
    )
