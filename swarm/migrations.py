"""Alembic invocation helpers for agnohive's OWN app-storage database
(chat_sessions/session_messages/failure_log/task_outcome_queue plus the Phase B
durable execution/evidence backbone -- swarm/db.py's `metadata`, reached via
swarm/db.py's `get_engine()`/`resolve_database_url()`).

Never touches:
  - the routing database (model_catalog/team_role_models etc., swarm/db.py's
    `routing_metadata`/`get_routing_engine()`/`ensure_routing_schema()`) --
    that store is explicitly out of scope for this migration project;
  - any connected project's own database (reached read-only via hive-mcp's
    db_schema/db_query tools, never through this module or swarm/db.py at all);
  - AGE or LightRAG's own tables, by construction: alembic/env.py's
    target_metadata is swarm.db.metadata, which never declares those tables.

Two entry points:
  - `expected_head()` -- a pure filesystem read (no DB connection) of
    alembic/versions/, used by swarm/db.py's ensure_schema() for its version
    check on (nearly) every request.
  - `run_upgrade()` / `stamp()` -- the actual migration operations, invoked
    ONLY by `hive migrate` (cli/hive). Never called from ensure_schema() or any
    other runtime/request-path code -- see that function's own docstring for
    why silent startup migration was deliberately retired.
"""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from swarm.db import resolve_database_url

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _REPO_ROOT / "alembic"
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _alembic_config() -> Config:
    """Built fresh on every call (not cached) so a test's monkeypatched
    config.database_url is always picked up -- resolve_database_url() itself
    is documented the same way in swarm/db.py, for the identical reason."""
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", resolve_database_url())
    return cfg


def expected_head() -> str | None:
    """The single migration revision the CURRENTLY INSTALLED CODE expects --
    i.e. the tip of alembic/versions/ as shipped with this checkout. Pure
    filesystem read, no database connection, safe to call as often as
    ensure_schema() needs to (every request-adjacent call site)."""
    script = ScriptDirectory.from_config(_alembic_config())
    return script.get_current_head()


def run_upgrade(revision: str = "head") -> None:
    """Applies pending migrations up to `revision` (default: the latest).
    Synchronous -- Alembic's own command API is sync throughout; see
    alembic/env.py for how it bridges to swarm/db.py's async engine.
    Used by `hive migrate` only."""
    from alembic import command
    command.upgrade(_alembic_config(), revision)


def stamp(revision: str) -> None:
    """Marks `revision` as applied WITHOUT running its DDL. For an existing
    installation whose schema already matches that revision -- callers are
    responsible for validating that first (see docs/guide/migrations.md); this
    function does not and cannot verify it, matching Alembic's own documented
    `stamp` semantics exactly."""
    from alembic import command
    command.stamp(_alembic_config(), revision)


def current_revision_display() -> str | None:
    """Human-facing current-revision lookup for `hive migrate`'s own output --
    prints via Alembic's own `current` command (which reports None cleanly on
    an unmigrated database rather than raising, unlike swarm/db.py's
    ensure_schema() which deliberately DOES raise for the same condition)."""
    from alembic import command
    command.current(_alembic_config())
    return None


async def inspect_migration_state() -> tuple[bool, bool, str | None]:
    """(has_alembic_version, has_baseline_tables, current_revision) for
    whatever database resolve_database_url() currently resolves to -- the
    three facts `hive migrate` (cli/hive's _cmd_migrate) needs to classify a
    database as fresh, existing-unversioned, or already-versioned before
    deciding whether an operator confirmation is required.

    Uses swarm.db.get_engine() -- the SAME engine ensure_schema() and the
    application itself use -- and swarm.db's own current-revision query
    (_current_app_db_revision), rather than a second database connection
    mechanism or a duplicate query. `has_baseline_tables` (chat_sessions'
    presence) is the one fact ensure_schema() itself doesn't need and this
    module didn't have before -- narrowly added here since it is what
    distinguishes a genuinely fresh database from an existing, pre-Alembic
    one, both of which look identical to ensure_schema() (both "unversioned").
    """
    import sqlalchemy as sa

    from swarm.db import _current_app_db_revision, get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        has_alembic = await conn.run_sync(lambda c: sa.inspect(c).has_table("alembic_version"))
        has_baseline = await conn.run_sync(lambda c: sa.inspect(c).has_table("chat_sessions"))
        current = await _current_app_db_revision(conn)
    return has_alembic, has_baseline, current
