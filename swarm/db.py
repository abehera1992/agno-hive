"""Shared SQLAlchemy async engine + table metadata for agno-hive's OWN application
storage (chat sessions, the self-improvement failure log, model routing). This is
NOT project-specific data — a connected project's own database (if any) is reached
separately, read-only, via hive-mcp's db_schema/db_query tools.

Engine-agnostic by design (AGNOHive 2.3.2 addendum, 2026-08-08): ships as a local
SQLite file with zero provisioning (config.database_url unset), or point it at
Postgres/MySQL/anything SQLAlchemy has a dialect for. See docs/guide/cloud-models.md.
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

metadata = MetaData()


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
model_catalog = Table(
    "model_catalog", metadata,
    Column("model_id", Text, primary_key=True),           # 'qwen3-coder:30b', 'claude-sonnet-cloud'
    Column("kind", Text, nullable=False),                  # 'local' | 'cloud'
    Column("provider", Text, nullable=False),               # 'ollama' | 'vllm' | 'anthropic' | 'openai' | ...
    Column("vllm_served_as", Text, nullable=True),          # consolidation override, local only
    Column("requires_cloud_gate", Boolean, nullable=False, default=False),
    Column("active", Boolean, nullable=False, default=True),
)

team_role_models = Table(
    "team_role_models", metadata,
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


_engine: AsyncEngine | None = None
_engine_url: str | None = None


def get_engine() -> AsyncEngine:
    """Process-wide engine, rebuilt if the resolved URL changes (test isolation —
    monkeypatching config.database_url between tests must not reuse a stale
    connection pool bound to the previous URL)."""
    global _engine, _engine_url
    url = resolve_database_url()
    if _engine is None or _engine_url != url:
        if "sqlite" in url and ":memory:" in url:
            # In-memory SQLite is per-connection by default, so a normal pool would
            # silently hand out a fresh (empty) database on every checkout. StaticPool
            # keeps ONE connection alive for the engine's lifetime — required for
            # in-memory SQLite to behave like a real shared database (used by tests).
            _engine = create_async_engine(
                url, poolclass=StaticPool, connect_args={"check_same_thread": False}
            )
        else:
            _engine = create_async_engine(url)
        _engine_url = url
        if url.startswith("sqlite"):
            # SQLite does not enforce foreign keys by default — without this,
            # ON DELETE CASCADE (chat_sessions -> session_messages) silently no-ops
            # and delete_session() would leave orphaned message rows, a behavior
            # divergence from Postgres (which enforces FKs unconditionally).
            @event.listens_for(_engine.sync_engine, "connect")
            def _enable_sqlite_fk(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
    return _engine


_TEAM_ROLE_MODELS_NEW_COLUMNS = {
    "temperature": "FLOAT",
    "max_tokens": "INTEGER",
    "tool_call_limit": "INTEGER",
}


def _existing_team_role_models_columns(sync_conn) -> set[str]:
    return {c["name"] for c in inspect(sync_conn).get_columns("team_role_models")}


async def ensure_schema() -> None:
    """Idempotent bootstrap — create_all() only creates tables that don't already
    exist, so this is safe to call on every startup (matches the old CREATE TABLE
    IF NOT EXISTS ethos) against either a fresh SQLite file or an existing ZGX
    Postgres database that already has these tables from prior deployments.

    team_role_models is the one table this codebase has ever needed to widen
    after it was already deployed and populated (Recommendation #4, 2026-08-13):
    create_all() only creates MISSING tables, it never ALTERs an existing one, so
    on ZGX's already-populated database a plain create_all() would silently leave
    temperature/max_tokens/tool_call_limit missing entirely -- not merely NULL,
    genuinely absent -- breaking the first INSERT or SELECT that touches them.
    Handled by introspecting the table's REAL columns first and only issuing
    `ALTER TABLE ADD COLUMN` for ones actually missing, rather than a blind
    try/except ALTER: Postgres aborts the whole enclosing transaction on any
    failed statement within it (unlike SQLite), so a failed "already exists"
    ALTER here would poison every later statement in this same `engine.begin()`
    block, including create_all() itself if this ran first. Introspecting avoids
    ever attempting the failing statement in the first place."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        existing_columns = await conn.run_sync(_existing_team_role_models_columns)
        for col_name, col_type in _TEAM_ROLE_MODELS_NEW_COLUMNS.items():
            if col_name not in existing_columns:
                await conn.execute(text(f"ALTER TABLE team_role_models ADD COLUMN {col_name} {col_type}"))


async def reset_engine_for_tests() -> None:
    """Dispose the cached engine so the next get_engine() call rebuilds against
    whatever config.database_url a test just monkeypatched. Test-only."""
    global _engine, _engine_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _engine_url = None
