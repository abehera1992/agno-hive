"""Phase B -- durable execution/evidence schema + Alembic migration foundation.

Schema-only: nothing in swarm/team.py or swarm/execution_context.py writes to
these tables yet (Phase A remains runtime-memory only, see that module's own
docstring). These tests cover the migration system itself and the six new
tables' structure/cascade behavior -- not any runtime persistence, since none
exists in this phase.

Deliberately plain (non-async) `def test_...()` functions wherever they call
run_upgrade()/stamp(): Alembic's command API is synchronous and its own
alembic/env.py internally calls asyncio.run() to bridge to swarm/db.py's async
engine (see that file's own docstring for why it reuses db.get_engine()
specifically) -- calling it from inside an already-running event loop (i.e.
from an `async def` pytest-asyncio test) raises "asyncio.run() cannot be
called from a running event loop". Verification that needs the async engine
runs via its own separate `asyncio.run(...)` call within the same sync test
function -- empirically confirmed safe for this project's actual engine
construction (StaticPool-backed aiosqlite survives across separate
asyncio.run() calls; this is what makes an in-memory SQLite migration
observable by the same test that ran it at all).
"""
import asyncio
import uuid

import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db
from swarm.migrations import expected_head, run_upgrade, stamp

_BASELINE_TABLES = {"chat_sessions", "session_messages", "failure_log", "task_outcome_queue"}
_DURABLE_TABLES = {"runs", "executions", "tool_calls", "evidence", "claims", "claim_evidence"}


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


def _index_names(table: str) -> list[dict]:
    async def _inspect():
        async with db.get_engine().begin() as conn:
            return await conn.run_sync(lambda c: sa.inspect(c).get_indexes(table))
    return asyncio.run(_inspect())


# ── 1/2/4/5/15/16. Fresh migration path (SQLite here; Postgres DDL compiled below) ──

def test_expected_head_is_the_durable_backbone_revision():
    assert expected_head() == "0002_durable_backbone"


def test_fresh_sqlite_upgrade_creates_all_ten_tables():
    run_upgrade("head")
    assert _BASELINE_TABLES | _DURABLE_TABLES <= _table_names()


def test_0001_alone_creates_only_the_four_baseline_tables():
    run_upgrade("0001_baseline")
    names = _table_names()
    assert _BASELINE_TABLES <= names
    assert not (_DURABLE_TABLES & names)


def test_0002_creates_the_six_durable_tables_on_top_of_0001():
    run_upgrade("0001_baseline")
    run_upgrade("0002_durable_backbone")
    assert _DURABLE_TABLES <= _table_names()


def test_known_dedupe_index_present_after_fresh_migration():
    run_upgrade("head")
    names = {i["name"] for i in _index_names("task_outcome_queue")}
    assert "task_outcome_queue_dedupe_idx" in names


def test_sqlite_ddl_runs_without_error():
    """SQLite DDL compatibility -- the migration itself is the test; a
    dialect-incompatible type/constraint would raise during run_upgrade()."""
    run_upgrade("head")  # must not raise


def test_postgres_ddl_compiles(monkeypatch):
    """PostgreSQL DDL compilation -- compiled offline (no live Postgres server
    needed/available in this environment), matching every existing table's own
    dialect-portability convention (Uuid/DateTime(timezone=True)/JSON all
    compile cleanly on both dialects already used by chat_sessions etc.)."""
    from sqlalchemy.schema import CreateTable
    for table in (db.runs, db.executions, db.tool_calls, db.evidence, db.claims, db.claim_evidence):
        ddl = str(CreateTable(table).compile(dialect=sa.dialects.postgresql.dialect()))
        assert table.name in ddl


# ── 3. Existing-install simulation ──────────────────────────────────────────

def _create_baseline_tables_only() -> None:
    async def _create():
        async with db.get_engine().begin() as conn:
            await conn.run_sync(lambda c: db.metadata.create_all(
                c, tables=[db.chat_sessions, db.session_messages, db.failure_log, db.task_outcome_queue]))
    asyncio.run(_create())


def test_existing_install_stamp_then_upgrade_applies_only_0002():
    _create_baseline_tables_only()

    async def _seed():
        sid = str(uuid.uuid4())
        async with db.get_engine().begin() as conn:
            await conn.execute(db.chat_sessions.insert().values(
                id=sid, project_id="p", title="pre-existing session", persist=False))
        return sid
    sid = asyncio.run(_seed())

    stamp("0001_baseline")
    run_upgrade("head")

    assert _BASELINE_TABLES | _DURABLE_TABLES <= _table_names()

    async def _check_row():
        async with db.get_engine().begin() as conn:
            row = (await conn.execute(
                sa.select(db.chat_sessions).where(db.chat_sessions.c.id == sid))).first()
            return row
    row = asyncio.run(_check_row())
    assert row is not None and row.title == "pre-existing session"


def test_existing_install_missing_dedupe_index_is_repaired():
    """Reproduces the exact live-confirmed ZGX drift: task_outcome_queue exists
    (created before the dedupe index was ever added) but its unique index does
    not -- 0002's idempotent CREATE UNIQUE INDEX IF NOT EXISTS must add it."""
    async def _create_drifted():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.text(
                "CREATE TABLE task_outcome_queue ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, task TEXT NOT NULL, "
                "result TEXT NOT NULL, owner TEXT, status TEXT NOT NULL DEFAULT 'pending', "
                "attempts INTEGER NOT NULL DEFAULT 0, error_message TEXT, task_hash TEXT, doc_path TEXT, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
            ))
            await conn.run_sync(lambda c: db.metadata.create_all(
                c, tables=[db.chat_sessions, db.session_messages, db.failure_log]))
    asyncio.run(_create_drifted())

    stamp("0001_baseline")
    run_upgrade("head")

    names = {i["name"] for i in _index_names("task_outcome_queue")}
    assert "task_outcome_queue_dedupe_idx" in names


# ── 7/8/9/10/11/12/13/14. FK constraints and cascade behavior ───────────────

def _seed_full_tree() -> dict:
    run_upgrade("head")
    ids = {k: str(uuid.uuid4()) for k in
           ("session", "run", "root_exec", "child_exec", "tool_call", "evidence", "claim")}

    async def _insert():
        async with db.get_engine().begin() as conn:
            await conn.execute(db.chat_sessions.insert().values(
                id=ids["session"], project_id="p", title="t", persist=False))
            await conn.execute(db.runs.insert().values(
                run_id=ids["run"], session_id=ids["session"], status="ok"))
            await conn.execute(db.executions.insert().values(
                execution_id=ids["root_exec"], run_id=ids["run"], parent_execution_id=None,
                agent_name="Coordinator", execution_type="coordinator", attempt_number=1, status="ok"))
            await conn.execute(db.executions.insert().values(
                execution_id=ids["child_exec"], run_id=ids["run"], parent_execution_id=ids["root_exec"],
                agent_name="Researcher", execution_type="delegation", attempt_number=1, status="ok"))
            await conn.execute(db.tool_calls.insert().values(
                tool_call_id=ids["tool_call"], execution_id=ids["child_exec"],
                tool_name="get_file_content", status="ok"))
            await conn.execute(db.evidence.insert().values(
                evidence_id=ids["evidence"], tool_call_id=ids["tool_call"],
                content="exact tool output", content_hash="deadbeef", success=True))
            await conn.execute(db.claims.insert().values(
                claim_id=ids["claim"], run_id=ids["run"], execution_id=ids["child_exec"],
                statement="the file exists", status="grounded"))
            await conn.execute(db.claim_evidence.insert().values(
                claim_id=ids["claim"], evidence_id=ids["evidence"]))
    asyncio.run(_insert())
    return ids


def _row_counts() -> dict:
    async def _count():
        async with db.get_engine().begin() as conn:
            out = {}
            for tbl in (db.runs, db.executions, db.tool_calls, db.evidence, db.claims, db.claim_evidence):
                out[tbl.name] = len((await conn.execute(sa.select(tbl))).all())
            return out
    return asyncio.run(_count())


def test_foreign_keys_reject_a_run_pointing_at_no_session():
    run_upgrade("head")

    async def _insert_orphan():
        async with db.get_engine().begin() as conn:
            await conn.execute(db.runs.insert().values(
                run_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()), status="running"))

    with pytest.raises(Exception):  # IntegrityError -- exact type varies by DBAPI
        asyncio.run(_insert_orphan())


def test_session_delete_cascades_through_every_durable_table():
    ids = _seed_full_tree()

    async def _delete_session():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.delete(db.chat_sessions).where(db.chat_sessions.c.id == ids["session"]))
    asyncio.run(_delete_session())

    counts = _row_counts()
    assert counts == {name: 0 for name in counts}


def test_runs_to_executions_cascade_specifically():
    ids = _seed_full_tree()

    async def _delete_run():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.delete(db.runs).where(db.runs.c.run_id == ids["run"]))
    asyncio.run(_delete_run())

    counts = _row_counts()
    assert counts["executions"] == 0
    assert counts["tool_calls"] == 0
    assert counts["evidence"] == 0


def test_executions_to_tool_calls_cascade_specifically():
    ids = _seed_full_tree()

    async def _delete_child_execution():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.delete(db.executions).where(db.executions.c.execution_id == ids["child_exec"]))
    asyncio.run(_delete_child_execution())

    counts = _row_counts()
    assert counts["tool_calls"] == 0
    assert counts["evidence"] == 0
    # The root execution (a different row) must survive a delete scoped to its child.
    assert counts["executions"] == 1


def test_tool_calls_to_evidence_cascade_specifically():
    ids = _seed_full_tree()

    async def _delete_tool_call():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.delete(db.tool_calls).where(db.tool_calls.c.tool_call_id == ids["tool_call"]))
    asyncio.run(_delete_tool_call())

    assert _row_counts()["evidence"] == 0


def test_runs_to_claims_cascade_specifically():
    ids = _seed_full_tree()

    async def _delete_run():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.delete(db.runs).where(db.runs.c.run_id == ids["run"]))
    asyncio.run(_delete_run())

    counts = _row_counts()
    assert counts["claims"] == 0
    assert counts["claim_evidence"] == 0


def test_claims_to_claim_evidence_cascade_specifically():
    ids = _seed_full_tree()

    async def _delete_claim():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.delete(db.claims).where(db.claims.c.claim_id == ids["claim"]))
    asyncio.run(_delete_claim())

    assert _row_counts()["claim_evidence"] == 0
    # Evidence itself is independent of the claim that cited it.
    assert _row_counts()["evidence"] == 1


def test_deleting_an_executions_parent_execution_id_target_sets_null_via_claim_fk():
    """claims.execution_id is ON DELETE SET NULL -- deleting the execution a
    claim cites must not delete the claim itself, only null out the reference."""
    ids = _seed_full_tree()

    async def _delete_child_execution():
        async with db.get_engine().begin() as conn:
            await conn.execute(sa.delete(db.executions).where(db.executions.c.execution_id == ids["child_exec"]))
    asyncio.run(_delete_child_execution())

    async def _check_claim():
        async with db.get_engine().begin() as conn:
            row = (await conn.execute(sa.select(db.claims).where(db.claims.c.claim_id == ids["claim"]))).first()
            return row
    row = asyncio.run(_check_claim())
    assert row is not None            # the claim itself survives
    assert row.execution_id is None   # but its execution reference is nulled


def test_claim_evidence_uniqueness_rejects_a_duplicate_pair():
    ids = _seed_full_tree()

    async def _duplicate():
        async with db.get_engine().begin() as conn:
            await conn.execute(db.claim_evidence.insert().values(
                claim_id=ids["claim"], evidence_id=ids["evidence"]))

    with pytest.raises(Exception):  # IntegrityError -- duplicate primary key
        asyncio.run(_duplicate())


# ── 17/18. ensure_schema() version-check behavior ───────────────────────────

def test_ensure_schema_succeeds_at_current_head():
    run_upgrade("head")

    async def _check():
        await db.ensure_schema()  # must not raise
    asyncio.run(_check())


def test_ensure_schema_rejects_an_unmigrated_database_with_an_actionable_message():
    async def _check():
        with pytest.raises(RuntimeError, match="hive migrate"):
            await db.ensure_schema()
    asyncio.run(_check())


def test_ensure_schema_rejects_a_database_only_at_the_baseline_revision():
    run_upgrade("0001_baseline")  # deliberately stop short of head

    async def _check():
        with pytest.raises(RuntimeError, match="hive migrate"):
            await db.ensure_schema()
    asyncio.run(_check())


# ── 19. No runtime persistence introduced by Phase B ────────────────────────

def test_execution_context_module_has_no_swarm_db_dependency():
    """Phase A's runtime objects remain pure in-memory -- Phase B adds a
    durable destination for them but does not wire anything into it yet."""
    import swarm.execution_context as ec
    import inspect as _inspect
    source = _inspect.getsource(ec)
    assert "swarm.db" not in source
    assert "swarm import db" not in source
    assert "INSERT" not in source.upper()


def test_tool_interception_hook_source_has_no_new_db_calls():
    """Spot-check: the Phase A hook (still the only place tool calls are
    observed) must not have gained any swarm.db/session/engine references in
    Phase B -- persistence wiring is a later, separate phase."""
    import swarm.team as team_mod
    import inspect as _inspect
    source = _inspect.getsource(team_mod._make_tool_interception_hook)
    assert "swarm.db" not in source
    assert "get_engine" not in source


# ── 20. Existing routing metadata/engine remains untouched ──────────────────

def test_routing_metadata_unaffected_by_the_new_durable_tables():
    routing_table_names = {t.name for t in db.routing_metadata.tables.values()}
    assert routing_table_names == {
        "model_catalog", "team_role_models", "team_role_tools", "team_role_skills",
        "team_role_instruction_overlays", "team_gate_flags", "tool_registry", "skill_registry",
    }
    assert not (routing_table_names & _DURABLE_TABLES)


def test_ensure_routing_schema_is_untouched_by_phase_b(monkeypatch):
    """ensure_routing_schema() must still create_all() exactly as before --
    Phase B only changes ensure_schema() (the app-storage engine), never the
    routing engine."""
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    asyncio.run(db.reset_engine_for_tests())

    asyncio.run(db.ensure_routing_schema())  # must not raise, must still create tables

    async def _names():
        async with db.get_routing_engine().begin() as conn:
            return await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
    names = asyncio.run(_names())
    assert "model_catalog" in names
    assert "team_role_models" in names
