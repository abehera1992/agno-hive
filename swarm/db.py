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
    JSON,
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

# task_outcome_queue (2026-08-29) — durable handoff for post-run experience indexing.
#
# Bound to `metadata`, beside failure_log, deliberately: this is the SUCCESS half of the
# same feedback loop, and the failure half has always been a table. Only the success half
# used fire-and-forget, and that is exactly the half that silently stopped working.
#
# The bug it fixes: record_success_bg() called asyncio.create_task(). Every run executes
# in an ephemeral worker subprocess (`main.py --run-worker`), which ends with
# `asyncio.run(_run_worker())` -- and asyncio.run() CANCELS pending tasks when the
# coroutine returns. The indexing task was destroyed microseconds after creation, every
# time, in a process that then exited. drain_background_tasks() exists and is correct,
# but it is registered on the SERVER's shutdown hook: a different process, which never
# runs the code path it protects.
#
# Indexing cannot be inlined instead: it calls vllm-extract for entity extraction and
# takes 30-60s, which would land on every /run response. So the worker must hand the
# work off rather than do it or promise it. An INSERT is ~1ms and, once committed,
# survives the process that wrote it -- which is the whole requirement.
#
# `status` is the retry/observability surface. The 50-day outage was invisible precisely
# because a dropped asyncio task leaves nothing to query; a stuck row here shows up in
# one SELECT.
#
# `owner` is unused today and present on purpose. The experience namespace is per
# project ({project}_experience), so several users on one project SHARING exemplars is
# the feature -- but separate tenants would need isolation, and retrofitting an owner
# column onto a corpus already in use is far worse than carrying a null one now.
task_outcome_queue = Table(
    "task_outcome_queue", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, nullable=False),
    Column("task", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("owner", Text, nullable=True),
    Column("status", Text, nullable=False, default="pending"),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error_message", Text, nullable=True),
    # sha256 of the normalised task text. The dedupe key (2026-08-29): one row per
    # (project, task), so re-verifying a task REPLACES its stored answer instead of
    # adding a second one. Measured need -- the experience namespace had reached 400
    # docs for 310 distinct tasks, 22.5% redundant, one task present FOURTEEN times.
    Column("task_hash", Text, nullable=True),
    # LightRAG's file_path for the doc this row produced, so a replacement can delete
    # the superseded doc before indexing the new one. Without it the DB would hold one
    # row per task while the semantic index kept every version -- dedupe in the place
    # nothing reads, and none in the place retrieval actually happens.
    Column("doc_path", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
# The drain loop's only query: oldest pending first.
Index("task_outcome_queue_pending_idx",
      task_outcome_queue.c.status, task_outcome_queue.c.created_at)
# Enforced at the DB, not just in application logic: two callers posting the same task
# concurrently would both pass a SELECT-then-INSERT check.
Index("task_outcome_queue_dedupe_idx",
      task_outcome_queue.c.project_id, task_outcome_queue.c.task_hash, unique=True)

# Columns added after task_outcome_queue was already deployed and populated. Same
# introspect-then-ALTER treatment as _TEAM_ROLE_MODELS_NEW_COLUMNS below and for the
# same reason: create_all() never widens an existing table, and a blind ALTER that
# fails poisons the whole transaction on Postgres.
_TASK_OUTCOME_QUEUE_NEW_COLUMNS = {
    "task_hash": "TEXT",
    "doc_path": "TEXT",
}


def _existing_task_outcome_queue_columns(sync_conn) -> set[str]:
    insp = inspect(sync_conn)
    if not insp.has_table("task_outcome_queue"):
        return set()
    return {c["name"] for c in insp.get_columns("task_outcome_queue")}


# ── Durable execution/evidence backbone (Phase B) ───────────────────────────────
#
# Schema only in this phase -- nothing in swarm/team.py or swarm/execution_context.py
# writes to these tables yet (see that module's own docstring: Phase A is runtime-
# memory only). Column names mirror Phase A's RunContext/ExecutionRecord/
# ToolCallRecord/EvidenceRecord (swarm/execution_context.py) directly, so a later
# phase can persist them without renaming anything.
#
# Bound to the SAME `metadata` as chat_sessions/session_messages/failure_log/
# task_outcome_queue above -- same engine, same effective database/schema, no
# relocation (see the accepted architecture review's schema-relocation finding:
# moving only NEW tables to a different schema than the existing four would create
# a cross-schema split, not resolve anything -- deferred, not decided here).
#
# session_id is deliberately NOT duplicated onto executions/tool_calls/evidence/
# claim_evidence. Postgres (and SQLite, once PRAGMA foreign_keys=ON is set -- see
# _build_engine below) cascades ON DELETE TRANSITIVELY through a multi-level FK
# chain on its own: deleting a chat_sessions row already reaches every descendant
# via runs.session_id alone, so a redundant session_id column on a deeper table
# would only be able to drift from its own ancestor, never help deletion work.
runs = Table(
    "runs", metadata,
    Column("run_id", Uuid(as_uuid=False), primary_key=True),
    Column("session_id", Uuid(as_uuid=False),
           ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
    # 'single' | 'chunk' | 'synthesis' -- set by a later phase; nullable here since
    # Phase A's RunContext does not track this field today.
    Column("run_type", Text, nullable=True),
    Column("team_name", Text, nullable=True),
    Column("task_preview", Text, nullable=True),
    Column("status", Text, nullable=False, default="running"),  # 'running' | 'ok' | 'failed'
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("error_message", Text, nullable=True),
)
Index("runs_session_idx", runs.c.session_id, runs.c.started_at.asc())

executions = Table(
    "executions", metadata,
    Column("execution_id", Uuid(as_uuid=False), primary_key=True),
    Column("run_id", Uuid(as_uuid=False), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    # Self-referencing. CASCADE (not RESTRICT/SET NULL): deleting an execution
    # should take its own subtree with it, matching the execution-tree semantics
    # Phase A's RunContext already enforces at the application layer (parent
    # closed only after every child under it has finished). In practice this FK
    # is only ever exercised transitively via runs.run_id -> chat_sessions.id
    # cascading -- no code path deletes a single execution on its own.
    Column("parent_execution_id", Uuid(as_uuid=False),
           ForeignKey("executions.execution_id", ondelete="CASCADE"), nullable=True),
    Column("agent_name", Text, nullable=False),
    Column("execution_type", Text, nullable=False),   # 'coordinator' | 'delegation'
    Column("attempt_number", Integer, nullable=False, default=1),
    Column("status", Text, nullable=False, default="running"),  # 'running' | 'ok' | 'failed'
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("error_message", Text, nullable=True),
)
Index("executions_run_idx", executions.c.run_id, executions.c.started_at.asc())
Index("executions_parent_idx", executions.c.parent_execution_id)

tool_calls = Table(
    "tool_calls", metadata,
    Column("tool_call_id", Uuid(as_uuid=False), primary_key=True),
    Column("execution_id", Uuid(as_uuid=False),
           ForeignKey("executions.execution_id", ondelete="CASCADE"), nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("arguments", JSON, nullable=True),
    Column("status", Text, nullable=False, default="running"),  # 'running' | 'ok' | 'error'
    Column("error_message", Text, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True), nullable=True),
)
Index("tool_calls_execution_idx", tool_calls.c.execution_id, tool_calls.c.started_at.asc())

evidence = Table(
    "evidence", metadata,
    Column("evidence_id", Uuid(as_uuid=False), primary_key=True),
    Column("tool_call_id", Uuid(as_uuid=False),
           ForeignKey("tool_calls.tool_call_id", ondelete="CASCADE"), nullable=False),
    # The EXACT tool result, never the 200-char preview team._tool_evidence keeps
    # for LLM context (swarm/team.py's _tool_evidence cap is a runtime/model-
    # context optimisation only -- see execution_context.py's EvidenceRecord
    # docstring). Text is unbounded on both SQLite and Postgres; no truncation.
    Column("content", Text, nullable=True),
    Column("content_hash", Text, nullable=False),
    Column("success", Boolean, nullable=False),
    Column("error_message", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("evidence_tool_call_idx", evidence.c.tool_call_id)
Index("evidence_content_hash_idx", evidence.c.content_hash)

claims = Table(
    "claims", metadata,
    Column("claim_id", Uuid(as_uuid=False), primary_key=True),
    Column("run_id", Uuid(as_uuid=False), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    # SET NULL, not CASCADE: a claim's supporting execution is incidental context,
    # not what makes the claim exist -- deleting one execution (e.g. a superseded
    # retry) must not silently delete a claim that cited its output. The claim
    # still disappears when the whole RUN (and therefore session) is deleted, via
    # claims.run_id's own CASCADE above.
    Column("execution_id", Uuid(as_uuid=False),
           ForeignKey("executions.execution_id", ondelete="SET NULL"), nullable=True),
    Column("statement", Text, nullable=False),
    Column("status", Text, nullable=False, default="unverified"),  # 'unverified' | 'grounded' | 'ungrounded'
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("claims_run_idx", claims.c.run_id)

claim_evidence = Table(
    "claim_evidence", metadata,
    Column("claim_id", Uuid(as_uuid=False),
           ForeignKey("claims.claim_id", ondelete="CASCADE"), primary_key=True),
    Column("evidence_id", Uuid(as_uuid=False),
           ForeignKey("evidence.evidence_id", ondelete="CASCADE"), primary_key=True),
    # The composite primary key above IS the uniqueness constraint -- the same
    # (claim_id, evidence_id) pair cannot be inserted twice.
)


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


async def _current_app_db_revision(conn) -> str | None:
    """The Alembic revision actually applied to whatever database `conn` is
    connected to -- None if the alembic_version table doesn't exist at all (a
    fresh, never-migrated database). A plain introspection query against the
    EXISTING app engine, not a call into Alembic's own (synchronous) runtime --
    this needs to stay cheap since ensure_schema() is called on nearly every
    session/feedback operation (see this module's docstring's "TWO separate
    engines" note for why get_engine() is the right engine here)."""
    has_table = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table("alembic_version"))
    if not has_table:
        return None
    row = (await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))).first()
    return row[0] if row else None


async def ensure_schema() -> None:
    """VERSION CHECK ONLY (Phase B) -- does NOT create or alter any table.

    Before Phase B this function called metadata.create_all() (plus a couple of
    hand-rolled ALTER TABLE statements for columns added after initial deploy).
    That silent-bootstrap behavior is deliberately retired: schema changes now
    come from Alembic migrations (see swarm/migrations.py, alembic/versions/),
    applied explicitly via `hive migrate` -- never implicitly, at startup or on
    the first session/feedback call that happens to touch the database. See the
    accepted architecture review's migration-strategy section for the reasoning
    (existing installations must not have their schema mutated without the
    operator choosing to run a migration).

    Every one of this function's existing callers (api/server.py's startup
    event, and eight lazy per-call sites across swarm/sessions.py and
    swarm/feedback.py) is UNCHANGED and still calls this exact function name —
    only its body changed, from "create what's missing" to "confirm nothing is
    missing, or explain clearly why it is." Raising here at startup causes
    FastAPI to fail to start rather than serve requests against an unmigrated
    schema; raising from one of the lazy per-call sites surfaces the same clear
    error the first time that code path is actually exercised.
    """
    from swarm.migrations import expected_head

    head = expected_head()
    engine = get_engine()
    async with engine.connect() as conn:
        current = await _current_app_db_revision(conn)
    if current != head:
        current_desc = repr(current) if current else "unversioned (no migrations applied)"
        raise RuntimeError(
            f"agnohive database schema is out of date "
            f"(at {current_desc}, this code expects {head!r}). Run `hive migrate` "
            f"(or `alembic upgrade head`) before starting agno-hive against this "
            f"database. See docs/guide/migrations.md."
        )


async def create_all_for_tests() -> None:
    """TEST-ONLY equivalent of the old ensure_schema() bootstrap -- creates every
    table in `metadata` (chat_sessions/session_messages/failure_log/
    task_outcome_queue plus the Phase B durable-backbone tables) directly via
    create_all(), then STAMPS the resulting database at the current code's
    expected Alembic head (a plain INSERT into alembic_version — not a real
    `alembic stamp` invocation, which would re-enter Alembic's own env.py and
    is unnecessary just to satisfy a version check).

    The stamp step is required, not cosmetic: sessions.py/feedback.py's own
    functions call ensure_schema() internally on nearly every operation (see
    that function's own docstring — Phase B made it a version check), so a
    schema created by create_all() alone, with no alembic_version row, would
    still make every one of those calls raise "database schema is out of
    date" — silently swallowed by their own try/except, producing a session
    that "creates" successfully but was never actually inserted. This exact
    failure mode is why this function stamps as well as creates.

    Production code must never call this: an operator's real database is
    expected to already be migrated via `hive migrate` (see ensure_schema()'s
    own docstring) before the app starts. Tests that only need a working
    schema to exercise sessions.py/feedback.py's own logic — not the migration
    system itself, which has its own tests/test_migrations.py — call this
    instead. Mirrors reset_engine_for_tests()'s naming and test-only scope in
    this same module.
    """
    from swarm.migrations import expected_head

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        ))
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": expected_head()},
        )


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
