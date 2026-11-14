"""Shared guard interfaces (Phase 1.18F): one execution reference, one decision record, one registry, used by BOTH engines.

What this is: a thin, PURE decision layer. A guard looks at a read-only context (the worker's liveness snapshot, the worker's
identity, how long it has been running) and either returns a reason to stop the run or None. The layer picks the first
reason in priority order and returns ONE GuardDecision. It never touches a process.

What this is not:
  * Not a process manager. The PARENT SUPERVISOR (api/server.py::_run_worker_subprocess) remains the only thing that ever
    kills a worker; this module has no handle to one. A decision is advice that the supervisor acts on.
  * Not a second enforcement path. api/server.py calls EITHER this layer (HIVE_SHARED_GUARDS=1) OR its original inline
    decision code, never both, and the layer is idempotent per worker (a second evaluation of a worker that already has a
    STOP decision returns that same decision and counts nothing new), so one worker can produce at most one stop decision.
  * Not a merge of stores. The Workflow ledger (acceptance facts) and the Team execution_store (run telemetry) are untouched.
  * Not an orchestration change. Team remains the default engine and Workflow stays opt-in; each engine keeps its own
    orchestration, its own snapshot writer and its own post-kill answer building. Only the decision is shared.

The decision functions themselves are NOT rewritten: the adapters in api/server.py wrap the existing, already-validated
`_liveness_kill_reason` and `_workflow_supervision_reason`, so the layer returns exactly the reason string the inline path
returns for the same inputs (checked by a differential test over a snapshot corpus before this was enabled anywhere).

A guard that raises is isolated: it is logged and skipped, so a guard bug can neither kill a healthy run nor hide the
decision of a later guard.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

TEAM, WORKFLOW = "team", "workflow"
STOP_RUN = "STOP_RUN"
ENFORCER = "parent_supervisor"          # the only authority that acts on a decision


@dataclass(frozen=True)
class ExecutionRef:
    """Identity of one execution, for either engine. Fields an engine cannot supply stay None (never invented)."""
    engine: str
    worker_pid: int
    task_id: Optional[str] = None           # Workflow task id (Team: None)
    run_id: Optional[str] = None            # Workflow run id, or the Team run id when the snapshot carries one
    unit_id: Optional[str] = None           # Workflow work item
    attempt_id: Optional[str] = None
    generation: Optional[int] = None
    nonce: Optional[str] = None             # per-spawn nonce (Workflow); proves a snapshot belongs to THIS spawn


@dataclass(frozen=True)
class GuardContext:
    engine: str
    ref: ExecutionRef
    snapshot: Optional[dict]                # the raw snapshot as read from disk (may be None / foreign)
    belongs: bool                           # snapshot is present and trusted for this worker
    started_at: float                       # wall clock at spawn
    now: float


@dataclass(frozen=True)
class GuardDecision:
    guard: str
    action: str
    reason: str
    engine: str
    ref: ExecutionRef
    at: float
    enforcer: str = ENFORCER

    def public(self) -> dict:
        d = asdict(self)
        d["reason"] = self.reason[:300]
        return d


class Guard:
    """Subclass or duck-type: `name`, `engines`, `priority` (lower runs first) and `evaluate(ctx) -> reason | None`."""
    name: str = ""
    engines: frozenset = frozenset()
    priority: int = 100

    def evaluate(self, ctx: GuardContext) -> Optional[str]:     # pragma: no cover - interface
        raise NotImplementedError


class CallableGuard(Guard):
    """Adapter: wraps an existing pure decision function so its logic is reused unchanged."""

    def __init__(self, name: str, engines, priority: int, fn: Callable[[GuardContext], Optional[str]]):
        self.name, self.engines, self.priority, self._fn = name, frozenset(engines), priority, fn

    def evaluate(self, ctx: GuardContext) -> Optional[str]:
        return self._fn(ctx)


class GuardLayer:
    def __init__(self, guards: list, *, enabled: bool = False, history: int = 50, log: Callable[[str], None] = print):
        names = [g.name for g in guards]
        if len(set(names)) != len(names) or any(not n for n in names):
            raise ValueError("guard names must be unique and non-empty (a guard registered twice would enforce twice)")
        self.guards = sorted(guards, key=lambda g: (g.priority, g.name))
        self.enabled = enabled
        self._log = log
        self.decisions: deque = deque(maxlen=history)
        self._decided: dict = {}                 # worker pid -> GuardDecision (idempotency)
        self.evaluations = 0
        self.guard_errors = 0
        self.stops_by_guard: dict = {}

    def forget(self, pid: int) -> None:
        """Called by the supervisor when a worker is gone (its pid may be reused later)."""
        self._decided.pop(pid, None)

    def evaluate(self, ctx: GuardContext) -> Optional[GuardDecision]:
        prior = self._decided.get(ctx.ref.worker_pid)
        if prior is not None:
            return prior
        self.evaluations += 1
        for g in self.guards:
            if ctx.engine not in g.engines:
                continue
            try:
                reason = g.evaluate(ctx)
            except Exception as exc:  # noqa: BLE001 -- isolate: never kill a healthy run, never mask a later guard
                self.guard_errors += 1
                self._log(f"[guards] guard {g.name!r} raised {type(exc).__name__}: {str(exc)[:120]} (skipped)")
                continue
            if reason is not None:
                d = GuardDecision(guard=g.name, action=STOP_RUN, reason=reason, engine=ctx.engine, ref=ctx.ref, at=time.time())
                self._decided[ctx.ref.worker_pid] = d
                self.decisions.append(d)
                self.stops_by_guard[g.name] = self.stops_by_guard.get(g.name, 0) + 1
                return d
        return None

    def status(self) -> dict:
        return {"enabled": self.enabled, "enforcer": ENFORCER,
                "guards": [{"name": g.name, "engines": sorted(g.engines), "priority": g.priority} for g in self.guards],
                "evaluations": self.evaluations, "stops_by_guard": dict(self.stops_by_guard), "guard_errors": self.guard_errors,
                "recent_decisions": [d.public() for d in list(self.decisions)[-10:]]}
