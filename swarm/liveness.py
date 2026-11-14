"""Workflow liveness telemetry (Phase 1.18B).

The parent worker supervisor in api/server.py is, and stays, the ONLY authority that terminates a worker process
(`proc.kill()`). This module only produces the evidence the parent judges:

  * ProgressTracker -- classifies each finished tool call as real research progress or not, and tracks the
    consecutive non-progress streak and the time since the last real progress.
  * build_snapshot / write_snapshot -- the Workflow side of the existing snapshot contract that
    api/server.py::_read_liveness_snapshot / _liveness_kill_reason already consume. Atomic (temp + os.replace).
    The snapshot carries the worker pid and a per-spawn nonce the parent generated, so a stale or foreign file can
    never be mistaken for this worker's health.

Nothing here cancels, kills or invalidates anything.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from swarm import acceptance as A

SCHEMA = "workflow-liveness/1"


class ProgressTracker:
    """Meaningful progress = a previously unread source file was read successfully, or a non-state tool returned a
    result never seen before in this attempt. Everything else (state-tool calls, repeated/identical results,
    duplicate-read and other refusal stubs, failed calls) is NOT progress. Narration and content chunks refresh
    `last_activity_at` (so silence is judged separately) but never count as progress."""

    def __init__(self, now=time.monotonic):
        self._now = now
        t = now()
        self.started_at = t
        self.last_activity_at = t
        self.last_progress_at = t
        self.seen_paths: set = set()
        self.seen_result_shas: set = set()
        self.state_result_shas: dict = {}
        self.streak = 0
        self.identical_repeats = 0
        self.calls = 0
        self.errors = 0
        self.refused = 0
        self.progress_events = 0
        self.last_tool: Optional[str] = None
        self.in_flight = 0

    def on_event(self) -> None:
        self.last_activity_at = self._now()

    def on_tool_start(self) -> None:
        self.in_flight += 1
        self.last_activity_at = self._now()

    def on_call(self, o) -> bool:
        """`o` is an acceptance.ToolOutcome that just finished. Returns True if it was real progress."""
        self.in_flight = max(0, self.in_flight - 1)
        now = self._now()
        self.last_activity_at = now
        self.calls += 1
        self.last_tool = o.tool
        progress = False
        if o.outcome == A.REFUSED:
            self.refused += 1
        elif o.outcome in (A.FAILED, A.TIMED_OUT, A.CANCELLED, A.UNKNOWN):
            self.errors += 1
        elif o.tool in A.STATE_TOOLS:
            sha = o.result_sha or ""
            if sha and sha in self.state_result_shas:
                self.identical_repeats += 1
            self.state_result_shas[sha] = self.state_result_shas.get(sha, 0) + 1
        elif o.outcome == A.SUCCESS:
            new_paths = [p for p in (o.paths_read or []) if p not in self.seen_paths]
            if o.evidence_kind == "source_read" and new_paths:
                self.seen_paths.update(new_paths)
                progress = True
            elif o.result_sha and o.result_sha not in self.seen_result_shas and o.evidence_kind != "source_read":
                progress = True
            if o.result_sha:
                self.seen_result_shas.add(o.result_sha)
        if progress:
            self.streak = 0
            self.last_progress_at = now
            self.progress_events += 1
        else:
            self.streak += 1
        return progress

    def fields(self) -> dict:
        now = self._now()
        return {"stagnant_seconds": round(now - self.last_activity_at, 1),
                "tool_calls_made": self.calls, "tool_errors": self.errors, "tool_refused": self.refused,
                "source_files_read": len(self.seen_paths), "progress_events": self.progress_events,
                "wf_no_progress_streak": self.streak, "wf_no_progress_seconds": round(now - self.last_progress_at, 1),
                "wf_identical_state_repeats": self.identical_repeats, "last_tool": self.last_tool,
                "tools_in_flight": self.in_flight}


def build_snapshot(*, nonce, state, identity: dict, tracker: Optional[ProgressTracker], task_deadline_wall,
                   attempt_deadline_wall=None, stream_events=0) -> dict:
    snap = {"schema": SCHEMA, "written_at": time.time(), "worker_pid": os.getpid(), "nonce": nonce, "wf_state": state,
            "wf_task_deadline_wall": task_deadline_wall, "wf_attempt_deadline_wall": attempt_deadline_wall,
            "stream_event_count": stream_events, "draft": "", "member_results": None}
    snap.update(identity or {})
    if tracker is not None:
        snap.update(tracker.fields())
    else:
        snap.setdefault("stagnant_seconds", 0)
    return snap


def write_snapshot(path: Optional[str], snap: dict) -> bool:
    """Atomic write. Returns False (never raises) on failure: a missing snapshot is itself judged by the parent."""
    if not path:
        return False
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
