"""Phase I -- operational durability & recovery: readiness/health probes
(swarm/db.check_storage_readiness, wired into api/server.py's GET
/health/db), read-only integrity diagnostics
(swarm/execution_store.check_storage_integrity), the Phase F/promote_claim
concurrent-promotion race fix, and confirmation that the PRE-EXISTING
session cleanup loop (swarm/sessions._cleanup_expired) already correctly
cascades through the entire Phase A-H durable tree while leaving
project-owned project_memory_promotions untouched.

Same layering as the prior phases' own suites: functions exercised
directly against a real (in-memory SQLite) database, plus source-level
guards proving the wiring is exactly what this phase's own scope requires
(no automatic repair, no new schema beyond nothing at all -- Phase I adds
zero tables/migrations by design).
"""
import asyncio
import types
import uuid

import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db
from swarm.execution_context import RunContext
from swarm.migrations import run_upgrade
import swarm.execution_store as execution_store


@pytest.fixture(autouse=True)
def _fresh_migrated_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    asyncio.run(db.reset_engine_for_tests())
    run_upgrade("head")   # plain sync call -- see test_migrations.py's own docstring
    yield


async def _create_session() -> str:
    sid = str(uuid.uuid4())
    async with db.get_engine().begin() as conn:
        await conn.execute(db.chat_sessions.insert().values(
            id=sid, project_id="p", title="t", persist=False))
    return sid


async def _team_with_persisted_run(session_id: str, run_id: str = "run-1") -> types.SimpleNamespace:
    team = types.SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    return team


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


async def _claim_row(claim_id: str):
    async with db.get_engine().begin() as conn:
        return (await conn.execute(
            sa.select(db.claims).where(db.claims.c.claim_id == claim_id)
        )).mappings().first()


# ── 1-3. Health / readiness --------------------------------------------

@pytest.mark.asyncio
async def test_readiness_reports_healthy_on_a_freshly_migrated_db():
    result = await db.check_storage_readiness()

    assert result["db_reachable"] is True
    assert result["schema_current"] is True
    assert result["error"] is None
    assert result["current_revision"] == result["expected_revision"]


@pytest.mark.asyncio
async def test_readiness_reports_schema_behind():
    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0002_durable_backbone'"))

    result = await db.check_storage_readiness()

    assert result["db_reachable"] is True
    assert result["schema_current"] is False
    assert result["current_revision"] == "0002_durable_backbone"
    assert "hive migrate" in result["error"]


@pytest.mark.asyncio
async def test_readiness_is_fail_open_when_db_is_unreachable(monkeypatch):
    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "get_engine", _boom)
    result = await db.check_storage_readiness()  # must not raise

    assert result["db_reachable"] is False
    assert result["schema_current"] is False
    assert "connection refused" in result["error"]


def test_readiness_never_exposes_connection_details():
    import inspect
    src = inspect.getsource(db.check_storage_readiness)
    assert "database_url" not in src
    assert "resolve_database_url" not in src
    assert "config.postgres_uri" not in src


# ── 4. /health vs /health/db distinction ---------------------------------

def test_health_endpoint_is_unchanged_liveness_only():
    """Phase I adds a NEW endpoint for readiness rather than changing the
    existing /health, which must keep meaning exactly what it always has:
    "the process is alive", nothing about durable storage."""
    import inspect
    import api.server as server_mod
    src = inspect.getsource(server_mod.health)
    assert "check_storage_readiness" not in src
    assert "db." not in src.replace("db.py", "")


def test_health_db_endpoint_wraps_check_storage_readiness():
    import inspect
    import api.server as server_mod
    src = inspect.getsource(server_mod.health_db)
    assert "db.check_storage_readiness()" in src
    assert "503" in src


# ── 5-6. Integrity diagnostics -------------------------------------------

@pytest.mark.asyncio
async def test_integrity_reports_zero_on_a_clean_database():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_checkpoint(rc, run_status="running")
    rc.finish_execution(rc.root_execution_id, status="ok")
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status="ok")
    await execution_store.persist_checkpoint(rc, run_status="ok")

    result = await execution_store.check_storage_integrity()

    assert result["error"] is None
    assert result["stuck_tool_calls"] == 0
    assert result["stuck_executions"] == 0
    assert result["stuck_runs"] == 0
    assert result["checkpoint_hash_mismatches"] == 0
    assert result["checkpoints_checked"] == 2


@pytest.mark.asyncio
async def test_integrity_detects_stuck_tool_calls_executions_and_runs():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_tool_call_created(
        rc.start_tool_call("run_command", {"cmd": "x"}))  # left "running"

    result = await execution_store.check_storage_integrity()

    assert result["stuck_tool_calls"] == 1
    assert result["stuck_executions"] == 1  # the root, never completed
    assert result["stuck_runs"] == 1        # runs.status still "running"


@pytest.mark.asyncio
async def test_integrity_detects_a_tampered_checkpoint():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.checkpoints.update().where(db.checkpoints.c.run_id == "run-1")
            .values(run_status="ok"))  # changed WITHOUT recomputing state_hash

    result = await execution_store.check_storage_integrity()

    assert result["checkpoint_hash_mismatches"] == 1
    assert result["checkpoints_checked"] == 1


@pytest.mark.asyncio
async def test_integrity_is_fail_open(monkeypatch):
    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom)
    result = await execution_store.check_storage_integrity()  # must not raise

    assert result["error"] is not None
    assert result["stuck_tool_calls"] is None


def test_integrity_never_mutates_anything():
    """Read-only by construction -- no INSERT/UPDATE/DELETE anywhere in the
    function, verified at the source level, not just by behavior."""
    import inspect
    src = inspect.getsource(execution_store.check_storage_integrity)
    for forbidden in (".insert(", ".update(", ".delete("):
        assert forbidden not in src


# ── 7-8. Cleanup + FK cascades, project-memory survival ----------------

@pytest.mark.asyncio
async def test_expired_session_cleanup_cascades_through_the_entire_tree():
    from swarm.sessions import _cleanup_expired
    from swarm.team import _make_tool_interception_hook

    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "some evidence"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)
    claim_id = await execution_store.persist_claim(rc, "a claim", "supported")
    # Phase K: cleanup now skips a session with a still-"running" run (see
    # swarm/sessions._cleanup_expired's own docstring) -- complete the run
    # first, matching the realistic scenario this test means to exercise
    # (a FINISHED run whose session later expires), not an active one.
    rc.finish_execution(rc.root_execution_id, status="ok")
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status="ok")
    await execution_store.persist_checkpoint(rc, run_status="ok")

    assert await _table_row_count(db.runs) == 1
    assert await _table_row_count(db.executions) == 1
    assert await _table_row_count(db.tool_calls) == 1
    assert await _table_row_count(db.evidence) == 1
    assert await _table_row_count(db.claims) == 1
    assert await _table_row_count(db.checkpoints) == 1

    # Force an already-expired timestamp directly (portable across dialects --
    # SQLite has no interval arithmetic to lean on here).
    from datetime import datetime, timedelta, timezone
    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.chat_sessions.update().where(db.chat_sessions.c.id == sid)
            .values(expires_at=past))

    deleted = await _cleanup_expired()

    assert deleted == 1
    assert await _table_row_count(db.chat_sessions) == 0
    assert await _table_row_count(db.runs) == 0
    assert await _table_row_count(db.executions) == 0
    assert await _table_row_count(db.tool_calls) == 0
    assert await _table_row_count(db.evidence) == 0
    assert await _table_row_count(db.claims) == 0
    assert await _table_row_count(db.checkpoints) == 0
    assert await execution_store.rehydrate_run("run-1") is None


@pytest.mark.asyncio
async def test_expired_session_cleanup_never_deletes_project_memory():
    from swarm.sessions import _cleanup_expired

    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    claim_id = await execution_store.persist_claim(rc, "a claim", "supported")
    promoted = await execution_store.promote_session_claims(sid, "p")
    assert len(promoted) == 1
    assert await _table_row_count(db.project_memory_promotions) == 1
    # Phase K: cleanup now skips a session with a still-"running" run --
    # complete it first (see the sibling test above for the same fix).
    rc.finish_execution(rc.root_execution_id, status="ok")
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status="ok")

    from datetime import datetime, timedelta, timezone
    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.chat_sessions.update().where(db.chat_sessions.c.id == sid)
            .values(expires_at=past))

    await _cleanup_expired()

    assert await _table_row_count(db.chat_sessions) == 0
    assert await _table_row_count(db.claims) == 0
    # ...but the promoted memory survives, completely untouched.
    assert await _table_row_count(db.project_memory_promotions) == 1


@pytest.mark.asyncio
async def test_persisted_session_is_not_cleaned_up():
    from swarm.sessions import _cleanup_expired

    sid = await _create_session()
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.chat_sessions.update().where(db.chat_sessions.c.id == sid)
            .values(persist=True, expires_at=None))

    deleted = await _cleanup_expired()

    assert deleted == 0
    assert await _table_row_count(db.chat_sessions) == 1


# ── 9. Concurrency: duplicate promotion race ------------------------------

@pytest.mark.asyncio
async def test_promote_claim_recovers_from_a_concurrent_promotion_race(monkeypatch):
    """Two /feedback calls racing on the SAME (project_id, claim_id): both
    pass the pre-check SELECT before either INSERTs. Simulated here by
    forcing promote_claim's own pre-check to see "not found" (a stale read)
    while a competing row already exists -- the real INSERT then collides
    for real against the live UniqueConstraint."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    claim_id = await execution_store.persist_claim(team._run_context, "raced claim", "supported")
    claim = await _claim_row(claim_id)

    winner_id = str(uuid.uuid4())
    async with db.get_engine().begin() as conn:
        await conn.execute(db.project_memory_promotions.insert().values(
            id=winner_id, project_id="p", claim_id=claim_id,
            run_id=claim["run_id"], execution_id=claim["execution_id"],
            statement=claim["statement"], claim_status="supported",
            validated_by="the-other-caller",
        ))

    from sqlalchemy.ext.asyncio import AsyncConnection
    real_execute = AsyncConnection.execute
    calls = {"n": 0}

    class _EmptyResult:
        def scalar(self):
            return None

    async def _patched_execute(self, statement, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _EmptyResult()  # the stale pre-check read
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", _patched_execute)

    result = await execution_store.promote_claim(
        "p", claim, None, None, validated_by="the-losing-caller")

    assert result == winner_id       # recovered the winner's id
    assert await _table_row_count(db.project_memory_promotions) == 1  # never duplicated


@pytest.mark.asyncio
async def test_promote_claim_race_recovery_failure_is_still_fail_open(monkeypatch):
    """If even the RECOVERY lookup fails after losing the race, promote_claim
    still returns None rather than raising -- the caller's /feedback
    response must never crash over this."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    claim_id = await execution_store.persist_claim(team._run_context, "raced claim", "supported")
    claim = await _claim_row(claim_id)

    async with db.get_engine().begin() as conn:
        await conn.execute(db.project_memory_promotions.insert().values(
            id=str(uuid.uuid4()), project_id="p", claim_id=claim_id,
            run_id=claim["run_id"], execution_id=claim["execution_id"],
            statement=claim["statement"], claim_status="supported",
            validated_by="the-other-caller",
        ))

    from sqlalchemy.ext.asyncio import AsyncConnection
    real_execute = AsyncConnection.execute
    calls = {"n": 0}

    class _EmptyResult:
        def scalar(self):
            return None

    async def _patched_execute(self, statement, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _EmptyResult()
        if calls["n"] == 3:
            raise RuntimeError("db died during recovery too")
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", _patched_execute)

    result = await execution_store.promote_claim(
        "p", claim, None, None, validated_by="the-losing-caller")

    assert result is None  # fail-open, not raised


# ── 10. Migration/startup safety (regression confirmation) -----------------

@pytest.mark.asyncio
async def test_ensure_schema_still_raises_on_mismatch_readiness_does_not():
    """The Phase B contract is unchanged by Phase I: ensure_schema() is the
    strict startup gate that RAISES; check_storage_readiness() is the new,
    never-raising probe for a health endpoint. Both must agree on the
    underlying fact, only one may ever raise."""
    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0001_baseline'"))

    with pytest.raises(RuntimeError, match="schema is out of date"):
        await db.ensure_schema()

    result = await db.check_storage_readiness()  # does not raise
    assert result["schema_current"] is False
    assert result["current_revision"] == "0001_baseline"
