"""Phase L -- operational durability, retention & recovery.

No new schema this phase (verified: no new migration was added) -- every
addition here is a new query/parameter against the EXISTING Phase A-K
schema: swarm.sessions._cleanup_expired gained a dry_run parameter,
swarm.execution_store.check_storage_integrity gained three new detection
counts (impossible lifecycle states, invalid promotions, duplicate
checkpoint sequences), and a new read-only swarm.execution_store.
list_stale_runs surfaces WHICH specific runs are stale (the per-run
companion to check_storage_integrity's aggregate stuck_runs count).

See docs/guide/lifecycle-and-retention.md for the full lifecycle matrix,
recovery policy, and backup/restore boundary this phase's forensics
produced, and docs/guide/deferred-limitations-ledger.md for the Phase
A-L limitations ledger.
"""
import asyncio
import types
import uuid
from datetime import datetime, timedelta, timezone

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


async def _create_session(project_id: str = "p") -> str:
    sid = str(uuid.uuid4())
    async with db.get_engine().begin() as conn:
        await conn.execute(db.chat_sessions.insert().values(
            id=sid, project_id=project_id, title="t", persist=False))
    return sid


async def _team_with_persisted_run(session_id: str, run_id: str = "run-1") -> types.SimpleNamespace:
    team = types.SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    return team


async def _complete_run(rc: RunContext, status: str = "ok") -> None:
    rc.finish_execution(rc.root_execution_id, status=status)
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status=status)


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


async def _expire_session(sid: str) -> None:
    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.chat_sessions.update().where(db.chat_sessions.c.id == sid)
            .values(expires_at=past))


# ── 1-2. Dry-run cleanup ------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_cleanup_previews_without_deleting():
    from swarm.sessions import _cleanup_expired

    sid = await _create_session()
    await _expire_session(sid)

    previewed = await _cleanup_expired(dry_run=True)

    assert previewed == 1
    assert await _table_row_count(db.chat_sessions) == 1  # nothing actually deleted


@pytest.mark.asyncio
async def test_dry_run_matches_the_real_run_exactly():
    from swarm.sessions import _cleanup_expired

    for _ in range(3):
        sid = await _create_session()
        await _expire_session(sid)

    previewed = await _cleanup_expired(dry_run=True)
    actual = await _cleanup_expired(dry_run=False)

    assert previewed == actual == 3


@pytest.mark.asyncio
async def test_dry_run_also_respects_the_active_run_protection():
    from swarm.sessions import _cleanup_expired

    sid = await _create_session()
    await _team_with_persisted_run(sid)  # left "running"
    await _expire_session(sid)

    previewed = await _cleanup_expired(dry_run=True)

    assert previewed == 0


# ── 3. Repeated cleanup idempotency ---------------------------------------

@pytest.mark.asyncio
async def test_repeated_cleanup_is_idempotent():
    from swarm.sessions import _cleanup_expired

    sid = await _create_session()
    await _expire_session(sid)

    first = await _cleanup_expired()
    second = await _cleanup_expired()

    assert first == 1
    assert second == 0  # already gone -- not an error, not a re-count


# ── 4. DB failure during cleanup is fail-safe ------------------------------

@pytest.mark.asyncio
async def test_cleanup_dry_run_failure_is_fail_safe(monkeypatch):
    from swarm.sessions import _cleanup_expired
    import swarm.db as db_mod

    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(db_mod, "get_engine", _boom)
    result = await _cleanup_expired(dry_run=True)

    assert result == 0  # never raised


# ── 5-6. Stale run detection (list_stale_runs) -----------------------------

@pytest.mark.asyncio
async def test_list_stale_runs_finds_a_run_with_no_recent_checkpoint():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    # Backdate started_at so it looks like this run began long ago, with no
    # checkpoint ever written for it (persist_checkpoint was never called).
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.runs.update().where(db.runs.c.run_id == "run-1")
            .values(started_at=datetime.now(timezone.utc) - timedelta(hours=2)))

    stale = await execution_store.list_stale_runs(older_than_seconds=3600)

    assert len(stale) == 1
    assert stale[0]["run_id"] == "run-1"
    assert stale[0]["session_id"] == sid


@pytest.mark.asyncio
async def test_list_stale_runs_excludes_a_run_with_a_recent_checkpoint():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    stale = await execution_store.list_stale_runs(older_than_seconds=3600)

    assert stale == []  # checkpoint just written -- not stale


@pytest.mark.asyncio
async def test_list_stale_runs_excludes_completed_runs():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await _complete_run(team._run_context)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.runs.update().where(db.runs.c.run_id == "run-1")
            .values(started_at=datetime.now(timezone.utc) - timedelta(hours=2)))

    stale = await execution_store.list_stale_runs(older_than_seconds=3600)

    assert stale == []  # not "running" -- done, not stale


@pytest.mark.asyncio
async def test_list_stale_runs_detects_but_never_mutates_or_resumes():
    """Detection only -- the run's own status/ownership are untouched by
    merely being listed as stale."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.runs.update().where(db.runs.c.run_id == "run-1")
            .values(started_at=datetime.now(timezone.utc) - timedelta(hours=2)))

    await execution_store.list_stale_runs(older_than_seconds=3600)

    async with db.get_engine().begin() as conn:
        row = (await conn.execute(
            sa.select(db.runs.c.status, db.runs.c.owner_worker_id)
            .where(db.runs.c.run_id == "run-1")
        )).mappings().first()
    assert row["status"] == "running"     # unchanged
    assert row["owner_worker_id"] is None  # never auto-claimed


def test_list_stale_runs_never_resumes_or_launches_execution():
    import inspect
    src = inspect.getsource(execution_store.list_stale_runs)
    for forbidden in (".arun(", "_build_team(", "run_task_async(", "run_task_stream(",
                      "acquire_run_ownership("):
        assert forbidden not in src


@pytest.mark.asyncio
async def test_list_stale_runs_is_fail_open(monkeypatch):
    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom)
    result = await execution_store.list_stale_runs()

    assert result == []


# ── 7. Cleanup/recovery race: a stale-but-active run is detected AND protected

@pytest.mark.asyncio
async def test_stale_active_run_is_both_detected_and_protected_from_cleanup():
    from swarm.sessions import _cleanup_expired

    sid = await _create_session()
    await _team_with_persisted_run(sid)  # never completed
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.runs.update().where(db.runs.c.run_id == "run-1")
            .values(started_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    await _expire_session(sid)

    stale = await execution_store.list_stale_runs(older_than_seconds=3600)
    deleted = await _cleanup_expired()

    assert len(stale) == 1  # an operator CAN see it needs attention
    assert deleted == 0     # but cleanup did not destroy it out from under itself
    assert await _table_row_count(db.chat_sessions) == 1


# ── 8-10. Extended integrity detection --------------------------------------

@pytest.mark.asyncio
async def test_integrity_reports_zero_new_checks_on_a_clean_database():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await _complete_run(team._run_context)

    result = await execution_store.check_storage_integrity()

    assert result["impossible_lifecycle_states"] == 0
    assert result["invalid_promotions"] == 0
    assert result["duplicate_checkpoint_sequences"] == 0


@pytest.mark.asyncio
async def test_integrity_detects_a_completed_run_with_no_completed_at():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.runs.update().where(db.runs.c.run_id == "run-1")
            .values(status="ok", completed_at=None))  # contradictory, external tampering

    result = await execution_store.check_storage_integrity()

    assert result["impossible_lifecycle_states"] == 1


@pytest.mark.asyncio
async def test_integrity_detects_a_running_run_with_completed_at_set():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.runs.update().where(db.runs.c.run_id == "run-1")
            .values(status="running", completed_at=sa.func.now()))

    result = await execution_store.check_storage_integrity()

    assert result["impossible_lifecycle_states"] == 1


@pytest.mark.asyncio
async def test_integrity_detects_an_invalid_promotion_status():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    claim_id = await execution_store.persist_claim(team._run_context, "fact", "supported")
    await execution_store.promote_session_claims(sid, "p")

    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.project_memory_promotions.update()
            .where(db.project_memory_promotions.c.claim_id == claim_id)
            .values(claim_status="contradicted"))  # violates promote_claim's own invariant

    result = await execution_store.check_storage_integrity()

    assert result["invalid_promotions"] == 1


def test_integrity_extension_never_repairs_anything():
    import inspect
    src = inspect.getsource(execution_store.check_storage_integrity)
    for forbidden in (".insert(", ".update(", ".delete("):
        assert forbidden not in src


# ── 11. Ambiguous ToolCall still never replayed (regression, re-confirmed) --

@pytest.mark.asyncio
async def test_ambiguous_tool_call_still_never_replayed():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_tool_call_created(
        rc.start_tool_call("apply_diff", {"path": "a.py"}))
    await execution_store.persist_checkpoint(rc, run_status="running")

    result = await execution_store.rehydrate_run("run-1")

    assert result["resumable"] is False
    assert "duplicate side effect" in result["reason"]


def test_no_automatic_replay_mechanism_exists_anywhere_in_this_phase():
    import inspect
    for fn in (execution_store.list_stale_runs, execution_store.check_storage_integrity):
        src = inspect.getsource(fn)
        assert "await function(" not in src
        assert "session.call_tool(" not in src


# ── 12. Project memory survives session deletion (regression) --------------

@pytest.mark.asyncio
async def test_project_memory_still_survives_session_deletion():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "fact", "supported")
    await execution_store.promote_session_claims(sid, "p")
    await _complete_run(team._run_context)
    await _expire_session(sid)

    from swarm.sessions import _cleanup_expired
    await _cleanup_expired()

    assert await _table_row_count(db.chat_sessions) == 0
    assert await _table_row_count(db.project_memory_promotions) == 1
