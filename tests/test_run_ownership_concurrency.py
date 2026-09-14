"""Phase K -- distributed execution & concurrency safety.

Forensics established the central fact this whole phase is built around:
run_id is ALWAYS freshly minted by exactly one process
(execution_context.new_id(), never supplied by a caller), and
api/server.py's _run_worker_subprocess spawns exactly one dedicated
ephemeral worker per /run, /run_chunked chunk, or /stream call. No code
path today lets a second worker attempt to advance an EXISTING run_id --
rehydrate_run (Phase H) is deliberately read-only and never launches
execution itself. So "two workers race for the same run_id" is not
reachable through any current code path, and run_task_async/
run_task_stream do NOT call the ownership primitives below at all.

What this phase builds and tests is the PRIMITIVE the invariant asks for
-- available now, for the day a caller DOES take a rehydrate_run verdict
and launch a continuation -- via a single atomic UPDATE...WHERE against
two new nullable columns on `runs` (owner_worker_id/owner_lease_expires_at,
0006_run_ownership), no new table, no in-process lock, no queue/broker.

Also covers the one CONCRETE race forensics did find reachable today:
swarm.sessions._cleanup_expired() could previously delete an expired
session's entire durable tree out from under a run still genuinely
"running" against it -- fixed by excluding sessions with an active run
from the cleanup DELETE.
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


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


# ── 1-2. Exactly-one ownership winner -----------------------------------

@pytest.mark.asyncio
async def test_two_workers_competing_for_the_same_run_exactly_one_wins():
    """Real transaction/constraint race window: two asyncio tasks calling
    acquire_run_ownership concurrently for the SAME run_id, serialized at
    the database engine level (not mocked) -- the WHERE clause's own
    atomicity is what is under test, not a pre-computed "desired result"."""
    sid = await _create_session()
    await _team_with_persisted_run(sid)

    results = await asyncio.gather(
        execution_store.acquire_run_ownership("run-1", "worker-A"),
        execution_store.acquire_run_ownership("run-1", "worker-B"),
    )

    assert sorted(results) == [False, True]  # exactly one winner
    state = await execution_store.run_ownership_state("run-1")
    assert state["owner_worker_id"] in ("worker-A", "worker-B")


@pytest.mark.asyncio
async def test_acquisition_is_denied_while_a_live_lease_is_held():
    sid = await _create_session()
    await _team_with_persisted_run(sid)

    first = await execution_store.acquire_run_ownership("run-1", "worker-A")
    second = await execution_store.acquire_run_ownership("run-1", "worker-B")

    assert first is True
    assert second is False


# ── 3. Stale-owner recovery -----------------------------------------------

@pytest.mark.asyncio
async def test_stale_owner_lease_is_reclaimed_by_a_new_worker():
    sid = await _create_session()
    await _team_with_persisted_run(sid)
    await execution_store.acquire_run_ownership("run-1", "worker-A", lease_seconds=300)

    # Simulate worker-A's lease having already expired (it crashed without
    # releasing) -- backdate it directly.
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.runs.update().where(db.runs.c.run_id == "run-1")
            .values(owner_lease_expires_at=sa.text(
                "datetime('now', '-1 hour')" if db.get_engine().dialect.name == "sqlite"
                else "now() - interval '1 hour'")))

    reclaimed = await execution_store.acquire_run_ownership("run-1", "worker-B")

    assert reclaimed is True
    state = await execution_store.run_ownership_state("run-1")
    assert state["owner_worker_id"] == "worker-B"


# ── 4. Ownership release/expiry -------------------------------------------

@pytest.mark.asyncio
async def test_release_by_the_owner_frees_the_run_for_another_worker():
    sid = await _create_session()
    await _team_with_persisted_run(sid)
    await execution_store.acquire_run_ownership("run-1", "worker-A")

    released = await execution_store.release_run_ownership("run-1", "worker-A")
    reacquired = await execution_store.acquire_run_ownership("run-1", "worker-B")

    assert released is True
    assert reacquired is True


@pytest.mark.asyncio
async def test_release_by_a_non_owner_is_a_safe_no_op():
    sid = await _create_session()
    await _team_with_persisted_run(sid)
    await execution_store.acquire_run_ownership("run-1", "worker-A")

    released = await execution_store.release_run_ownership("run-1", "worker-B")  # never owned it

    assert released is False
    state = await execution_store.run_ownership_state("run-1")
    assert state["owner_worker_id"] == "worker-A"  # worker-A's claim untouched


@pytest.mark.asyncio
async def test_reentrant_acquisition_renews_the_same_workers_own_lease():
    sid = await _create_session()
    await _team_with_persisted_run(sid)
    first = await execution_store.acquire_run_ownership("run-1", "worker-A", lease_seconds=1)
    renewed = await execution_store.acquire_run_ownership("run-1", "worker-A", lease_seconds=300)

    assert first is True
    assert renewed is True  # not a race with itself


# ── 5. Ownership scoped to an existing run; observability -----------------

@pytest.mark.asyncio
async def test_acquisition_against_a_nonexistent_run_id_fails():
    result = await execution_store.acquire_run_ownership("no-such-run", "worker-A")
    assert result is False


@pytest.mark.asyncio
async def test_ownership_state_reports_unowned_by_default():
    sid = await _create_session()
    await _team_with_persisted_run(sid)

    state = await execution_store.run_ownership_state("run-1")

    assert state["owned"] is False
    assert state["owner_worker_id"] is None


@pytest.mark.asyncio
async def test_ownership_state_returns_none_for_a_nonexistent_run():
    assert await execution_store.run_ownership_state("no-such-run") is None


# ── 6. Fail-safe (not fail-open) on infrastructure failure -----------------

@pytest.mark.asyncio
async def test_acquire_run_ownership_fails_safe_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom)
    result = await execution_store.acquire_run_ownership("run-1", "worker-A")

    assert result is False  # never pretends ownership was acquired


@pytest.mark.asyncio
async def test_release_run_ownership_fails_safe_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom)
    result = await execution_store.release_run_ownership("run-1", "worker-A")

    assert result is False


# ── 7. run_task_async/run_task_stream do NOT call the ownership primitives ---

def test_ownership_primitives_are_not_wired_into_the_live_runtime():
    """Confirms the forensic scope decision this phase's own docstring
    states explicitly: no code path today races on the same run_id, so
    run_task_async/run_task_stream must NOT call acquire_run_ownership --
    doing so would add locking overhead to a flow that cannot race,
    contradicting 'do not solve hypothetical problems.'"""
    import inspect
    import swarm.team as team_mod
    source = inspect.getsource(team_mod)
    assert "acquire_run_ownership" not in source
    assert "release_run_ownership" not in source


# ── 8. Cleanup-vs-active-run race -----------------------------------------

@pytest.mark.asyncio
async def test_cleanup_skips_a_session_with_a_still_running_run():
    from swarm.sessions import _cleanup_expired
    from datetime import datetime, timedelta, timezone

    sid = await _create_session()
    await _team_with_persisted_run(sid)  # runs.status stays "running" -- never completed
    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.chat_sessions.update().where(db.chat_sessions.c.id == sid)
            .values(expires_at=past))

    deleted = await _cleanup_expired()

    assert deleted == 0
    assert await _table_row_count(db.chat_sessions) == 1
    assert await _table_row_count(db.runs) == 1  # the active run's durable tree survives


@pytest.mark.asyncio
async def test_cleanup_still_removes_a_session_whose_run_has_finished():
    from swarm.sessions import _cleanup_expired
    from datetime import datetime, timedelta, timezone

    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    rc.finish_execution(rc.root_execution_id, status="ok")
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status="ok")

    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.chat_sessions.update().where(db.chat_sessions.c.id == sid)
            .values(expires_at=past))

    deleted = await _cleanup_expired()

    assert deleted == 1
    assert await _table_row_count(db.chat_sessions) == 0
    assert await _table_row_count(db.runs) == 0


@pytest.mark.asyncio
async def test_cleanup_still_removes_a_session_with_no_runs_at_all():
    """Regression: the ordinary case (no run ever created for this
    session) must be completely unaffected by the new active-run check."""
    from swarm.sessions import _cleanup_expired
    from datetime import datetime, timedelta, timezone

    sid = await _create_session()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.chat_sessions.update().where(db.chat_sessions.c.id == sid)
            .values(expires_at=past))

    deleted = await _cleanup_expired()

    assert deleted == 1


# ── 9. Duplicate durable records prevented (regression, not new) -----------

@pytest.mark.asyncio
async def test_duplicate_promotion_still_prevented_under_this_phases_changes():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "a claim", "supported")

    first = await execution_store.promote_session_claims(sid, "p")
    second = await execution_store.promote_session_claims(sid, "p")

    assert first == second
    assert await _table_row_count(db.project_memory_promotions) == 1


@pytest.mark.asyncio
async def test_checkpoint_sequence_remains_gapless_and_unique_under_repeats():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context

    ids = [await execution_store.persist_checkpoint(rc, run_status="running") for _ in range(5)]

    assert len(set(ids)) == 5  # no collisions
    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(
            sa.select(db.checkpoints.c.sequence).where(db.checkpoints.c.run_id == "run-1")
            .order_by(db.checkpoints.c.sequence)
        )).scalars().all()
    assert rows == [1, 2, 3, 4, 5]


# ── 10. ToolCall safety unchanged (Phase H rule preserved) -----------------

@pytest.mark.asyncio
async def test_ambiguous_tool_call_still_blocks_resumability_unchanged():
    """Phase H's rule is untouched by this phase -- re-confirmed directly:
    an unfinished ToolCall makes rehydrate_run refuse resumability rather
    than treating it as safe to replay."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_tool_call_created(
        rc.start_tool_call("run_command", {"cmd": "rm -rf something"}))
    await execution_store.persist_checkpoint(rc, run_status="running")

    result = await execution_store.rehydrate_run("run-1")

    assert result["resumable"] is False
    assert "duplicate side effect" in result["reason"]


def test_no_automatic_tool_call_replay_was_introduced():
    import inspect
    src = inspect.getsource(execution_store)
    # No new code path re-invokes a tool function anywhere in this module.
    assert "await function(" not in src
    assert "session.call_tool(" not in src


# ── 11. Tenant/project isolation remains enforced (regression) -------------

@pytest.mark.asyncio
async def test_run_ownership_does_not_bypass_project_isolation():
    """Acquiring ownership of a run_id says nothing about which project it
    belongs to -- promote_session_claims' own ownership check (Phase J) is
    unaffected by, and independent of, run-level acquisition."""
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "fact", "supported")
    await execution_store.acquire_run_ownership("run-1", "worker-A")

    promoted = await execution_store.promote_session_claims(sid, "project-b")

    assert promoted == []  # Phase J's check still refuses the cross-project attempt


# ── 12. Migration / schema ------------------------------------------------

@pytest.mark.asyncio
async def test_ownership_columns_exist_and_default_to_unowned():
    sid = await _create_session()
    await _team_with_persisted_run(sid)

    async with db.get_engine().begin() as conn:
        row = (await conn.execute(
            sa.select(db.runs.c.owner_worker_id, db.runs.c.owner_lease_expires_at)
            .where(db.runs.c.run_id == "run-1")
        )).mappings().first()

    assert row["owner_worker_id"] is None
    assert row["owner_lease_expires_at"] is None
