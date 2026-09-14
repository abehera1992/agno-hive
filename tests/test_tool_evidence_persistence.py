"""Phase D -- durable ToolCall + exact Evidence persistence
(swarm/execution_store.py's persist_tool_call_created/persist_tool_call_completed,
wired into swarm/team.py's _tool_interception_hook).

Same two-level convention as test_execution_store.py (Phase C):

  * execution_store's own functions/helpers (_canonicalize_result,
    _content_hash, persist_tool_call_created, persist_tool_call_completed),
    exercised directly against a real (in-memory SQLite) database.
  * _tool_interception_hook's actual integration points, driven the same way
    test_execution_context.py/test_execution_store.py already do (a
    lightweight fake `team` carrying a real RunContext), asserting on the
    DURABLE tool_calls/evidence rows those calls produce.

Every DB-touching helper is `async def` and every test is
`@pytest.mark.asyncio async def`, except the migration fixture itself (see
test_migrations.py's documented reasoning for why Alembic's sync command API
cannot run inside pytest-asyncio's own event loop).
"""
import asyncio
import types
import uuid

import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db
from swarm.execution_context import EvidenceRecord, RunContext, ToolCallRecord
from swarm.migrations import run_upgrade
from swarm.team import _make_tool_interception_hook
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


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


async def _tool_call_rows():
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(db.tool_calls))).mappings().all()


async def _evidence_rows():
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(db.evidence))).mappings().all()


async def _team_with_persisted_run(session_id: str, run_id: str = "run-1") -> types.SimpleNamespace:
    team = types.SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    return team


# ── 1-4. _canonicalize_result -----------------------------------------------

def test_canonicalize_str_is_verbatim():
    assert execution_store._canonicalize_result("exact text") == "exact text"


def test_canonicalize_none_is_empty_string():
    assert execution_store._canonicalize_result(None) == ""


def test_canonicalize_unwraps_mcp_style_content_string():
    wrapper = types.SimpleNamespace(content="the real tool text")
    assert execution_store._canonicalize_result(wrapper) == "the real tool text"


def test_canonicalize_unwraps_mcp_style_content_block_list():
    block = types.SimpleNamespace(text="block one")
    wrapper = types.SimpleNamespace(content=[block, types.SimpleNamespace(text="block two")])
    assert execution_store._canonicalize_result(wrapper) == "block one\nblock two"


def test_canonicalize_dict_is_canonical_json_regardless_of_key_order():
    a = execution_store._canonicalize_result({"b": 1, "a": 2})
    b = execution_store._canonicalize_result({"a": 2, "b": 1})
    assert a == b
    assert a == '{"a": 2, "b": 1}'


def test_canonicalize_non_serializable_object_gets_a_deterministic_type_marker():
    def _gen():
        yield 1

    marker1 = execution_store._canonicalize_result(_gen())
    marker2 = execution_store._canonicalize_result(_gen())
    assert marker1 == marker2  # same type -> same marker, not a memory address
    assert "generator" in marker1


# ── 5-7. _content_hash --------------------------------------------------

def test_content_hash_is_deterministic_for_the_same_content():
    assert execution_store._content_hash("same") == execution_store._content_hash("same")


def test_content_hash_differs_for_different_content():
    assert execution_store._content_hash("a") != execution_store._content_hash("b")


def test_content_hash_is_sha256_hex():
    import hashlib
    assert execution_store._content_hash("x") == hashlib.sha256(b"x").hexdigest()


# ── 8-14. persist_tool_call_created / persist_tool_call_completed ----------

@pytest.mark.asyncio
async def test_persist_tool_call_created_mints_a_genuine_uuid_not_the_runtime_id():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    tc = rc.start_tool_call("get_file_content", {"path": "a.py"})

    durable_id = await execution_store.persist_tool_call_created(tc)

    assert durable_id is not None
    assert durable_id != tc.tool_call_id  # NOT execution_context.new_id()'s 12-hex scheme
    uuid.UUID(durable_id)  # raises if not well-formed


@pytest.mark.asyncio
async def test_persist_tool_call_created_row_matches_current_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    root = rc.root_execution_id
    tc = rc.start_tool_call("get_file_content", {"path": "a.py"})

    durable_id = await execution_store.persist_tool_call_created(tc)

    rows = await _tool_call_rows()
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == durable_id
    assert rows[0]["execution_id"] == root
    assert rows[0]["status"] == "running"


@pytest.mark.asyncio
async def test_persist_tool_call_created_returns_none_with_no_current_execution():
    tc = ToolCallRecord(tool_call_id="abc123", execution_id=None, run_id="run-1",
                         tool_name="get_file_content", arguments={})
    assert await execution_store.persist_tool_call_created(tc) is None
    assert await _table_row_count(db.tool_calls) == 0


@pytest.mark.asyncio
async def test_persist_tool_call_completed_updates_status_and_inserts_evidence():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    tc = rc.start_tool_call("get_file_content", {"path": "a.py"})
    durable_id = await execution_store.persist_tool_call_created(tc)
    ev = rc.finish_tool_call(tc, content="the exact result", success=True, error=None)

    await execution_store.persist_tool_call_completed(durable_id, tc, ev)

    rows = await _tool_call_rows()
    assert rows[0]["status"] == "ok"
    assert rows[0]["completed_at"] is not None
    erows = await _evidence_rows()
    assert len(erows) == 1
    assert erows[0]["tool_call_id"] == durable_id
    assert erows[0]["content"] == "the exact result"
    assert erows[0]["content_hash"] == execution_store._content_hash("the exact result")
    assert erows[0]["success"] is True


@pytest.mark.asyncio
async def test_persist_tool_call_completed_is_a_noop_with_no_durable_id():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    tc = rc.start_tool_call("get_file_content", {"path": "a.py"})
    ev = rc.finish_tool_call(tc, content="x", success=True, error=None)

    await execution_store.persist_tool_call_completed(None, tc, ev)  # no raise

    assert await _table_row_count(db.tool_calls) == 0
    assert await _table_row_count(db.evidence) == 0


@pytest.mark.asyncio
async def test_evidence_never_references_a_nonexistent_tool_call():
    """FK enforcement: evidence.tool_call_id must reference a real row.
    persist_tool_call_completed only ever inserts evidence for a
    durable_tool_call_id it (or its caller) already created, so this is a
    structural guarantee, verified here against the real FK."""
    sid = await _create_session()
    with pytest.raises(Exception):
        async with db.get_engine().begin() as conn:
            await conn.execute(db.evidence.insert().values(
                evidence_id=str(uuid.uuid4()),
                tool_call_id=str(uuid.uuid4()),  # no matching tool_calls row
                content="x", content_hash="x", success=True,
            ))


@pytest.mark.asyncio
async def test_two_tool_calls_get_two_distinct_durable_ids():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    tc1 = rc.start_tool_call("get_file_content", {"path": "a.py"})
    tc2 = rc.start_tool_call("get_file_content", {"path": "b.py"})

    id1 = await execution_store.persist_tool_call_created(tc1)
    id2 = await execution_store.persist_tool_call_created(tc2)

    assert id1 != id2
    assert await _table_row_count(db.tool_calls) == 2


# ── 15-18. _tool_interception_hook integration: ordinary tool calls --------

@pytest.mark.asyncio
async def test_hook_persists_a_tool_call_and_evidence_row_on_success():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "the exact tool output"

    result = await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    assert result == "the exact tool output"
    rows = await _tool_call_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    erows = await _evidence_rows()
    assert erows[0]["content"] == "the exact tool output"
    assert erows[0]["success"] is True


@pytest.mark.asyncio
async def test_hook_persists_a_failed_tool_call_and_evidence_row():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def failing_tool(**kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await hook("get_file_content", failing_tool, {"path": "a.py"}, team=team)

    rows = await _tool_call_rows()
    assert rows[0]["status"] == "error"
    assert rows[0]["error_message"] == "boom"
    erows = await _evidence_rows()
    assert erows[0]["success"] is False


@pytest.mark.asyncio
async def test_hook_never_reconstructs_evidence_from_a_string_preview():
    """Regression guard for the exact-result-fidelity contract: a long,
    exact MCP-wrapper-shaped result is stored in full, not truncated to
    whatever a 200-char preview elsewhere in the hook uses."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()
    long_text = "x" * 5000

    async def fake_tool(**kwargs):
        return types.SimpleNamespace(content=long_text)

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    erows = await _evidence_rows()
    assert erows[0]["content"] == long_text


@pytest.mark.asyncio
async def test_hook_tool_call_row_execution_id_matches_current_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "ok"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    rows = await _tool_call_rows()
    assert rows[0]["execution_id"] == root


# ── 19-20. Delegation attribution -------------------------------------------

@pytest.mark.asyncio
async def test_delegate_tool_call_is_attributed_to_the_caller_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "delegated"

    await hook("delegate_task_to_member", fake_delegate,
               {"member_id": "researcher", "task": "x"}, team=team)

    rows = await _tool_call_rows()
    assert len(rows) == 1
    assert rows[0]["execution_id"] == root  # the caller, not the new child


@pytest.mark.asyncio
async def test_broadcast_delegation_still_gets_an_ordinary_tool_call_row():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_broadcast(**kwargs):
        return "broadcast dispatched"

    await hook("delegate_task_to_members", fake_broadcast, {"task": "x"}, team=team)

    rows = await _tool_call_rows()
    assert len(rows) == 1
    assert rows[0]["execution_id"] == root


# ── 21. Retry attribution -----------------------------------------------

@pytest.mark.asyncio
async def test_retry_tool_calls_attach_to_the_retry_execution_not_root():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    root = rc.root_execution_id
    retry_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=root)
    await execution_store.persist_execution_created(rc.executions[retry_id])
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "retry output"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    rows = await _tool_call_rows()
    assert rows[0]["execution_id"] == retry_id
    assert rows[0]["execution_id"] != root


# ── 22-23. Cancellation ---------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_tool_call_is_durably_marked_not_running():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def cancelled_tool(**kwargs):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await hook("get_file_content", cancelled_tool, {"path": "a.py"}, team=team)

    rows = await _tool_call_rows()
    assert len(rows) == 1
    assert rows[0]["status"] != "running"
    erows = await _evidence_rows()
    assert len(erows) == 1  # cancellation still produces a durable Evidence row


@pytest.mark.asyncio
async def test_cancelled_tool_call_still_propagates_cancelled_error():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def cancelled_tool(**kwargs):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await hook("get_file_content", cancelled_tool, {"path": "a.py"}, team=team)


# ── 24-26. Fail-open at the runtime call site --------------------------------

@pytest.mark.asyncio
async def test_persist_tool_call_created_failure_does_not_change_the_tool_result(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def _boom(*a, **k):
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "persist_tool_call_created", _boom)

    async def fake_tool(**kwargs):
        return "unaffected result"

    result = await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)
    assert result == "unaffected result"


@pytest.mark.asyncio
async def test_persist_tool_call_completed_failure_does_not_swallow_a_real_tool_error(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def _boom(*a, **k):
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "persist_tool_call_completed", _boom)

    async def failing_tool(**kwargs):
        raise RuntimeError("the real tool error")

    with pytest.raises(RuntimeError, match="the real tool error"):
        await hook("get_file_content", failing_tool, {"path": "a.py"}, team=team)


@pytest.mark.asyncio
async def test_guard_returns_none_on_failure_and_the_value_on_success():
    async def ok():
        return "value"

    async def bad():
        raise RuntimeError("x")

    assert await execution_store.guard(ok()) == "value"
    assert await execution_store.guard(bad()) is None


# ── 27. No database read-back ------------------------------------------------

def test_tool_interception_hook_never_selects_from_tool_calls_or_evidence():
    """Phase D is write-only during execution -- the hook must never read
    the rows it just wrote back into runtime/prompt state."""
    import inspect
    import swarm.team as team_mod
    source = inspect.getsource(team_mod._make_tool_interception_hook)
    assert "select" not in source.lower().replace("selected", "")


# ── 28. Session cascade reaches tool_calls/evidence --------------------------

@pytest.mark.asyncio
async def test_deleting_the_session_cascades_through_to_tool_calls_and_evidence():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "cascade me"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)
    assert await _table_row_count(db.tool_calls) == 1
    assert await _table_row_count(db.evidence) == 1

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        await conn.execute(db.chat_sessions.delete().where(db.chat_sessions.c.id == sid))

    assert await _table_row_count(db.tool_calls) == 0
    assert await _table_row_count(db.evidence) == 0
