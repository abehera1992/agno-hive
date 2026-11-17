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

SCHEMA_VERSION = 4   # Phase 1.22: + attempt.trace_sha (see init_schema()'s migration).
                     # Phase 1.55: + attempt.role/stage_name/stage_index/parent_attempt_id
                     # (nullable, forward-compatible lineage columns -- see init_schema()'s
                     # migration and start_attempt()'s own docstring for what populates them
                     # today vs. what remains unset pending real multi-stage orchestration)
                     # and the new proposal/proposal_work_item tables (see record_proposal()).
                     # Phase 1.56c: + proposal.raw_proposal_json -- the complete original
                     # WorkProposal (every work item's objective/role/dependencies/
                     # capabilities/skills/acceptance_criteria/expected_evidence), persisted for
                     # BOTH valid and invalid proposals (NULL only when proposal itself is None --
                     # generation/parsing produced nothing at all to persist). validate_proposal()'s
                     # verdict (valid/errors/unauthorized_skills/unverified_capabilities) stays in
                     # its own existing columns, unchanged -- this is additive storage of the
                     # ORIGINAL proposal, not a second copy of the validation outcome.
                     # proposal_work_item is UNCHANGED: still written only when valid=1, still the
                     # only place anything work-item-shaped for a REJECTED proposal does not exist.
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
  invalidated_reason TEXT, terminal_json TEXT, text_sha TEXT, trace_sha TEXT,
  -- Phase 1.55, all nullable/additive: `role` is the T._member_key name run_attempt() actually
  -- selected (populated by start_attempt() for every attempt from Phase 1.55 onward, NULL for
  -- every attempt recorded before this phase -- honest, not backfilled). `stage_name`/
  -- `stage_index` exist for a FUTURE multi-stage sequencer this phase does not implement --
  -- left NULL by every current caller, see start_attempt()'s own docstring. `parent_attempt_id`
  -- is populated ONLY for a recovery attempt (recover_and_reconcile()'s own inner run_attempt()
  -- call) -- the first-class lineage pointer for a relationship this codebase already tracked,
  -- less directly, inside decision.extra_json's `recovery_info` since Phase 1.25.
  role TEXT, stage_name TEXT, stage_index INTEGER, parent_attempt_id TEXT REFERENCES attempt(attempt_id),
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
-- Phase 1.55: the Researcher's structured WorkProposal (swarm.proposal.WorkProposal) and its
-- deterministic validate_proposal() outcome, for the attempt that produced it. `work_item_id`
-- here is the APPLICATION'S OWN dispatch work item (the real `work_item` table's own row the
-- acting attempt was running for) -- never to be confused with `proposal_work_item`'s own
-- `proposed_work_item_id`, which is the Researcher's PROPOSED sub-item identifier, a nested,
-- separate concept with no row in `work_item` at all. `version` is a forward-compatible field
-- (always 1 today -- no proposal-revision mechanism exists yet).
CREATE TABLE IF NOT EXISTS proposal (
  proposal_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempt(attempt_id),
  task_id TEXT NOT NULL, work_item_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  objective TEXT NOT NULL, valid INTEGER NOT NULL, errors_json TEXT NOT NULL,
  unauthorized_skills_json TEXT NOT NULL, unverified_capabilities_json TEXT NOT NULL,
  created_at REAL NOT NULL, raw_proposal_json TEXT);
-- One row per proposed sub-item of a proposal. Written ONLY when that proposal's own `valid`
-- is 1 -- record_proposal()'s own enforcement of "an invalid proposal never becomes executable
-- work": an invalid proposal's `proposal` row still records what was proposed and why it was
-- rejected (objective/errors_json), but produces ZERO rows here, so there is nothing
-- work-item-shaped for any future orchestration code to read and dispatch.
CREATE TABLE IF NOT EXISTS proposal_work_item (
  proposal_id TEXT NOT NULL REFERENCES proposal(proposal_id), proposed_work_item_id TEXT NOT NULL,
  objective TEXT NOT NULL, role TEXT NOT NULL, depends_on_json TEXT NOT NULL,
  required_capabilities_json TEXT NOT NULL, proposed_skills_json TEXT NOT NULL,
  acceptance_criteria TEXT, expected_evidence TEXT,
  PRIMARY KEY (proposal_id, proposed_work_item_id));
"""

# Phase 1.55: split out from _DDL above and run SEPARATELY, AFTER the version-gated ALTER
# TABLE migration below. ix_attempt_parent references attempt.parent_attempt_id, a column
# that does not exist yet on a pre-1.55 database until that migration's ALTER runs --
# CREATE INDEX IF NOT EXISTS still raises "no such column" against a table that is missing
# the column, so index creation on a possibly-new column can never be unconditional the way
# _DDL's CREATE TABLE statements are (those only ever create what's entirely absent).
_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_attempt_state ON attempt(state, owner_instance);
CREATE INDEX IF NOT EXISTS ix_attempt_item ON attempt(task_id, work_item_id);
CREATE INDEX IF NOT EXISTS ix_attempt_parent ON attempt(parent_attempt_id);
CREATE INDEX IF NOT EXISTS ix_proposal_task_item ON proposal(task_id, work_item_id);
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
            # 1) Tables only (CREATE TABLE IF NOT EXISTS, every time, any version, fresh or
            # existing) -- a brand-new table (proposal/proposal_work_item, Phase 1.55) needs no
            # version-gated migration at all. Indexes are deliberately NOT included here and run
            # LAST (step 3 below): ix_attempt_parent references attempt.parent_attempt_id, a
            # column that may not exist yet on a pre-1.55 database until step 2's ALTER runs --
            # unlike CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS still fails with "no
            # such column" if the table exists but the column doesn't, so index creation can
            # never safely precede a column migration the way table creation can.
            for stmt in [s for s in _DDL.split(";") if s.strip()]:
                c.execute(stmt)

            # 2) Version-gated column migration for the EXISTING attempt table (SQLite's CREATE
            # TABLE IF NOT EXISTS above cannot retroactively add a column to it).
            row = c.execute("SELECT v FROM ledger_meta WHERE k='schema_version'").fetchone()
            if row is None:
                # Fresh database: _DDL above already declares every current column (trace_sha,
                # role/stage_name/stage_index/parent_attempt_id), nothing to migrate.
                c.execute("INSERT INTO ledger_meta(k,v) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            else:
                current = int(row["v"])
                if current != SCHEMA_VERSION:
                    if current not in (1, 2, 3):
                        raise LedgerError(f"ledger schema version {current} != expected {SCHEMA_VERSION} "
                                          f"(no migration path from {current}); refusing to use it")
                    # Sequential, not a single pairwise check: a database still at version 1 must
                    # step through BOTH migrations below to reach SCHEMA_VERSION, not just the
                    # latest one.
                    if current == 1:
                        # Phase 1.22, additive only: separate the full diagnostic trace from the
                        # canonical attempt result. No existing row is altered or removed;
                        # trace_sha is NULL for every attempt recorded before this migration (a
                        # full trace was never captured for them -- this is honest, not a backfill).
                        cols = {r["name"] for r in c.execute("PRAGMA table_info(attempt)").fetchall()}
                        if "trace_sha" not in cols:
                            c.execute("ALTER TABLE attempt ADD COLUMN trace_sha TEXT")
                        current = 2
                    if current == 2:
                        # Phase 1.55, additive only: role/stage/parent-attempt lineage columns.
                        # NULL for every attempt recorded before this migration -- not
                        # backfilled, since none of this information was ever captured for them.
                        cols = {r["name"] for r in c.execute("PRAGMA table_info(attempt)").fetchall()}
                        for col, decl in (("role", "TEXT"), ("stage_name", "TEXT"), ("stage_index", "INTEGER"),
                                          ("parent_attempt_id", "TEXT")):
                            if col not in cols:
                                c.execute(f"ALTER TABLE attempt ADD COLUMN {col} {decl}")
                        current = 3
                    if current == 3:
                        # Phase 1.56c, additive only: the complete original WorkProposal, for
                        # both valid and invalid proposals. NULL for every proposal row recorded
                        # before this migration -- not backfilled, since the original structured
                        # output was never captured for them (only the validation verdict was).
                        cols = {r["name"] for r in c.execute("PRAGMA table_info(proposal)").fetchall()}
                        if "raw_proposal_json" not in cols:
                            c.execute("ALTER TABLE proposal ADD COLUMN raw_proposal_json TEXT")
                        current = 4
                    c.execute("UPDATE ledger_meta SET v=? WHERE k='schema_version'", (str(SCHEMA_VERSION),))

            # 3) Indexes -- every column any index references is now guaranteed to exist,
            # whether this was a fresh database (step 1's CREATE TABLE already declared it) or an
            # existing one that step 2 just migrated.
            for stmt in [s for s in _DDL_INDEXES.split(";") if s.strip()]:
                c.execute(stmt)
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

    def start_attempt(self, task_id, work_item_id, workflow_run_id, max_attempts: int, owner_pid: Optional[int] = None,
                      role: Optional[str] = None, stage_name: Optional[str] = None, stage_index: Optional[int] = None,
                      parent_attempt_id: Optional[str] = None) -> dict:
        """Allocates the next generation and persists the attempt as RUNNING, superseding older live attempts of the SAME
        work item only, in one transaction. Enforces the retry budget across restarts.

        `role`/`stage_name`/`stage_index`/`parent_attempt_id` (Phase 1.55, all optional, all default None --
        every pre-1.55 caller is unaffected): `role` is run_attempt()'s own T._member_key name for whichever
        member actually acts this attempt -- the SAME value already threaded since Phase 1.52, now also
        persisted rather than only held in-memory. `stage_name`/`stage_index` exist for a future multi-stage
        sequencer this phase does not implement; no current caller sets them. `parent_attempt_id` is the
        ORIGINAL attempt's id when this attempt is a recovery pass (recover_and_reconcile()'s own inner call)
        -- None for every original/retry attempt, which have no parent."""
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
            c.execute("INSERT INTO attempt(attempt_id,task_id,work_item_id,generation,workflow_run_id,state,owner_instance,"
                      "owner_pid,started_at,role,stage_name,stage_index,parent_attempt_id)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, task_id, work_item_id, gen, workflow_run_id, RUNNING, self.instance_id, owner_pid or os.getpid(), time.time(),
                       role, stage_name, stage_index, parent_attempt_id))
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

    def record_terminal(self, attempt_id, terminal: dict, agent_run_id, text: str, new_state: Optional[str] = None,
                        trace_text: Optional[str] = None):
        """Persist the Agent's own terminal facts, the canonical text hash, and (Phase 1.22, additive) the full
        diagnostic trace's own hash under a SEPARATE column -- the same content-addressed evidence_blob store, so an
        identical trace never duplicates storage. `trace_text=None` (every caller before Phase 1.22, and this one's
        own default) leaves trace_sha NULL rather than writing an empty-string hash, so "no trace was captured" stays
        distinguishable from "a trace was captured and happened to be empty". Transitions RUNNING->FINISHED unless the
        attempt was already invalidated (state kept; facts still recorded)."""
        def go(c):
            a = c.execute("SELECT state FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if a is None:
                raise LedgerError(f"unknown attempt {attempt_id}")
            sha = self._store_blob(c, text or "")
            trace_sha = self._store_blob(c, trace_text) if trace_text is not None else None
            st = a["state"]
            target = new_state or FINISHED
            if st == RUNNING and target in TRANSITIONS[RUNNING]:
                st = target
            c.execute("UPDATE attempt SET agent_run_id=COALESCE(?,agent_run_id), terminal_json=?, text_sha=?, "
                      "trace_sha=COALESCE(?,trace_sha), state=?, ended_at=COALESCE(ended_at,?) WHERE attempt_id=?",
                      (agent_run_id, _j(terminal), sha, trace_sha, st, time.time(), attempt_id))
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
                       _j(d.get("requirements")), _j({"execution_reason": d.get("execution_reason"), "identity": d.get("identity"),
                       # Phase 1.23: additive, no schema change -- extra_json is the pre-existing designed
                       # extension point for exactly this (see this column's own DDL comment history).
                       "terminal_integrity": d.get("terminal_integrity"), "coverage_status": d.get("coverage_status"),
                       # Phase 1.25: durable recovery provenance + budget snapshot, same extra_json
                       # extension point -- no schema change. is_recovery/recovery_info are None/False
                       # for every initial attempt; budget is set for every attempt (initial or
                       # recovery) by run_attempt()'s own finally block.
                       "is_recovery": d.get("is_recovery"), "recovery_info": d.get("recovery_info"),
                       "budget": d.get("budget")}),
                       sha, time.time()))
            c.execute("INSERT INTO registration(attempt_id,task_id,work_item_id,generation,kind,status,reason,registered_at) VALUES(?,?,?,?,?,?,?,?)",
                      (attempt_id, a["task_id"], a["work_item_id"], a["generation"], kind, status, cause or "", time.time()))
            if a["state"] in (RUNNING, FINISHED):
                c.execute("UPDATE attempt SET state=?, ended_at=COALESCE(ended_at,?) WHERE attempt_id=?", (new_state, time.time(), attempt_id))
            return {"registered": kind == "accepted", "reason": acceptance if kind == "accepted" else (cause or f"WITHHELD_{acceptance}"),
                    "kind": kind, "acceptance": acceptance, "generation": a["generation"], "reason_text": reason_text, "override": override}
        return self._tx(go)

    # ------------------------------------------------------------------ structured proposals (Phase 1.55)
    def record_proposal(self, attempt_id: str, task_id: str, work_item_id: str, proposal, validation) -> dict:
        """Persist the attempt's structured WorkProposal (swarm.proposal.WorkProposal, or None if
        generation itself failed) and its deterministic ValidationResult. Idempotent per attempt_id,
        same pattern as commit_decision(): a second call for the same attempt_id is a no-op, never a
        duplicate row. `work_item_id` here is the application's OWN dispatch work item (this attempt's
        real work_item row) -- never the proposal's internally-proposed sub-item ids.

        ENFORCES "an invalid proposal never becomes executable work": proposal_work_item rows are
        written ONLY when validation.valid is True. An invalid (or altogether missing, proposal=None)
        proposal still gets its own `proposal` row -- objective/errors/disclosures, for audit -- but
        zero proposal_work_item rows, so nothing work-item-shaped exists for any future orchestration
        code to read and dispatch from a rejected proposal.

        Phase 1.56c: `raw_proposal_json` persists the COMPLETE original WorkProposal (every work
        item's objective/role/dependencies/required_capabilities/proposed_skills/
        acceptance_criteria/expected_evidence) for BOTH valid and invalid proposals -- the whole
        point is to keep a rejected proposal's full original content auditable, not just that it
        was rejected and why. NULL only when `proposal` itself is None (generation/parsing produced
        no structured object at all -- nothing to persist). This is independent of, and never
        substitutes for, proposal_work_item: a rejected proposal's items still never get a
        proposal_work_item row, no matter what raw_proposal_json holds."""
        def go(c):
            self._maybe_fail("proposal")
            prior = c.execute("SELECT proposal_id FROM proposal WHERE attempt_id=?", (attempt_id,)).fetchone()
            if prior is not None:
                return {"recorded": False, "reason": "IDEMPOTENT_DUPLICATE", "proposal_id": prior["proposal_id"]}
            a = c.execute("SELECT attempt_id FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if a is None:
                raise LedgerError(f"unknown attempt {attempt_id}")
            proposal_id = f"prop-{attempt_id}"
            objective = (getattr(proposal, "objective", None) or "") if proposal is not None else ""
            raw_proposal_json = _j(proposal.model_dump()) if proposal is not None else None
            c.execute("INSERT INTO proposal(proposal_id,attempt_id,task_id,work_item_id,version,objective,valid,"
                      "errors_json,unauthorized_skills_json,unverified_capabilities_json,created_at,"
                      "raw_proposal_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (proposal_id, attempt_id, task_id, work_item_id, 1, objective, int(bool(validation.valid)),
                       _j(validation.errors), _j(validation.unauthorized_skills), _j(validation.unverified_capabilities),
                       time.time(), raw_proposal_json))
            n = 0
            if validation.valid and proposal is not None:
                for wi in proposal.work_items:
                    c.execute("INSERT INTO proposal_work_item(proposal_id,proposed_work_item_id,objective,role,"
                              "depends_on_json,required_capabilities_json,proposed_skills_json,acceptance_criteria,"
                              "expected_evidence) VALUES(?,?,?,?,?,?,?,?,?)",
                              (proposal_id, wi.work_item_id, wi.objective, wi.role, _j(wi.depends_on),
                               _j(wi.required_capabilities), _j(wi.proposed_skills), wi.acceptance_criteria,
                               wi.expected_evidence))
                    n += 1
            return {"recorded": True, "proposal_id": proposal_id, "valid": bool(validation.valid), "work_items_persisted": n}
        return self._tx(go)

    def get_proposal(self, attempt_id: str) -> Optional[dict]:
        """Read-side counterpart to record_proposal(). None if no proposal was ever recorded for
        this attempt_id (propose=False, or the attempt never reached the proposal step).

        `raw_proposal` (Phase 1.56c) is the complete original WorkProposal (model_dump()'d dict),
        parsed back from raw_proposal_json -- present for both valid and invalid proposals, None
        only when the column itself is NULL (proposal was None at record time, or a pre-1.56c row
        that predates this field). `work_items` (existing, unchanged) stays the EXECUTABLE view:
        empty for any invalid proposal regardless of what raw_proposal holds, since
        proposal_work_item is never written for one."""
        c = self._conn()
        try:
            p = c.execute("SELECT * FROM proposal WHERE attempt_id=?", (attempt_id,)).fetchone()
            if p is None:
                return None
            items = [dict(r) for r in c.execute(
                "SELECT * FROM proposal_work_item WHERE proposal_id=? ORDER BY proposed_work_item_id",
                (p["proposal_id"],)).fetchall()]
            d = dict(p)
            d["valid"] = bool(d["valid"])
            d["errors"] = json.loads(d.pop("errors_json"))
            d["unauthorized_skills"] = json.loads(d.pop("unauthorized_skills_json"))
            d["unverified_capabilities"] = json.loads(d.pop("unverified_capabilities_json"))
            raw = d.pop("raw_proposal_json", None)
            d["raw_proposal"] = json.loads(raw) if raw else None
            d["work_items"] = items
            return d
        finally:
            c.close()

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
                                 # Phase 1.55: NULL/None for every attempt recorded before this phase, same
                                 # honest-not-backfilled convention as trace_sha (Phase 1.22) above.
                                 "role": a["role"], "stage_name": a["stage_name"], "stage_index": a["stage_index"],
                                 "parent_attempt_id": a["parent_attempt_id"],
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
        """Raw durable records (decisions, attempt text, spec) from which the final answer can be
        rebuilt.

        Phase 1.41b: an attempt with no `decision` row (killed before `decide()`/`record_terminal()`
        ever ran -- confirmed live for the actual T11/T13b battery incidents: `record_tool_call()`
        durably persists EVERY tool call's own full result content, content-addressed in
        `evidence_blob`, as it happens, well before any terminal-level write) is NOT left with
        nothing recoverable. For exactly that case (`decision is None` only -- an attempt that DID
        reach a decision, however unfavourable, already has its own complete, unchanged rendering
        and this never runs for it), this also fetches the attempt's own `tool_call` rows and
        resolves each genuinely useful one's real content via `evidence_blob`, attached as
        `recovered_evidence`. "Genuinely useful" mirrors the SAME generic definition
        `acceptance.has_new_source_evidence()`/`describe_evidence_without_text()` already use
        elsewhere in this codebase for the identical concept (a SUCCESS outcome from a tool that is
        not one of the four STATE_TOOLS) -- reused here as a literal set rather than importing
        `swarm.acceptance` from this low-level module, to avoid adding a new module-level
        dependency for four well-known, rarely-changing names.

        Never touches `decision`/`text`/accepted-pointer handling for ANY attempt -- an accepted
        attempt, or one with any decision at all, is returned exactly as before this phase."""
        _state_tools = {"update_session_state", "load_skill", "forward_member_answer", "request_clarification"}
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
                    rec = {"attempt": dict(a), "decision": dict(d) if d else None, "text": txt["content"] if txt else ""}
                    if d is None:
                        recovered = []
                        for tc in c.execute(
                                "SELECT seq, tool, outcome, evidence_kind, paths_read_json, result_sha, result_len "
                                "FROM tool_call WHERE attempt_id=? ORDER BY seq", (a["attempt_id"],)).fetchall():
                            if tc["outcome"] != "SUCCESS" or tc["tool"] in _state_tools or not tc["result_sha"]:
                                continue
                            try:
                                paths = json.loads(tc["paths_read_json"] or "[]")
                            except Exception:  # noqa: BLE001
                                paths = []
                            recovered.append({"seq": tc["seq"], "tool": tc["tool"], "evidence_kind": tc["evidence_kind"],
                                             "paths": paths, "result_sha": tc["result_sha"], "result_len": tc["result_len"]})
                        if recovered:
                            rec["recovered_evidence"] = recovered
                    atts.append(rec)
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
            "requirements": requirements, "execution_reason": d.execution_reason, "identity": d.identity,
            # Phase 1.23: no schema change -- these ride inside the existing JSON blob this function already
            # produces (same additive principle as `gate` already carrying d.gate's new enum_detail/not_evaluated
            # keys with zero changes needed here). A pre-Phase-1.23 historical decision simply has no such key
            # when read back (not a fabricated "N/A" written retroactively) -- see decision_from_dict-equivalent
            # readers, which must treat a missing key as absence of information, never as a value.
            "terminal_integrity": getattr(d, "terminal_integrity", None),
            "coverage_status": getattr(d, "coverage_status", None),
            # Phase 1.25: same additive principle -- a pre-Phase-1.25 historical decision has none
            # of these keys when read back; is_recovery/recovery_info default to False/None (never
            # fabricated as "yes" for an attempt that predates this phase).
            "is_recovery": getattr(d, "is_recovery", False),
            "recovery_info": getattr(d, "recovery_info", None),
            "budget": getattr(d, "budget", None)}
