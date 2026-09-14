"""Phase H -- durable checkpointing and safe rehydration of a Run's
execution state (swarm/execution_store.persist_checkpoint/rehydrate_run,
wired into swarm/team.py's run_task_async/run_task_stream at the SAME two
boundaries persist_run_started/persist_run_completed already use).

Same layering as the prior phases' own suites: the checkpoint/rehydration
primitives exercised directly against a real (in-memory SQLite) database,
plus source-level guards proving the wiring never touches Evidence/Claims/
project memory and never launches or drives execution itself.
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


async def _checkpoint_rows(run_id: str):
    async with db.get_engine().begin() as conn:
        return (await conn.execute(
            sa.select(db.checkpoints).where(db.checkpoints.c.run_id == run_id)
            .order_by(db.checkpoints.c.sequence)
        )).mappings().all()


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


# ── 1-3. Checkpoint creation, idempotent repetition, lineage -----------------

@pytest.mark.asyncio
async def test_checkpoint_is_created_with_sequence_one():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)

    checkpoint_id = await execution_store.persist_checkpoint(
        team._run_context, run_status="running")

    assert checkpoint_id is not None
    rows = await _checkpoint_rows("run-1")
    assert len(rows) == 1
    assert rows[0]["sequence"] == 1
    assert rows[0]["schema_version"] == execution_store.CHECKPOINT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_repeated_checkpointing_is_safe_and_increments_sequence():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)

    id1 = await execution_store.persist_checkpoint(team._run_context, run_status="running")
    id2 = await execution_store.persist_checkpoint(team._run_context, run_status="running")
    id3 = await execution_store.persist_checkpoint(team._run_context, run_status="ok")

    assert len({id1, id2, id3}) == 3  # no raise, no collision
    rows = await _checkpoint_rows("run-1")
    assert [r["sequence"] for r in rows] == [1, 2, 3]
    assert rows[-1]["run_status"] == "ok"


@pytest.mark.asyncio
async def test_checkpoint_lineage_points_at_the_root_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id

    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    rows = await _checkpoint_rows("run-1")
    assert rows[0]["last_execution_id"] == root
    assert rows[0]["run_id"] == "run-1"


# ── 4/11. Checkpoint version/integrity validation ----------------------------

@pytest.mark.asyncio
async def test_rehydrate_rejects_an_unrecognized_schema_version():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.checkpoints.update().where(db.checkpoints.c.run_id == "run-1")
            .values(schema_version=999))

    result = await execution_store.rehydrate_run("run-1")
    assert result["resumable"] is False
    assert "schema_version" in result["reason"]


@pytest.mark.asyncio
async def test_rehydrate_rejects_a_tampered_checkpoint_row():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    async with db.get_engine().begin() as conn:
        await conn.execute(
            db.checkpoints.update().where(db.checkpoints.c.run_id == "run-1")
            .values(run_status="ok"))  # changed WITHOUT recomputing state_hash

    result = await execution_store.rehydrate_run("run-1")
    assert result["resumable"] is False
    assert "integrity" in result["reason"]


# ── 5-6. Safe rehydration + run_id preservation ------------------------------

@pytest.mark.asyncio
async def test_safe_rehydration_of_a_cleanly_checkpointed_running_run():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    from swarm.team import _make_tool_interception_hook
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "ok"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)  # completes cleanly
    await execution_store.persist_checkpoint(rc, run_status="running")

    result = await execution_store.rehydrate_run("run-1")

    assert result is not None
    assert result["resumable"] is True
    assert result["run_id"] == "run-1"          # original run_id preserved
    assert result["root_execution_id"] == rc.root_execution_id
    assert result["session_id"] == sid


@pytest.mark.asyncio
async def test_rehydrate_returns_none_for_a_never_checkpointed_run():
    sid = await _create_session()
    await _team_with_persisted_run(sid)  # run row exists, no checkpoint ever written

    assert await execution_store.rehydrate_run("run-1") is None


@pytest.mark.asyncio
async def test_rehydrate_returns_none_for_an_unknown_run_id():
    assert await execution_store.rehydrate_run("no-such-run") is None


@pytest.mark.asyncio
async def test_a_terminal_run_is_not_resumable():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_run_completed(team._run_context, status="ok")
    await execution_store.persist_checkpoint(team._run_context, run_status="ok")

    result = await execution_store.rehydrate_run("run-1")
    assert result["resumable"] is False
    assert "ok" in result["reason"]


# ── 7. Retry/attempt preservation ---------------------------------------

@pytest.mark.asyncio
async def test_attempt_number_and_root_execution_are_preserved_across_a_retry():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    root = rc.root_execution_id
    # A retry sibling under root -- root's own attempt_number is what
    # rehydration reports (the coordinator-level attempt counter).
    retry_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=root)
    await execution_store.persist_execution_created(rc.executions[retry_id])
    rc.finish_execution(retry_id, status="ok")
    await execution_store.persist_execution_completed(rc.executions[retry_id])
    await execution_store.persist_checkpoint(rc, run_status="running")

    result = await execution_store.rehydrate_run("run-1")

    assert result["resumable"] is True
    assert result["root_execution_id"] == root  # the TRUE root, not the retry sibling
    assert result["attempt_number"] == rc.executions[root].attempt_number


# ── 8. Evidence/Claim references remain unchanged ----------------------------

@pytest.mark.asyncio
async def test_checkpointing_never_touches_evidence_or_claims():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    claim_id = await execution_store.persist_claim(rc, "some claim", "supported")
    before = await _table_row_count(db.claims)
    before_ev = await _table_row_count(db.evidence)

    await execution_store.persist_checkpoint(rc, run_status="running")
    await execution_store.rehydrate_run("run-1")

    assert await _table_row_count(db.claims) == before
    assert await _table_row_count(db.evidence) == before_ev
    async with db.get_engine().begin() as conn:
        claim_row = (await conn.execute(
            sa.select(db.claims).where(db.claims.c.claim_id == claim_id)
        )).mappings().first()
    assert claim_row["statement"] == "some claim"  # byte-identical, untouched


def test_checkpoint_functions_never_write_to_evidence_or_claims():
    import inspect
    src = (inspect.getsource(execution_store.persist_checkpoint)
           + inspect.getsource(execution_store.rehydrate_run))
    for forbidden in ("db.evidence.insert(", "db.evidence.update(", "db.evidence.delete(",
                      "db.claims.insert(", "db.claims.update(", "db.claims.delete("):
        assert forbidden not in src


# ── 9-10. Unsafe replay refusal ----------------------------------------------

@pytest.mark.asyncio
async def test_unfinished_tool_call_makes_the_run_unresumable():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context

    # A ToolCall that never finished -- e.g. the worker was SIGKILLed mid-call.
    await execution_store.persist_tool_call_created(
        rc.start_tool_call("run_command", {"cmd": "rm -rf something"}))
    await execution_store.persist_checkpoint(rc, run_status="running")

    result = await execution_store.rehydrate_run("run-1")

    assert result["resumable"] is False
    assert "tool call" in result["reason"]
    assert "duplicate side effect" in result["reason"]


@pytest.mark.asyncio
async def test_unfinished_execution_makes_the_run_unresumable():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    root = rc.root_execution_id
    # A delegation Execution left "running" -- e.g. killed mid-delegation,
    # before finish_execution ever ran.
    child_id = rc.start_execution("researcher", "delegation", parent_execution_id=root)
    await execution_store.persist_execution_created(rc.executions[child_id])
    await execution_store.persist_checkpoint(rc, run_status="running")

    result = await execution_store.rehydrate_run("run-1")

    assert result["resumable"] is False
    assert "execution(s)" in result["reason"]


@pytest.mark.asyncio
async def test_a_cleanly_failed_tool_call_does_not_block_resumability():
    """A tool call that finished (even with an error) is TERMINAL, not
    ambiguous -- only a still-"running" state blocks resumability."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    from swarm.team import _make_tool_interception_hook
    hook = _make_tool_interception_hook()

    async def failing_tool(**kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await hook("get_file_content", failing_tool, {"path": "a.py"}, team=team)
    await execution_store.persist_checkpoint(rc, run_status="running")

    result = await execution_store.rehydrate_run("run-1")
    assert result["resumable"] is True


# ── 12. Checkpoint persistence failure is fail-open --------------------------

@pytest.mark.asyncio
async def test_persist_checkpoint_failure_is_fail_open(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)

    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom)
    direct = await execution_store.persist_checkpoint(team._run_context, run_status="running")
    assert direct is None

    guarded = await execution_store.guard(execution_store.persist_checkpoint(
        team._run_context, run_status="running"))
    assert guarded is None


@pytest.mark.asyncio
async def test_rehydrate_lookup_failure_is_fail_open(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom)
    result = await execution_store.rehydrate_run("run-1")
    assert result["resumable"] is False
    assert "rehydration lookup failed" in result["reason"]


# ── 13. Session deletion cascades to checkpoints ------------------------------

@pytest.mark.asyncio
async def test_session_deletion_removes_checkpoints_through_cascade():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")
    assert await _table_row_count(db.checkpoints) == 1

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        await conn.execute(db.chat_sessions.delete().where(db.chat_sessions.c.id == sid))

    assert await _table_row_count(db.checkpoints) == 0
    assert await execution_store.rehydrate_run("run-1") is None


# ── 14. Project memory is unaffected -----------------------------------------

@pytest.mark.asyncio
async def test_checkpointing_never_writes_project_memory():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")
    await execution_store.rehydrate_run("run-1")

    assert await _table_row_count(db.project_memory_promotions) == 0


def test_checkpoint_functions_never_reference_promotion_or_feedback():
    import inspect
    src = (inspect.getsource(execution_store.persist_checkpoint)
           + inspect.getsource(execution_store.rehydrate_run))
    for forbidden in ("promote_claim(", "promote_session_claims(",
                      "project_memory_promotions", "load_project_memory_context"):
        assert forbidden not in src


def test_feedback_module_never_references_checkpoints():
    """Phase G's project-memory context path must never surface checkpoint
    state (prior EXECUTION state, not prior VALIDATED knowledge)."""
    import inspect
    import swarm.feedback as feedback_mod
    assert "checkpoint" not in inspect.getsource(feedback_mod).lower()


# ── 15. Phase H does not launch or drive execution itself --------------------

def test_rehydrate_run_never_calls_arun_or_starts_a_team():
    import inspect
    src = inspect.getsource(execution_store.rehydrate_run)
    for forbidden in (".arun(", "_build_team(", "run_task_async(", "run_task_stream(",
                      "asyncio.create_subprocess"):
        assert forbidden not in src


def test_team_py_checkpoints_at_exactly_four_call_sites():
    import inspect
    import swarm.team as team_mod
    source = inspect.getsource(team_mod)
    assert source.count("execution_store.persist_checkpoint(") == 4
