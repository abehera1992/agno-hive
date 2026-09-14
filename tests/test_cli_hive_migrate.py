"""Phase B -- hive --migrate (cli/hive's _cmd_migrate). Covers the CLI's own
migration-state classification and confirmation gate, on top of
tests/test_migrations.py's coverage of the underlying Alembic revisions
themselves (fresh upgrade, existing-install stamp+upgrade, cascade behavior,
ensure_schema()'s version check) -- not duplicated here.

Deliberately plain (non-async) `def test_...()` functions: _cmd_migrate()
calls swarm.migrations.run_upgrade()/stamp(), which invoke Alembic's
synchronous command API, which in turn calls asyncio.run() inside
alembic/env.py to bridge to swarm/db.py's async engine (see that file's own
docstring). Calling any of this from inside an already-running event loop
(an `async def` pytest-asyncio test) raises "asyncio.run() cannot be called
from a running event loop" -- the same constraint tests/test_migrations.py
documents and works around the same way.

Uses the `load_cli_hive` fixture (tests/conftest.py) -- the established
pattern this suite already uses for cli/hive, which has no .py extension and
is not a package.
"""
import asyncio

import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db
from swarm.migrations import expected_head


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    asyncio.run(db.reset_engine_for_tests())
    yield


def _table_names() -> set[str]:
    async def _inspect():
        async with db.get_engine().begin() as conn:
            return await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
    return set(asyncio.run(_inspect()))


def _create_baseline_tables_only() -> None:
    async def _create():
        async with db.get_engine().begin() as conn:
            await conn.run_sync(lambda c: db.metadata.create_all(
                c, tables=[db.chat_sessions, db.session_messages, db.failure_log, db.task_outcome_queue]))
    asyncio.run(_create())


def _alembic_version() -> str | None:
    async def _read():
        async with db.get_engine().begin() as conn:
            has = await conn.run_sync(lambda c: sa.inspect(c).has_table("alembic_version"))
            if not has:
                return None
            row = (await conn.execute(sa.text("SELECT version_num FROM alembic_version LIMIT 1"))).first()
            return row[0] if row else None
    return asyncio.run(_read())


# ── 1. --migrate flag is registered ─────────────────────────────────────────

def test_migrate_flag_is_wired_to_cmd_migrate(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    calls = []
    monkeypatch.setattr(hive, "_cmd_migrate", lambda: calls.append(True))
    monkeypatch.setattr("sys.argv", ["hive", "--migrate"])

    hive.main()

    assert calls == [True]


# ── 2. Fresh DB migration (CASE 1) ──────────────────────────────────────────

def test_fresh_database_migrates_with_no_prompt(load_cli_hive, monkeypatch):
    hive = load_cli_hive()

    def _no_prompt(*a, **k):
        raise AssertionError("must not prompt for a fresh database")
    monkeypatch.setattr("builtins.input", _no_prompt)

    hive._cmd_migrate()

    names = _table_names()
    assert {"chat_sessions", "runs", "executions", "tool_calls", "evidence",
            "claims", "claim_evidence"} <= names
    assert _alembic_version() == expected_head()


# ── 3/4. Existing unversioned DB detection + confirmation required (CASE 2) ─

def test_existing_unversioned_database_requires_confirmation(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    _create_baseline_tables_only()
    prompts = []

    def _capture_prompt(text):
        prompts.append(text)
        return "y"
    monkeypatch.setattr("builtins.input", _capture_prompt)

    hive._cmd_migrate()

    assert len(prompts) == 1
    assert "Proceed?" in prompts[0]


# ── 5. Decline causes zero mutation (CASE 3) ────────────────────────────────

def test_declining_confirmation_leaves_database_untouched(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    _create_baseline_tables_only()
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    with pytest.raises(SystemExit) as exc_info:
        hive._cmd_migrate()

    assert exc_info.value.code != 0
    # No alembic_version table at all -- stamp() was never called.
    assert _alembic_version() is None
    # No durable-backbone tables -- upgrade() was never called either.
    names = _table_names()
    assert not ({"runs", "executions", "tool_calls", "evidence", "claims", "claim_evidence"} & names)
    # The pre-existing baseline tables are exactly as they were -- nothing dropped or altered.
    assert {"chat_sessions", "session_messages", "failure_log", "task_outcome_queue"} <= names


def test_declining_at_a_lowercase_or_blank_answer_also_aborts(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    _create_baseline_tables_only()
    monkeypatch.setattr("builtins.input", lambda *a: "")

    with pytest.raises(SystemExit):
        hive._cmd_migrate()

    assert _alembic_version() is None


# ── 6. Confirmation stamps 0001 then upgrades to head ───────────────────────

def test_confirming_stamps_0001_then_upgrades_to_head(load_cli_hive, monkeypatch):
    hive = load_cli_hive()
    _create_baseline_tables_only()
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    hive._cmd_migrate()

    assert _alembic_version() == expected_head()
    names = _table_names()
    assert {"runs", "executions", "tool_calls", "evidence", "claims", "claim_evidence"} <= names
    # The known dedupe-index repair ran as part of this same upgrade.
    async def _indexes():
        async with db.get_engine().begin() as conn:
            return await conn.run_sync(lambda c: sa.inspect(c).get_indexes("task_outcome_queue"))
    idx_names = {i["name"] for i in asyncio.run(_indexes())}
    assert "task_outcome_queue_dedupe_idx" in idx_names


def test_confirming_preserves_pre_existing_data(load_cli_hive, monkeypatch):
    import uuid
    hive = load_cli_hive()
    _create_baseline_tables_only()
    sid = str(uuid.uuid4())

    async def _seed():
        async with db.get_engine().begin() as conn:
            await conn.execute(db.chat_sessions.insert().values(
                id=sid, project_id="p", title="pre-existing session", persist=False))
    asyncio.run(_seed())

    monkeypatch.setattr("builtins.input", lambda *a: "y")
    hive._cmd_migrate()

    async def _check():
        async with db.get_engine().begin() as conn:
            return (await conn.execute(
                sa.select(db.chat_sessions).where(db.chat_sessions.c.id == sid))).first()
    row = asyncio.run(_check())
    assert row is not None and row.title == "pre-existing session"


# ── 7. Already-versioned DB at head is safe/idempotent (CASE 4) ─────────────

def test_already_at_head_is_a_no_op(load_cli_hive, monkeypatch):
    hive = load_cli_hive()

    def _no_prompt(*a, **k):
        raise AssertionError("must not prompt when already at head")
    monkeypatch.setattr("builtins.input", _no_prompt)

    hive._cmd_migrate()          # first run: fresh -> head
    hive._cmd_migrate()          # second run: already at head -> no-op, no prompt, no error

    assert _alembic_version() == expected_head()


# ── 8. DB at 0001 upgrades to 0002, no prompt (CASE 5) ──────────────────────

def test_database_at_0001_upgrades_to_0002_without_prompting(load_cli_hive, monkeypatch):
    from swarm.migrations import run_upgrade
    hive = load_cli_hive()
    run_upgrade("0001_baseline")  # simulate an already-Alembic-managed DB, stopped short of head
    assert _alembic_version() == "0001_baseline"

    def _no_prompt(*a, **k):
        raise AssertionError("an already-versioned database must not be re-confirmed")
    monkeypatch.setattr("builtins.input", _no_prompt)

    hive._cmd_migrate()

    assert _alembic_version() == expected_head()
    assert {"runs", "executions", "tool_calls", "evidence", "claims", "claim_evidence"} <= _table_names()


# ── 9. Uses the existing database-resolution path, not a second one ────────

def test_cmd_migrate_source_uses_no_second_connection_mechanism(load_cli_hive):
    import inspect as _inspect
    hive = load_cli_hive()
    source = _inspect.getsource(hive._cmd_migrate)
    assert "resolve_database_url" in source
    assert "create_engine" not in source
    assert "create_async_engine" not in source
    assert "psycopg2.connect" not in source
    assert "sqlite3.connect" not in source


# ── 10. Expected Alembic head is 0005_projects ──────────────────────────────

def test_expected_head_is_0005_projects():
    assert expected_head() == "0005_projects"
