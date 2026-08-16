"""Tests for swarm/db.py -- the shared SQLAlchemy engine/schema layer (AGNOHive
2.3.2 addendum, 2026-08-08). Engine-agnostic by design: these run against SQLite
(the ships-by-default engine), but the URL-normalization and dialect-portability
choices (Uuid, DateTime(timezone=True), PRAGMA foreign_keys) exist specifically so
the same schema also works against Postgres/MySQL/etc -- see docs/guide/cloud-models.md.

TWO separate engines (split 2026-08-16, see swarm/db.py's module docstring for the
full incident this fixes): get_engine()/ensure_schema() for chat_sessions/
session_messages/failure_log, get_routing_engine()/ensure_routing_schema() for
model_catalog/team_role_models. Tests are grouped accordingly."""
import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    monkeypatch.setattr(config, "postgres_uri", "")
    monkeypatch.setattr(config, "model_routing_database_url", "")
    await db.reset_engine_for_tests()
    yield


# ── URL resolution — app-storage DB (chat_sessions/session_messages/failure_log) ─

def test_resolve_database_url_defaults_to_local_sqlite_file():
    url = db.resolve_database_url()
    assert url.startswith("sqlite+aiosqlite:///")
    assert "agnohive.db" in url


def test_resolve_database_url_prefers_database_url_over_postgres_uri(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "postgresql://user:pass@host/db")
    assert db.resolve_database_url() == "sqlite+aiosqlite:///:memory:"


def test_resolve_database_url_falls_back_to_legacy_postgres_uri(monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    monkeypatch.setattr(config, "postgres_uri", "postgresql://user:pass@host/db")
    assert db.resolve_database_url() == "postgresql+psycopg://user:pass@host/db"


def test_normalize_async_url_upgrades_plain_sqlite_dsn():
    assert db._normalize_async_url("sqlite:///data/x.db") == "sqlite+aiosqlite:///data/x.db"


def test_normalize_async_url_upgrades_plain_postgres_dsn():
    assert db._normalize_async_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_normalize_async_url_upgrades_postgres_scheme_alias():
    assert db._normalize_async_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_normalize_async_url_leaves_already_dialected_url_unchanged():
    assert db._normalize_async_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"
    assert db._normalize_async_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


# ── URL resolution — routing DB (model_catalog/team_role_models) ────────────────

def test_resolve_routing_database_url_defaults_to_its_own_local_sqlite_file():
    url = db.resolve_routing_database_url()
    assert url.startswith("sqlite+aiosqlite:///")
    assert "model_routing.db" in url


def test_resolve_routing_database_url_does_not_fall_back_to_postgres_uri(monkeypatch):
    """The specific bug this whole split fixes: resolve_database_url() falls back
    to postgres_uri, and that fallback is what silently landed model_catalog/
    team_role_models inside ZGX's Apache AGE graph Postgres instance on 2026-08-08
    (postgres_uri was already set for the OTHER three tables). The routing
    resolver must never consult postgres_uri or database_url at all -- only its
    own dedicated env var, model_routing_database_url."""
    monkeypatch.setattr(config, "database_url", "postgresql://user:pass@host/db")
    monkeypatch.setattr(config, "postgres_uri", "postgresql://user:pass@host/db")
    monkeypatch.setattr(config, "model_routing_database_url", "")
    url = db.resolve_routing_database_url()
    assert url.startswith("sqlite+aiosqlite:///")
    assert "model_routing.db" in url


def test_resolve_routing_database_url_honors_explicit_override(monkeypatch):
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    assert db.resolve_routing_database_url() == "sqlite+aiosqlite:///:memory:"


# ── Schema bootstrap — app-storage DB ────────────────────────────────────────────

async def test_ensure_schema_creates_the_three_session_tables(monkeypatch):
    """ensure_schema() only owns chat_sessions/session_messages/failure_log now --
    model_catalog/team_role_models moved to ensure_routing_schema()/a separate
    engine, so they must NOT appear here even though they share model definitions
    module-wide."""
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    await db.ensure_schema()

    async with db.get_engine().begin() as conn:
        names = await conn.run_sync(lambda sync_conn: sa.inspect(sync_conn).get_table_names())
    assert set(names) == {"chat_sessions", "session_messages", "failure_log"}


async def test_ensure_schema_is_idempotent(monkeypatch):
    """create_all with checkfirst must be a safe no-op on a second call -- this is
    what replaces the old CREATE TABLE IF NOT EXISTS ethos."""
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    await db.ensure_schema()
    await db.ensure_schema()  # must not raise


async def test_sqlite_foreign_keys_enforced_for_cascade_delete(monkeypatch):
    """Without the PRAGMA foreign_keys=ON connect listener, SQLite silently
    ignores ON DELETE CASCADE -- this is the specific portability gap swarm/db.py
    exists to close (Postgres enforces FKs unconditionally)."""
    import uuid

    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await db.ensure_schema()

    sid = str(uuid.uuid4())
    async with db.get_engine().begin() as conn:
        await conn.execute(db.chat_sessions.insert().values(id=sid, project_id="p", title="t", persist=False))
        await conn.execute(db.session_messages.insert().values(session_id=sid, role="user", content="hi"))
        await conn.execute(sa.delete(db.chat_sessions).where(db.chat_sessions.c.id == sid))
        remaining = (await conn.execute(sa.select(db.session_messages))).all()

    assert remaining == []


# ── Schema bootstrap — routing DB (model_catalog / team_role_models) ────────────

async def test_ensure_routing_schema_creates_the_two_routing_tables(monkeypatch):
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    await db.ensure_routing_schema()

    async with db.get_routing_engine().begin() as conn:
        names = await conn.run_sync(lambda sync_conn: sa.inspect(sync_conn).get_table_names())
    assert set(names) == {"model_catalog", "team_role_models"}


async def test_ensure_routing_schema_is_idempotent(monkeypatch):
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    await db.ensure_routing_schema()
    await db.ensure_routing_schema()  # must not raise


async def test_team_role_models_model_id_fk_rejects_unknown_model(monkeypatch):
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await db.ensure_routing_schema()

    with pytest.raises(Exception):  # IntegrityError -- exact type varies by DBAPI
        async with db.get_routing_engine().begin() as conn:
            await conn.execute(
                db.team_role_models.insert().values(
                    team_name="engineering", role_name="Coder", model_id="never-seeded-model",
                )
            )


async def test_ensure_routing_schema_adds_missing_columns_to_an_already_deployed_table(monkeypatch):
    """The operationally critical case (Recommendation #4, 2026-08-13, see DOCS.md
    "Declarative Per-Role Policy"): create_all() only creates tables that don't
    already exist -- it never ALTERs one that does, so a database that already
    had team_role_models before temperature/max_tokens/tool_call_limit were added
    to its Python definition (ZGX's real, populated deployment) would silently
    keep missing those 3 columns forever under plain create_all(), breaking the
    first INSERT/SELECT that touches them. Simulates that exact pre-upgrade state
    by creating the table with the OLD 3-column shape via raw SQL first, then
    confirms ensure_routing_schema() adds the missing columns without erroring or
    touching the model_catalog FK/existing data."""
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    async with db.get_routing_engine().begin() as conn:
        await conn.execute(sa.text(
            "CREATE TABLE model_catalog ("
            "  model_id TEXT PRIMARY KEY, kind TEXT NOT NULL, provider TEXT NOT NULL,"
            "  vllm_served_as TEXT, requires_cloud_gate BOOLEAN NOT NULL, active BOOLEAN NOT NULL"
            ")"
        ))
        await conn.execute(sa.text(
            "CREATE TABLE team_role_models ("
            "  team_name TEXT NOT NULL, role_name TEXT NOT NULL, model_id TEXT NOT NULL,"
            "  PRIMARY KEY (team_name, role_name),"
            "  FOREIGN KEY (model_id) REFERENCES model_catalog(model_id)"
            ")"
        ))
        await conn.execute(
            sa.text("INSERT INTO model_catalog VALUES ('pre-existing-model', 'local', 'local', NULL, 0, 1)")
        )
        await conn.execute(
            sa.text("INSERT INTO team_role_models VALUES ('engineering', 'Coder', 'pre-existing-model')")
        )

    await db.ensure_routing_schema()  # must not raise, must not touch the row already there

    async with db.get_routing_engine().begin() as conn:
        columns = await conn.run_sync(db._existing_team_role_models_columns)
        rows = (await conn.execute(sa.select(db.team_role_models))).mappings().all()

    assert {"team_name", "role_name", "model_id", "temperature", "max_tokens", "tool_call_limit"} <= columns
    assert len(rows) == 1
    assert rows[0]["model_id"] == "pre-existing-model"
    assert rows[0]["temperature"] is None  # newly-added column, NULL for a pre-existing row
    assert rows[0]["max_tokens"] is None
    assert rows[0]["tool_call_limit"] is None


async def test_ensure_routing_schema_column_migration_is_idempotent(monkeypatch):
    """Running ensure_routing_schema() a second time after the columns already
    exist (a fresh SQLite deployment where create_all() created the full shape
    from the start, OR a database this migration has already run against once)
    must not error -- the introspect-first check exists specifically to make the
    ALTER TABLE path skip cleanly instead of hitting "duplicate column"."""
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    await db.ensure_routing_schema()
    await db.ensure_routing_schema()  # must not raise


async def test_model_catalog_model_id_is_primary_key(monkeypatch):
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await db.ensure_routing_schema()

    async with db.get_routing_engine().begin() as conn:
        await conn.execute(
            db.model_catalog.insert().values(
                model_id="dupe-test", kind="local", provider="local",
                vllm_served_as=None, requires_cloud_gate=False, active=True,
            )
        )

    with pytest.raises(Exception):  # IntegrityError -- duplicate primary key
        async with db.get_routing_engine().begin() as conn:
            await conn.execute(
                db.model_catalog.insert().values(
                    model_id="dupe-test", kind="cloud", provider="openai",
                    vllm_served_as=None, requires_cloud_gate=True, active=True,
                )
            )


# ── Engine independence — the actual point of the split ─────────────────────────

async def test_app_and_routing_engines_are_independent_databases(monkeypatch):
    """The whole point of the split: writing to the routing DB must not create
    ANY table in the app-storage DB, and vice versa -- they are genuinely two
    separate SQLite files (or could be two separate servers entirely), not one
    database with two logical halves."""
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    await db.ensure_schema()
    await db.ensure_routing_schema()

    async with db.get_engine().begin() as conn:
        app_names = await conn.run_sync(lambda sync_conn: sa.inspect(sync_conn).get_table_names())
    async with db.get_routing_engine().begin() as conn:
        routing_names = await conn.run_sync(lambda sync_conn: sa.inspect(sync_conn).get_table_names())

    assert set(app_names) == {"chat_sessions", "session_messages", "failure_log"}
    assert set(routing_names) == {"model_catalog", "team_role_models"}
    assert db.get_engine() is not db.get_routing_engine()
