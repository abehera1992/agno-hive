"""Shared SQLAlchemy async engine + table metadata for agno-hive's OWN application
storage (chat sessions, the self-improvement failure log, model routing). This is
NOT project-specific data — a connected project's own database (if any) is reached
separately, read-only, via hive-mcp's db_schema/db_query tools.

Engine-agnostic by design (AGNOHive 2.3.2 addendum, 2026-08-08): ships as a local
SQLite file with zero provisioning (config.database_url unset), or point it at
Postgres/MySQL/anything SQLAlchemy has a dialect for. See docs/guide/cloud-models.md.

TWO separate engines, not one (split 2026-08-16). chat_sessions/session_messages/
failure_log use resolve_database_url()/get_engine() (DATABASE_URL, falling back to
the legacy POSTGRES_URI, falling back to SQLite) — unchanged. model_catalog/
team_role_models use resolve_routing_database_url()/get_routing_engine(), which
deliberately does NOT fall back to POSTGRES_URI: that fallback is exactly what
caused these two tables to land inside ZGX's Apache AGE graph-storage Postgres
instance on 2026-08-08, coupling model-routing config to a graph database for no
reason other than POSTGRES_URI already being set for the OTHER three tables — the
design page for this feature explicitly ruled out that coupling, but the code's
compatibility fallback ("so ZGX needs no .env change") reintroduced it anyway,
silently, the same day. Routing config now defaults to its own dedicated SQLite
file unconditionally unless MODEL_ROUTING_DATABASE_URL is explicitly set — no
implicit inheritance from whatever the session/feedback tables happen to use.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    event,
    inspect,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import func

from config.config import config

_DEFAULT_SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "agnohive.db"
_DEFAULT_ROUTING_SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "model_routing.db"

metadata = MetaData()
# Deliberately a SEPARATE MetaData, not a second binding of the same one -- there is
# no foreign key crossing between {chat_sessions, session_messages, failure_log} and
# {model_catalog, team_role_models}, so nothing is lost by giving the routing tables
# their own metadata.create_all() scope, and it's what makes get_routing_engine()
# create ONLY these two tables in the dedicated SQLite file instead of all five.
routing_metadata = MetaData()


# ── Schema ────────────────────────────────────────────────────────────────────
# All columns baked in from the start (no incremental ALTER TABLE ADD COLUMN
# needed here the way the old raw-psycopg bootstrap required) — an existing ZGX
# Postgres deployment already has every one of these columns from its prior
# additive migrations, so create_all() below is a safe no-op there; a fresh
# SQLite deployment gets the full shape on first run.

chat_sessions = Table(
    "chat_sessions", metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("persist", Boolean, nullable=False, default=False),
    Column("summary", Text, nullable=True),
    Column("summary_through", Integer, nullable=False, default=0),
    # No FK to session_messages.id here (matches the original schema) — avoids a
    # table-creation-order cycle between the two tables below.
    Column("current_leaf_id", Integer, nullable=True),
)
Index("chat_sessions_project_idx", chat_sessions.c.project_id, chat_sessions.c.created_at.desc())

session_messages = Table(
    "session_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", Uuid(as_uuid=False), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("parent_message_id", Integer, ForeignKey("session_messages.id", ondelete="SET NULL"), nullable=True),
)
Index("session_messages_session_idx", session_messages.c.session_id, session_messages.c.created_at.asc())
Index("session_messages_parent_idx", session_messages.c.parent_message_id)

failure_log = Table(
    "failure_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, nullable=False),
    Column("task", Text, nullable=False),
    Column("error_type", Text, nullable=False, default="unknown"),
    Column("error_message", Text, nullable=False, default=""),
    Column("agent", Text, nullable=False, default="unknown"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("rejected_output", Text, nullable=True),
    Column("corrected_output", Text, nullable=True),
)
Index("failure_log_project_idx", failure_log.c.project_id, failure_log.c.created_at.desc())

# model_catalog / team_role_models (AGNOHive 2.3.2 addendum) — replaces
# swarm/agents.py's old _VLLM_MODEL_MAP dict + _CLOUD_ALIASES set. See
# swarm/model_routing.py for the cache + get_model() integration.
#
# Bound to routing_metadata, NOT metadata — see the module docstring's "TWO
# separate engines" note. These two tables live in their own dedicated SQLite
# file via get_routing_engine(), independent of wherever chat_sessions/
# session_messages/failure_log happen to be.
model_catalog = Table(
    "model_catalog", routing_metadata,
    Column("model_id", Text, primary_key=True),           # 'qwen3-coder:30b', 'claude-sonnet-cloud'
    Column("kind", Text, nullable=False),                  # 'local' | 'cloud'
    Column("provider", Text, nullable=False),               # 'ollama' | 'vllm' | 'anthropic' | 'openai' | ...
    Column("vllm_served_as", Text, nullable=True),          # consolidation override, local only
    Column("requires_cloud_gate", Boolean, nullable=False, default=False),
    Column("active", Boolean, nullable=False, default=True),
)

team_role_models = Table(
    "team_role_models", routing_metadata,
    Column("team_name", Text, primary_key=True),
    Column("role_name", Text, primary_key=True),            # 'Coordinator', 'Executor', ...
    Column("model_id", Text, ForeignKey("model_catalog.model_id"), nullable=False),
    # Declarative per-role policy (Recommendation #4, 2026-08-13, see DOCS.md
    # "Declarative Per-Role Policy") -- replaces swarm/agents.py's old hardcoded
    # `if spec.name == "Coder"` special-case and config.py's global-only
    # member_temperature/member_max_tokens/tool_call_limit for anything that needs
    # a per-role override. NULL (the default for every row) means "use config.py's
    # existing global default" -- adding these columns changes nothing for a team
    # that never sets them, same precedence model_id already established: a team
    # YAML's own field wins when present, this DB row fills the gap otherwise.
    Column("temperature", Float, nullable=True),
    Column("max_tokens", Integer, nullable=True),
    Column("tool_call_limit", Integer, nullable=True),
)

# AGNOHive 2.3.3 (2026-08-18) -- moving team YAML config (tools/skills/gate flags/
# supplementary instructions) to the SAME dedicated routing SQLite engine, NOT a
# new one -- see the Notion design page "AGNOHive 2.3.3 - Moving team yaml
# configs to sqlite db" for the full three-tier rationale. Bound to
# routing_metadata like model_catalog/team_role_models above, for the identical
# reason: no FK crossing into {chat_sessions, session_messages, failure_log}.
#
# Tier 1 -- per-role tool/skill allowlist. Same override-with-DB-fallback
# precedence as model_id (swarm/model_routing.py's team_role_models): a team
# YAML's own tools:/skills:, when present, always wins outright -- the "pin it
# back in the YAML to take it out of DB control" escape hatch; when the YAML
# omits the field, these tables supply the role's full list. Changed 2026-08-18
# from an initial additive-union design (DB rows layered on top of the YAML's
# own list) to this replace-or-fallback design, specifically so the DB is the
# actual runtime source of a role's tools/skills rather than a YAML-plus-extras
# layer -- all 4 shipped teams/*.yaml have had their tools:/skills: fields
# removed accordingly, with seeds/team_config.yaml (a static snapshot of the
# former YAML content) seeding these tables on a fresh deployment. A team YAML deliberately hardcoding a
# full roster (e.g. a future engineering-cloud.yaml-style reference team) stays
# unaffected as long as it keeps an explicit tools:/skills: list.
team_role_tools = Table(
    "team_role_tools", routing_metadata,
    Column("team_name", Text, primary_key=True),
    Column("role_name", Text, primary_key=True),   # 'Coordinator' for the coordinator's own allowlist
    Column("tool_name", Text, primary_key=True),
)

team_role_skills = Table(
    "team_role_skills", routing_metadata,
    Column("team_name", Text, primary_key=True),
    Column("role_name", Text, primary_key=True),
    Column("skill_name", Text, primary_key=True),
)

# Registry of KNOWN tool/skill names -- Open Question #2's resolution (write-time
# reject, not silent read-time skip). A row here means "this name was seen on a
# live MCP connection / skill catalog as of last_seen_at" -- team_role_tools/
# team_role_skills inserts are validated against these at the admin-API layer
# (api/server.py), not at the DB layer (SQLite has no easy "value must exist in
# this OTHER table's column" constraint short of a real FK, which would also
# block inserting a tool grant before that tool has ever been seen once -- a
# chicken-and-egg problem a plain application-level check avoids). Deliberately
# NOT hand-maintained: refreshed FROM a live tool/skill enumeration via
# swarm/team_config.py's refresh_registry(), the same "reload re-reads the live
# source of truth" pattern model_routing.reload() already uses -- see that
# module for why a registry that could go stale on its own would be worse than
# the problem it exists to solve.
tool_registry = Table(
    "tool_registry", routing_metadata,
    Column("tool_name", Text, primary_key=True),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

skill_registry = Table(
    "skill_registry", routing_metadata,
    Column("skill_name", Text, primary_key=True),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Tier 2 -- additive-only SUPPLEMENTARY instructions, layered on top of a role's
# existing base instructions (the hardcoded/_COORDINATOR_INSTRUCTIONS and each
# team YAML's own instructions: list) -- which stay completely OUT of this
# migration, untouched, git-tracked, code-reviewed, exactly as before. A row here
# can only ADD a line, never remove or replace one of the tested base
# instructions, so (per the Notion design decision) this needs no versioning/
# audit-trail ceremony the way a REPLACE mechanism would -- plain CRUD is enough.
# Soft-capped at write time (see api/server.py's admin endpoint) to
# _INSTRUCTION_OVERLAY_SOFT_CAP active rows per (team_name, role_name) --
# Engineering Team 2.0's own Phase 5 already found and fixed a real instruction-
# bloat problem once, and an unbounded user-editable list would reintroduce it.
team_role_instruction_overlays = Table(
    "team_role_instruction_overlays", routing_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("team_name", Text, nullable=False),
    Column("role_name", Text, nullable=False),
    Column("instruction_text", Text, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_by", Text, nullable=True),
)
Index("team_role_instruction_overlays_role_idx", team_role_instruction_overlays.c.team_name, team_role_instruction_overlays.c.role_name)

# Open Question #1's resolution -- per-gate on/off flags as a Tier-1-style
# boolean row, even though the gate's own LOGIC stays code (Tier 3). A row here
# OVERRIDES swarm/team.py's hardcoded _GATE_ENABLED_TEAMS/_SEARCH_GATE_ENABLED_TEAMS
# set-membership check for that one (team_name, gate_name) pair; no row for a
# given team+gate falls back to the existing hardcoded set exactly as today --
# see swarm/team_config.py's get_gate_enabled() and its call site in
# swarm/team.py's _build_team(). gate_name is one of "decompose_first" /
# "search_before_browse", matching the two mechanical gates that actually exist.
team_gate_flags = Table(
    "team_gate_flags", routing_metadata,
    Column("team_name", Text, primary_key=True),
    Column("gate_name", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False),
)


# ── Engine ────────────────────────────────────────────────────────────────────

def _normalize_async_url(url: str) -> str:
    """Rewrite a plain vendor-style DSN to the async-driver dialect this app uses,
    so docs/env files can say "sqlite:///..." or "postgresql://..." (the DSN forms
    everyone already knows) without every deployment needing to know SQLAlchemy's
    driver-suffix convention."""
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


def resolve_database_url() -> str:
    """DATABASE_URL, falling back to the legacy POSTGRES_URI, falling back to a
    local SQLite file under <repo>/data/ — evaluated fresh on every call (not
    cached at import time) so tests can monkeypatch config.database_url."""
    if config.database_url:
        return _normalize_async_url(config.database_url)
    if config.postgres_uri:
        return _normalize_async_url(config.postgres_uri)
    _DEFAULT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{_DEFAULT_SQLITE_PATH}"


def resolve_routing_database_url() -> str:
    """MODEL_ROUTING_DATABASE_URL if explicitly set, otherwise a dedicated SQLite
    file — deliberately NOT falling back to DATABASE_URL/POSTGRES_URI the way
    resolve_database_url() does. That fallback chain is exactly what put
    model_catalog/team_role_models inside ZGX's Apache AGE graph Postgres instance
    on 2026-08-08 (POSTGRES_URI was already set for the other three tables, so the
    routing tables inherited it with no explicit decision made for them). Routing
    config gets its own default so it can never again silently inherit wherever
    the session/feedback tables happen to live."""
    if config.model_routing_database_url:
        return _normalize_async_url(config.model_routing_database_url)
    _DEFAULT_ROUTING_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{_DEFAULT_ROUTING_SQLITE_PATH}"


def _build_engine(url: str) -> AsyncEngine:
    if "sqlite" in url and ":memory:" in url:
        # In-memory SQLite is per-connection by default, so a normal pool would
        # silently hand out a fresh (empty) database on every checkout. StaticPool
        # keeps ONE connection alive for the engine's lifetime — required for
        # in-memory SQLite to behave like a real shared database (used by tests).
        engine = create_async_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    else:
        engine = create_async_engine(url)
    if url.startswith("sqlite"):
        # SQLite does not enforce foreign keys by default — without this,
        # ON DELETE CASCADE (chat_sessions -> session_messages, and
        # team_role_models -> model_catalog) silently no-ops, a behavior
        # divergence from Postgres (which enforces FKs unconditionally).
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


_engine: AsyncEngine | None = None
_engine_url: str | None = None


def get_engine() -> AsyncEngine:
    """Process-wide engine for chat_sessions/session_messages/failure_log,
    rebuilt if the resolved URL changes (test isolation — monkeypatching
    config.database_url between tests must not reuse a stale connection pool
    bound to the previous URL)."""
    global _engine, _engine_url
    url = resolve_database_url()
    if _engine is None or _engine_url != url:
        _engine = _build_engine(url)
        _engine_url = url
    return _engine


_routing_engine: AsyncEngine | None = None
_routing_engine_url: str | None = None


def get_routing_engine() -> AsyncEngine:
    """Process-wide engine for model_catalog/team_role_models ONLY — separate
    from get_engine() by design, see resolve_routing_database_url()."""
    global _routing_engine, _routing_engine_url
    url = resolve_routing_database_url()
    if _routing_engine is None or _routing_engine_url != url:
        _routing_engine = _build_engine(url)
        _routing_engine_url = url
    return _routing_engine


_TEAM_ROLE_MODELS_NEW_COLUMNS = {
    "temperature": "FLOAT",
    "max_tokens": "INTEGER",
    "tool_call_limit": "INTEGER",
}


def _existing_team_role_models_columns(sync_conn) -> set[str]:
    return {c["name"] for c in inspect(sync_conn).get_columns("team_role_models")}


async def ensure_schema() -> None:
    """Idempotent bootstrap for chat_sessions/session_messages/failure_log —
    create_all() only creates tables that don't already exist, so this is safe to
    call on every startup against either a fresh SQLite file or an existing ZGX
    Postgres database that already has these tables from prior deployments.
    model_catalog/team_role_models are handled separately by
    ensure_routing_schema() against get_routing_engine()."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


async def ensure_routing_schema() -> None:
    """Idempotent bootstrap for model_catalog/team_role_models, against the
    SEPARATE routing engine (see resolve_routing_database_url()).

    team_role_models is the one table this codebase has ever needed to widen
    after it was already deployed and populated (Recommendation #4, 2026-08-13):
    create_all() only creates MISSING tables, it never ALTERs an existing one, so
    on an already-populated database a plain create_all() would silently leave
    temperature/max_tokens/tool_call_limit missing entirely -- not merely NULL,
    genuinely absent -- breaking the first INSERT or SELECT that touches them.
    Handled by introspecting the table's REAL columns first and only issuing
    `ALTER TABLE ADD COLUMN` for ones actually missing, rather than a blind
    try/except ALTER: Postgres aborts the whole enclosing transaction on any
    failed statement within it (unlike SQLite), so a failed "already exists"
    ALTER here would poison every later statement in this same `engine.begin()`
    block, including create_all() itself if this ran first. Introspecting avoids
    ever attempting the failing statement in the first place. Kept even though
    the routing store is SQLite-only in practice now, since a future
    MODEL_ROUTING_DATABASE_URL could still point at Postgres."""
    engine = get_routing_engine()
    async with engine.begin() as conn:
        await conn.run_sync(routing_metadata.create_all)
        existing_columns = await conn.run_sync(_existing_team_role_models_columns)
        for col_name, col_type in _TEAM_ROLE_MODELS_NEW_COLUMNS.items():
            if col_name not in existing_columns:
                await conn.execute(text(f"ALTER TABLE team_role_models ADD COLUMN {col_name} {col_type}"))


async def reset_engine_for_tests() -> None:
    """Dispose both cached engines so the next get_engine()/get_routing_engine()
    call rebuilds against whatever config a test just monkeypatched. Test-only."""
    global _engine, _engine_url, _routing_engine, _routing_engine_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _engine_url = None
    if _routing_engine is not None:
        await _routing_engine.dispose()
    _routing_engine = None
    _routing_engine_url = None
