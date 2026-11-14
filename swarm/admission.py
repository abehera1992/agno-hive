"""Worker admission control (Phase 1.18D): a bounded number of active worker processes, a small bounded FIFO queue, and a
hard wait deadline. Beyond that a request is REJECTED (HTTP 429 at the API layer) rather than spawning another worker.

Scope and honesty about scope: the counters are PROCESS-LOCAL. The service runs as one uvicorn process (main.py `--serve`,
no `workers=`), so one counter is the global cap for the service. A second API process on the same host (for example an
isolated test instance) has its own counter; a multi-process deployment would need a shared admission mechanism.

Guarantees
  * a slot is held only while a worker exists, and is released exactly once on every exit path (idempotent release);
  * a request that is waiting leaves the queue on disconnect, timeout or cancellation without ever holding a slot;
  * a slot freed by a release is handed directly to the oldest live waiter (FIFO), so a fast-path request cannot jump it;
  * nothing here is persisted: a restart clears the counters, and workers die with the service (systemd control-group), so
    no slot can leak across a restart.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional


class AdmissionRejected(Exception):
    def __init__(self, reason: str, *, active: int, queued: int, cap: int, max_queue: int, retry_after_s: int):
        super().__init__(reason)
        self.reason, self.active, self.queued, self.cap, self.max_queue, self.retry_after_s = reason, active, queued, cap, max_queue, retry_after_s

    def detail(self) -> dict:
        return {"error": "worker capacity", "reason": self.reason, "active_workers": self.active, "queued": self.queued,
                "max_active_workers": self.cap, "max_queue": self.max_queue, "retry_after_s": self.retry_after_s}


class Ticket:
    __slots__ = ("kind", "released", "proc", "granted_at", "waited_s")

    def __init__(self, kind: str):
        self.kind, self.released, self.proc, self.granted_at, self.waited_s = kind, False, None, None, 0.0


class WorkerAdmission:
    def __init__(self, cap: int, max_queue: int, wait_s: float):
        self.cap, self.max_queue, self.wait_s = cap, max_queue, wait_s
        self.active = 0
        self.max_active_seen = 0
        self.waiters: deque = deque()                 # of (future, kind)
        self.admitted = self.rejected = self.queued_total = self.disconnected_waiting = 0
        self.by_kind_active: dict = {}

    # ------------------------------------------------------------------ introspection
    def status(self) -> dict:
        return {"enabled": self.cap > 0, "max_active_workers": self.cap, "max_queue": self.max_queue, "queue_wait_s": self.wait_s,
                "active_workers": self.active, "active_by_kind": dict(self.by_kind_active), "queued": len(self.waiters),
                "max_active_seen": self.max_active_seen, "admitted_total": self.admitted, "queued_total": self.queued_total,
                "rejected_total": self.rejected, "left_while_waiting": self.disconnected_waiting}

    def would_reject(self) -> bool:
        """True if a request arriving now would be rejected outright (slots busy AND queue full). Non-acquiring."""
        return self.cap > 0 and self.active >= self.cap and len(self.waiters) >= self.max_queue

    def _take(self, kind: str) -> Ticket:
        self.active += 1
        self.max_active_seen = max(self.max_active_seen, self.active)
        self.by_kind_active[kind] = self.by_kind_active.get(kind, 0) + 1
        self.admitted += 1
        t = Ticket(kind)
        t.granted_at = time.monotonic()
        return t

    def _reject(self, reason: str) -> AdmissionRejected:
        self.rejected += 1
        return AdmissionRejected(reason, active=self.active, queued=len(self.waiters), cap=self.cap,
                                 max_queue=self.max_queue, retry_after_s=30)

    # ------------------------------------------------------------------ acquire / release
    async def acquire(self, kind: str, http_request=None) -> Ticket:
        if self.cap <= 0:                              # disabled: previous (unlimited) behaviour
            return self._take(kind)
        if self.active < self.cap and not self.waiters:
            return self._take(kind)
        if len(self.waiters) >= self.max_queue:
            raise self._reject(f"all {self.cap} worker slots are busy and the wait queue ({self.max_queue}) is full")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (fut, kind)
        self.waiters.append(entry)
        self.queued_total += 1
        t0 = time.monotonic()
        try:
            while True:
                remaining = self.wait_s - (time.monotonic() - t0)
                if remaining <= 0:
                    raise self._reject(f"waited {self.wait_s:.0f}s for a worker slot")
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=min(2.0, remaining))
                    ticket = fut.result()                 # the slot was handed to us by release()
                    ticket.waited_s = time.monotonic() - t0
                    return ticket
                except asyncio.TimeoutError:
                    if http_request is not None and await http_request.is_disconnected():
                        self.disconnected_waiting += 1
                        raise asyncio.CancelledError("client disconnected while waiting for a worker slot")
        except BaseException:
            # Leaving the queue for ANY reason. If the slot was granted in the same instant, give it back.
            if entry in self.waiters:
                self.waiters.remove(entry)
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                self.release(fut.result())
            elif not fut.done():
                fut.cancel()
            raise

    def release(self, ticket: Optional[Ticket]) -> None:
        if ticket is None or ticket.released:
            return
        ticket.released = True
        self.active = max(0, self.active - 1)
        self.by_kind_active[ticket.kind] = max(0, self.by_kind_active.get(ticket.kind, 1) - 1)
        if self.cap <= 0:
            return
        while self.waiters and self.active < self.cap:   # hand the freed slot to the oldest live waiter
            fut, kind = self.waiters.popleft()
            if fut.done():                                # it gave up; try the next one
                continue
            fut.set_result(self._take(kind))
            break
