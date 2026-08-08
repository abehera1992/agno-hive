"""Tests for swarm/db.py -- the shared SQLAlchemy engine/schema layer (AGNOHive
2.3.2 addendum, 2026-08-08). Engine-agnostic by design: these run against SQLite
(the ships-by-default engine), but the URL-normalization and dialect-portability
choices (Uuid, DateTime(timezone=True), PRAGMA foreign_keys) exist specifically so
the same schema also works against Postgres/MySQL/etc -- see docs/guide/cloud-models.md."""
import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    monkeypatch.setattr(config, "postgres_uri", "")
    await db.reset_engine_for_tests()
    yield


# ── URL resolution ────────────────────────────────────────────────────────────

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


# ── Schema bootstrap ──────────────────────────────────────────────────────────

async def test_ensure_schema_creates_all_five_tables(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()

    await db.ensure_schema()

    async with db.get_engine().begin() as conn:
        names = await conn.run_sync(lambda sync_conn: sa.inspect(sync_conn).get_table_names())
    assert set(names) == {
        "chat_sessions", "session_messages", "failure_log", "model_catalog", "team_role_models",
    }


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


# ── model_catalog / team_role_models shape ───────────────────────────────────

async def test_team_role_models_model_id_fk_rejects_unknown_model(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await db.ensure_schema()

    with pytest.raises(Exception):  # IntegrityError -- exact type varies by DBAPI
        async with db.get_engine().begin() as conn:
            await conn.execute(
                db.team_role_models.insert().values(
                    team_name="engineering", role_name="Coder", model_id="never-seeded-model",
                )
            )


async def test_model_catalog_model_id_is_primary_key(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await db.ensure_schema()

    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.model_catalog.insert().values(
                model_id="dupe-test", kind="local", provider="local",
                vllm_served_as=None, requires_cloud_gate=False, active=True,
            )
        )

    with pytest.raises(Exception):  # IntegrityError -- duplicate primary key
        async with db.get_engine().begin() as conn:
            await conn.execute(
                db.model_catalog.insert().values(
                    model_id="dupe-test", kind="cloud", provider="openai",
                    vllm_served_as=None, requires_cloud_gate=True, active=True,
                )
            )
