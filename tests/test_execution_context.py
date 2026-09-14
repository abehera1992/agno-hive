"""Phase A/C (durable execution/evidence backbone). Covers:

  * RunContext itself (pure, no team/agno involved) -- Phase A, in-memory only
  * _tool_interception_hook's ToolCall/Evidence capture (in-memory, still
    Phase A -- ToolCall/Evidence are explicitly NOT persisted, see Phase C's
    swarm/execution_store.py) and delegation -> child-Execution wiring
  * _stream_team_run's retry -> sibling-Execution wiring
  * Phase C's Run/Execution DURABLE persistence, wired at the exact same
    points listed above -- a fake `team` carrying a real RunContext will now
    also attempt real (fail-open) database writes via swarm/execution_store.py,
    so this file points config.database_url at an isolated in-memory SQLite
    database for its own duration, exactly like tests/test_migrations.py
    does, rather than letting those writes reach the default on-disk
    data/agnohive.db.

Follows this suite's existing convention (see test_team_tool_interception_hook.py,
test_relay_drop_reconciliation.py) of driving the hook/function directly with a
lightweight fake `team`, rather than building a real agno Team.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.execution_context as ec
import swarm.team as team_mod
from config.config import config
from swarm import db


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """Every test in this file may exercise a code path that now attempts a
    real (fail-open) database write -- see this module's own docstring.
    Points at an isolated in-memory SQLite database, migrated to head, so
    those writes land somewhere real and inspectable rather than silently
    failing against (or worse, touching) the default on-disk database."""
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    asyncio.run(db.reset_engine_for_tests())
    from swarm.migrations import run_upgrade
    run_upgrade("head")
    yield
from swarm.execution_context import RunContext
from swarm.team import _make_tool_interception_hook, _stream_team_run


# ── RunContext: pure unit tests, no team/agno involved ──────────────────────

def test_root_execution_has_null_parent_and_is_recorded_as_root():
    rc = RunContext("session-1", "run-1")
    exec_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    assert rc.executions[exec_id].parent_execution_id is None
    assert rc.root_execution_id == exec_id


def test_child_execution_records_the_given_parent():
    rc = RunContext("session-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    rc.finish_execution(root, "ok")
    child = rc.start_execution("Researcher", "delegation", parent_execution_id=root)

    assert rc.executions[child].parent_execution_id == root
    # A delegation never becomes "the root" even if started with no other root set.
    assert rc.root_execution_id == root


def test_attempt_number_increments_for_the_same_parent_and_agent():
    rc = RunContext("session-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    a1 = rc.start_execution("Researcher", "delegation", parent_execution_id=root)
    a2 = rc.start_execution("Researcher", "delegation", parent_execution_id=root)

    assert rc.executions[a1].attempt_number == 1
    assert rc.executions[a2].attempt_number == 2
    assert a1 != a2


def test_attempt_number_is_independent_per_agent():
    rc = RunContext("session-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    r1 = rc.start_execution("Researcher", "delegation", parent_execution_id=root)
    c1 = rc.start_execution("Coder", "delegation", parent_execution_id=root)

    assert rc.executions[r1].attempt_number == 1
    assert rc.executions[c1].attempt_number == 1


def test_a_coordinator_retry_is_a_new_execution_not_a_mutation():
    """A retry (a second 'coordinator' execution parented to the root) must get
    its OWN execution_id and attempt_number 2 -- never overwrite the original."""
    rc = RunContext("session-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    rc.finish_execution(root, "ok")
    retry = rc.start_execution("Coordinator", "coordinator", parent_execution_id=root)

    assert retry != root
    assert rc.executions[retry].attempt_number == 2
    assert rc.executions[root].status == "ok"          # untouched by the retry
    assert rc.executions[retry].parent_execution_id == root


def test_tool_call_ids_are_unique_within_a_run():
    rc = RunContext("session-1", "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    tc1 = rc.start_tool_call("get_file_content", {"path": "a.py"})
    tc2 = rc.start_tool_call("get_file_content", {"path": "b.py"})

    assert tc1.tool_call_id != tc2.tool_call_id
    assert len({t.tool_call_id for t in rc.tool_calls}) == 2


def test_tool_call_is_attributed_to_the_currently_open_execution():
    rc = RunContext("session-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    tc_root = rc.start_tool_call("delegate_task_to_member", {"member_id": "researcher"})
    child = rc.start_execution("Researcher", "delegation", parent_execution_id=root)
    tc_child = rc.start_tool_call("get_file_content", {"path": "a.py"})

    assert tc_root.execution_id == root
    assert tc_child.execution_id == child


def test_finish_tool_call_records_the_exact_content_and_run_relationship():
    rc = RunContext("session-1", "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    exact_result = {"lines": ["a", "b"], "marker": object()}
    tc = rc.start_tool_call("get_file_content", {"path": "a.py"})
    ev = rc.finish_tool_call(tc, content=exact_result, success=True, error=None)

    assert ev.content is exact_result       # identity, not just equality
    assert ev.tool_call_id == tc.tool_call_id
    assert ev.run_id == "run-1"
    assert ev.success is True
    assert ev.error is None
    assert rc.evidence[-1] is ev


def test_finish_tool_call_records_failure_with_no_content():
    rc = RunContext("session-1", "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    tc = rc.start_tool_call("apply_diff", {"path": "a.py"})
    ev = rc.finish_tool_call(tc, content=None, success=False, error="boom")

    assert ev.success is False
    assert ev.error == "boom"
    assert tc.status == "error"


def test_two_run_contexts_never_share_state():
    """No global/process-wide identity state -- two RunContexts, even with
    identical inputs, are fully independent objects."""
    rc1 = RunContext("session-1", "run-A")
    rc2 = RunContext("session-1", "run-B")
    e1 = rc1.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    e2 = rc2.start_execution("Coordinator", "coordinator", parent_execution_id=None)

    assert e1 != e2
    assert e1 not in rc2.executions
    assert e2 not in rc1.executions
    assert rc1.run_id != rc2.run_id


def test_new_id_values_are_unique():
    ids = {ec.new_id() for _ in range(200)}
    assert len(ids) == 200


# ── _tool_interception_hook: ToolCall/Evidence capture ──────────────────────

def _fake_team(session_id="sess-1", run_id="run-1"):
    team = SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    return team


@pytest.mark.asyncio
async def test_hook_gives_every_call_a_tool_call_id_and_run_id():
    team = _fake_team(run_id="run-42")
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "ok"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    rc = team._run_context
    assert len(rc.tool_calls) == 1
    tc = rc.tool_calls[0]
    assert tc.tool_call_id
    assert tc.run_id == "run-42"
    assert tc.execution_id == rc.root_execution_id


@pytest.mark.asyncio
async def test_hook_returns_the_exact_same_result_object_the_tool_produced():
    """Byte/content faithfulness: the hook must hand back the identical object,
    not a copy, a summary, or a stringified version."""
    team = _fake_team()
    hook = _make_tool_interception_hook()
    sentinel = {"raw": "x" * 5000, "marker": object()}

    async def fake_tool(**kwargs):
        return sentinel

    returned = await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    assert returned is sentinel


@pytest.mark.asyncio
async def test_evidence_receives_the_exact_tool_output():
    team = _fake_team()
    hook = _make_tool_interception_hook()
    exact = "line1\nline2\n" * 1000  # large-ish, unmodified text

    async def fake_tool(**kwargs):
        return exact

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    ev = team._run_context.evidence[-1]
    assert ev.content == exact
    assert ev.content is exact
    assert ev.success is True


@pytest.mark.asyncio
async def test_evidence_is_recorded_on_a_failing_tool_call_too():
    team = _fake_team()
    hook = _make_tool_interception_hook()

    async def failing_tool(**kwargs):
        raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError):
        await hook("apply_diff", failing_tool, {"path": "a.py"}, team=team)

    ev = team._run_context.evidence[-1]
    assert ev.success is False
    assert "disk on fire" in ev.error
    assert ev.content is None


@pytest.mark.asyncio
async def test_delegate_call_creates_a_child_execution_under_the_current_one():
    team = _fake_team()
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member did the work"

    await hook("delegate_task_to_member", fake_delegate,
               {"member_id": "researcher", "task": "look into X"}, team=team)

    rc = team._run_context
    children = [e for e in rc.executions.values() if e.parent_execution_id == root]
    assert len(children) == 1
    assert children[0].execution_type == "delegation"
    assert children[0].agent_name == "researcher"
    assert children[0].attempt_number == 1
    assert children[0].status == "ok"


@pytest.mark.asyncio
async def test_the_delegate_tool_call_itself_is_attributed_to_the_caller_not_the_child():
    team = _fake_team()
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member did the work"

    await hook("delegate_task_to_member", fake_delegate,
               {"member_id": "researcher", "task": "look into X"}, team=team)

    rc = team._run_context
    delegate_tool_call = rc.tool_calls[0]
    assert delegate_tool_call.execution_id == root  # not the new child


@pytest.mark.asyncio
async def test_a_failed_delegation_closes_its_child_execution_as_failed():
    team = _fake_team()
    hook = _make_tool_interception_hook()

    async def failing_delegate(**kwargs):
        raise RuntimeError("member crashed")

    with pytest.raises(RuntimeError):
        await hook("delegate_task_to_member", failing_delegate,
                   {"member_id": "researcher", "task": "x"}, team=team)

    rc = team._run_context
    child = next(e for e in rc.executions.values() if e.execution_type == "delegation")
    assert child.status == "failed"
    assert "member crashed" in child.error
    # The stack must not be left with the failed child still "current".
    assert rc.current_execution_id == rc.root_execution_id


@pytest.mark.asyncio
async def test_a_non_delegate_call_never_creates_a_new_execution():
    team = _fake_team()
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "ok"

    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)

    assert set(team._run_context.executions.keys()) == {root}


@pytest.mark.asyncio
async def test_hook_still_works_with_no_run_context_on_team_at_all():
    """Backward compatibility: every pre-existing test that calls the hook with
    team=None (the default) or a team lacking _run_context must be unaffected."""
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "ok"

    result = await hook("get_file_content", fake_tool, {"path": "a.py"})
    assert result == "ok"

    team_without_context = SimpleNamespace()
    result2 = await hook("get_file_content", fake_tool, {"path": "a.py"},
                          team=team_without_context)
    assert result2 == "ok"


# ── _stream_team_run: retries create sibling Executions under the same run ──

class _FakeEvent:
    def __init__(self, event, content=None):
        self.event = event
        self.content = content
        self.agent_name = ""


async def _fake_arun_yields_text(prompt, stream=True, yield_run_output=True):
    yield _FakeEvent("TeamRunContent", content="a grounded retry answer")


@pytest.mark.asyncio
async def test_stream_team_run_creates_an_execution_under_the_run_root(monkeypatch):
    team = SimpleNamespace()
    rc = RunContext("sess-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    rc.finish_execution(root, "ok")
    team._run_context = rc
    team.arun = _fake_arun_yields_text

    await _stream_team_run(team, "retry prompt")

    retries = [e for e in rc.executions.values() if e.execution_id != root]
    assert len(retries) == 1
    assert retries[0].parent_execution_id == root
    assert retries[0].run_id == "run-1"
    assert retries[0].status == "ok"


@pytest.mark.asyncio
async def test_two_stream_team_run_calls_create_two_distinct_executions(monkeypatch):
    team = SimpleNamespace()
    rc = RunContext("sess-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    rc.finish_execution(root, "ok")
    team._run_context = rc
    team.arun = _fake_arun_yields_text

    await _stream_team_run(team, "first retry")
    await _stream_team_run(team, "second retry")

    retries = [e for e in rc.executions.values() if e.execution_id != root]
    assert len(retries) == 2
    assert retries[0].execution_id != retries[1].execution_id
    assert {r.attempt_number for r in retries} == {2, 3}  # root itself was attempt 1
    assert all(r.run_id == "run-1" for r in retries)       # same run throughout


@pytest.mark.asyncio
async def test_stream_team_run_marks_its_execution_failed_on_a_backend_error():
    team = SimpleNamespace()
    rc = RunContext("sess-1", "run-1")
    root = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    rc.finish_execution(root, "ok")
    team._run_context = rc

    async def _raising_arun(prompt, stream=True, yield_run_output=True):
        raise RuntimeError("vLLM connection dropped")
        yield  # pragma: no cover - make this an async generator

    team.arun = _raising_arun

    with pytest.raises(RuntimeError):
        await _stream_team_run(team, "retry prompt")

    retries = [e for e in rc.executions.values() if e.execution_id != root]
    assert len(retries) == 1
    assert retries[0].status == "failed"


@pytest.mark.asyncio
async def test_stream_team_run_still_works_with_no_run_context_on_team():
    team = SimpleNamespace()
    team.arun = _fake_arun_yields_text

    content, run_output = await _stream_team_run(team, "retry prompt")

    assert content == "a grounded retry answer"


# ── canonical run_id selection (Phase0Run.run_id when present, fresh id otherwise) ──

def test_run_context_uses_the_given_run_id_verbatim():
    """Exercises the same selection run_task_async/run_task_stream perform:
    `_phase0.run_id if _phase0 is not None else execution_context.new_id()`."""
    fake_phase0 = SimpleNamespace(run_id="abc123def456")
    run_id = fake_phase0.run_id if fake_phase0 is not None else ec.new_id()
    rc = RunContext("sess-1", run_id)

    assert rc.run_id == "abc123def456"


def test_run_context_gets_a_fresh_id_when_phase0_is_absent():
    _phase0 = None
    run_id_1 = _phase0.run_id if _phase0 is not None else ec.new_id()
    run_id_2 = _phase0.run_id if _phase0 is not None else ec.new_id()

    assert run_id_1 != run_id_2   # every invocation without telemetry still gets its own id


# ── Phase A finding A (fixed for Phase C): broadcast delegation ─────────────

@pytest.mark.asyncio
async def test_broadcast_delegation_does_not_create_a_bogus_empty_agent_execution():
    """delegate_task_to_members (plural, agno's broadcast tool) must not match
    the same handling as delegate_task_to_member (singular) -- it carries no
    member_id at all, so treating it as a delegation created a child Execution
    with an empty/meaningless agent_name."""
    team = _fake_team()
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_broadcast(**kwargs):
        return "broadcast dispatched"

    await hook("delegate_task_to_members", fake_broadcast,
               {"task": "look into X"}, team=team)

    rc = team._run_context
    # No new execution was created -- the broadcast call is still an ordinary
    # ToolCall on the calling (root) execution, just not a delegation with its
    # own child.
    assert set(rc.executions.keys()) == {root}
    assert rc.tool_calls[0].execution_id == root


@pytest.mark.asyncio
async def test_singular_delegation_is_unaffected_by_the_broadcast_fix():
    """Regression guard: fixing the prefix match to an exact match must not
    stop matching the real, singular delegate_task_to_member call."""
    team = _fake_team()
    root = team._run_context.root_execution_id
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member did the work"

    await hook("delegate_task_to_member", fake_delegate,
               {"member_id": "researcher", "task": "look into X"}, team=team)

    rc = team._run_context
    children = [e for e in rc.executions.values() if e.parent_execution_id == root]
    assert len(children) == 1
    assert children[0].agent_name == "researcher"


# ── Phase A finding B (fixed for Phase C): CancelledError status accounting ─

@pytest.mark.asyncio
async def test_cancelled_delegation_is_marked_failed_not_ok():
    """asyncio.CancelledError is a BaseException, not caught by the hook's own
    `except Exception` -- before the fix, the finally block unconditionally
    marked a cancelled delegation's execution "ok". sys.exc_info() must now
    see the in-flight cancellation and mark it "failed" instead."""
    import asyncio as _asyncio

    team = _fake_team()
    hook = _make_tool_interception_hook()

    async def cancelled_delegate(**kwargs):
        raise _asyncio.CancelledError()

    with pytest.raises(_asyncio.CancelledError):
        await hook("delegate_task_to_member", cancelled_delegate,
                   {"member_id": "researcher", "task": "x"}, team=team)

    rc = team._run_context
    child = next(e for e in rc.executions.values() if e.execution_type == "delegation")
    assert child.status == "failed"
    # The stack must not be left with the cancelled child still "current".
    assert rc.current_execution_id == rc.root_execution_id


@pytest.mark.asyncio
async def test_a_successful_delegation_is_still_marked_ok_after_the_fix():
    """Regression guard: the sys.exc_info()-based finally must not start
    marking ordinary successful delegations as failed."""
    team = _fake_team()
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member did the work"

    await hook("delegate_task_to_member", fake_delegate,
               {"member_id": "researcher", "task": "x"}, team=team)

    rc = team._run_context
    child = next(e for e in rc.executions.values() if e.execution_type == "delegation")
    assert child.status == "ok"
