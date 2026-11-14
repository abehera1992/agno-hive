"""API-level idempotency (Phase 1.18E): decide, BEFORE any worker is spawned, whether a keyed request is new, a duplicate of
one still running, a duplicate of a finished one, or a conflicting reuse of the key. Applies to Team and Workflow alike.

Storage: a DEDICATED SQLite file (default <repo>/data/api_idempotency.db, override HIVE_IDEMPOTENCY_DB). It is deliberately
separate from the Workflow ledger (swarm/ledger.py, per-work-item acceptance facts) and from the Team execution_store
(Postgres, per-run telemetry): those answer different questions and are not merged. The Workflow ledger's own task-level
duplicate check inside the worker is kept as a second line of defence for Workflow runs.

Decision table (one transaction, BEGIN IMMEDIATE, so two simultaneous submissions cannot both be NEW):
  no row                                   -> NEW              (claimed RUNNING, attempt 1)
  row exists, request_sha differs          -> CONFLICT         (key reused for a different request; never runs)
  row FINAL                                -> DUPLICATE_FINAL  (stored response is returned; nothing re-runs)
  row RUNNING                              -> DUPLICATE_RUNNING (a RUNNING row ends only via finish/fail, or via startup
                                              recovery, which marks this instance's cut-off rows INTERRUPTED; a row of another
                                              instance cannot be verified from here and is reported as running)
  row FAILED / INTERRUPTED                 -> NEW              (a failed attempt never poisons the key; attempt + 1)

A request WITHOUT a key is not deduplicated at all: every submission runs (previous behaviour, unchanged).
`attempt` > 1 is surfaced to the worker as "<key>#a<attempt>" so the Workflow ledger (which derives its task id from the
key and refuses to restart a non-finalized task) sees a fresh task for the retry instead of the abandoned one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "api_idempotency.db"
MAX_KEY_LEN = 200

NEW, DUPLICATE_RUNNING, DUPLICATE_FINAL, CONFLICT = "NEW", "DUPLICATE_RUNNING", "DUPLICATE_FINAL", "CONFLICT"
RUNNING, FINAL, FAILED, INTERRUPTED = "RUNNING", "FINAL", "FAILED", "INTERRUPTED"

_DDL = """
CREATE TABLE IF NOT EXISTS idem (
  key TEXT PRIMARY KEY, request_sha TEXT NOT NULL, engine TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL,
  instance_id TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
  response_json TEXT, failure TEXT);
"""


def normalize_key(raw) -> Optional[str]:
    """None for no key; a stripped key; ValueError for an unusable one."""
    if raw is None:
        return None
    k = str(raw).strip()
    if not k:
        return None
    if len(k) > MAX_KEY_LEN or any(ord(ch) < 32 for ch in k):
        raise ValueError(f"idempotency_key must be at most {MAX_KEY_LEN} printable characters")
    return k


def request_sha(fields: dict) -> str:
    """Hash of the fields that DEFINE the request. Deliberately excludes session_id, mcp urls, persist and the key itself:
    a retry may legitimately differ in those without being a different request."""
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8", "replace")).hexdigest()


class IdempotencyStore:
    def __init__(self, path: Optional[str] = None, instance_id: Optional[str] = None):
        self.path = str(path or os.environ.get("HIVE_IDEMPOTENCY_DB") or DEFAULT_PATH)
        self.instance_id = instance_id or f"inst-{os.environ.get('AGNO_PORT', '9001')}"
        self.inflight: dict = {}          # key -> attempt, for requests THIS process is currently running
        self._ready = False
        self.counts = {NEW: 0, DUPLICATE_RUNNING: 0, DUPLICATE_FINAL: 0, CONFLICT: 0}

    def _conn(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        if not self._ready:
            c.executescript(_DDL)
            self._ready = True
        return c

    def _tx(self, fn):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                out = fn(c)
                c.execute("COMMIT")
                return out
            except BaseException:
                c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    # ------------------------------------------------------------------ decision
    def begin(self, key: str, sha: str, engine: str) -> dict:
        now = time.time()

        def go(c):
            r = c.execute("SELECT * FROM idem WHERE key=?", (key,)).fetchone()
            if r is None:
                c.execute("INSERT INTO idem(key,request_sha,engine,status,attempt,instance_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (key, sha, engine, RUNNING, 1, self.instance_id, now, now))
                return {"outcome": NEW, "attempt": 1}
            if r["request_sha"] != sha:
                return {"outcome": CONFLICT, "status": r["status"], "created_at": r["created_at"]}
            if r["status"] == FINAL:
                return {"outcome": DUPLICATE_FINAL, "response_json": r["response_json"], "finished_at": r["finished_at"], "attempt": r["attempt"]}
            if r["status"] == RUNNING:               # only startup recovery (or fail/finish) ever ends a RUNNING row
                return {"outcome": DUPLICATE_RUNNING, "created_at": r["created_at"], "attempt": r["attempt"], "instance": r["instance_id"]}
            att = r["attempt"] + 1                  # FAILED / INTERRUPTED
            c.execute("UPDATE idem SET status=?, attempt=?, instance_id=?, updated_at=?, finished_at=NULL, failure=NULL, response_json=NULL WHERE key=?",
                      (RUNNING, att, self.instance_id, now, key))
            return {"outcome": NEW, "attempt": att, "retry_of": r["status"]}

        d = self._tx(go)
        self.counts[d["outcome"]] += 1
        if d["outcome"] == NEW:
            self.inflight[key] = d["attempt"]
        return d

    def finish(self, key: str, attempt: int, response_json: str) -> bool:
        def go(c):
            cur = c.execute("UPDATE idem SET status=?, response_json=?, finished_at=?, updated_at=? WHERE key=? AND attempt=? AND status=?",
                            (FINAL, response_json, time.time(), time.time(), key, attempt, RUNNING))
            return cur.rowcount == 1
        try:
            return self._tx(go)
        finally:
            self.inflight.pop(key, None)

    def fail(self, key: str, attempt: int, reason: str) -> bool:
        def go(c):
            cur = c.execute("UPDATE idem SET status=?, failure=?, finished_at=?, updated_at=? WHERE key=? AND attempt=? AND status=?",
                            (FAILED, (reason or "")[:300], time.time(), time.time(), key, attempt, RUNNING))
            return cur.rowcount == 1
        try:
            return self._tx(go)
        finally:
            self.inflight.pop(key, None)

    def recover_startup(self) -> int:
        """At process start nothing of this instance can be in flight: its RUNNING rows were cut off by the restart."""
        def go(c):
            cur = c.execute("UPDATE idem SET status=?, failure=?, finished_at=?, updated_at=? WHERE status=? AND instance_id=?",
                            (INTERRUPTED, "service restarted while the request was running", time.time(), time.time(), RUNNING, self.instance_id))
            return cur.rowcount
        return self._tx(go)

    def get(self, key: str) -> Optional[dict]:
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM idem WHERE key=?", (key,)).fetchone()
            return None if r is None else {k: r[k] for k in r.keys() if k != "response_json"} | {"has_response": r["response_json"] is not None}
        finally:
            c.close()

    def stats(self) -> dict:
        c = self._conn()
        try:
            by = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM idem GROUP BY 1")}
            return {"path": self.path, "rows_by_status": by, "inflight_here": len(self.inflight), "decisions_this_process": dict(self.counts)}
        finally:
            c.close()
