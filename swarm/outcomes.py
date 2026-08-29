"""Durable handoff for post-run task outcomes.

WHY THIS EXISTS
---------------
`record_success_bg()` scheduled experience indexing with `asyncio.create_task()`. Every
run executes in an ephemeral worker subprocess (`main.py --run-worker`), which ends with
`asyncio.run(_run_worker())` -- and asyncio.run() cancels pending tasks when its
coroutine returns. The indexing task was destroyed microseconds after creation, every
run, in a process that immediately exited.

`drain_background_tasks()` was written for exactly this and is correct, but it is
registered on the SERVER process's shutdown hook -- a different process from the worker,
so it never ran for the tasks it was meant to protect.

The result: recording ran on every completed task, logged nothing, and wrote nothing.
Combined with a separate filename-dedupe bug, `ekam_experience` took its last successful
insert on 2026-07-09 and accumulated 1,221 rejected rows before anyone looked.

WHY A TABLE AND NOT AN INLINE AWAIT
-----------------------------------
Indexing calls vllm-extract for entity extraction: 30-60s. Awaiting it in the worker
would put that on every /run response, which is what the fire-and-forget design was
avoiding in the first place. The worker must HAND OFF the work rather than do it or
promise it. An INSERT is ~1ms and, once committed, outlives the process that wrote it.

WHY A SINK INTERFACE
--------------------
So the worker's call site does not name a transport. There is exactly ONE implementation
today and it writes a row to the database already running on ZGX -- no broker, no new
container, no new dependency. If several agno-api instances ever feed one shared corpus,
a KafkaOutcomeSink slots in behind the same `publish()` and the run path does not change.
This is the same seam the project already used when _VLLM_MODEL_MAP became DB-backed
model_catalog behind get_model().

The Postgres sink is not a stepping stone to be discarded: agno-hive defaults to
`sqlite:///data/agnohive.db` so adopters need zero provisioning, so a no-broker path has
to exist permanently regardless of what else is added.
"""

from __future__ import annotations

import hashlib
import re

import sqlalchemy as sa

from . import db

_WS_RE = re.compile(r"\s+")


def task_hash(task: str) -> str:
    """Dedupe key for a task: sha256 of its whitespace-normalised, lowercased text.

    Normalising means the same question posted twice with different wrapping or casing
    is recognised as the same task. Deliberately EXACT beyond that -- a genuinely
    reworded task is a different row, and deciding how similar is "the same" is a
    judgement this must not make silently. Exact-match dedupe removes the redundancy we
    can prove (400 docs for 310 tasks, one repeated fourteen times); near-duplicates are
    left visible rather than merged by a similarity threshold nobody chose.
    """
    return hashlib.sha256(_WS_RE.sub(" ", (task or "").strip().lower()).encode()).hexdigest()

# Give up on a row after this many failed drains. Bounded so one poisoned outcome (an
# extraction model that rejects it, a malformed result) cannot be retried forever and
# starve the rest of the queue behind it.
MAX_ATTEMPTS = 3

# Rows per drain pass. Small on purpose: each one costs a vllm-extract call, and the
# loop runs again shortly. A big batch would hold the extraction model for minutes and
# compete with live runs for the same GPU.
DRAIN_BATCH = 5


class OutcomeSink:
    """Where a finished, guard-clean task outcome goes to be recorded.

    One method, deliberately. The caller has an outcome and wants it durably accepted;
    everything about HOW is the implementation's business.
    """

    async def publish(self, task: str, result: str, project_id: str,
                      owner: str | None = None) -> None:
        raise NotImplementedError


class PostgresOutcomeSink(OutcomeSink):
    """Writes the outcome to `task_outcome_queue` in the main application database.

    Named for the deployment that matters here, but it is engine-agnostic: the same code
    runs against the default SQLite file, which is what keeps a broker-free install
    viable.

    publish() is intentionally the whole of the worker's involvement. It does no
    indexing, contacts no model, and holds no state that has to survive -- the row is
    committed before it returns, and the worker is free to exit immediately.
    """

    async def publish(self, task: str, result: str, project_id: str,
                      owner: str | None = None) -> None:
        try:
            key = task_hash(task)
            async with db.get_engine().begin() as conn:
                existing = (await conn.execute(
                    sa.select(db.task_outcome_queue.c.id, db.task_outcome_queue.c.doc_path)
                    .where(db.task_outcome_queue.c.project_id == project_id,
                           db.task_outcome_queue.c.task_hash == key)
                )).first()
                if existing:
                    # REPLACE, never accumulate. A second verified answer for the same
                    # task supersedes the first -- that is the point of re-verifying.
                    # status back to 'pending' so the drain loop re-indexes; doc_path is
                    # preserved so it can delete the superseded LightRAG doc first.
                    await conn.execute(
                        sa.update(db.task_outcome_queue)
                        .where(db.task_outcome_queue.c.id == existing.id)
                        .values(task=task, result=result, owner=owner,
                                status="pending", attempts=0, error_message=None,
                                updated_at=sa.func.now())
                    )
                    print(f"[outcomes] replaced outcome #{existing.id} for this task "
                          f"(same project + task hash) — not adding a duplicate",
                          flush=True)
                else:
                    await conn.execute(db.task_outcome_queue.insert().values(
                        project_id=project_id, task=task, result=result,
                        owner=owner, status="pending", attempts=0, task_hash=key,
                    ))
        except Exception as exc:
            # Never let bookkeeping break a completed run: the answer is already correct
            # and already on its way back to the caller. Loud, because the whole reason
            # this module exists is that the previous failure mode was silent.
            print(f"[outcomes] publish failed — outcome NOT queued: {exc}", flush=True)


_sink: OutcomeSink | None = None


def get_sink() -> OutcomeSink:
    """The process-wide sink. Swapping transports later is a change here, nowhere else."""
    global _sink
    if _sink is None:
        _sink = PostgresOutcomeSink()
    return _sink


async def claim_pending(limit: int = DRAIN_BATCH) -> list[dict]:
    """Oldest pending outcomes, marked `processing` so a second drainer skips them.

    The claim is a single UPDATE ... RETURNING where the dialect supports it, so two
    concurrent drainers cannot hand the same row to the extraction model twice. On
    SQLite (single writer, one process) the select-then-update below is equivalent.
    """
    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(
            sa.select(db.task_outcome_queue)
            .where(db.task_outcome_queue.c.status == "pending")
            .order_by(db.task_outcome_queue.c.created_at)
            .limit(limit)
        )).mappings().all()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        await conn.execute(
            sa.update(db.task_outcome_queue)
            .where(db.task_outcome_queue.c.id.in_(ids))
            .values(status="processing", updated_at=sa.func.now())
        )
        return [dict(r) for r in rows]


async def mark_done(row_id: int, doc_path: str | None = None) -> None:
    """Mark indexed, and remember WHICH LightRAG doc this row produced.

    doc_path is what lets the next verification of the same task delete the answer it
    supersedes. Without storing it, dedupe would stop at the database and the semantic
    index would keep every version.
    """
    values = {"status": "done", "updated_at": sa.func.now()}
    if doc_path:
        values["doc_path"] = doc_path
    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.task_outcome_queue)
            .where(db.task_outcome_queue.c.id == row_id)
            .values(**values)
        )


async def mark_failed(row_id: int, attempts: int, error: str) -> None:
    """Back to `pending` for another attempt, or `failed` once the budget is spent.

    A row left in `failed` is the point: it stays queryable. The outage this module
    exists to fix was invisible for 50 days precisely because a cancelled asyncio task
    leaves nothing behind to look at.
    """
    status = "pending" if attempts < MAX_ATTEMPTS else "failed"
    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.task_outcome_queue)
            .where(db.task_outcome_queue.c.id == row_id)
            .values(status=status, attempts=attempts,
                    error_message=error[:2000], updated_at=sa.func.now())
        )


async def requeue_stuck(older_than_seconds: int = 900) -> int:
    """Return rows abandoned mid-flight (server killed during extraction) to `pending`.

    Without this a restart during a drain strands those rows in `processing` forever --
    the same class of silent loss as the original bug, just with a row to show for it.
    """
    cutoff = sa.func.now() - sa.text(f"interval '{int(older_than_seconds)} seconds'") \
        if db.get_engine().dialect.name == "postgresql" else None
    if cutoff is None:      # SQLite has no interval literal; compare in Python instead
        import datetime as _dt
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=older_than_seconds)
    async with db.get_engine().begin() as conn:
        result = await conn.execute(
            sa.update(db.task_outcome_queue)
            .where(db.task_outcome_queue.c.status == "processing",
                   db.task_outcome_queue.c.updated_at < cutoff)
            .values(status="pending", updated_at=sa.func.now())
        )
        return result.rowcount or 0
