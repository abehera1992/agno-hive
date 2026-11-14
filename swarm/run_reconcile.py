"""Team run ownership and stale-run reconciliation (Phase 1.18C).

Connects the EXISTING durable backbone (swarm/execution_store.py: acquire_run_ownership / release_run_ownership,
the `runs.owner_worker_id` / `owner_lease_expires_at` columns, the runs/executions/tool_calls tables) to the production
lifecycle. Nothing here kills anything (the API parent supervisor remains the only termination authority) and nothing
here adds a table or a status value: a run that was cut off is recorded with the EXISTING terminal status `failed` plus an
explanatory error message, its unfinished executions become `failed` and its unfinished tool calls `error`.

Principles
  * A lease is not proof of death. A run is reconciled only when (a) the parent has just observed its worker exit, or
    (b) at startup the owning pid is verifiably NOT a live worker of this host, or (c) it is an old, unowned, inactive
    historical row (classified from evidence, dry-run by default).
  * Another instance's runs (a different port) and recent unowned runs are reported, never touched.
  * Every transition is one compare-and-set (`WHERE status='running'`), so repeating reconciliation can never overwrite a
    terminal state or a newer execution's state.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import func

from swarm import db, execution_store
from swarm.db import get_engine

LEASE_SECONDS = int(os.getenv("HIVE_RUN_LEASE_S", "300"))
RENEW_INTERVAL_S = float(os.getenv("HIVE_RUN_LEASE_RENEW_S", "60"))
# An unowned running row is only treated as abandoned history when it is at least this old AND has shown no activity
# (checkpoint, execution or tool call) for this long. Anything newer is ambiguous and is left alone.
SAFE_AGE_S = float(os.getenv("HIVE_RECONCILE_SAFE_AGE_S", str(6 * 3600)))

APPLICABLE = ("TERMINATED_OWNER_GONE", "ABANDONED_AFTER_ROOT", "ABANDONED_IN_FLIGHT", "ABANDONED_BARREN")


def instance_id() -> str:
    return f"inst-{os.environ.get('AGNO_PORT', '9001')}"


def worker_id_for(pid: int) -> str:
    return f"{instance_id()}:{pid}"


def _pid_of(worker_id: Optional[str]) -> Optional[int]:
    try:
        return int((worker_id or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def pid_is_live_worker(pid: Optional[int]) -> Optional[bool]:
    """True/False when it can be determined on this host (Linux /proc), None when it cannot. Checks the command line so a
    reused pid that now belongs to something else is not mistaken for the worker."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().decode("utf-8", "replace").split("\x00")
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return any(a in ("--run-worker", "--stream-worker") for a in argv) and any(a.endswith("main.py") for a in argv)


# ---------------------------------------------------------------------------- worker side: claim, renew, release
class RunLease:
    def __init__(self, run_id: str, worker_id: str, task: Optional[asyncio.Task], acquired: bool):
        self.run_id, self.worker_id, self.task, self.acquired = run_id, worker_id, task, acquired


async def claim_run(run_id: Optional[str]) -> Optional[RunLease]:
    """Called by the worker right after its `runs` row exists. Never raises: a failed claim just means the run is
    reconciled by the historical rules instead of the owner rules."""
    if not run_id:
        return None
    wid = worker_id_for(os.getpid())
    try:
        ok = await execution_store.acquire_run_ownership(run_id, wid, LEASE_SECONDS)
    except Exception:  # noqa: BLE001
        ok = False
    task = asyncio.create_task(_renew_loop(run_id, wid)) if ok else None
    return RunLease(run_id, wid, task, ok)


async def _renew_loop(run_id: str, wid: str) -> None:
    while True:
        await asyncio.sleep(RENEW_INTERVAL_S)
        try:
            await execution_store.acquire_run_ownership(run_id, wid, LEASE_SECONDS)   # re-entrant renewal
        except Exception:  # noqa: BLE001
            pass


async def release_run(lease: Optional[RunLease]) -> None:
    if lease is None:
        return
    if lease.task is not None:
        lease.task.cancel()
    if lease.acquired:
        try:
            await execution_store.release_run_ownership(lease.run_id, lease.worker_id)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------- the single transition
async def reconcile_run(run_id: str, message: str, status: str = "failed") -> dict:
    """Atomically move a still-`running` run (and its unfinished executions / tool calls) to a terminal state. A no-op
    (applied=False) if the run is no longer `running`: terminal states and newer state are never overwritten."""
    msg = message[:500]
    try:
        async with get_engine().begin() as conn:
            r = await conn.execute(
                db.runs.update().where(db.runs.c.run_id == run_id, db.runs.c.status == "running")
                .values(status=status, error_message=msg, completed_at=func.now(),
                        owner_worker_id=None, owner_lease_expires_at=None))
            if r.rowcount != 1:
                return {"run_id": run_id, "applied": False}
            ex = await conn.execute(
                db.executions.update().where(db.executions.c.run_id == run_id, db.executions.c.status == "running")
                .values(status="failed", completed_at=func.now(), error_message=msg))
            ids = sa.select(db.executions.c.execution_id).where(db.executions.c.run_id == run_id)
            tc = await conn.execute(
                db.tool_calls.update().where(db.tool_calls.c.execution_id.in_(ids), db.tool_calls.c.status == "running")
                .values(status="error", completed_at=func.now(), error_message=msg))
        print(f"[reconcile] run_id={run_id} -> {status} (executions_closed={ex.rowcount} tool_calls_closed={tc.rowcount}): {msg}", flush=True)
        return {"run_id": run_id, "applied": True, "executions": ex.rowcount, "tool_calls": tc.rowcount}
    except Exception as exc:  # noqa: BLE001
        print(f"[reconcile] run_id={run_id} FAILED to reconcile: {type(exc).__name__}: {exc}", flush=True)
        return {"run_id": run_id, "applied": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------- classification (read-only)
def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


async def classify_running_runs(now: Optional[datetime] = None) -> list:
    """Evidence-based, read-only classification of every run still marked `running`. Each item:
    {run_id, cls, reason, applicable, evidence}. Never mutates."""
    now = now or datetime.now(timezone.utc)
    me = instance_id()
    out = []
    async with get_engine().begin() as conn:
        runs = (await conn.execute(sa.select(db.runs).where(db.runs.c.status == "running"))).mappings().all()
        for r in runs:
            rid = r["run_id"]
            ck = (await conn.execute(sa.select(db.checkpoints.c.run_status, db.checkpoints.c.created_at)
                                     .where(db.checkpoints.c.run_id == rid).order_by(db.checkpoints.c.sequence.desc()).limit(1))).first()
            ex = (await conn.execute(sa.select(db.executions.c.status, db.executions.c.started_at, db.executions.c.completed_at)
                                     .where(db.executions.c.run_id == rid))).all()
            ids = sa.select(db.executions.c.execution_id).where(db.executions.c.run_id == rid)
            tc = (await conn.execute(sa.select(db.tool_calls.c.status, db.tool_calls.c.started_at, db.tool_calls.c.completed_at)
                                     .where(db.tool_calls.c.execution_id.in_(ids)))).all()
            stamps = [x for x in [_aware(r["started_at"]), _aware(ck[1]) if ck else None]
                      + [_aware(e[1]) for e in ex] + [_aware(e[2]) for e in ex]
                      + [_aware(t[1]) for t in tc] + [_aware(t[2]) for t in tc] if x is not None]
            last_activity = max(stamps) if stamps else _aware(r["started_at"])
            idle_s = (now - last_activity).total_seconds()
            root_terminal = any(e[0] in ("ok", "failed") for e in ex)
            ev = {"started_at": str(r["started_at"]), "last_activity": str(last_activity), "idle_s": round(idle_s),
                  "last_checkpoint_status": ck[0] if ck else None, "executions": len(ex),
                  "root_or_any_execution_terminal": root_terminal, "tool_calls": len(tc),
                  "tool_calls_running": sum(1 for t in tc if t[0] == "running"), "owner": r["owner_worker_id"]}
            owner = r["owner_worker_id"]
            if ck and ck[0] in ("ok", "ok_truncated", "failed"):
                cls, why = "NEEDS_REVIEW", "a terminal checkpoint exists but the run row never completed (completion write lost); not auto-applied"
            elif owner:
                if not owner.startswith(me + ":"):
                    cls, why = "FOREIGN_OWNER", f"owned by another instance ({owner}); not this instance's to judge"
                else:
                    live = pid_is_live_worker(_pid_of(owner))
                    if live is True:
                        cls, why = "ACTIVE", "owner worker process is alive"
                    elif live is None:
                        cls, why = "UNVERIFIABLE", "owner pid could not be checked on this host"
                    else:
                        cls, why = "TERMINATED_OWNER_GONE", "owner worker process is not running (lease not relied on)"
            elif idle_s < SAFE_AGE_S:
                cls, why = "RECENT_UNOWNED", f"unowned and active within the last {SAFE_AGE_S / 3600:.0f}h: ambiguous, left alone"
            elif root_terminal:
                cls, why = "ABANDONED_AFTER_ROOT", "root execution finished but the run never recorded completion (cut off during retry/guard/finalization)"
            elif ev["tool_calls"] == 0 and len(ex) <= 1:
                cls, why = "ABANDONED_BARREN", "no tool call and no progress beyond the root execution"
            else:
                cls, why = "ABANDONED_IN_FLIGHT", "interrupted while executing; no terminal evidence"
            out.append({"run_id": rid, "cls": cls, "reason": why, "applicable": cls in APPLICABLE, "evidence": ev})
    return out


def summarize(items: list) -> dict:
    by: dict = {}
    for i in items:
        by[i["cls"]] = by.get(i["cls"], 0) + 1
    return {"total": len(items), "by_class": by, "applicable": sum(1 for i in items if i["applicable"])}


async def reconcile_classified(items: list, *, apply: bool, only_cls: Optional[tuple] = None) -> dict:
    """Applies the transition to the applicable items (apply=False is a pure dry run)."""
    applied = skipped = 0
    for i in items:
        if not i["applicable"] or (only_cls and i["cls"] not in only_cls):
            skipped += 1
            continue
        if not apply:
            continue
        msg = f"reconciled ({i['cls']}): {i['reason']}; last activity {i['evidence']['last_activity']}; the delivered outcome is unknown"
        if (await reconcile_run(i["run_id"], msg)).get("applied"):
            applied += 1
    return {"applied": applied, "skipped": skipped, "dry_run": not apply}


# ---------------------------------------------------------------------------- lifecycle entry points
async def reconcile_worker_exit(pid: int) -> dict:
    """Parent-side, immediately after it observed the worker's exit: any run still `running` and owned by that worker is
    terminated by definition. Best effort, never raises."""
    try:
        async with get_engine().begin() as conn:
            ids = [x[0] for x in (await conn.execute(sa.select(db.runs.c.run_id).where(
                db.runs.c.status == "running", db.runs.c.owner_worker_id == worker_id_for(pid)))).all()]
        done = [await reconcile_run(i, f"worker process {pid} exited without recording completion (killed, cancelled or crashed)") for i in ids]
        return {"pid": pid, "runs": len(ids), "applied": sum(1 for d in done if d.get("applied"))}
    except Exception as exc:  # noqa: BLE001
        print(f"[reconcile] worker-exit reconciliation skipped ({type(exc).__name__}: {exc})", flush=True)
        return {"pid": pid, "error": str(exc)}


async def reconcile_startup(historical_mode: str = "report") -> dict:
    """At API start. Own-instance runs whose owner is verifiably gone are terminated; the rest are classified. Historical
    (unowned, old, inactive) rows are only APPLIED when historical_mode == "apply"; "report" logs the classification."""
    try:
        items = await classify_running_runs()
        s = summarize(items)
        own = await reconcile_classified(items, apply=True, only_cls=("TERMINATED_OWNER_GONE",))
        hist = await reconcile_classified(items, apply=(historical_mode == "apply"),
                                          only_cls=("ABANDONED_AFTER_ROOT", "ABANDONED_IN_FLIGHT", "ABANDONED_BARREN"))
        print(f"[reconcile] startup: {s} owned_reconciled={own['applied']} historical_mode={historical_mode} "
              f"historical_applied={hist['applied']}", flush=True)
        return {"summary": s, "owned": own, "historical": hist}
    except Exception as exc:  # noqa: BLE001
        print(f"[reconcile] startup reconciliation skipped ({type(exc).__name__}: {exc})", flush=True)
        return {"error": str(exc)}
