"""Durable execution and acceptance ledger for the engineering Workflow (Phase 1.17).

Storage: a DEDICATED SQLite file (default <repo>/data/workflow_ledger.db, override HIVE_LEDGER_DB), with its own
schema-version table. Deliberately NOT in the app database and NOT an Alembic revision: a new Alembic head would make
every service start (including the Team default) refuse to run until `hive migrate`, and would break rollback to older
code. This is the same decoupling the routing DB uses. No new service or dependency (stdlib sqlite3, WAL).

Rollback: stop using the Workflow path / delete or ignore the file; nothing else reads it. Schema evolution: a forward-only
`SCHEMA_VERSION` row; a file at a different version is refused (fail closed), never silently altered.

Atomicity: every state change is one `BEGIN IMMEDIATE` transaction (single writer) containing its own compare-and-set
conditions. Acceptance is conditional on the attempt being the work item's CURRENT generation and in a non-invalidated
state, and is idempotent per attempt. Errors raise LedgerError; callers must treat that as NOT accepted (fail closed).

Late data for an invalidated attempt is still stored against that attempt (tool calls, terminal facts, text) but can never
change a decision or an accepted record.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "workflow_ledger.db"

# attempt states. Terminal ones never change again except RUNNING/FINISHED -> invalidated.
RUNNING, FINISHED, ACCEPTED, WITHHELD, REJECTED = "RUNNING", "FINISHED", "ACCEPTED", "WITHHELD", "REJECTED"
SUPERSEDED, CANCELLED, ABANDONED, TIMED_OUT = "SUPERSEDED", "CANCELLED", "ABANDONED", "TIMED_OUT"
INVALIDATED = (SUPERSEDED, CANCELLED, ABANDONED, TIMED_OUT)
# legal transitions (from -> to)
TRANSITIONS = {
    RUNNING: {FINISHED, SUPERSEDED, CANCELLED, ABANDONED, TIMED_OUT, REJECTED},
    FINISHED: {ACCEPTED, WITHHELD, REJECTED, SUPERSEDED, CANCELLED, ABANDONED},
    ACCEPTED: set(), WITHHELD: set(), REJECTED: set(),
    SUPERSEDED: {REJECTED}, CANCELLED: {REJECTED}, ABANDONED: {REJECTED}, TIMED_OUT: {REJECTED},
}
ACCEPTING = ("ACCEPTED_COMPLETE", "ACCEPTED_PARTIAL")


class LedgerError(RuntimeError):
    """Any persistence failure. The caller must fail closed (nothing is accepted)."""


class RetryBudgetExhausted(LedgerError):
    pass


_DDL = """
CREATE TABLE IF NOT EXISTS ledger_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task (
  task_id TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL, instance_id TEXT NOT NULL,
  request_sha TEXT NOT NULL, request_preview TEXT, plan_json TEXT NOT NULL,
  status TEXT NOT NULL, created_at REAL NOT NULL, finalized_at REAL,
  final_sha TEXT, final_text TEXT, audit_json TEXT);
CREATE TABLE IF NOT EXISTS work_item (
  task_id TEXT NOT NULL REFERENCES task(task_id), work_item_id TEXT NOT NULL,
  title TEXT, spec_json TEXT NOT NULL,
  current_generation INTEGER NOT NULL DEFAULT -1, attempts_started INTEGER NOT NULL DEFAULT 0,
  accepted_attempt_id TEXT, accepted_generation INTEGER, accepted_status TEXT,
  PRIMARY KEY (task_id, work_item_id));
CREATE TABLE IF NOT EXISTS attempt (
  attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, work_item_id TEXT NOT NULL, generation INTEGER NOT NULL,
  workflow_run_id TEXT NOT NULL, agent_run_id TEXT, state TEXT NOT NULL,
  owner_instance TEXT NOT NULL, owner_pid INTEGER, started_at REAL NOT NULL, ended_at REAL,
  invalidated_reason TEXT, terminal_json TEXT, text_sha TEXT,
  FOREIGN KEY (task_id, work_item_id) REFERENCES work_item(task_id, work_item_id),
  UNIQUE (task_id, work_item_id, generation));
CREATE TABLE IF NOT EXISTS tool_call (
  attempt_id TEXT NOT NULL REFERENCES attempt(attempt_id), seq INTEGER NOT NULL,
  task_id TEXT NOT NULL, work_item_id TEXT NOT NULL, tool TEXT NOT NULL, args_json TEXT,
  outcome TEXT NOT NULL, reached_tool INTEGER, refusal_kind TEXT, refusal_detected_by TEXT,
  evidence_kind TEXT, paths_read_json TEXT, files_json TEXT, reason TEXT, result_sha TEXT, result_len INTEGER,
  agent_run_id TEXT, recorded_at REAL NOT NULL, PRIMARY KEY (attempt_id, seq));
CREATE TABLE IF NOT EXISTS evidence_blob (sha TEXT PRIMARY KEY, content TEXT NOT NULL, chars INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision (
  attempt_id TEXT PRIMARY KEY REFERENCES attempt(attempt_id), task_id TEXT NOT NULL, work_item_id TEXT NOT NULL,
  generation INTEGER NOT NULL, acceptance TEXT NOT NULL, execution_status TEXT, validity TEXT,
  assessor_status TEXT, assessor_reason TEXT, not_accepted_cause TEXT, reason TEXT,
  overrides_json TEXT, gate_json TEXT, counts_json TEXT, assessor_claims_json TEXT, requirements_json TEXT,
  extra_json TEXT, text_sha TEXT, decided_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS registration (
  attempt_id TEXT PRIMARY KEY REFERENCES attempt(attempt_id), task_id TEXT NOT NULL, work_item_id TEXT NOT NULL,
  generation INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, registered_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_attempt_state ON attempt(state, owner_instance);
CREATE INDEX IF NOT EXISTS ix_attempt_item ON attempt(task_id, work_item_id);
"""


def _sha(s) -> str:
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()


def _j(o) -> str:
    return json.dumps(o, default=lambda x: sorted(x) if isinstance(x, (set, frozenset)) else str(x), sort_keys=True)


class Ledger:
    def __init__(self, path: Optional[str] = None, instance_id: Optional[str] = None):
        self.path = str(path or os.environ.get("HIVE_LEDGER_DB") or DEFAULT_PATH)
        self.instance_id = instance_id or f"inst-{os.environ.get('AGNO_PORT', '9001')}"
        self.fail_next: Optional[str] = None      # test hook: "start" | "evidence" | "commit" | "finalize"
        self._ready = False

    # ------------------------------------------------------------------ plumbing
    def _conn(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)   # explicit transactions
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _tx(self, fn):
        """One BEGIN IMMEDIATE transaction (single writer). Any exception rolls back and is raised as LedgerError."""
        try:
            c = self._conn()
        except Exception as e:  # noqa: BLE001
            raise LedgerError(f"ledger open failed: {e!r}") from e
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                out = fn(c)
                c.execute("COMMIT")
                return out
            except BaseException:
                try:
                    c.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        except (LedgerError, RetryBudgetExhausted):
            raise
        except Exception as e:  # noqa: BLE001
            raise LedgerError(f"ledger operation failed: {e!r}") from e
        finally:
            c.close()

    async def _a(self, fn, *a, **k):
        return await asyncio.to_thread(fn, *a, **k)

    def _maybe_fail(self, kind: str):
        """Test hook. "<kind>" fails the next op of that kind once; "<kind>!" fails every one (persistent outage)."""
        if self.fail_next == kind:
            self.fail_next = None
            raise LedgerError(f"injected ledger failure ({kind})")
        if self.fail_next == kind + "!":
            raise LedgerError(f"injected persistent ledger failure ({kind})")

    # ------------------------------------------------------------------ schema
    def init_schema(self) -> None:
        def go(c):
            for stmt in [s for s in _DDL.split(";") if s.strip()]:
                c.execute(stmt)
            row = c.execute("SELECT v FROM ledger_meta WHERE k='schema_version'").fetchone()
            if row is None:
                c.execute("INSERT INTO ledger_meta(k,v) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            elif int(row["v"]) != SCHEMA_VERSION:
                raise LedgerError(f"ledger schema version {row['v']} != expected {SCHEMA_VERSION}; refusing to use it")
        self._tx(go)
        self._ready = True

    async def ainit(self):
        if not self._ready:
            await self._a(self.init_schema)

    # ------------------------------------------------------------------ task / work items
    def create_task(self, task_id, workflow_run_id, request_text, plan_items: list):
        """plan_items: [{work_item_id, title, spec(dict)}]. Persisted BEFORE any attempt can exist."""
        def go(c):
            c.execute("INSERT INTO task(task_id,workflow_run_id,instance_id,request_sha,request_preview,plan_json,status,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (task_id, workflow_run_id, self.instance_id, _sha(request_text), (request_text or "")[:160],
                       _j([{"work_item_id": i["work_item_id"], "title": i.get("title")} for i in plan_items]), "RUNNING", time.time()))
            for i in plan_items:
                c.execute("INSERT INTO work_item(task_id,work_item_id,title,spec_json) VALUES(?,?,?,?)",
                          (task_id, i["work_item_id"], i.get("title"), _j(i["spec"])))
        self._tx(go)

    def start_attempt(self, task_id, work_item_id, workflow_run_id, max_attempts: int, owner_pid: Optional[int] = None) -> dict:
        """Allocates the next generation and persists the attempt as RUNNING, superseding older live attempts of the SAME
        work item only, in one transaction. Enforces the retry budget across restarts."""
        def go(c):
            self._maybe_fail("start")
            w = c.execute("SELECT current_generation, attempts_started FROM work_item WHERE task_id=? AND work_item_id=?",
                          (task_id, work_item_id)).fetchone()
            if w is None:
                raise LedgerError(f"unknown work item {task_id}/{work_item_id}")
            if w["attempts_started"] >= max_attempts:
                raise RetryBudgetExhausted(f"work item {work_item_id} already started {w['attempts_started']} attempt(s) (budget {max_attempts})")
            gen = w["current_generation"] + 1
            aid = f"att-{task_id[-6:]}-{work_item_id}-g{gen}"
            c.execute("UPDATE attempt SET state=?, invalidated_reason=?, ended_at=COALESCE(ended_at,?) "
                      "WHERE task_id=? AND work_item_id=? AND state IN (?,?)",
                      (SUPERSEDED, f"superseded by generation {gen}", time.time(), task_id, work_item_id, RUNNING, FINISHED))
            c.execute("INSERT INTO attempt(attempt_id,task_id,work_item_id,generation,workflow_run_id,state,owner_instance,owner_pid,started_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (aid, task_id, work_item_id, gen, workflow_run_id, RUNNING, self.instance_id, owner_pid or os.getpid(), time.time()))
            c.execute("UPDATE work_item SET current_generation=?, attempts_started=attempts_started+1 WHERE task_id=? AND work_item_id=?",
                      (gen, task_id, work_item_id))
            return {"attempt_id": aid, "generation": gen, "attempts_started": w["attempts_started"] + 1}
        return self._tx(go)

    # ------------------------------------------------------------------ evidence and terminal facts
    def _store_blob(self, c, text) -> Optional[str]:
        if text is None:
            return None
        sha = _sha(text)
        c.execute("INSERT OR IGNORE INTO evidence_blob(sha,content,chars) VALUES(?,?,?)", (sha, text, len(text)))
        return sha

    def record_tool_call(self, o, attempt_id, task_id, work_item_id):
        """Idempotent on (attempt_id, seq). Stored even for an invalidated attempt (history), never alters decisions."""
        def go(c):
            self._maybe_fail("evidence")
            sha = self._store_blob(c, o.result_text) if o.result_text is not None else None
            c.execute("INSERT OR REPLACE INTO tool_call(attempt_id,seq,task_id,work_item_id,tool,args_json,outcome,reached_tool,"
                      "refusal_kind,refusal_detected_by,evidence_kind,paths_read_json,files_json,reason,result_sha,result_len,"
                      "agent_run_id,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (attempt_id, o.seq, task_id, work_item_id, o.tool, _j(o.args), o.outcome,
                       None if o.reached_tool is None else int(bool(o.reached_tool)), o.refusal_kind, o.refusal_detected_by,
                       o.evidence_kind, _j(o.paths_read), _j([{"path": f.path, "status": f.status, "reason": f.reason} for f in o.files]),
                       (o.reason or "")[:300], sha, o.result_len, o.agent_run_id, time.time()))
        self._tx(go)

    def record_terminal(self, attempt_id, terminal: dict, agent_run_id, text: str, new_state: Optional[str] = None):
        """Persist the Agent's own terminal facts and text hash. Transitions RUNNING->FINISHED unless the attempt was
        already invalidated (state kept; facts still recorded)."""
        def go(c):
            a = c.execute("SELECT state FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if a is None:
                raise LedgerError(f"unknown attempt {attempt_id}")
            sha = self._store_blob(c, text or "")
            st = a["state"]
            target = new_state or FINISHED
            if st == RUNNING and target in TRANSITIONS[RUNNING]:
                st = target
            c.execute("UPDATE attempt SET agent_run_id=COALESCE(?,agent_run_id), terminal_json=?, text_sha=?, state=?, ended_at=COALESCE(ended_at,?) WHERE attempt_id=?",
                      (agent_run_id, _j(terminal), sha, st, time.time(), attempt_id))
        self._tx(go)

    def invalidate(self, attempt_id, state: str, reason: str):
        """Application decision that an attempt is dead (cancel / timeout / abandon). Only from a live state."""
        def go(c):
            a = c.execute("SELECT state FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if a is None:
                raise LedgerError(f"unknown attempt {attempt_id}")
            if a["state"] in (RUNNING, FINISHED) and state in TRANSITIONS[a["state"]]:
                c.execute("UPDATE attempt SET state=?, invalidated_reason=?, ended_at=COALESCE(ended_at,?) WHERE attempt_id=?",
                          (state, reason, time.time(), attempt_id))
                return True
            return False
        return self._tx(go)

    # ------------------------------------------------------------------ atomic decision + registration
    def commit_decision(self, attempt_id, d: dict, text: str) -> dict:
        """`d` is the application's Decision as a plain dict (see decision_to_dict). Atomically: idempotent per attempt;
        accept only if this attempt is the work item's CURRENT generation, not invalidated, and no accepted record of an
        equal or newer generation exists. Anything else is stored as a REJECTED/withheld decision, never as accepted."""
        def go(c):
            self._maybe_fail("commit")
            prior = c.execute("SELECT acceptance FROM decision WHERE attempt_id=?", (attempt_id,)).fetchone()
            if prior is not None:
                r = c.execute("SELECT status, reason FROM registration WHERE attempt_id=?", (attempt_id,)).fetchone()
                return {"registered": False, "reason": "IDEMPOTENT_DUPLICATE", "no_status_regression": True,
                        "first_acceptance": prior["acceptance"], "first_registration": r["status"] if r else None}
            a = c.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if a is None:
                raise LedgerError(f"unknown attempt {attempt_id}")
            w = c.execute("SELECT * FROM work_item WHERE task_id=? AND work_item_id=?", (a["task_id"], a["work_item_id"])).fetchone()
            acceptance, cause, validity = d["acceptance"], d.get("not_accepted_cause"), d.get("validity")
            invalid = None
            override = None          # set only when the LEDGER forced the outcome to NOT_ACCEPTED (not the caller)
            if a["state"] in INVALIDATED:
                invalid = f"ATTEMPT_{a['state']}"
            elif a["generation"] != w["current_generation"]:
                invalid = "SUPERSEDED_STALE_GENERATION"
            elif a["state"] not in (RUNNING, FINISHED):
                invalid = f"ATTEMPT_ALREADY_{a['state']}"
            if invalid is not None:
                acceptance, cause, validity = "NOT_ACCEPTED", cause or invalid, invalid
                override = invalid
            sha = self._store_blob(c, text or "")
            kind, status, reason = "withheld", acceptance, None
            reason_text = (d.get("reason") or "")[:600]
            if invalid is not None:
                reason_text = f"attempt not current: {invalid}"
            new_state = WITHHELD
            if acceptance in ACCEPTING:
                if w["accepted_generation"] is not None and w["accepted_generation"] >= a["generation"]:
                    acceptance, cause, validity = "NOT_ACCEPTED", "WOULD_OVERWRITE_NEWER_OR_EQUAL_GENERATION", "STALE"
                    kind, status, new_state = "rejected", "NOT_ACCEPTED", REJECTED
                    reason_text = f"attempt not current: {cause}"
                    override = cause
                else:
                    cur = c.execute("UPDATE work_item SET accepted_attempt_id=?, accepted_generation=?, accepted_status=? "
                                    "WHERE task_id=? AND work_item_id=? AND current_generation=? AND "
                                    "(accepted_generation IS NULL OR accepted_generation < ?)",
                                    (attempt_id, a["generation"], acceptance, a["task_id"], a["work_item_id"], a["generation"], a["generation"]))
                    if cur.rowcount != 1:
                        acceptance, cause, validity = "NOT_ACCEPTED", "LOST_RACE", "STALE"
                        kind, status, new_state = "rejected", "NOT_ACCEPTED", REJECTED
                        reason_text = f"attempt not current: {cause}"
                        override = cause
                    else:
                        kind, status, new_state = "accepted", acceptance, ACCEPTED
            elif acceptance == "NOT_ACCEPTED":
                kind, new_state = "rejected", REJECTED
            c.execute("INSERT INTO decision(attempt_id,task_id,work_item_id,generation,acceptance,execution_status,validity,assessor_status,"
                      "assessor_reason,not_accepted_cause,reason,overrides_json,gate_json,counts_json,assessor_claims_json,requirements_json,"
                      "extra_json,text_sha,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (attempt_id, a["task_id"], a["work_item_id"], a["generation"], acceptance, d.get("execution_status"), validity,
                       d.get("assessor_status"), (d.get("assessor_reason") or "")[:300], cause, reason_text,
                       _j(d.get("overrides")), _j(d.get("gate")), _j(d.get("counts")), _j(d.get("assessor_claims")),
                       _j(d.get("requirements")), _j({"execution_reason": d.get("execution_reason"), "identity": d.get("identity")}),
                       sha, time.time()))
            c.execute("INSERT INTO registration(attempt_id,task_id,work_item_id,generation,kind,status,reason,registered_at) VALUES(?,?,?,?,?,?,?,?)",
                      (attempt_id, a["task_id"], a["work_item_id"], a["generation"], kind, status, cause or "", time.time()))
            if a["state"] in (RUNNING, FINISHED):
                c.execute("UPDATE attempt SET state=?, ended_at=COALESCE(ended_at,?) WHERE attempt_id=?", (new_state, time.time(), attempt_id))
            return {"registered": kind == "accepted", "reason": acceptance if kind == "accepted" else (cause or f"WITHHELD_{acceptance}"),
                    "kind": kind, "acceptance": acceptance, "generation": a["generation"], "reason_text": reason_text, "override": override}
        return self._tx(go)

    # ------------------------------------------------------------------ finalization (idempotent)
    def finalize_task(self, task_id, final_text: str, audit: dict) -> dict:
        def go(c):
            self._maybe_fail("finalize")
            t = c.execute("SELECT status, final_sha, final_text FROM task WHERE task_id=?", (task_id,)).fetchone()
            if t is None:
                raise LedgerError(f"unknown task {task_id}")
            if t["status"] == "FINALIZED":
                return {"finalized_now": False, "identical": t["final_sha"] == _sha(final_text), "final_text": t["final_text"],
                        "reason": "IDEMPOTENT_DUPLICATE"}
            c.execute("UPDATE task SET status='FINALIZED', finalized_at=?, final_sha=?, final_text=?, audit_json=? WHERE task_id=? AND status<>'FINALIZED'",
                      (time.time(), _sha(final_text), final_text, _j(audit), task_id))
            return {"finalized_now": True, "identical": True, "final_text": final_text, "reason": "FINALIZED"}
        return self._tx(go)

    # ------------------------------------------------------------------ recovery
    def recover(self, instance_id: Optional[str] = None, pid: Optional[int] = None, reason: Optional[str] = None) -> dict:
        """Mark orphaned live attempts ABANDONED (never accepted, never resumed). With `pid`: only that worker's attempts.
        Without: every live attempt of this instance (called at service start, when no worker of the previous process
        can still be legitimately running). Tasks left with no live attempts and no final are marked INTERRUPTED."""
        inst = instance_id or self.instance_id

        def go(c):
            q = "SELECT attempt_id, task_id FROM attempt WHERE owner_instance=? AND state IN (?,?)"
            args = [inst, RUNNING, FINISHED]
            if pid is not None:
                q += " AND owner_pid=?"; args.append(pid)
            rows = c.execute(q, args).fetchall()
            for r in rows:
                c.execute("UPDATE attempt SET state=?, invalidated_reason=?, ended_at=COALESCE(ended_at,?) WHERE attempt_id=?",
                          (ABANDONED, reason or "owner process gone (recovery)", time.time(), r["attempt_id"]))
            tasks = {r["task_id"] for r in rows}
            # A task that is still RUNNING after its worker (pid) or its whole service instance is gone can never be
            # finalized by its own process: mark it INTERRUPTED (accepted attempts and decisions are kept untouched).
            if pid is not None:
                q2 = ("SELECT DISTINCT t.task_id FROM task t JOIN attempt a ON a.task_id=t.task_id "
                      "WHERE t.status='RUNNING' AND a.owner_instance=? AND a.owner_pid=?")
                stale = {r["task_id"] for r in c.execute(q2, (inst, pid)).fetchall()}
            else:
                stale = {r["task_id"] for r in c.execute("SELECT task_id FROM task WHERE status='RUNNING' AND instance_id=?", (inst,)).fetchall()}
            for t in tasks | stale:
                live = c.execute("SELECT COUNT(*) n FROM attempt WHERE task_id=? AND state IN (?,?)", (t, RUNNING, FINISHED)).fetchone()["n"]
                if not live:
                    c.execute("UPDATE task SET status='INTERRUPTED' WHERE task_id=? AND status='RUNNING'", (t,))
            return {"abandoned": [r["attempt_id"] for r in rows], "tasks_interrupted": sorted(tasks | stale)}
        return self._tx(go)

    # ------------------------------------------------------------------ read side
    def get_task(self, task_id) -> Optional[dict]:
        c = self._conn()
        try:
            t = c.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
            if t is None:
                return None
            items = []
            for w in c.execute("SELECT * FROM work_item WHERE task_id=? ORDER BY work_item_id", (task_id,)).fetchall():
                atts = []
                for a in c.execute("SELECT * FROM attempt WHERE task_id=? AND work_item_id=? ORDER BY generation", (task_id, w["work_item_id"])).fetchall():
                    calls = c.execute("SELECT outcome, COUNT(*) n FROM tool_call WHERE attempt_id=? GROUP BY outcome", (a["attempt_id"],)).fetchall()
                    d = c.execute("SELECT acceptance, execution_status, validity, assessor_status, assessor_reason, not_accepted_cause, reason, overrides_json, gate_json FROM decision WHERE attempt_id=?", (a["attempt_id"],)).fetchone()
                    gate = json.loads(d["gate_json"]) if d and d["gate_json"] else {}
                    atts.append({"attempt_id": a["attempt_id"], "generation": a["generation"], "state": a["state"],
                                 "workflow_run_id": a["workflow_run_id"], "agent_run_id": a["agent_run_id"],
                                 "owner_instance": a["owner_instance"], "invalidated_reason": a["invalidated_reason"],
                                 "agent_terminal": json.loads(a["terminal_json"]) if a["terminal_json"] else None,
                                 "tool_calls": {r["outcome"]: r["n"] for r in calls},
                                 "decision": ({k: d[k] for k in ("acceptance", "execution_status", "validity", "assessor_status", "assessor_reason", "not_accepted_cause", "reason")} |
                                              {"overrides": json.loads(d["overrides_json"] or "[]"),
                                               "requirements_missing": [m.get("id") for m in gate.get("missing", [])],
                                               "requirements_satisfied": [m.get("id") for m in gate.get("satisfied", [])]}) if d else None})
                items.append({"work_item_id": w["work_item_id"], "title": w["title"], "current_generation": w["current_generation"],
                              "attempts_started": w["attempts_started"], "accepted_attempt_id": w["accepted_attempt_id"],
                              "accepted_generation": w["accepted_generation"], "accepted_status": w["accepted_status"], "attempts": atts})
            return {"task_id": t["task_id"], "workflow_run_id": t["workflow_run_id"], "instance_id": t["instance_id"], "status": t["status"],
                    "created_at": t["created_at"], "finalized_at": t["finalized_at"], "final_sha": t["final_sha"], "work_items": items}
        finally:
            c.close()

    def list_tasks(self, limit: int = 20) -> list:
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT task_id, workflow_run_id, instance_id, status, created_at, finalized_at, final_sha FROM task "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
        finally:
            c.close()

    def load_for_reconstruction(self, task_id) -> Optional[dict]:
        """Raw durable records (decisions, attempt text, spec) from which the final answer can be rebuilt."""
        c = self._conn()
        try:
            t = c.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
            if t is None:
                return None
            items = []
            for w in c.execute("SELECT * FROM work_item WHERE task_id=? ORDER BY work_item_id", (task_id,)).fetchall():
                atts = []
                for a in c.execute("SELECT * FROM attempt WHERE task_id=? AND work_item_id=? ORDER BY generation", (task_id, w["work_item_id"])).fetchall():
                    d = c.execute("SELECT * FROM decision WHERE attempt_id=?", (a["attempt_id"],)).fetchone()
                    txt = c.execute("SELECT content FROM evidence_blob WHERE sha=?", (a["text_sha"],)).fetchone() if a["text_sha"] else None
                    atts.append({"attempt": dict(a), "decision": dict(d) if d else None, "text": txt["content"] if txt else ""})
                items.append({"work_item": dict(w), "attempts": atts})
            return {"task": dict(t), "items": items}
        finally:
            c.close()

    def evidence_text(self, sha: str) -> Optional[str]:
        c = self._conn()
        try:
            r = c.execute("SELECT content FROM evidence_blob WHERE sha=?", (sha,)).fetchone()
            return r["content"] if r else None
        finally:
            c.close()


def decision_to_dict(d, requirements=None) -> dict:
    """Serialise an acceptance.Decision (plus its evidence requirements) to the plain dict the ledger stores."""
    return {"acceptance": d.acceptance, "execution_status": d.execution_status, "validity": d.validity,
            "assessor_status": d.assessor_status, "assessor_reason": d.assessor_reason, "not_accepted_cause": d.not_accepted_cause,
            "reason": d.reason, "overrides": d.overrides, "gate": d.gate, "counts": d.counts, "assessor_claims": d.assessor_claims,
            "requirements": requirements, "execution_reason": d.execution_reason, "identity": d.identity}
