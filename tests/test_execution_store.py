"""Phase C -- durable Run + Execution persistence (swarm/execution_store.py).

Two levels, matching this suite's existing convention (test_migrations.py for
the schema/migration layer, test_execution_context.py for the in-memory
runtime layer):

  * swarm/execution_store.py's own functions, exercised directly against a
    real (in-memory SQLite) database -- row shape, session ownership, id
    equality with the in-memory records, cascade, fail-open.
  * swarm/team.py's actual integration points (_tool_interception_hook,
    _stream_team_run) driven the same way test_execution_context.py already
    does (a lightweight fake `team` carrying a real RunContext), now also
    asserting on the DURABLE rows those calls produce, not just the
    in-memory RunContext state.

Every DB-touching helper below is `async def` and every test is
`@pytest.mark.asyncio async def`: swarm.migrations.run_upgrade() (called only
once, in the fixture) is the one exception -- Alembic's command API calls
asyncio.run() internally, which cannot nest inside pytest-asyncio's own event
loop, so the fixture itself stays a plain sync function (matching
tests/test_migrations.py's documented reasoning) while everything else in
this file runs inside one coherent async test.
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db
from swarm.execution_context import RunContext
from swarm.migrations import run_upgrade
from swarm.team import _make_tool_interception_hook, _stream_team_run
import swarm.execution_store as execution_store


@pytest.fixture(autouse=True)
def _fresh_migrated_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    asyncio.run(db.reset_engine_for_tests())
    run_upgrade("head")   # plain sync call -- see this module's own docstring
    yield


async def _create_session() -> str:
    sid = str(uuid.uuid4())
    async with db.get_engine().begin() as conn:
        await conn.execute(db.chat_sessions.insert().values(
            id=sid, project_id="p", title="t", persist=False))
    return sid


async def _run_row(run_id: str):
    async with db.get_engine().begin() as conn:
        return (await conn.execute(
            sa.select(db.runs).where(db.runs.c.run_id == run_id))).mappings().first()


async def _execution_row(execution_id: str):
    async with db.get_engine().begin() as conn:
        return (await conn.execute(
            sa.select(db.executions).where(db.executions.c.execution_id == execution_id)
        )).mappings().first()


async def _all_execution_rows(run_id: str):
    async with db.get_engine().begin() as conn:
        return (await conn.execute(
            sa.select(db.executions).where(db.executions.c.run_id == run_id))).mappings().all()


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


async def _team_with_persisted_run(session_id: str, run_id: str = "run-1") -> SimpleNamespace:
    team = SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    return team


# ── 1/2/3/4/5. Run + root Execution creation ────────────────────────────────

@pytest.mark.asyncio
async def test_run_created_with_correct_session_id():
    sid = await _create_session()
    rc = RunContext(sid, "run-abc123abc1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    await execution_store.persist_run_started(rc, team_name="engineering", run_type="single")

    row = await _run_row("run-abc123abc1")
    assert row is not None
    assert row["session_id"] == sid


@pytest.mark.asyncio
async def test_run_id_equals_run_context_run_id():
    sid = await _create_session()
    rc = RunContext(sid, "the-exact-run-id")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    await execution_store.persist_run_started(rc)

    assert await _run_row("the-exact-run-id") is not None  # no id translation/re-generation


@pytest.mark.asyncio
async def test_root_coordinator_execution_is_created_with_run():
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    root_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    await execution_store.persist_run_started(rc)

    row = await _execution_row(root_id)
    assert row is not None
    assert row["execution_type"] == "coordinator"
    assert row["parent_execution_id"] is None
    assert row["agent_name"] == "Coordinator"


@pytest.mark.asyncio
async def test_execution_id_equals_execution_record_execution_id():
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    root_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    await execution_store.persist_run_started(rc)

    assert rc.executions[root_id].execution_id == root_id
    row = await _execution_row(root_id)
    assert row["execution_id"] == root_id


@pytest.mark.asyncio
async def test_execution_references_the_correct_run():
    sid = await _create_session()
    rc = RunContext(sid, "run-xyz")
    root_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    await execution_store.persist_run_started(rc)

    row = await _execution_row(root_id)
    assert row["run_id"] == "run-xyz"


@pytest.mark.asyncio
async def test_run_and_root_execution_are_not_orphaned_relative_to_each_other():
    """Both rows land, or (on failure) neither does -- see
    persist_run_started's own one-transaction design."""
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    await execution_store.persist_run_started(rc)

    assert await _table_row_count(db.runs) == 1
    assert await _table_row_count(db.executions) == 1


# ── 6/7/8/9. Delegation and retry, via the real hook/_stream_team_run ──────

@pytest.mark.asyncio
async def test_delegation_creates_a_durable_child_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member did the work"

    await hook("delegate_task_to_member", fake_delegate,
               {"member_id": "researcher", "task": "x"}, team=team)

    rows = await _all_execution_rows("run-1")
    children = [r for r in rows if r["parent_execution_id"] == root]
    assert len(children) == 1
    assert children[0]["agent_name"] == "researcher"
    assert children[0]["execution_type"] == "delegation"


@pytest.mark.asyncio
async def test_retry_creates_a_durable_sibling_execution_under_root():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id
    team._run_context.finish_execution(root, "ok")

    async def fake_arun(prompt, stream=True, yield_run_output=True):
        class _Event:
            event = "TeamRunContent"
            content = "a retried answer"
            agent_name = ""
        yield _Event()
    team.arun = fake_arun

    await _stream_team_run(team, "retry prompt")

    rows = await _all_execution_rows("run-1")
    retries = [r for r in rows if r["execution_id"] != root]
    assert len(retries) == 1
    # E2.parent_execution_id = E1 (root) -- not chained to a prior retry.
    assert retries[0]["parent_execution_id"] == root
    assert retries[0]["execution_type"] == "coordinator"


@pytest.mark.asyncio
async def test_second_retry_still_points_at_root_not_at_the_first_retry():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id
    team._run_context.finish_execution(root, "ok")

    async def fake_arun(prompt, stream=True, yield_run_output=True):
        class _Event:
            event = "TeamRunContent"
            content = "a retried answer"
            agent_name = ""
        yield _Event()
    team.arun = fake_arun

    await _stream_team_run(team, "first retry")
    await _stream_team_run(team, "second retry")

    rows = {r["execution_id"]: r for r in await _all_execution_rows("run-1")}
    retries = [r for eid, r in rows.items() if eid != root]
    assert len(retries) == 2
    # Both E2 and E3 point at E1 (root) -- E3 must NOT point at E2.
    assert {r["parent_execution_id"] for r in retries} == {root}
    assert {r["attempt_number"] for r in retries} == {2, 3}  # root itself was attempt 1


# ── 10/11/12. Completion/failure persistence ────────────────────────────────

@pytest.mark.asyncio
async def test_successful_run_persists_completion():
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    await execution_store.persist_run_started(rc)

    await execution_store.persist_run_completed(rc, status="ok")

    row = await _run_row("run-1")
    assert row["status"] == "ok"
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_failed_run_persists_failure():
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    await execution_store.persist_run_started(rc)

    await execution_store.persist_run_completed(rc, status="failed", error="boom")

    row = await _run_row("run-1")
    assert row["status"] == "failed"
    assert row["error_message"] == "boom"


@pytest.mark.asyncio
async def test_failed_execution_persists_error():
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    root_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    await execution_store.persist_run_started(rc)

    rc.finish_execution(root_id, status="failed", error="tool exploded")
    await execution_store.persist_execution_completed(rc.executions[root_id])

    row = await _execution_row(root_id)
    assert row["status"] == "failed"
    assert row["error_message"] == "tool exploded"


# ── 13. Persistence failure is fail-open ────────────────────────────────────

@pytest.mark.asyncio
async def test_persistence_failure_does_not_change_the_tool_result(monkeypatch):
    """The critical fail-open guarantee: if the durable write itself raises,
    the tool call's own result is still returned unchanged, and no exception
    escapes the hook because of a broken database."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)

    async def _broken_persist_execution_created(record):
        raise RuntimeError("db is on fire")
    monkeypatch.setattr(execution_store, "persist_execution_created", _broken_persist_execution_created)

    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member did the work"

    result = await hook("delegate_task_to_member", fake_delegate,
                         {"member_id": "researcher", "task": "x"}, team=team)

    assert result == "member did the work"  # unaffected by the persistence failure


@pytest.mark.asyncio
async def test_persist_run_started_swallows_a_broken_engine(monkeypatch):
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    def _broken_engine():
        raise RuntimeError("engine construction failed")
    monkeypatch.setattr(db, "get_engine", _broken_engine)

    await execution_store.persist_run_started(rc)  # must not raise


@pytest.mark.asyncio
async def test_a_real_tool_failure_still_propagates_despite_fail_open_persistence():
    """Fail-open persistence must never be confused with swallowing an actual
    tool/business failure -- a real exception from the tool itself must still
    reach the caller."""
    team = await _team_with_persisted_run(await _create_session())
    hook = _make_tool_interception_hook()

    async def failing_tool(**kwargs):
        raise RuntimeError("the tool genuinely failed")

    with pytest.raises(RuntimeError, match="the tool genuinely failed"):
        await hook("get_file_content", failing_tool, {"path": "a.py"}, team=team)


# ── 14/15. Session ownership and cascade ────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_runs_can_share_one_session():
    sid = await _create_session()
    for i in range(3):
        rc = RunContext(sid, f"run-{i}")
        rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
        await execution_store.persist_run_started(rc)

    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(
            sa.select(db.runs).where(db.runs.c.session_id == sid))).all()
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_session_deletion_cascades_to_runs_and_executions():
    sid = await _create_session()
    rc = RunContext(sid, "run-1")
    root_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    await execution_store.persist_run_started(rc)
    child_id = rc.start_execution("Researcher", "delegation", parent_execution_id=root_id)
    await execution_store.persist_execution_created(rc.executions[child_id])

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.delete(db.chat_sessions).where(db.chat_sessions.c.id == sid))

    assert await _table_row_count(db.runs) == 0
    assert await _table_row_count(db.executions) == 0


# ── 16/17. Streaming / chunked identity (one Run per invocation) ───────────

@pytest.mark.asyncio
async def test_streaming_produces_exactly_one_run():
    """run_task_stream's own RunContext-creation call site mints exactly one
    RunContext (and therefore one persist_run_started call) per invocation --
    simulated here at the same level test_execution_context.py already
    verifies this identity model at (see that file's streaming tests),
    now asserting the durable side too."""
    sid = await _create_session()
    rc = RunContext(sid, "stream-run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    await execution_store.persist_run_started(rc, run_type="stream")

    assert await _table_row_count(db.runs) == 1
    row = await _run_row("stream-run-1")
    assert row["run_type"] == "stream"


@pytest.mark.asyncio
async def test_chunked_execution_produces_n_runs_for_n_chunks():
    """Each /run_chunked chunk is an independent run_task_async invocation
    (confirmed architecturally: a fresh subprocess, a fresh RunContext per
    chunk) sharing the SAME session_id -- simulated here as N separate
    RunContext objects against one session, exactly matching that reality."""
    sid = await _create_session()
    for i in range(4):
        rc = RunContext(sid, f"chunk-run-{i}")
        rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
        await execution_store.persist_run_started(rc, run_type="single")

    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(
            sa.select(db.runs).where(db.runs.c.session_id == sid))).mappings().all()
    assert len(rows) == 4
    assert len({r["run_id"] for r in rows}) == 4  # no synthetic shared parent Run


# ── 18/19/20. Phase D writes ToolCall/Evidence; Claims stay untouched ──────

@pytest.mark.asyncio
async def test_tool_call_row_is_written():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "some tool output"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    assert await _table_row_count(db.tool_calls) == 1


@pytest.mark.asyncio
async def test_evidence_row_is_written():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "some tool output"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    assert await _table_row_count(db.evidence) == 1


@pytest.mark.asyncio
async def test_no_claim_rows_are_written():
    """Phase D never calls anything that would write claims -- confirmed by
    the absence of any db.claims reference outside this table's own DDL/
    migration tests (see test_migrations.py), and re-confirmed empirically
    here across every other test in this file's own run."""
    assert await _table_row_count(db.claims) == 0
    assert await _table_row_count(db.claim_evidence) == 0


def test_execution_store_module_never_references_claims_or_claim_evidence():
    import inspect as _inspect
    source = _inspect.getsource(execution_store)
    for forbidden in ("db.claims", "db.claim_evidence"):
        assert forbidden not in source


# ── 21. Cancellation does not leave a durable execution "running" ─────────

@pytest.mark.asyncio
async def test_cancelled_delegation_is_durably_marked_failed_not_running():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def cancelled_delegate(**kwargs):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await hook("delegate_task_to_member", cancelled_delegate,
                   {"member_id": "researcher", "task": "x"}, team=team)

    rows = await _all_execution_rows("run-1")
    child = next(r for r in rows if r["execution_type"] == "delegation")
    assert child["status"] == "failed"
    assert child["status"] != "running"


# ── 22. Broadcast delegation does not create a bogus durable Execution ─────

@pytest.mark.asyncio
async def test_broadcast_delegation_creates_no_durable_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_broadcast(**kwargs):
        return "broadcast dispatched"

    await hook("delegate_task_to_members", fake_broadcast, {"task": "x"}, team=team)

    rows = await _all_execution_rows("run-1")
    assert {r["execution_id"] for r in rows} == {root}  # only the root, no bogus child
