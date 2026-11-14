"""Application-owned acceptance boundary (Phase 1.14c/d).

Four independent layers of truth, never conflated:
  1. Workflow execution state      (recorded, never promotes anything)
  2. Agent attempt state           (terminal gate)
  3. Tool execution evidence       (per-invocation outcome + evidence kind)
  4. AgnoHive acceptance state     (ACCEPTED_COMPLETE / ACCEPTED_PARTIAL / UNRESOLVED / BLOCKED / NOT_ACCEPTED)

The existing claim-level assessor (swarm.team._assess_member_result) is reused unchanged in
spirit and stays visible as a diagnostic verdict; this module can only LOWER its verdict (or
refuse to accept), never raise it or manufacture evidence.

No persistence here: attempts/generations live in memory (Phase 1.15 owns the durable ledger).
This module must stay import-light: swarm.team imports it at module load.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import itertools
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

# ---------------------------------------------------------------------------- identity plumbing
# Set by whoever runs an attempt; read by swarm.team's evidence recorder so evidence recorded under
# one attempt id can never be consumed by another (D4).
ATTEMPT_ID: contextvars.ContextVar = contextvars.ContextVar("hive_attempt_id", default=None)

KEY = "researcher"

SUCCESS, FAILED, REFUSED, CANCELLED, TIMED_OUT, UNKNOWN = (
    "SUCCESS", "FAILED", "REFUSED", "CANCELLED", "TIMED_OUT", "UNKNOWN")
OUTCOMES = (SUCCESS, FAILED, REFUSED, CANCELLED, TIMED_OUT, UNKNOWN)

ACCEPTED_COMPLETE, ACCEPTED_PARTIAL, UNRESOLVED, BLOCKED, NOT_ACCEPTED = (
    "ACCEPTED_COMPLETE", "ACCEPTED_PARTIAL", "UNRESOLVED", "BLOCKED", "NOT_ACCEPTED")

# Precedence (which is more restrictive), NOT a confidence scale. A gate may only move a verdict
# to a more restrictive state.
_RESTRICTION = {"COMPLETE": 0, "PARTIAL": 1, "UNRESOLVED": 2, "BLOCKED": 3}


class ToolRefusal(str):
    """A tool-call response that the APPLICATION produced to deny execution (read ceiling, batch cap,
    repeated-failure collapse, duplicate-read stub...). Behaves exactly like the str it wraps for Agno
    and the model; carries a structured marker so accounting never depends on matching message text (D2)."""
    kind: str = "refusal"

    def __new__(cls, text, kind: str = "refusal"):
        obj = super().__new__(cls, text)
        obj.kind = kind
        return obj


# ---------------------------------------------------------------------------- evidence classification
READ_TOOLS = {"get_file_content", "get_files_batch"}
SEARCH_TOOLS = {"search_files", "search_files_batch", "find_files", "count_matches", "lightrag_query"}
DIR_TOOLS = {"list_directory", "list_directory_tree"}
STATE_TOOLS = {"update_session_state", "load_skill", "forward_member_answer", "request_clarification"}

_NUMBERED = re.compile(r"^\s*\d+\t", re.M)
_BATCH_HEADER = re.compile(r"^=== (.+?) ===$", re.M)
_FAILURE_PREFIXES = ("File not found", "ERROR:", "Error:", "Error ", "error:", "Traceback", "Tool error",
                     "No such file", "Permission denied", "PermissionError")
# Fallback ONLY (structured ToolRefusal is primary). Source lines in swarm/team.py.
_REFUSAL_TEXT_FALLBACK = ("REPORT NOW:", "STOP READING AND ANSWER", "REFUSED:", "REPEATED FAILURE COLLAPSED",
                          "Already returned this exact", "STOP calling ")


def norm_path(p) -> str:
    s = str(p or "").strip().strip("`'\"").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/").lower()


def sha(s) -> str:
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()[:12]


@dataclass
class FileResult:
    path: str
    status: str          # READ | SKELETON | TRUNCATED | EMPTY | ERROR | NO_SOURCE_CONTENT | MISSING
    reason: str = ""


def classify_file_body(body: str) -> tuple:
    """(status, reason) for ONE file's returned text (a get_file_content result or one batch section)."""
    b = body or ""
    if not b.strip():
        return "EMPTY", "no content returned"
    s = b.lstrip()
    if s.startswith(_FAILURE_PREFIXES):
        return "ERROR", s.splitlines()[0][:160]
    if "STRUCTURAL SKELETON below" in b[:600] or "too large to return whole" in b[:300]:
        return "SKELETON", "oversized file reduced to a declaration skeleton (bodies elided)"
    if not _NUMBERED.search(b[:6000]):
        return "NO_SOURCE_CONTENT", "result has no line-numbered source content"
    if "... TRUNCATED at" in b[-400:]:
        return "TRUNCATED", "result cut off by the output cap"
    return "READ", ""


def parse_files_batch(text: str, requested: list) -> list:
    """Parse hive-mcp get_files_batch output: sections '=== <path> ===\\n<get_file_content text | ERROR: ...>'
    joined by a blank line, the whole clamped by one output cap (which can cut the LAST section).
    Anything not parseable yields NO_SOURCE_CONTENT / MISSING — never a fabricated read."""
    text = text or ""
    heads = list(_BATCH_HEADER.finditer(text))
    out, seen = [], set()
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        path, body = h.group(1).strip(), text[h.end():end].strip("\n")
        st, why = classify_file_body(body)
        if st == "READ" and i == len(heads) - 1 and "... TRUNCATED at" in text[-400:]:
            st, why = "TRUNCATED", "last section cut off by the batch output cap"
        out.append(FileResult(path, st, why)); seen.add(norm_path(path))
    for r in requested or []:
        if norm_path(r) not in seen:
            out.append(FileResult(str(r), "MISSING", "no section for this requested path in the batch result"))
    return out


@dataclass
class ToolOutcome:
    seq: int
    tool: str
    args: dict
    outcome: str = UNKNOWN
    reason: str = ""
    reached_tool: Optional[bool] = None   # innermost hook ran => the real tool entrypoint was invoked
    refusal_kind: str = ""
    refusal_detected_by: str = ""         # "structured" | "text-fallback"
    evidence_kind: str = "none"           # source_read | search | directory | comparison | state | none
    paths_read: list = field(default_factory=list)
    files: list = field(default_factory=list)   # per-file results for get_files_batch / single read
    cached: bool = False
    result_len: Optional[int] = None
    result_sha: Optional[str] = None
    result_head: str = ""
    tool_call_id: Optional[str] = None
    attempt_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    origin: str = "live-hook"
    result_text: Optional[str] = field(default=None, repr=False)


def evidence_of(tool: str, args, text: str):
    """(evidence_kind, paths_read, per_file_results) for a call whose tool ACTUALLY RAN and returned."""
    a = args if isinstance(args, dict) else {}
    if tool in STATE_TOOLS:
        return "state", [], []
    if tool in READ_TOOLS:
        if tool == "get_file_content":
            p = a.get("relative_path") or a.get("path") or ""
            st, why = classify_file_body(text)
            fr = [FileResult(str(p), st, why)]
        else:
            req = a.get("paths") or a.get("relative_paths") or []
            req = [req] if isinstance(req, str) else list(req)
            fr = parse_files_batch(text, req)
        paths = [norm_path(f.path) for f in fr if f.status == "READ"]
        return ("source_read" if paths else "none"), paths, fr
    if tool in SEARCH_TOOLS:
        return "search", [], []
    if tool in DIR_TOOLS:
        return "directory", [], []
    if tool == "compare_enumerations":
        return "comparison", [], []
    return "none", [], []


def classify_return(c: ToolOutcome, out, out_text: str, prior_success_shas: set) -> None:
    """Classify a call that RETURNED normally. Structural signals first (ToolRefusal type; whether the
    real tool ran); message text is consulted only as a labelled fallback to NAME a refusal."""
    c.result_len = len(out_text or ""); c.result_sha = sha(out_text)
    c.result_head = (out_text or "")[:160]; c.result_text = out_text
    if isinstance(out, ToolRefusal):
        c.outcome, c.refusal_kind, c.refusal_detected_by = REFUSED, out.kind, "structured"
        c.reason = (out_text or "")[:200]
        return
    if c.reached_tool is False:
        for m in _REFUSAL_TEXT_FALLBACK:
            if (out_text or "").lstrip().startswith(m):
                c.outcome, c.refusal_kind, c.refusal_detected_by = REFUSED, "text-marker:" + m.strip(), "text-fallback"
                c.reason = (out_text or "")[:200]
                return
        if c.result_sha in prior_success_shas:   # hook re-served an earlier genuine result (read cache)
            c.outcome, c.cached = SUCCESS, True
            c.evidence_kind, c.paths_read, c.files = evidence_of(c.tool, c.args, out_text)
            return
        c.outcome, c.reason = UNKNOWN, "hook-generated response; tool did not execute; unrecognised"
        return
    if (out_text or "").lstrip().startswith(_FAILURE_PREFIXES) and c.tool not in READ_TOOLS:
        c.outcome, c.reason = FAILED, (out_text or "")[:200]
        return
    c.evidence_kind, c.paths_read, c.files = evidence_of(c.tool, c.args, out_text)
    if c.tool in READ_TOOLS and c.files and all(f.status in ("ERROR", "MISSING") for f in c.files):
        c.outcome, c.reason = FAILED, "; ".join(f"{f.path}: {f.reason}" for f in c.files)[:200]
        return
    c.outcome = SUCCESS


_CALL: contextvars.ContextVar = contextvars.ContextVar("hive_tool_call", default=None)


class OutcomeRecorder:
    """Two hooks per attempt. `outer` (first in tool_hooks) records identity at call start and a final outcome
    independent of normal return; `inner` (last in tool_hooks) runs only if the real tool entrypoint is reached,
    which proves the tool executed rather than a hook answering in its place. No monkeypatching of tools."""

    def __init__(self, attempt_id: str, tool_deadline: float = None):
        self.attempt_id = attempt_id
        self.calls: list = []
        self._seq = itertools.count(1)
        self.deadline = tool_deadline
        self._succ_shas: set = set()

    def hooks(self):
        me = self

        async def acceptance_outcome_outer(function_name, function, args, agent=None, run_context=None):
            c = ToolOutcome(seq=next(me._seq), tool=function_name, args=dict(args or {}), reached_tool=False,
                            attempt_id=me.attempt_id, agent_run_id=getattr(run_context, "run_id", None))
            me.calls.append(c)
            tok = _CALL.set(c)
            tok_a = ATTEMPT_ID.set(me.attempt_id)
            try:
                coro = function(**args)
                out = await (asyncio.wait_for(coro, me.deadline) if me.deadline else coro)
            except asyncio.CancelledError:
                c.outcome, c.reason = CANCELLED, "CancelledError"
                raise
            except (asyncio.TimeoutError, TimeoutError) as e:
                c.outcome, c.reason = TIMED_OUT, f"{type(e).__name__} (deadline={me.deadline})"
                raise
            except BaseException as e:  # noqa: BLE001 -- recorded, then re-raised unchanged
                c.outcome, c.reason = FAILED, repr(e)[:300]
                raise
            finally:
                _CALL.reset(tok)
                ATTEMPT_ID.reset(tok_a)
            text = out if isinstance(out, str) else _text_of(out)
            classify_return(c, out, text, me._succ_shas)
            if c.outcome == SUCCESS and c.result_sha:
                me._succ_shas.add(c.result_sha)
            return out

        async def acceptance_outcome_inner(function_name, function, args):
            c = _CALL.get()
            if c is not None:
                c.reached_tool = True
            return await function(**args)

        return acceptance_outcome_outer, acceptance_outcome_inner


def _text_of(result) -> str:
    if isinstance(result, str):
        return result
    for attr in ("content", "text", "data", "output", "result"):
        v = getattr(result, attr, None)
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            parts = [b if isinstance(b, str) else getattr(b, "text", None) for b in v]
            parts = [p for p in parts if isinstance(p, str)]
            if parts:
                return "\n".join(parts)
    return str(result)


def count_outcomes(outcomes) -> dict:
    allc = {k: 0 for k in OUTCOMES}
    inv = {k: 0 for k in OUTCOMES}
    for o in outcomes:
        allc[o.outcome] += 1
        if o.tool not in STATE_TOOLS:
            inv[o.outcome] += 1
    reads = sorted({p for o in outcomes if o.outcome == SUCCESS and o.evidence_kind == "source_read" for p in o.paths_read})
    return {"all": allc, "investigation": inv, "source_files_read": reads}


# ---------------------------------------------------------------------------- required-evidence policy
@dataclass
class Requirement:
    id: str
    description: str
    any_of_paths: list


@dataclass
class Link:
    """R5: a relationship between two required files, evidenced in the SOURCE TEXT this attempt read
    (not in the model's prose), and named in the report."""
    id: str
    from_req: str
    to_req: str
    from_regex: str
    to_regex: str
    report_tokens: list
    description: str = ""


@dataclass
class EvidencePolicy:
    name: str
    requirements: list
    links: list = field(default_factory=list)


_FORM = "auth/register/business/businessregistrationform.tsx"
T11_DOC_UPLOAD_POLICY = EvidencePolicy(
    name="t11-doc-upload-chain",
    requirements=[
        Requirement("R1_frontend_business_registration_form", "frontend business-registration form (upload initiation)", [_FORM]),
        Requirement("R2_storage_upload_handler", "storage-service upload endpoint and handler", ["api/storage-service/router/storage_api.py"]),
        Requirement("R3_business_document_registration", "business-service document registration", ["api/business-service/router/business_api.py"]),
        Requirement("R4_business_admin_seller_verification", "business-admin seller verification", ["api/business-service/router/business_admin_api.py"]),
    ],
    links=[
        Link("L1_frontend_to_storage", "R1_frontend_business_registration_form", "R2_storage_upload_handler",
             r"/api/storageservice/upload", r'@router\.post\(\s*"/upload"', ["storageservice/upload", "upload_file"],
             "form posts to the upload URL; storage router exposes POST /upload"),
        Link("L2_frontend_to_registration", "R1_frontend_business_registration_form", "R3_business_document_registration",
             r"/api/businessservice/business/[^`'\"]*/documents", r'@router\.post\(\s*"/\{business_id\}/documents"',
             ["/documents", "register_documents"], "form posts file refs to /documents; business router registers them"),
        Link("L3_registration_to_verification", "R3_business_document_registration", "R4_business_admin_seller_verification",
             r"BusinessDocument\(", r'@router\.post\(\s*"/verify/\{business_id\}"', ["verify_seller", "/verify/"],
             "registration creates BusinessDocument rows; admin router verifies the seller by business_id"),
    ])


def _read_sources(outcomes) -> dict:
    """normalized path -> concatenated source text, from this attempt's own SUCCESS source_read calls only."""
    src: dict = {}
    for o in outcomes:
        if o.outcome == SUCCESS and o.evidence_kind == "source_read":
            for p in o.paths_read:
                src[p] = src.get(p, "") + "\n" + _section_for(o, p)
    return src


def _section_for(o, p) -> str:
    if o.tool == "get_file_content":
        return o.result_text or ""
    t = o.result_text or ""
    heads = list(_BATCH_HEADER.finditer(t))
    for i, h in enumerate(heads):
        if norm_path(h.group(1)) == p:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(t)
            return t[h.end():end]
    return ""


def _req_source(req, srcs):
    for p, t in srcs.items():
        for cand in req.any_of_paths:
            if p.endswith(norm_path(cand)):
                return t
    return None


def gate_evaluate(policy, outcomes, text) -> dict:
    """Only SUCCESS source_read outcomes of THIS attempt satisfy R1-R4; R5 needs source-text evidence of each hop."""
    if policy is None:
        return {"applicable": False, "satisfied": [], "missing": [], "reads": {}}
    srcs = _read_sources(outcomes)
    sat, miss = [], []
    by_req = {}
    for r in policy.requirements:
        t = _req_source(r, srcs)
        calls = sorted({o.seq for o in outcomes if o.outcome == SUCCESS and o.evidence_kind == "source_read"
                        and any(p.endswith(norm_path(c)) for p in o.paths_read for c in r.any_of_paths)})
        by_req[r.id] = t
        (sat if t is not None else miss).append({"id": r.id, "description": r.description, "satisfied_by_calls": calls})
    link_results = []
    if policy.links:
        report = (text or "").lower()
        for ln in policy.links:
            f_src, t_src = by_req.get(ln.from_req), by_req.get(ln.to_req)
            problems = []
            if f_src is None or t_src is None:
                problems.append("a required file in this hop was not read")
            else:
                if not re.search(ln.from_regex, f_src):
                    problems.append("source-side relationship pattern not found in the 'from' file as read")
                if not re.search(ln.to_regex, t_src):
                    problems.append("source-side relationship pattern not found in the 'to' file as read")
            missing_tok = [t for t in ln.report_tokens if t.lower() not in report]
            if missing_tok:
                problems.append("report does not name: " + ", ".join(missing_tok))
            link_results.append({"id": ln.id, "ok": not problems, "problems": problems})
        ok5 = not miss and all(x["ok"] for x in link_results)
        (sat if ok5 else miss).append({"id": "R5_traceable_relationships",
                                       "description": "evidence-backed relationships between upload, storage, registration, verification",
                                       "links": link_results})
    return {"applicable": True, "satisfied": sat, "missing": miss, "links": link_results,
            "reads": {p: True for p in srcs}}


# ---------------------------------------------------------------------------- attempts and registry
@dataclass
class Attempt:
    task_id: str
    work_item_id: str
    attempt_id: str
    generation: int
    workflow_run_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    workflow_status: Optional[str] = None
    terminal: dict = field(default_factory=dict)
    outcomes: list = field(default_factory=list)
    text: str = ""
    source: str = ""
    state: str = "RUNNING"            # RUNNING | FINISHED | SUPERSEDED | CANCELLED
    preloaded: dict = field(default_factory=dict)


class Registry:
    """In-memory attempts/generations/accepted results. Durable storage is Phase 1.15."""

    def __init__(self):
        self.current_gen: dict = {}
        self.attempts: dict = {}
        self.accepted: dict = {}
        self.withheld: list = []
        self.log: list = []
        self._n = itertools.count(1)

    def start(self, task_id, work_item_id, workflow_run_id=None) -> Attempt:
        g = self.current_gen.get(work_item_id, -1) + 1
        for a in self.attempts.values():
            if a.work_item_id == work_item_id and a.state in ("RUNNING", "FINISHED") and a.generation < g:
                a.state = "SUPERSEDED"
        self.current_gen[work_item_id] = g
        a = Attempt(task_id, work_item_id, f"att-{next(self._n):03d}-g{g}", g, workflow_run_id)
        self.attempts[a.attempt_id] = a
        self.log.append(("start", a.attempt_id, g))
        return a

    def cancel(self, attempt_id: str) -> None:
        """The APPLICATION's decision that this attempt is dead. Does not depend on the Agent/tool actually stopping."""
        self.attempts[attempt_id].state = "CANCELLED"
        self.log.append(("cancel", attempt_id))

    def validity(self, a: Attempt) -> str:
        reg = self.attempts.get(a.attempt_id)
        if reg is None:
            return "UNKNOWN_ATTEMPT"
        if reg.state == "CANCELLED":
            return "ATTEMPT_CANCELLED"
        if reg.state == "SUPERSEDED" or a.generation < self.current_gen.get(a.work_item_id, 0):
            return "SUPERSEDED_STALE_GENERATION"
        if a.generation != reg.generation:
            return "GENERATION_MISMATCH"
        return "CURRENT"

    def register(self, a: Attempt, d: "Decision") -> dict:
        v = self.validity(a)
        if v != "CURRENT":
            self.log.append(("reject", a.attempt_id, v))
            return {"registered": False, "reason": v}
        if d.acceptance == NOT_ACCEPTED:
            self.log.append(("reject", a.attempt_id, "not-accepted-execution"))
            return {"registered": False, "reason": "NOT_ACCEPTED:" + d.execution_status}
        if any(x["attempt_id"] == a.attempt_id for x in self.accepted.values()) or \
                any(x["attempt_id"] == a.attempt_id for x in self.withheld):
            return {"registered": False, "reason": "IDEMPOTENT_DUPLICATE", "no_status_regression": True}
        rec = {"work_item_id": a.work_item_id, "attempt_id": a.attempt_id, "generation": a.generation,
               "status": d.acceptance, "agent_run_id": a.agent_run_id, "workflow_run_id": a.workflow_run_id}
        if d.acceptance in (ACCEPTED_COMPLETE, ACCEPTED_PARTIAL):
            prev = self.accepted.get(a.work_item_id)
            if prev and prev["generation"] > a.generation:
                return {"registered": False, "reason": "WOULD_OVERWRITE_NEWER_GENERATION"}
            self.accepted[a.work_item_id] = rec
            self.log.append(("accept", a.attempt_id, d.acceptance))
            return {"registered": True, "reason": d.acceptance, "generation": a.generation}
        self.withheld.append(rec)
        self.log.append(("withhold", a.attempt_id, d.acceptance))
        return {"registered": False, "reason": "WITHHELD_" + d.acceptance, "generation": a.generation}


# ---------------------------------------------------------------------------- terminal gate
def terminal_gate(a: Attempt) -> tuple:
    """Agent terminal outcome from the Agent's OWN facts. Workflow status and attempt validity are separate layers."""
    t = a.terminal or {}
    if t.get("cancelled_event"):
        return "CANCELLED", "agent emitted RunCancelled"
    if t.get("timed_out"):
        return "TIMED_OUT", "attempt exceeded its deadline"
    if t.get("run_error"):
        return "FAILED", f"agent RunError: {str(t['run_error'])[:120]}"
    if t.get("stream_closed_early"):
        return "INCOMPLETE", "stream closed before a terminal event"
    ros = str(t.get("run_output_status") or "").lower()
    if not t.get("completed_event") or "completed" not in ros:
        return "INCOMPLETE", (f"no normal terminal completion (completed_event={t.get('completed_event')}, "
                              f"run_output_status={t.get('run_output_status')})")
    return "COMPLETED", "agent completed normally"


# ---------------------------------------------------------------------------- attempt-scoped assessor feed
def build_assessor_state(a: Attempt, outcomes):
    """A FRESH state object per attempt, fed only from THIS attempt's classified outcomes through the production
    recorder. FAILED/REFUSED/TIMED_OUT -> err bucket (no evidence obtained); CANCELLED/UNKNOWN -> not recorded."""
    from swarm import team as T
    ev = T._new_delegation_evidence(T.unavailable_tool_snapshot().get("count", 0))
    ev["attempt_id"] = a.attempt_id
    for o in outcomes:
        if o.tool in STATE_TOOLS:
            continue
        if o.outcome == SUCCESS and o.evidence_kind != "state":
            if o.tool in READ_TOOLS and o.evidence_kind != "source_read":
                # executed but returned no genuine source content: not evidence
                T._record_call_in_evidence(ev, o.tool, {}, "", ok=True)
            else:
                T._record_call_in_evidence(ev, o.tool, o.args, o.result_text or "", ok=True)
        elif o.outcome in (FAILED, REFUSED, TIMED_OUT):
            T._record_call_in_evidence(ev, o.tool, o.args, o.result_text or "", ok=False)
            if o.outcome == REFUSED:
                ev["refused"][o.tool] = ev["refused"].get(o.tool, 0) + 1
    ns = SimpleNamespace(members=[], _delegation_generation={KEY: a.generation},
                         _delegation_progress={"calls": {}, "evidence": {KEY: ev}},
                         _delegation_preloaded={KEY: dict(a.preloaded)}, _unavailable_forced_generation={},
                         _result_assessments={}, _member_assessments={})
    return ns, ev


# ---------------------------------------------------------------------------- decision pipeline
@dataclass
class Decision:
    identity: dict
    workflow_status: Optional[str]
    execution_status: str
    execution_reason: str
    counts: dict
    gate: dict
    validity: str = "CURRENT"
    assessor_status: Optional[str] = None
    assessor_reason: Optional[str] = None
    assessor_claims: Optional[dict] = None
    acceptance: str = NOT_ACCEPTED
    overrides: list = field(default_factory=list)
    reason: str = ""
    registration: Optional[dict] = None
    steps: list = field(default_factory=list)


def _to_acceptance(assessor_status: str) -> str:
    return {"COMPLETE": ACCEPTED_COMPLETE, "PARTIAL": ACCEPTED_PARTIAL, "UNRESOLVED": UNRESOLVED,
            "BLOCKED": BLOCKED}.get(assessor_status, UNRESOLVED)


def decide(a: Attempt, policy, registry: Registry = None, outcomes=None, register: bool = True) -> Decision:
    """The single application-owned acceptance pipeline. Order is fixed and each stage is recorded in `steps`.
    1 identity -> 2 Agent terminal state -> 3 attempt validity -> 4 tool-outcome integrity -> 5 required-evidence
    policy -> 6 existing assessor (this attempt's evidence only) -> 7 reconcile (gates only restrict) -> 8 register."""
    from swarm import team as T
    outcomes = a.outcomes if outcomes is None else outcomes
    ident = {"task_id": a.task_id, "work_item_id": a.work_item_id, "attempt_id": a.attempt_id,
             "generation": a.generation, "workflow_run_id": a.workflow_run_id, "agent_run_id": a.agent_run_id,
             "source": a.source}
    steps = [("1.identity", ident)]
    ex, ex_reason = terminal_gate(a)
    steps.append(("2.agent_terminal_state", {"workflow": a.workflow_status, "agent": ex, "reason": ex_reason}))
    cnt = count_outcomes(outcomes)
    ns, ev = build_assessor_state(a, outcomes)
    try:
        asm = T._assessment_for(ns, KEY, a.text or "", generation=a.generation, attempt_id=a.attempt_id)
    except Exception as e:  # noqa: BLE001
        asm = {"status": "ERROR", "reason": repr(e), "claims": [], "supported": [], "unsupported": []}
    gate = gate_evaluate(policy, outcomes, a.text)
    d = Decision(ident, a.workflow_status, ex, ex_reason, cnt, gate, assessor_status=asm["status"],
                 assessor_reason=asm["reason"],
                 assessor_claims={"claims": len(asm["claims"]), "supported": len(asm["supported"]),
                                  "unsupported": list(asm["unsupported"])[:20]})
    d.validity = registry.validity(a) if registry else "CURRENT"
    steps.append(("3.attempt_validity", d.validity))
    if d.validity != "CURRENT":
        d.acceptance = NOT_ACCEPTED
        d.overrides.append({"gate": "attempt-validity", "assessor": asm["status"], "final": NOT_ACCEPTED, "why": d.validity})
        d.reason = f"attempt not current: {d.validity}"
    elif ex != "COMPLETED":
        d.acceptance = NOT_ACCEPTED
        d.overrides.append({"gate": "terminal-state", "assessor": asm["status"], "final": NOT_ACCEPTED, "why": f"{ex}: {ex_reason}"})
        d.reason = f"agent {ex}: {ex_reason}; no result accepted (assessor verdict {asm['status']} is diagnostic only)"
    else:
        steps.append(("4.tool_outcome_integrity", {"counts": cnt["all"], "unknown_never_success": True}))
        steps.append(("5.required_evidence_policy", gate))
        final = asm["status"] if asm["status"] in _RESTRICTION else "UNRESOLVED"
        if gate["applicable"] and gate["missing"]:
            n_req = len(policy.requirements)
            n_sat = sum(1 for s in gate["satisfied"] if not s["id"].startswith("R5"))
            prevented = cnt["investigation"][FAILED] + cnt["investigation"][REFUSED] + cnt["investigation"][TIMED_OUT]
            if n_sat == 0 and prevented > 0 and cnt["investigation"][SUCCESS] == 0:
                target = "BLOCKED"
            elif n_sat == 0:
                target = "UNRESOLVED"
            else:
                target = "PARTIAL"
            if _RESTRICTION[target] > _RESTRICTION[final]:
                d.overrides.append({"gate": "required-evidence", "assessor": final, "final": target,
                                    "why": "missing: " + ", ".join(m["id"] for m in gate["missing"])})
                final = target
        steps.append(("6-7.assessor_reconciled", {"assessor": asm["status"], "final": final}))
        d.acceptance = _to_acceptance(final)
        d.reason = (f"assessor: {asm['reason']}" if not d.overrides else "; ".join(
            f"{o['gate']} restricted {o['assessor']}->{o['final']} ({o['why']})" for o in d.overrides))
        if asm["status"] in _RESTRICTION:   # invariant: restrict only, never upgrade
            assert _RESTRICTION[final] >= _RESTRICTION[asm["status"]], "gate attempted to upgrade the assessor verdict"
    if registry is not None and register:
        d.registration = registry.register(a, d)
        steps.append(("8.register", d.registration))
    d.steps = steps
    return d


def summarize(d: Decision) -> dict:
    return {"attempt": d.identity["attempt_id"], "generation": d.identity["generation"], "workflow": d.workflow_status,
            "agent": d.execution_status, "validity": d.validity,
            "outcomes_S_F_R_C_T_U": [d.counts["all"][k] for k in OUTCOMES], "source_files_read": len(d.counts["source_files_read"]),
            "requirements_missing": [m["id"] for m in d.gate["missing"]] if d.gate["applicable"] else "n/a",
            "assessor": d.assessor_status, "acceptance": d.acceptance, "reason": d.reason[:200],
            "registration": (d.registration or {}).get("reason")}
