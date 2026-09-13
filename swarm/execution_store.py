"""Phase C -- durable persistence for Run and Execution ONLY.

LLM messages remain a view of execution state; this module is what makes the
underlying execution state itself durable, in the Phase B schema
(swarm/db.py's `runs`/`executions` tables), mirroring Phase A's in-memory
runtime identity (swarm/execution_context.py's RunContext/ExecutionRecord)
without changing anything about how that identity is computed.

Deliberately narrow: no ToolCall/Evidence/Claim persistence here (Phase A's
ToolCallRecord/EvidenceRecord remain in-memory only, exactly as before) -- see
this module's own test suite for an explicit regression guard on that
boundary.

Fail-open by design, and this is the one thing every function here must never
violate: a persistence failure is logged and swallowed, never raised,
because runtime execution (delegation, retries, tool calls, the final answer)
must continue identically whether or not the database is reachable. This
mirrors the exact try/except-and-print convention already used throughout
swarm/sessions.py and swarm/feedback.py for the same reason -- not a new
failure-handling idiom, the established one.

Identity is never re-derived or re-generated here: every run_id/execution_id
written to the database is read verbatim from the RunContext/ExecutionRecord
objects Phase A already created -- this module has no id-generation logic of
its own, by design (Phase A's execution_context.new_id() remains the only
minting point).
"""
from __future__ import annotations

from typing import Awaitable

from sqlalchemy.sql import func

from swarm import db
from swarm.db import get_engine
from swarm.execution_context import ExecutionRecord, RunContext


async def guard(awaitable: Awaitable[None]) -> None:
    """Defense-in-depth wrapper every swarm/team.py call site uses around
    every function in this module. Each persist_* function below already has
    its own internal try/except (the realistic failure mode: a DB connection
    error, an IntegrityError, etc.) -- this is the LAST-RESORT layer that
    guarantees nothing from this module can ever propagate into team.py's own
    control flow even if that inner handling has a bug, is bypassed by a
    future refactor, or (as one of this module's own tests demonstrated) a
    caller replaces a persist_* function with something that doesn't have
    the same protection. Usage: `await execution_store.guard(execution_store.
    persist_run_started(run_context))` -- the call itself only builds a
    coroutine object; nothing runs until `guard` awaits it here, inside the
    try.
    """
    try:
        await awaitable
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] unexpected failure escaped a persist_* "
              f"call -- swallowed here as the last line of defense: "
              f"{type(exc).__name__}: {exc}", flush=True)


async def persist_run_started(
    run_context: RunContext,
    *,
    team_name: str | None = None,
    run_type: str | None = None,
    task_preview: str | None = None,
) -> None:
    """INSERT the `runs` row AND its root coordinator `executions` row in ONE
    transaction, so a persisted execution can never reference a run that does
    not exist, and a persisted run can never be missing its root execution
    because the second insert failed independently -- either both land or
    neither does.

    Call once, immediately after RunContext.start_execution() has created the
    root execution in memory (see swarm/team.py's run_task_async/
    run_task_stream) -- reads run_context.root_execution_id/executions for
    the row to persist rather than taking those fields as separate arguments,
    so the durable row can never drift from what the in-memory object says.

    Silently skipped (not an error) when run_context.session_id is None --
    runs.session_id is NOT NULL with a real FK to chat_sessions.id, and a
    caller with no session (e.g. main.py's bare CLI one-shot path, which
    calls run_task_async with no session_id at all) has nothing for a durable
    Run to attach to. This is an expected, legitimate case, not a failure.
    """
    root_id = run_context.root_execution_id
    root = run_context.executions.get(root_id) if root_id else None
    if root is None:
        print(f"[execution_store] persist_run_started: no root execution on "
              f"run_id={run_context.run_id!r} -- skipping", flush=True)
        return
    if run_context.session_id is None:
        return
    try:
        async with get_engine().begin() as conn:
            await conn.execute(db.runs.insert().values(
                run_id=run_context.run_id,
                session_id=run_context.session_id,
                run_type=run_type,
                team_name=team_name,
                task_preview=task_preview,
                status="running",
            ))
            await conn.execute(db.executions.insert().values(
                execution_id=root.execution_id,
                run_id=root.run_id,
                parent_execution_id=root.parent_execution_id,
                agent_name=root.agent_name,
                execution_type=root.execution_type,
                attempt_number=root.attempt_number,
                status=root.status,
            ))
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_run_started failed "
              f"(run_id={run_context.run_id!r}): {type(exc).__name__}: {exc}",
              flush=True)


async def persist_run_completed(
    run_context: RunContext, status: str, error: str | None = None,
) -> None:
    """UPDATE the `runs` row's status/error/completed_at. Called exactly once,
    at the true end of run_task_async/run_task_stream (their outermost
    finally, which runs regardless of which return/yield path was taken or
    whether an exception propagated) -- deliberately NOT tied to the root
    execution's own completion, since retries can still be running (as new
    sibling Executions under the same Run) after the root execution's own
    attempt has already finished.

    A no-op (not an error) if persist_run_started() never ran or failed for
    this run_id -- the UPDATE simply affects zero rows, which is the correct,
    silent behavior: this function does not fabricate a Run row that was
    never created.
    """
    if run_context.session_id is None:
        return
    try:
        async with get_engine().begin() as conn:
            await conn.execute(
                db.runs.update()
                .where(db.runs.c.run_id == run_context.run_id)
                .values(status=status, error_message=error, completed_at=func.now())
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_run_completed failed "
              f"(run_id={run_context.run_id!r}): {type(exc).__name__}: {exc}",
              flush=True)


async def persist_execution_created(record: ExecutionRecord) -> None:
    """INSERT one `executions` row from an already-created in-memory
    ExecutionRecord -- called for every delegation and every retry, right
    after RunContext.start_execution() creates it (swarm/team.py's
    _tool_interception_hook and _stream_team_run). Never called for the root
    execution, which is created together with its Run by
    persist_run_started() so the two can share one transaction.

    A no-op (not raised) if the parent run/execution row does not exist in
    the database -- the INSERT's own FK constraint rejects it, caught and
    logged like any other persistence failure; the in-memory execution and
    the rest of the run are completely unaffected.
    """
    try:
        async with get_engine().begin() as conn:
            await conn.execute(db.executions.insert().values(
                execution_id=record.execution_id,
                run_id=record.run_id,
                parent_execution_id=record.parent_execution_id,
                agent_name=record.agent_name,
                execution_type=record.execution_type,
                attempt_number=record.attempt_number,
                status=record.status,
            ))
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_execution_created failed "
              f"(execution_id={record.execution_id!r}, run_id={record.run_id!r}): "
              f"{type(exc).__name__}: {exc}", flush=True)


async def persist_execution_completed(record: ExecutionRecord) -> None:
    """UPDATE the `executions` row's status/error/completed_at from an
    already-finished in-memory ExecutionRecord (i.e. called AFTER
    RunContext.finish_execution() has updated it) -- for the root execution
    (in run_task_async's/run_task_stream's own finally blocks), delegations,
    and retries alike. A no-op if the row was never created (the same
    fail-open reasoning as persist_execution_created)."""
    try:
        async with get_engine().begin() as conn:
            await conn.execute(
                db.executions.update()
                .where(db.executions.c.execution_id == record.execution_id)
                .values(status=record.status, error_message=record.error,
                        completed_at=func.now())
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_execution_completed failed "
              f"(execution_id={record.execution_id!r}): {type(exc).__name__}: {exc}",
              flush=True)
