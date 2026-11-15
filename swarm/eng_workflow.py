"""Application-controlled engineering Workflow (Phase 1.15).

The APPLICATION owns: planning, dispatch, attempt identity, terminal-state observation, per-attempt outcome
and evidence collection, acceptance, retry, and the final answer. The Researcher Agent only investigates.
No Coordinator, no model-decided routing, no model-authored finalization.

Reversible: reached only when a request asks for orchestration="workflow" (or HIVE_ORCHESTRATION=workflow).
The Team path in swarm/team.py is untouched and remains the default. This slice is INVESTIGATION-ONLY
(Researcher; mutating tools are always stripped); Planner/Coder/Executor/Reviewer stay on the Team path.

Persistence is deliberately NOT here: the registry is the in-memory one from swarm.acceptance (Phase 1.16
owns the durable ledger).

Workflow-engine notes (Agno 2.5.17, verified): Step defaults are max_retries=3 and on_error=skip, which would
silently re-run a failed step and hide the failure. Every step below is built with max_retries=0, and steps
never raise: they record what happened into RunState, so the outcome is decided from application state, not
from the Workflow's own status.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Optional

from swarm import acceptance as A
from swarm.ledger import Ledger, LedgerError, RetryBudgetExhausted, decision_to_dict
from swarm import liveness as L
from swarm import knowledge as K

SNAPSHOT_INTERVAL_S = 5.0
MIN_ATTEMPT_START_S = 120.0

MAX_ATTEMPTS_PER_ITEM = 2                 # 1 original + at most 1 retry
RETRYABLE_TERMINALS = {"FAILED", "INCOMPLETE"}   # never CANCELLED / TIMED_OUT (those are app/operator decisions)
ATTEMPT_DEADLINE_S = float(os.environ.get("HIVE_WORKFLOW_ATTEMPT_DEADLINE_S", "1500"))
CONTROLS_ENV = "HIVE_WORKFLOW_TEST_CONTROLS"
LAST: dict = {"state": None}               # most recent RunState (in-process observability only)


# ---------------------------------------------------------------------------- plan
@dataclass
class WorkItem:
    work_item_id: str
    title: str
    target: str
    objective: str
    evidence_required: str
    completion_criteria: str
    paths: list
    policy: object = None


@dataclass
class WorkPlan:
    task_id: str
    items: list
    rejected_reason: Optional[str] = None


_PATH_RE = re.compile(r"(?<![\w./-])((?:[\w()\[\]@.-]+/)+[\w()\[\]@.-]+\.[A-Za-z0-9]{1,8})(?![\w/])")


def extract_paths(text: str) -> list:
    seen, out = set(), []
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(1).strip(".,;:)")
        if p not in seen and "://" not in p:
            seen.add(p)
            out.append(p)
    return out


# Phase 1.23: a small, explicit, hand-maintained registry mapping a KNOWN runbook task's own
# distinctive wording to its own pre-authored content-coverage policy. This exists because T11 and
# T13a deliberately name no files in their prompt text (that is the point of those two tests -- see
# docs/runbooks/groundedness-battery.md), so extract_paths()-based policy derivation below can never
# reach them; without this registry those tasks fall into the generic "read at least one file" policy,
# which is the root cause Phase 1.23 investigated (a file was read -> gate satisfied, regardless of
# whether the answer actually covers what the task asked for). Matching is a short, distinctive
# substring, case-insensitive -- never a full-prompt hash, so a word-level prompt edit degrades to the
# safer failure direction (falls through to the generic/per-path policy, looser coverage) rather than a
# false, stricter block. Extend this list only for a runbook prompt with its own vetted ground truth;
# never invent a policy for a task that hasn't been independently verified.
_KNOWN_TASK_POLICIES = (
    ("when a seller uploads a document", A.T11_DOC_UPLOAD_POLICY),
    ("audit the vouchers module", A.T13_VOUCHER_POLICY),
)


def _known_policy_for(task: str):
    t = (task or "").lower()
    for phrase, policy in _KNOWN_TASK_POLICIES:
        if phrase in t:
            return policy
    return None


def derive_policy(paths: list, task: str = ""):
    """Deterministic evidence policy: one requirement per named file (a SUCCESS source_read of that file by the
    attempt). The known T11 doc-upload chain additionally gets its evidence-backed relationship links. A task
    matching a known runbook prompt (Phase 1.23, _known_policy_for) also gets that prompt's own content-coverage
    obligations (EvidencePolicy.enum_requirements), merged with whatever per-named-file requirements the task's
    own wording independently produces -- never replacing them."""
    known = _known_policy_for(task)
    if not paths:
        return known if known is not None else A.EvidencePolicy(name="generic-source-evidence", requirements=[
            A.Requirement("G1_source_read", "at least one source file was actually read in this attempt (any file)", [A.ANY_SOURCE])])
    t11 = {A.norm_path(c): r for r in A.T11_DOC_UPLOAD_POLICY.requirements for c in r.any_of_paths}
    reqs, used = [], set()
    for i, p in enumerate(paths, 1):
        n = A.norm_path(p)
        hit = next((r for c, r in t11.items() if n.endswith(c)), None)
        if hit is not None:
            reqs.append(hit)
            used.add(hit.id)
        else:
            reqs.append(A.Requirement(f"F{i}_read_{os.path.basename(p)}", f"read source file {p}", [n]))
    links = list(A.T11_DOC_UPLOAD_POLICY.links) if used >= {r.id for r in A.T11_DOC_UPLOAD_POLICY.requirements} else []
    enum_reqs = list(known.enum_requirements) if known is not None else []
    return A.EvidencePolicy(name="derived-" + hashlib.sha256("|".join(paths).encode()).hexdigest()[:8],
                            requirements=reqs, links=links, enum_requirements=enum_reqs)


MAX_WORK_ITEMS = 4


def plan_work_items(items: list, task_id: str) -> WorkPlan:
    """Caller-provided explicit work items (each a string naming its own repository files). Deterministic and
    validated: 1..MAX_WORK_ITEMS items, each must name at least one file. Every item gets its own id, evidence
    policy and attempts; nothing is decomposed or routed by a model. One bad item rejects the whole plan."""
    texts = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not texts or len(texts) > MAX_WORK_ITEMS:
        return WorkPlan(task_id, [], f"work_items must contain 1..{MAX_WORK_ITEMS} non-empty items (got {len(texts)})")
    out = []
    for k, t in enumerate(texts, 1):
        p = plan_request(t, task_id)
        if not p.items:
            return WorkPlan(task_id, [], f"work item {k} rejected: {p.rejected_reason}")
        wi = p.items[0]
        wi.work_item_id, wi.title = f"wi-{k}", f"Work item {k}"
        out.append(wi)
    return WorkPlan(task_id, out)


def plan_request(task: str, task_id: str) -> WorkPlan:
    """Application-owned plan. Files the request NAMES become required source evidence (one requirement per named file).
    A request that names none is planned as ONE work item with a generic requirement: at least one source file actually read
    by the attempt (any file). Files suggested by the Knowledge Dossier are LEADS handed to the Researcher; they are never
    acceptance requirements, and a file the Researcher finds on its own is equally valid evidence."""
    task = (task or "").strip()
    if not task:
        return WorkPlan(task_id, [], "empty request")
    paths = extract_paths(task)
    evidence = ("For each named file: the file path you read, the function/handler name, and the route path "
                "or URL, taken from file content you actually read.") if paths else (
        "For every claim: the file path and line you read it in, taken from source content you actually read with your own tools. "
        "Name the files you found, including any the knowledge brief did not list.")
    criteria = ("Done when each named file has a source-backed finding, or an explicit statement of "
                "which file or step could not be evidenced and why.") if paths else (
        "Done when every part of the request is answered with file-and-line evidence from files you read, or an explicit statement of "
        "which part could not be evidenced and why.")
    wi = WorkItem(
        work_item_id="wi-1", title="Investigate the named files" if paths else "Investigate the request",
        target=("Files named in the request: " + " ; ".join(paths)) if paths else
               "The request as stated. It names no file; use your own search and read tools to find and read the relevant source.",
        objective=task, evidence_required=evidence, completion_criteria=criteria,
        paths=paths, policy=derive_policy(paths, task))
    return WorkPlan(task_id, [wi])


# ---------------------------------------------------------------------------- run state
@dataclass
class AttemptRecord:
    attempt: object
    decision: object = None
    summary: dict = field(default_factory=dict)
    error: Optional[str] = None
    ledger_error: Optional[str] = None       # a durable write for THIS attempt failed (or its budget was refused)
    tokens: dict = field(default_factory=lambda: {"in": 0, "out": 0})


@dataclass
class RunState:
    task: str
    task_id: str
    workflow_run_id: str
    registry: object
    plan: Optional[WorkPlan] = None
    records: list = field(default_factory=list)          # every attempt, in order, never erased
    progress: list = field(default_factory=list)
    final: Optional[str] = None
    final_audit: Optional[dict] = None
    steps_run: list = field(default_factory=list)
    ledger: object = None
    ledger_task_created: bool = False
    t0: float = field(default_factory=time.time)
    # Phase 1.18B: overall task deadline + liveness telemetry (the parent supervisor judges the snapshot)
    deadline_mono: Optional[float] = None       # None = not enforced by the worker (parent still enforces the wall deadline)
    deadline_wall: Optional[float] = None
    reserve_s: float = 90.0
    deadline_hit: bool = False
    tracker: object = None
    identity: dict = field(default_factory=dict)
    wf_state: str = "starting"
    attempt_deadline_wall: Optional[float] = None
    stream_events: int = 0
    knowledge: object = None              # Phase 1.20: the run's KnowledgeDossier (prepared once by the shared service)
    # Phase 1.25: TASK-scoped (one logical delegated task = one work_item_id) cumulative discovery/
    # source-read consumption, carried across attempts within that work item (initial + any
    # recovery) -- so a recovery attempt sees the balance already spent, not a fresh 90K/90K.
    # Per-attempt figures are reported separately (see run_attempt()'s own att.terminal["budget"])
    # and are NEVER lost; this dict only ever holds the running CUMULATIVE total per work item.
    task_discovery_chars: dict = field(default_factory=dict)
    task_read_chars: dict = field(default_factory=dict)
    # Phase 1.30: the task-specific AUTHORIZED ceiling per work item (distinct from the
    # cumulative-consumption dicts above) -- absent means "still at the unextended default."
    # Both the upfront (Phase 1.29d) and just-in-time (Phase 1.30) grant paths read and write
    # the SAME dict, so there is exactly one authorized-ceiling number per work item, never two
    # independently-tracked ones racing to describe the same task.
    task_read_ceiling: dict = field(default_factory=dict)
    task_discovery_ceiling: dict = field(default_factory=dict)

    def remaining(self) -> Optional[float]:
        return None if self.deadline_mono is None else self.deadline_mono - time.monotonic()

    def emit(self, kind: str, **kw):
        rec = {"t": round(time.time() - self.t0, 2), "event": kind, **kw}
        self.progress.append(rec)
        print("[workflow] " + " ".join(f"{k}={v}" for k, v in rec.items()), flush=True)


# ---------------------------------------------------------------------------- one Researcher attempt
class _Trace:
    """Phase 1.22: tracks BOTH the complete diagnostic trace (`text`, unchanged
    behavior from before this phase) AND, additively, which turn in the
    tool-calling loop is the TERMINAL one (`terminal_text`).

    Terminal-result boundary (established live against the installed Agno
    2.5.17, not assumed from documentation -- see docs/reports/phase-1.22-report.md
    Section 2 for the full event-sequence evidence): Agno's own agentic loop
    (agno/models/base.py's `while True:`) only issues another model request
    when the PRIOR turn's assistant message carried tool_calls; it stops the
    instant a turn carries none. One real model turn is therefore bounded by
    exactly one ModelRequestStarted .. ModelRequestCompleted span (confirmed
    live: a scripted 3-turn model produced EXACTLY that pairing, in order,
    once per turn, with RunContentEvent deltas landing strictly inside each
    span). A span is "closed" -- provably NOT the turn that ended the loop --
    the moment a ToolCallStarted fires after it. The terminal segment is the
    LAST span that is never closed, PROVIDED the run's own RunCompleted event
    fired (never RunError/RunCancelled/an early-closed stream) -- i.e. the
    exact structural condition under which Agno's own loop stopped normally.
    This is a pure event/position signal: no content, no phrase, no length
    heuristic of any kind is consulted.
    """
    def __init__(self, T, RunContentEvent):
        self.T, self.RCE = T, RunContentEvent
        self.parts, self.pending = [], False
        self.agent_run_id = None
        self.run_output = None
        self.completed = self.cancelled = False
        self.error = None
        self.tin = self.tout = 0
        # Phase 1.22 additions -- segment-aware terminal-result tracking, built
        # from the SAME event stream `add()` already consumes; no new event
        # source, no change to the diagnostic trace above.
        self._segments: list = []           # one entry per ModelRequestStarted..Completed span
        self._last_segment_closed = False   # True once a ToolCallStarted fires after the last-opened span

    def add(self, ev):
        name = getattr(ev, "event", None) or type(ev).__name__
        if name == "RunStarted":
            self.agent_run_id = ev.run_id
        elif name == "ModelRequestStarted":
            self._segments.append("")
            self._last_segment_closed = False
        elif name == "ModelRequestCompleted":
            self.tin += ev.input_tokens or 0
            self.tout += ev.output_tokens or 0
        elif name == "ToolCallStarted":
            self._last_segment_closed = True
        elif name == "RunCompleted":
            self.completed = True
        elif name == "RunError":
            self.error = getattr(ev, "content", None) or "error"
        elif name == "RunCancelled":
            self.cancelled = True
        if isinstance(ev, self.RCE) and isinstance(getattr(ev, "content", None), str):
            if self.pending and self.parts:
                self.parts.append(self.T._SEGMENT_SEPARATOR)
            self.pending = False
            self.parts.append(ev.content)
            if self._segments:
                self._segments[-1] += ev.content
        elif name in self.T._SEGMENT_BOUNDARY_EVENTS:
            self.pending = True

    @property
    def text(self):
        """Complete diagnostic trace -- every turn, unchanged from before Phase 1.22.
        Evidence only; never the canonical result (see terminal_text)."""
        return "".join(self.parts)

    @property
    def terminal_text(self):
        """Phase 1.22: the canonical terminal answer, or "" if none was
        structurally selectable. Callers MUST treat "" as a missing/invalid
        result and must NOT fall back to `text` -- that is exactly the
        substitution Phase 1.22 was scoped to prevent."""
        if not self.completed or self.cancelled or self.error:
            return ""
        if not self._segments or self._last_segment_closed:
            return ""
        return self._segments[-1]

    @property
    def segment_count(self):
        return len(self._segments)


def _controls(raw) -> dict:
    """Fault/budget controls for controlled validation. Honoured ONLY when the process was started with
    HIVE_WORKFLOW_TEST_CONTROLS=1; otherwise ignored (and logged) so a production request cannot inject faults."""
    if not raw:
        return {}
    if os.environ.get(CONTROLS_ENV) != "1":
        print("[workflow] workflow_controls ignored: test controls are not enabled in this process", flush=True)
        return {}
    return dict(raw)


def item_controls(controls: dict, work_item_id: str) -> dict:
    """Global controls with per-work-item overrides (controls["items"][work_item_id]) applied."""
    base = {k: v for k, v in (controls or {}).items() if k != "items"}
    base.update(((controls or {}).get("items") or {}).get(work_item_id) or {})
    return base


async def run_attempt(st: RunState, wi: WorkItem, ctx: dict, controls: dict, attempt_no: int, task_override: str = None,
                      extra_activity: dict = None):
    """Create a NEW attempt (new id + generation via the registry), run the real Researcher under it, observe the
    Agent's own terminal facts, and return the AttemptRecord. Never raises.
    `task_override` (Phase 1.24): if given, replaces wi.objective ONLY for the text actually sent to the
    Researcher this attempt (a bounded recovery pass targeting specific missing obligations) -- wi.target/
    wi.policy/wi.evidence_required/wi.completion_criteria are unchanged, so the SAME coverage policy still
    governs this attempt's own decide() call. The work item's own `objective` field is never mutated.
    `extra_activity` (Phase 1.30): optional extra seed values merged into this attempt's own `activity`
    dict before the team is built -- currently used by recover_and_reconcile() to carry
    missing_source_paths/missing_obligation_ids (deterministically derived from the ORIGINAL attempt's
    own Decision via the existing requirement_source_paths(), the same call build_recovery_task() already
    makes) so the just-in-time extension check in swarm/team.py's budget hook has something real to match
    a blocked read's path against, never model text."""
    import functools
    import inspect
    import swarm.team as T
    from agno.agent import Agent
    from agno.run.agent import RunContentEvent

    led = st.ledger
    lstart, lerr = None, None
    if led is not None:
        try:     # persisted BEFORE the Agent can run, so no accepted output can exist without a durable attempt
            lstart = await led._a(led.start_attempt, st.task_id, wi.work_item_id, st.workflow_run_id, MAX_ATTEMPTS_PER_ITEM)
        except RetryBudgetExhausted as e:
            lerr = f"RETRY_EXHAUSTED: {e}"
        except Exception as e:  # noqa: BLE001
            lerr = f"LEDGER_ERROR: {repr(e)[:200]}"
    att = st.registry.start(st.task_id, wi.work_item_id, workflow_run_id=st.workflow_run_id,
                            attempt_id=(lstart or {}).get("attempt_id"), generation=(lstart or {}).get("generation"))
    rec = AttemptRecord(attempt=att)
    rec.ledger_error = lerr
    st.records.append(rec)
    if lerr:
        st.emit("attempt_not_started", work_item=wi.work_item_id, attempt=att.attempt_id, reason=lerr[:120])
        att.terminal = {"completed_event": False, "run_output_status": None, "run_error": f"not started: {lerr[:120]}",
                        "cancelled_event": False, "timed_out": False, "stream_closed_early": False,
                        "result_selection_status": "MISSING_TERMINAL", "segments_total": 0,
                        "terminal_chars": 0, "trace_chars": 0}
        att.outcomes, att.text, att.trace_text, att.source, att.state = [], "", "", "workflow-real", "FINISHED"
        return rec
    st.emit("attempt_started", work_item=wi.work_item_id, attempt=att.attempt_id, generation=att.generation,
            workflow_run=st.workflow_run_id, attempt_no=attempt_no)
    trace = _Trace(T, RunContentEvent)
    old_ceiling = T._DELEGATION_READ_CEILING
    old_discovery_ceiling = T._DELEGATION_DISCOVERY_CEILING   # Phase 1.24: same test-control pattern as above
    async def persist_call(o):
        if led is not None:
            await led._a(led.record_tool_call, o, att.attempt_id, st.task_id, wi.work_item_id)

    async def invalidate(state, reason):
        if led is not None:
            try:
                await led._a(led.invalidate, att.attempt_id, state, reason)
            except Exception as e:  # noqa: BLE001
                rec.ledger_error = rec.ledger_error or f"LEDGER_ERROR: {repr(e)[:160]}"

    st.tracker = L.ProgressTracker()                       # progress is judged per attempt
    st.identity = {"task_id": st.task_id, "workflow_run_id": st.workflow_run_id, "work_item_id": wi.work_item_id,
                   "attempt_id": att.attempt_id, "generation": att.generation, "attempt_no": attempt_no, "agent_run_id": None}
    st.wf_state = "executing"

    async def on_finished(o):
        st.tracker.on_call(o)
        await persist_call(o)

    outcome_rec = A.OutcomeRecorder(att.attempt_id, tool_deadline=controls.get("tool_deadline"), on_call=on_finished)
    fault = {"n": 0}
    # Phase 1.25: the balance this work item has ALREADY spent, across every attempt so far (0 for
    # the initial attempt; non-zero for a recovery attempt) -- read BEFORE the team is built so the
    # fresh per-attempt hook closure can be told the task-level starting point via `activity`
    # (the existing, pre-Phase-1.25 extension point _make_read_cache_tool_hook already writes
    # diagnostics into), rather than silently starting a recovery attempt at 0/90,000 again.
    _task_discovery_before = st.task_discovery_chars.get(wi.work_item_id, 0)
    _task_read_before = st.task_read_chars.get(wi.work_item_id, 0)
    activity = {"task_discovery_offset": _task_discovery_before, "task_read_offset": _task_read_before,
                # Phase 1.30: the task's currently-authorized ceiling, keyed by work_item_id so it
                # persists correctly across the initial attempt and any recovery attempt -- this is
                # the ONE value both the upfront (Phase 1.29d) and just-in-time (Phase 1.30) grant
                # paths read and raise; defaults to the module-level constant the very first time a
                # work item is ever attempted, never a separately-tracked number.
                "work_item_id": wi.work_item_id,
                "read_ceiling": st.task_read_ceiling.get(wi.work_item_id, T._DELEGATION_READ_CEILING),
                "discovery_ceiling": st.task_discovery_ceiling.get(wi.work_item_id, T._DELEGATION_DISCOVERY_CEILING),
                # Phase 1.32: explicit, scratch/comparison-only test controls -- absent
                # (the production default) means FULLY ENABLED. Neither flag exists in
                # any real caller's normal path; both are read straight through from
                # `controls`, which only a scratch harness ever sets, so there is no
                # second, silently-different production configuration -- a run that
                # never sets these behaves exactly as Phases 1.30/1.31 already do.
                "disable_jit_extension": bool(controls.get("disable_jit_extension")),
                "disable_symbol_tools": bool(controls.get("disable_symbol_tools"))}
    if extra_activity:
        activity.update(extra_activity)
    try:
        if controls.get("ceiling") is not None:
            T._DELEGATION_READ_CEILING = controls["ceiling"]
        if controls.get("discovery_ceiling") is not None:
            T._DELEGATION_DISCOVERY_CEILING = controls["discovery_ceiling"]
        # A fresh team (hence a fresh Researcher, hooks and evidence state) per attempt: a retry inherits nothing.
        team = T._build_team(
            ctx["specs"], ctx["coordinator"], ctx["ctools"], "coordinate", ctx["mcp_list"], [],
            read_only=True, skill_catalog=ctx["catalog"], task=wi.objective, team_name="engineering",
            activity=activity,
            project_id=ctx["project_id"], tool_call_limit_bonus=ctx["profile"].tool_call_limit_bonus)
        team._hive_mcp_url = ctx["hive_url"]
        team._task = wi.objective
        team._project_id = ctx["project_id"]
        researcher = next(m for m in team.members if T._member_key(m.name) == "researcher")
        o_hook, i_hook = outcome_rec.hooks()
        researcher.tool_hooks = [o_hook] + list(researcher.tool_hooks or []) + [i_hook]
        if controls.get("fail_first_read") or controls.get("delay_tool_s"):
            for fn in (researcher.tools or []):
                orig = getattr(fn, "entrypoint", None)
                if orig is None or getattr(fn, "name", "") == "update_session_state":
                    continue

                def mk(orig=orig, name=fn.name):
                    @functools.wraps(orig)
                    async def wrapped(*a, **kw):
                        if controls.get("delay_tool_s") and name in ("get_file_content", "get_files_batch"):
                            await asyncio.sleep(controls["delay_tool_s"])
                        sub = controls.get("fail_first_read")
                        if (sub and name in ("get_file_content", "get_files_batch") and fault["n"] == 0
                                and sub in str(kw.get("relative_path", "")) + str(kw.get("paths", ""))):
                            fault["n"] = 1
                            raise RuntimeError("controlled MCP failure (workflow test control)")
                        r = orig(*a, **kw)
                        return await r if inspect.isawaitable(r) else r
                    return wrapped
                fn.entrypoint = mk()
        _objective = task_override if task_override is not None else wi.objective
        echo = f"{wi.target} {_objective} {wi.evidence_required} {wi.completion_criteria}"
        att.preloaded = {"text": ctx.get("knowledge_brief") or "", "echo": echo, "target": wi.target, "objective": _objective,
                         "evidence_required": wi.evidence_required, "task_text": echo}
        team._delegation_preloaded[A.KEY] = dict(att.preloaded)
        canon = T._build_canonical_researcher_task(
            target=wi.target, objective=_objective, evidence_required=wi.evidence_required,
            completion_criteria=wi.completion_criteria, knowledge_brief=ctx.get("knowledge_brief"))
        team._knowledge_run = getattr(st.knowledge, "run_key", "")      # tags this attempt's AGENT_* tool events
        _kb = ctx.get("knowledge_brief") or ""
        K.emit("KNOWLEDGE_DOSSIER_REUSED", run=getattr(st.knowledge, "run_key", None), task=st.task_id, attempt=att.attempt_id,
               status=getattr(st.knowledge, "status", None), brief_chars=len(_kb))
        K.emit("KNOWLEDGE_CONTEXT_SUPPLIED", run=getattr(st.knowledge, "run_key", None), attempt=att.attempt_id, task_chars=len(canon),
               brief_chars=len(_kb), brief_in_task=bool(_kb.strip() and _kb.strip() in canon))
        if controls.get("break_model") or attempt_no in (controls.get("break_model_attempts") or ()):
            researcher.model.base_url = "http://127.0.0.1:9/v1"
            for attr in ("client", "async_client"):
                setattr(researcher.model, attr, None)
        if controls.get("model_base_url"):                 # test control: point the Researcher at a scripted endpoint
            researcher.model.base_url = controls["model_base_url"]
            for attr in ("client", "async_client"):
                setattr(researcher.model, attr, None)

        async def consume():
            tok = A.ATTEMPT_ID.set(att.attempt_id)
            try:
                async for ev in researcher.arun(canon, stream=True, stream_events=True, yield_run_output=True,
                                                session_state={"read_log": [], "delegations_made": []}):
                    if type(ev).__name__ == "RunOutput":
                        trace.run_output = ev
                    else:
                        trace.add(ev)
                        st.stream_events += 1
                        if getattr(ev, "event", None) == "ToolCallStarted":
                            st.tracker.on_tool_start()
                        else:
                            st.tracker.on_event()
                        if trace.agent_run_id and att.agent_run_id is None:
                            att.agent_run_id = trace.agent_run_id
                            st.identity["agent_run_id"] = att.agent_run_id
                            st.emit("agent_started", attempt=att.attempt_id, agent_run=att.agent_run_id)
            finally:
                A.ATTEMPT_ID.reset(tok)

        timed_out = False
        exc_text = None
        task = asyncio.ensure_future(consume())
        cancel_after = controls.get("cancel_after_s")
        try:
            if cancel_after:
                done, _ = await asyncio.wait({task}, timeout=cancel_after)
                if not done:
                    # Operator/application cancel: the APPLICATION invalidates the attempt first, independent of
                    # whether Agent.cancel_run() (or the tool) actually stops anything.
                    st.registry.cancel(att.attempt_id)
                    await invalidate("CANCELLED", "cancelled by the application")
                    ret = Agent.cancel_run(trace.agent_run_id) if trace.agent_run_id else None
                    st.emit("attempt_cancelled", attempt=att.attempt_id, agent_cancel_returned=ret)
            deadline = float(controls.get("attempt_deadline_s") or ATTEMPT_DEADLINE_S)
            cause = "attempt"
            remaining = st.remaining()
            if remaining is not None and remaining - st.reserve_s < deadline:
                deadline, cause = max(1.0, remaining - st.reserve_s), "task"     # the OVERALL deadline binds first
            st.attempt_deadline_wall = time.time() + deadline
            await asyncio.wait_for(asyncio.shield(task), deadline)
        except asyncio.TimeoutError:
            timed_out = True
            if cause == "task":
                st.deadline_hit = True
            st.registry.cancel(att.attempt_id)
            await invalidate("TIMED_OUT", f"{cause} deadline {deadline:.0f}s exceeded")
            task.cancel()
            st.emit("attempt_timed_out", attempt=att.attempt_id, deadline_s=round(deadline, 1), cause=cause)
        except asyncio.CancelledError:
            task.cancel()
            raise
        except BaseException as e:  # noqa: BLE001
            exc_text = repr(e)[:300]
        if task.done() and not task.cancelled() and task.exception() is not None and exc_text is None:
            exc_text = repr(task.exception())[:300]
        att.agent_run_id = trace.agent_run_id or att.agent_run_id
        ro = trace.run_output
        # Phase 1.22: select the canonical terminal result via _Trace's own
        # structural boundary (see its docstring), never by content/phrase.
        # trace.terminal_text is "" both when the run genuinely produced no
        # terminal segment AND when it did not complete normally -- the two
        # cases terminal_gate()'s new MISSING_TERMINAL check (swarm/acceptance.py)
        # exists to route to NOT_ACCEPTED rather than let an empty att.text
        # reach the assessor on its own.
        _terminal = trace.terminal_text
        _selection_status = "TERMINAL_SELECTED" if _terminal else "MISSING_TERMINAL"
        att.terminal = {
            "completed_event": trace.completed,
            "run_output_status": str(getattr(ro, "status", None)) if ro is not None else None,
            "run_error": trace.error or exc_text,
            "cancelled_event": trace.cancelled,
            "timed_out": timed_out,
            "stream_closed_early": (not trace.completed) and not trace.cancelled and not (trace.error or exc_text),
            # Phase 1.22 provenance (Section 3C): how the canonical result below was
            # selected, independent of whether the selection succeeded.
            "result_selection_status": _selection_status,
            "segments_total": trace.segment_count,
            "terminal_chars": len(_terminal or ""),
            "trace_chars": len(trace.text or ""),
        }
        att.outcomes = outcome_rec.calls
        att.text = T._strip_leaked_tool_tags(_terminal or "")
        att.trace_text = T._strip_leaked_tool_tags(trace.text or "")
        att.source = "workflow-real"
        rec.tokens = {"in": trace.tin, "out": trace.tout}
        if att.state == "RUNNING":
            att.state = "FINISHED"
    except BaseException as e:  # noqa: BLE001 -- setup failure: recorded as an Agent-level failure, never raised
        rec.error = repr(e)[:300]
        att.terminal = {"completed_event": False, "run_output_status": None, "run_error": f"setup: {rec.error}",
                        "cancelled_event": False, "timed_out": False, "stream_closed_early": False,
                        "result_selection_status": "MISSING_TERMINAL", "segments_total": 0,
                        "terminal_chars": 0, "trace_chars": 0}
        att.outcomes = outcome_rec.calls
        att.text = ""
        att.trace_text = ""
        att.source = "workflow-real"
        st.emit("attempt_setup_error", attempt=att.attempt_id, error=rec.error)
    finally:
        T._DELEGATION_READ_CEILING = old_ceiling
        T._DELEGATION_DISCOVERY_CEILING = old_discovery_ceiling
        # Phase 1.25: read back what THIS attempt actually spent (0 if it never reached a tool
        # call, e.g. a setup failure) and fold it into the TASK-level (work_item_id-scoped)
        # cumulative total, which the NEXT attempt on this same work item -- a recovery pass --
        # will read as its own starting offset. Runs in `finally` so a failed/cancelled/aborted
        # attempt still correctly contributes whatever it actually spent, never silently
        # resetting or being excluded from the task-level total.
        _attempt_discovery = activity.get("attempt_discovery_chars", 0)
        _attempt_read = activity.get("attempt_read_chars", 0)
        _task_discovery_after = _task_discovery_before + _attempt_discovery
        _task_read_after = _task_read_before + _attempt_read
        st.task_discovery_chars[wi.work_item_id] = _task_discovery_after
        st.task_read_chars[wi.work_item_id] = _task_read_after
        # Phase 1.30: the authorized ceiling actually used by this attempt's own budget
        # hook is `activity["read_ceiling"]`/["discovery_ceiling"] -- it may have been
        # raised mid-attempt by a just-in-time grant, which never touches the bare
        # module global. Persist it back to RunState (task-scoped, so the NEXT attempt
        # on this work item, if any, inherits it) and use it for reporting below instead
        # of the possibly-stale module global.
        _read_ceiling_final = activity.get("read_ceiling", st.task_read_ceiling.get(wi.work_item_id, T._DELEGATION_READ_CEILING))
        _disc_ceiling_final = activity.get("discovery_ceiling", st.task_discovery_ceiling.get(wi.work_item_id, T._DELEGATION_DISCOVERY_CEILING))
        st.task_read_ceiling[wi.work_item_id] = _read_ceiling_final
        st.task_discovery_ceiling[wi.work_item_id] = _disc_ceiling_final
        if att.terminal is not None:
            # Phase 1.26: reservation leak check -- see swarm/team.py's own comment at the
            # activity["attempt_discovery_reserved"] write site. Should be 0/0 whenever the
            # hook released every reservation it made, on both the success and exception
            # paths; a scratch harness and the live battery both check this.
            _disc_reserved = activity.get("attempt_discovery_reserved", 0)
            _read_reserved = activity.get("attempt_read_reserved", 0)
            att.terminal["budget"] = {
                "attempt_discovery_chars": _attempt_discovery, "attempt_read_chars": _attempt_read,
                "task_discovery_chars_before": _task_discovery_before, "task_read_chars_before": _task_read_before,
                "task_discovery_chars_after": _task_discovery_after, "task_read_chars_after": _task_read_after,
                "task_discovery_ceiling": _disc_ceiling_final, "task_read_ceiling": _read_ceiling_final,
                "discovery_reserved_leaked": _disc_reserved, "read_reserved_leaked": _read_reserved,
                "task_discovery_remaining": max(_disc_ceiling_final - _task_discovery_after, 0),
                "task_read_remaining": max(_read_ceiling_final - _task_read_after, 0),
                # Phase 1.29: durable evidence of any budget-contract violation this
                # attempt's own activity dict recorded (swarm/team.py's own write site,
                # just above its _reservation_amt check) -- empty list means none.
                "budget_contract_violations": activity.get("budget_contract_violations", []),
                # Phase 1.30: durable record of every just-in-time extension decision
                # (grant or denial) this attempt's own budget hook made, whether or not
                # any of them were ultimately granted -- see swarm/team.py's own write
                # site at the admission-refusal branch for the exact fields recorded.
                "jit_extensions": activity.get("jit_extension_decisions", []),
            }
        if led is not None:
            try:     # terminal facts + canonical text + full trace, even for an invalidated attempt (history);
                      # never changes a decision
                await led._a(led.record_terminal, att.attempt_id, att.terminal, att.agent_run_id, att.text,
                             trace_text=att.trace_text)
            except Exception as e:  # noqa: BLE001
                rec.ledger_error = rec.ledger_error or f"LEDGER_ERROR: {repr(e)[:160]}"
            if outcome_rec.persist_error:
                rec.ledger_error = rec.ledger_error or f"LEDGER_ERROR: evidence not durable: {outcome_rec.persist_error}"
    return rec


async def assess_and_register(st: RunState, wi: WorkItem, rec: AttemptRecord, controls: dict = None):
    """Acceptance authority = swarm.acceptance.decide (terminal state, validity, tool-outcome integrity, evidence policy,
    assessor restricted-only). With the durable ledger the REGISTRATION is one atomic conditional write in the ledger; the
    in-memory registry only mirrors what the ledger decided. Workflow status is NOT consulted."""
    att = rec.attempt
    led = st.ledger
    if (controls or {}).get("hold_before_commit_s"):          # test control: widen the window between Agent end and acceptance
        st.emit("hold", where="before_commit", seconds=controls["hold_before_commit_s"])
        await asyncio.sleep(float(controls["hold_before_commit_s"]))
    att.workflow_status = "RunStatus.running"       # observed separately; never an input to acceptance
    try:
        d = A.decide(att, wi.policy, st.registry, register=(led is None))
    except BaseException as e:  # noqa: BLE001 -- the decision pipeline itself failed: nothing is accepted
        rec.error = (rec.error or "") + f" decide-error:{repr(e)[:200]}"
        d = A.Decision(identity={"task_id": att.task_id, "work_item_id": att.work_item_id,
                                 "attempt_id": att.attempt_id, "generation": att.generation,
                                 "workflow_run_id": att.workflow_run_id, "agent_run_id": att.agent_run_id,
                                 "source": att.source},
                       workflow_status=None, execution_status="UNKNOWN", execution_reason="decision pipeline error",
                       counts=A.count_outcomes(att.outcomes), gate={"applicable": False, "satisfied": [], "missing": []},
                       acceptance=A.NOT_ACCEPTED, not_accepted_cause="DECISION_ERROR", reason=rec.error)
    if rec.ledger_error and d.acceptance != A.NOT_ACCEPTED:
        d.overrides.append({"gate": "ledger", "assessor": d.assessor_status, "final": A.NOT_ACCEPTED, "why": rec.ledger_error})
        d.acceptance = A.NOT_ACCEPTED
        d.not_accepted_cause = "RETRY_EXHAUSTED" if rec.ledger_error.startswith("RETRY_EXHAUSTED") else "LEDGER_ERROR"
        d.reason = f"durable ledger refused or failed ({rec.ledger_error[:100]}); nothing is accepted"
    if led is not None:
        reqs = [{"id": r.id, "description": r.description, "any_of_paths": r.any_of_paths} for r in wi.policy.requirements]
        try:
            res = await led._a(led.commit_decision, att.attempt_id, decision_to_dict(d, reqs), att.text)
            if res.get("reason") != "IDEMPOTENT_DUPLICATE":
                d.acceptance = res.get("acceptance", d.acceptance)
                if res.get("override"):          # the ledger itself invalidated this attempt (stale / cancelled / abandoned ...)
                    d.validity = res["override"]
                    d.not_accepted_cause = d.not_accepted_cause or res["override"]
                if res.get("reason_text"):
                    d.reason = res["reason_text"]
            st.registry.apply(att, d, res)
            if (controls or {}).get("duplicate_register"):
                again = await led._a(led.commit_decision, att.attempt_id, decision_to_dict(d, reqs), att.text)
                st.emit("duplicate_register", attempt=att.attempt_id, first=res.get("reason"), second=again.get("reason"),
                        accepted_for_item=(st.registry.accepted.get(wi.work_item_id) or {}).get("attempt_id"))
        except Exception as e:  # noqa: BLE001 -- fail closed: no in-memory fallback to an accepted result
            rec.ledger_error = rec.ledger_error or f"LEDGER_ERROR: {repr(e)[:160]}"
            d.acceptance, d.not_accepted_cause = A.NOT_ACCEPTED, "LEDGER_ERROR"
            d.reason = f"durable ledger could not record the decision ({repr(e)[:100]}); nothing is accepted"
            st.registry.apply(att, d, {"registered": False, "reason": "LEDGER_ERROR", "kind": "rejected"})
    elif (controls or {}).get("duplicate_register"):
        again = st.registry.register(att, d)         # control: re-register the SAME attempt/decision
        st.emit("duplicate_register", attempt=att.attempt_id, first=(d.registration or {}).get("reason"),
                second=again.get("reason"), accepted_for_item=(st.registry.accepted.get(wi.work_item_id) or {}).get("attempt_id"))
    rec.decision = d
    rec.summary = A.summarize(d)
    st.emit("attempt_text", attempt=att.attempt_id, chars=len(att.text or ""),
            sha=A.sha(att.text), head=repr((att.text or "")[:100]),
            # Phase 1.22 (Section 5, bounded to Workflow execution -- Team logging untouched):
            # the terminal-selection outcome and the full trace's own identity, so a human
            # watching the event stream can see WHY att.text is what it is without querying
            # the ledger: how many model-request turns ran, whether a terminal segment was
            # found at all, and how the selected text's size compares to the full trace.
            result_selection_status=att.terminal.get("result_selection_status"),
            segments_total=att.terminal.get("segments_total"),
            trace_chars=len(att.trace_text or ""), trace_sha=A.sha(att.trace_text))
    st.emit("attempt_decided", attempt=att.attempt_id, agent=d.execution_status, acceptance=d.acceptance,
            registered=(d.registration or {}).get("registered"), reason=(d.reason or "")[:120])
    return d


# ---------------------------------------------------------------------------- Phase 1.24: recovery and reconciliation
def build_recovery_task(wi: WorkItem, orig_att, orig_d) -> str:
    """Phase 1.24 Track 3.3/5: the recovery attempt's own task text. Carries the ORIGINAL
    objective and ESTABLISHED FINDINGS (never just a bare list of missing obligation IDs), plus
    exactly which mandatory obligations remain, why, and which specific files to target -- all
    derived deterministically from this work item's own policy (Track 4: reuse, never a parallel
    framework) and the original attempt's own Decision. No model judgment is involved in deciding
    WHAT to ask for; only the Researcher's own subsequent reads decide what it finds there."""
    missing_lines = []
    for m in orig_d.gate["missing"]:
        paths = A.requirement_source_paths(wi.policy, m["id"])
        target = ", ".join(sorted(set(paths))) if paths else "the relevant source (no specific file resolved)"
        why = ""
        if "items" in m:
            unmet = [i["item_id"] for i in m["items"] if i.get("status") in ("MISSING", "INSUFFICIENT_EVIDENCE")]
            if unmet:
                why = f" -- specifically still needed: {', '.join(unmet)}"
        elif "links" in m:
            probs = [p for ln in m["links"] for p in ln.get("problems", [])]
            if probs:
                why = " -- " + "; ".join(probs[:3])
        missing_lines.append(f"- {m['id']} ({m['description']}){why}. Read: {target}.")
    established = (orig_att.text or "").strip()
    return (
        f"RECOVERY PASS on a task you have already partly investigated. Original objective: "
        f"{wi.objective}\n\n"
        f"You already produced the ESTABLISHED FINDINGS below in a prior attempt on this exact "
        f"task. They are already correct and accepted -- do NOT re-derive, re-read, or discard "
        f"them; build on them:\n---\n{established}\n---\n\n"
        f"The following MANDATORY obligations from this task's own evidence policy remain "
        f"unresolved. Read the specific file(s) named for each one and establish the missing "
        f"fact directly from that source -- do not guess, and do not conclude something is "
        f"absent merely because an earlier attempt could not read it (a budget limit is not "
        f"evidence of non-existence):\n" + "\n".join(missing_lines) + "\n\n"
        f"Write your answer as the full, updated result: restate what is still true from the "
        f"established findings above (briefly -- do not re-paste large blocks verbatim unless "
        f"they are short), and ADD what you establish now for the obligations listed. If you "
        f"still cannot establish one of them, say so plainly rather than guessing."
    )


def reconcile_attempts(orig_att, orig_d, rec_att) -> tuple:
    """Phase 1.24 Track 6: deterministic evidence reconciliation -- never an LLM merge, never a
    blind concatenation or a blind overwrite. Both passes' full text are kept, clearly attributed
    by provenance (original vs. recovery), so a reader or the assessor can see and judge any
    disagreement directly rather than have one silently absorbed into the other (Track 6: 'Do not
    rely on an LLM merge as the authority for evidence validity' -- this function detects no
    semantic conflict; it exposes both passages side by side for that judgment). Exact-duplicate
    paragraphs (after stripping) are dropped from the recovery section only -- a deterministic,
    non-semantic de-duplication, consistent with this project's existing discipline against
    heuristic/phrase-based mechanisms. Returns (reconciled_text, reconciled_trace_text,
    combined_outcomes, before_after)."""
    orig_paras = [p.strip() for p in (orig_att.text or "").split("\n\n") if p.strip()]
    rec_paras = [p.strip() for p in (rec_att.text or "").split("\n\n") if p.strip()]
    seen = set(orig_paras)
    rec_new = [p for p in rec_paras if p not in seen]
    dropped = len(rec_paras) - len(rec_new)
    missing_ids = [m["id"] for m in orig_d.gate["missing"]]
    header = f"## Recovery pass — added to resolve: {', '.join(missing_ids)}" if missing_ids else "## Recovery pass"
    body = "\n\n".join(rec_new) if rec_new else "(no new content beyond what the original pass already established)"
    reconciled_text = "## Original pass\n\n" + (orig_att.text or "").strip() + f"\n\n{header}\n\n{body}"
    reconciled_trace = (orig_att.trace_text or "") + "\n\n---(recovery pass trace)---\n\n" + (rec_att.trace_text or "")
    combined_outcomes = list(orig_att.outcomes) + list(rec_att.outcomes)
    # Phase 1.25 Track 5: durable recovery provenance. Deliberately NOT duplicating fields the
    # final decision row already carries on its own -- is_recovery/execution_status/
    # terminal_integrity/acceptance (this same row), per-attempt+task-level budget (d.budget,
    # set separately in run_attempt()'s finally block), and the AFTER-recovery obligation
    # statuses (this same row's own gate_json, computed by decide() right after this function
    # returns -- not yet known here). What IS captured here, and nowhere else: the BEFORE state
    # (orig_d's own gate, as it stood before recovery ran), the trigger/reason, the parent
    # attempt link, and the reconciliation outcome.
    recovery_info = {
        "original_attempt_id": orig_att.attempt_id, "recovery_attempt_id": rec_att.attempt_id,
        "recovery_trigger": "coverage_missing_mandatory",
        "recovery_reason": "mandatory coverage obligation(s) missing or insufficient after the initial "
                           "attempt: " + (", ".join(missing_ids) if missing_ids else "(none recorded)"),
        "missing_before": missing_ids,
        "satisfied_before": [s["id"] for s in orig_d.gate.get("satisfied", [])],
        "dropped_duplicate_paragraphs": dropped,
    }
    return reconciled_text, reconciled_trace, combined_outcomes, recovery_info


async def recover_and_reconcile(st: RunState, wi: WorkItem, ctx: dict, controls: dict,
                                orig_rec: AttemptRecord, orig_d, attempt_no: int,
                                extension_info: dict = None):
    """Phase 1.24 Track 5/6: ONE bounded, application-controlled recovery attempt targeting
    specifically the mandatory obligations orig_d.gate['missing'] identifies, followed by
    deterministic evidence reconciliation and an INDEPENDENT final decide() (Track 6: the final
    answer is rebuilt from reconciled evidence and re-evaluated, never just the recovery pass's
    own raw output). The recovery attempt is a real run_attempt() call -- its own raw text/trace
    are durably recorded under its own attempt_id by record_terminal() exactly as any attempt's
    are (Track 6: 'Retain... Recovery answer... Recovery trace'), BEFORE this function ever runs.
    This function then mutates that SAME attempt object's text/trace/outcomes/terminal in place
    (never a separate copy -- st.records already holds the one object _final_record() will look
    up by attempt_id) and runs the ONE decision/ledger commit for that attempt_id through the
    existing, unchanged assess_and_register() -- so the ledger's `decision` row for this
    attempt_id reflects the reconciled, independently re-evaluated result, while its `attempt` row
    (text_sha/trace_sha, written earlier by record_terminal()) still holds exactly what the
    recovery pass itself produced raw, unaffected by the later mutation.
    Returns (AttemptRecord, Decision) to use as this work item's result; never raises -- a failure
    anywhere in recovery leaves the ORIGINAL (orig_rec, orig_d) as the work item's result, since
    the caller only replaces its own (rec, d) locals on a non-None return."""
    task_override = build_recovery_task(wi, orig_rec.attempt, orig_d)
    # Phase 1.30: the same deterministic derivation build_recovery_task() just used to
    # build the recovery prompt's own "Read: <paths>" lines -- reused here, not
    # reimplemented, so the just-in-time extension check in swarm/team.py's budget hook
    # has the identical set of expected source paths to match a blocked read against.
    _missing_ids = [m["id"] for m in orig_d.gate["missing"]]
    _missing_paths = sorted({p for mid in _missing_ids for p in A.requirement_source_paths(wi.policy, mid)})
    rec2 = await run_attempt(st, wi, ctx, controls, attempt_no, task_override=task_override,
                             extra_activity={"missing_source_paths": _missing_paths,
                                             "missing_obligation_ids": _missing_ids})
    # rec2.attempt.state is "FINISHED" even on a ledger-start failure or setup error (see
    # run_attempt()'s own early-exit paths) -- rec2.error/rec2.ledger_error are what actually
    # distinguish a genuine run from one that never got to call the Researcher at all.
    _t2 = rec2.attempt.terminal or {}
    _recovery_run_failed = bool(_t2.get("run_error") or _t2.get("cancelled_event") or _t2.get("timed_out")
                                or _t2.get("result_selection_status") == "MISSING_TERMINAL")
    if rec2.attempt.state != "FINISHED" or rec2.error or (rec2.ledger_error and not rec2.attempt.text) or _recovery_run_failed:
        # Track 8.2: a recovery pass that itself errors, times out, is cancelled, or selects no
        # valid terminal result aborts recovery cleanly -- the ORIGINAL (orig_rec, orig_d) stays
        # the work item's result; the recovery attempt's own failure is never treated as a reason
        # to discard an already-accepted, if partial, original answer.
        st.emit("recovery_aborted", work_item=wi.work_item_id,
                reason=rec2.error or rec2.ledger_error or _t2.get("run_error") or
                       ("cancelled" if _t2.get("cancelled_event") else None) or
                       ("timed_out" if _t2.get("timed_out") else None) or
                       ("missing_terminal" if _t2.get("result_selection_status") == "MISSING_TERMINAL" else None) or
                       "recovery attempt did not finish")
        return None
    # The RAW recovery text/trace are already durably recorded under rec2.attempt.attempt_id by
    # record_terminal() (called inside run_attempt() above, before this function ever runs) --
    # captured here, before being overwritten below, purely so this function's own return/emit can
    # still reference what the recovery pass produced on its own (Track 6: "Retain... Recovery
    # answer... Recovery trace"); the ledger's own attempt row is untouched by what follows.
    raw_recovery_text, raw_recovery_trace = rec2.attempt.text, rec2.attempt.trace_text
    reconciled_text, reconciled_trace, combined_outcomes, recovery_info = reconcile_attempts(
        orig_rec.attempt, orig_d, rec2.attempt)
    recovery_info["raw_recovery_text_sha"] = A.sha(raw_recovery_text)
    recovery_info["raw_recovery_chars"] = len(raw_recovery_text or "")
    # Phase 1.29d Track 2.3: fold the extension GRANT decision (computed by the caller
    # BEFORE this recovery attempt ran, from the ORIGINAL attempt's own budget facts) into
    # THIS recovery attempt's own "budget" dict -- never replacing it, since run_attempt()'s
    # finally block already wrote the recovery attempt's real post-recovery figures there
    # (which reflect whatever extended ceiling controls carried into this call). Durable
    # record of the reason/outstanding-obligations/prior-allocation/new-allocation whether
    # or not the recovery attempt that followed actually used the extra room.
    if extension_info is not None and rec2.attempt.terminal is not None:
        rec2.attempt.terminal["budget"] = dict(rec2.attempt.terminal.get("budget") or {},
                                               extension=extension_info)
    # Mutate rec2.attempt IN PLACE (never construct a separate object) -- rec2 is the SAME
    # AttemptRecord instance run_attempt() already appended to st.records, and _final_record()
    # looks up the accepted attempt by attempt_id inside st.records; a separate reconciled copy
    # would silently never be found there, leaving the raw (un-reconciled) text displayed instead.
    rec2.attempt.text = reconciled_text
    rec2.attempt.trace_text = reconciled_trace
    rec2.attempt.outcomes = combined_outcomes
    # Phase 1.25: merge into the EXISTING terminal dict (preserving the "budget" key run_attempt()'s
    # own finally block already wrote for this same attempt) rather than replacing it outright --
    # Phase 1.24 replaced the whole dict here, which would have silently discarded Phase 1.25's own
    # budget snapshot for this attempt had this still been a flat reassignment.
    rec2.attempt.terminal = dict(rec2.attempt.terminal or {},
                                 result_selection_status="RECONCILED", completed_event=True,
                                 run_output_status="RunStatus.completed", run_error=None,
                                 cancelled_event=False, timed_out=False, stream_closed_early=False,
                                 recovery_of=orig_rec.attempt.attempt_id, recovery_info=recovery_info)
    d2 = await assess_and_register(st, wi, rec2, controls)
    st.emit("recovery_reconciled", work_item=wi.work_item_id, original=orig_rec.attempt.attempt_id,
            recovery=rec2.attempt.attempt_id, missing_before=recovery_info["missing_before"],
            coverage_after=d2.coverage_status, acceptance_after=d2.acceptance)
    return rec2, d2


# ---------------------------------------------------------------------------- deterministic finalization
_STATUS_ORDER = (A.ACCEPTED_COMPLETE, A.ACCEPTED_PARTIAL, A.UNRESOLVED, A.BLOCKED, A.NOT_ACCEPTED)


def _final_record(st: RunState, wi: WorkItem):
    """The record that speaks for a work item: the registry's accepted attempt (newest valid generation), else
    the latest attempt (which is then reported as unresolved/blocked/not accepted, never with its text)."""
    acc = st.registry.accepted.get(wi.work_item_id)
    if acc:
        for r in st.records:
            if r.attempt.attempt_id == acc["attempt_id"]:
                return r, True
    mine = [r for r in st.records if r.attempt.work_item_id == wi.work_item_id]
    return (mine[-1] if mine else None), False


def _unresolved_items(d) -> list:
    items = []
    gate = getattr(d, "gate", None) or {}
    for m in gate.get("missing", []):
        why = ""
        for l in m.get("links", []) or []:
            if not l.get("ok"):
                why += f" [{l['id']}: " + "; ".join(l.get("problems", [])) + "]"
        items.append(f"{m['id']}: {m['description']}{why}")
    uns = (getattr(d, "assessor_claims", None) or {}).get("unsupported") or []
    if uns:
        items.append("claims in the report not supported by this attempt's own evidence: " + ", ".join(str(u) for u in uns[:10]))
    return items


def build_final(st: RunState) -> str:
    """Pure function of the plan, the attempt records and the registry. No model, no randomness, no text from
    any attempt that was not accepted."""
    plan = st.plan
    lines = []
    per_item = []
    for wi in plan.items:
        rec, accepted = _final_record(st, wi)
        per_item.append((wi, rec, accepted))
    n_c = sum(1 for _, r, a in per_item if a and r.decision.acceptance == A.ACCEPTED_COMPLETE)
    n_p = sum(1 for _, r, a in per_item if a and r.decision.acceptance == A.ACCEPTED_PARTIAL)
    overall = ("COMPLETE" if n_c == len(per_item) and per_item else
               "PARTIAL" if (n_c + n_p) > 0 else "NOT ESTABLISHED")
    lines.append(f"## Investigation result: {overall}")
    if getattr(st, "deadline_hit", False):
        lines.append("_The overall task deadline was reached; work that had not completed is reported as not established._")
    lines.append(f"task `{st.task_id}` · workflow run `{st.workflow_run_id}` · "
                 f"{len(per_item)} work item(s) · {len(st.records)} attempt(s)")
    lines.append("")
    for wi, rec, accepted in per_item:
        d = rec.decision if rec else None
        status = d.acceptance if d else A.NOT_ACCEPTED
        lines.append(f"### {wi.work_item_id}: {wi.title} — {status}")
        if rec is not None:
            a = rec.attempt
            lines.append(f"attempt `{a.attempt_id}` · generation {a.generation} · agent run `{a.agent_run_id}`")
        if accepted:
            shown = {A.norm_path(p): p for it in plan.items for p in it.paths}
            reads = [shown.get(p, p) for p in (d.counts or {}).get("source_files_read", [])]
            lines.append("")
            lines.append(rec.attempt.text.strip())
            lines.append("")
            lines.append("Source files read by this attempt: " + (", ".join(f"`{p}`" for p in reads) or "none"))
            al = (d.counts or {}).get("all", {})
            lines.append("Tool outcomes for this attempt: " + ", ".join(f"{al[k]} {k}" for k in A.OUTCOMES if al.get(k)))
            if status == A.ACCEPTED_PARTIAL:
                lines.append("")
                lines.append("**Not established by this attempt (unresolved):**")
                for u in _unresolved_items(d) or ["the result is partial: " + (d.reason or "")[:200]]:
                    lines.append(f"- {u}")
        else:
            lines.append("")
            lines.append("No finding from this work item is accepted. The attempt's text is withheld.")
            if d is not None:
                lines.append(f"Reason: {(d.reason or d.execution_reason or '')[:300]}")
                for u in _unresolved_items(d):
                    lines.append(f"- not established: {u}")
            else:
                lines.append("Reason: not started because the overall task deadline was reached."
                             if getattr(st, "deadline_hit", False) else "Reason: no attempt produced a decision.")
        hist = [r for r in st.records if r.attempt.work_item_id == wi.work_item_id]
        if len(hist) > 1 or (hist and not accepted):
            lines.append("")
            lines.append("Attempt history: " + " → ".join(
                f"`{r.attempt.attempt_id}` {r.decision.acceptance if r.decision else 'NO_DECISION'}"
                f" (agent {r.decision.execution_status if r.decision else 'n/a'})" for r in hist))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def audit_final(st: RunState, final: str) -> dict:
    """Deterministic integrity check of the final answer against the registry: no withheld/not-accepted attempt
    text, and every work item's status label is the registry's. Returns {"ok": bool, "problems": [...]}."""
    problems = []
    accepted_ids = {v["attempt_id"] for v in st.registry.accepted.values()}
    accepted_text = "\n".join((r.attempt.text or "") for r in st.records if r.attempt.attempt_id in accepted_ids)
    for r in st.records:
        if r.attempt.attempt_id in accepted_ids:
            continue
        # Every substantial line/sentence of a non-accepted attempt must be absent from the final answer, unless
        # the same sentence is legitimately present in an ACCEPTED attempt's own text (e.g. an identical retry).
        frags = [f.strip() for f in re.split(r"[\n]|(?<=[.!?])\s+", r.attempt.text or "") if len(f.strip()) >= 40]
        leaked = [f for f in frags if f in final and f not in accepted_text]
        if leaked:
            problems.append(f"text of non-accepted attempt {r.attempt.attempt_id} appears in the final answer "
                            f"({len(leaked)} fragment(s))")
    for wi in st.plan.items:
        rec, accepted = _final_record(st, wi)
        want = rec.decision.acceptance if rec and rec.decision else A.NOT_ACCEPTED
        if f"### {wi.work_item_id}: {wi.title} — {want}" not in final:
            problems.append(f"status label for {wi.work_item_id} does not match the registry ({want})")
        if accepted and want not in (A.ACCEPTED_COMPLETE, A.ACCEPTED_PARTIAL):
            problems.append(f"{wi.work_item_id} marked accepted with status {want}")
    if "Investigation result: COMPLETE" in final:
        for wi in st.plan.items:
            rec, accepted = _final_record(st, wi)
            if not (accepted and rec.decision.acceptance == A.ACCEPTED_COMPLETE):
                problems.append(f"overall COMPLETE but {wi.work_item_id} is not ACCEPTED_COMPLETE")
    return {"ok": not problems, "problems": problems}


def _knowledge_audit(k) -> dict:
    """What the audit records about retrieval: the outcome and sizes, never the content."""
    if k is None:
        return {"status": None}
    return {"status": k.status, "reason": k.reason, "source_files": len(k.source_files()), "fallback": k.fallback_required,
            "raw_chars": (k.stats or {}).get("raw_chars_total"), "brief_chars": (k.stats or {}).get("brief_chars_researcher")}


def _safe_final(st: RunState, problems: list) -> str:
    return ("## Investigation result: NOT ESTABLISHED\n"
            f"task `{st.task_id}` · workflow run `{st.workflow_run_id}`\n\n"
            "No findings are reported because the application could not establish a trustworthy final answer. "
            "Problems: " + "; ".join(problems[:5]) + "\n")


def reconstruct_final(led, task_id: str) -> Optional[str]:
    """Rebuild the final answer from DURABLE records only (task spec, attempts, stored decisions, stored attempt text,
    the ledger's accepted pointer). Used as a check against the stored final and available after a restart."""
    import json as _json
    from types import SimpleNamespace as NS
    raw = led.load_for_reconstruction(task_id)
    if raw is None:
        return None
    st = RunState(task="", task_id=task_id, workflow_run_id=raw["task"]["workflow_run_id"], registry=A.Registry())
    try:
        st.deadline_hit = bool(_json.loads(raw["task"].get("audit_json") or "{}").get("deadline_hit"))
    except Exception:  # noqa: BLE001
        st.deadline_hit = False
    items = []
    for it in raw["items"]:
        w = it["work_item"]
        spec = _json.loads(w["spec_json"])
        wi = WorkItem(w["work_item_id"], w["title"], spec["target"], spec["objective"], spec["evidence_required"],
                      spec["completion_criteria"], spec["paths"], derive_policy(spec["paths"], spec["objective"]))
        items.append(wi)
        for a in it["attempts"]:
            at = a["attempt"]
            att = NS(attempt_id=at["attempt_id"], work_item_id=at["work_item_id"], generation=at["generation"],
                     agent_run_id=at["agent_run_id"], text=a["text"])
            d = a["decision"]
            dec = None
            if d is not None:
                extra = _json.loads(d.get("extra_json") or "{}")
                dec = NS(acceptance=d["acceptance"], execution_status=d["execution_status"], validity=d["validity"],
                         reason=d["reason"], execution_reason=extra.get("execution_reason") or "",
                         counts=_json.loads(d["counts_json"] or "{}"), gate=_json.loads(d["gate_json"] or "{}"),
                         assessor_claims=_json.loads(d["assessor_claims_json"] or "{}"), assessor_status=d["assessor_status"])
            st.records.append(AttemptRecord(attempt=att, decision=dec))
        if w["accepted_attempt_id"]:
            st.registry.accepted[w["work_item_id"]] = {"attempt_id": w["accepted_attempt_id"], "work_item_id": w["work_item_id"]}
    st.plan = WorkPlan(task_id, items)
    return build_final(st)


# ---------------------------------------------------------------------------- the Workflow
async def run_engineering_workflow(
    task: str, agent_specs, coordinator_model, coordinator_tools, mcp_url, mcp_urls,
    project_id: str, session_id, controls_raw=None, registry=None, work_items=None, idempotency_key=None,
    liveness_path=None, liveness_nonce=None, knowledge=None,
):
    """Entry point called from swarm.team.run_task_async when orchestration == "workflow".
    Returns (content, tokens, clarification) exactly like the Team path."""
    from agno.tools.mcp import MCPTools
    from agno.workflow import Workflow, Step
    import swarm.team as T
    from swarm import model_routing, team_config

    controls = _controls(controls_raw)
    task_id = (f"task-{hashlib.sha256(str(idempotency_key).encode()).hexdigest()[:10]}" if idempotency_key
               else f"task-{uuid.uuid4().hex[:10]}")
    wf_run_id = f"wf-{uuid.uuid4().hex[:8]}"
    st = RunState(task=task, task_id=task_id, workflow_run_id=wf_run_id, registry=registry or A.Registry())
    led = Ledger()
    try:
        await led.ainit()
    except Exception as e:  # noqa: BLE001 -- fail closed: create_task below will refuse to dispatch
        print(f"[workflow] ledger init failed: {repr(e)[:160]}", flush=True)
    if controls.get("ledger_fail"):
        led.fail_next = controls["ledger_fail"]          # test control: next ledger op of that kind raises
    st.ledger = led
    # Overall task deadline. `ignore_task_deadline` (test control) leaves the WORKER unenforced so the parent's
    # independent hard cap can be demonstrated; the wall-clock deadline is published either way.
    from config.config import config as _cfg
    _dl = float(controls.get("task_deadline_s") or _cfg.workflow_task_deadline_s)
    st.reserve_s = float(_cfg.workflow_deadline_reserve_s)
    st.deadline_wall = time.time() + _dl
    st.deadline_mono = None if controls.get("ignore_task_deadline") else time.monotonic() + _dl
    st.identity = {"task_id": task_id, "workflow_run_id": wf_run_id}

    async def _snapshot_loop():
        while True:
            L.write_snapshot(liveness_path, L.build_snapshot(
                nonce=liveness_nonce, state=st.wf_state, identity=st.identity, tracker=st.tracker,
                task_deadline_wall=st.deadline_wall, attempt_deadline_wall=st.attempt_deadline_wall,
                stream_events=st.stream_events))
            await asyncio.sleep(SNAPSHOT_INTERVAL_S)

    hb_task = asyncio.create_task(_snapshot_loop()) if (liveness_path and not controls.get("no_snapshot")) else None   # no_snapshot: test control
    if idempotency_key:
        # Duplicate submission of the SAME task: never re-run. A finalized task returns its stored final answer; any
        # other state is reported as-is (it is either still running elsewhere or was interrupted).
        try:
            prior = await led._a(led.get_task, task_id)
        except Exception as e:  # noqa: BLE001
            prior, st.final = None, _safe_final(st, [f"durable ledger unavailable ({repr(e)[:100]})"])
            return st.final, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, None
        if prior is not None:
            if prior["status"] == "FINALIZED":
                raw = await led._a(led.load_for_reconstruction, task_id)
                st.emit("duplicate_submission", task=task_id, outcome="returned_stored_final")
                return (raw["task"]["final_text"] + "\n_Duplicate submission: this task was already finalized; the stored result is returned and nothing was re-run._\n",
                        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, None)
            st.emit("duplicate_submission", task=task_id, outcome=f"refused_status_{prior['status']}")
            return (f"## Investigation result: NOT ESTABLISHED\ntask `{task_id}`\n\nThis task was already submitted and is "
                    f"{prior['status']}; it was not started again. Inspect it via /ledger/tasks/{task_id}.\n",
                    {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, None)
    LAST["state"] = st
    st.emit("workflow_start", task=task_id, workflow_run=wf_run_id, controls=sorted(controls) or None)

    effective_mcp_url = mcp_url or T.config.mcp_url
    all_mcp_urls = [u for u in (mcp_urls or []) + [effective_mcp_url] if u]
    specs = list(agent_specs or [])
    if not specs:
        return ("## Investigation result: NOT ESTABLISHED\nNo agent roster was supplied for the workflow path.\n",
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, None)
    # Task-shape skills: same injection the Team path applies to the Researcher's own instructions.
    shapes = T._matching_task_shapes(task)
    if shapes:
        hive_for_skills = T._pick_hive_mcp_url(all_mcp_urls, effective_mcp_url)
        for name in shapes:
            text = await T._fetch_skill_text(hive_for_skills, name)
            if not text:
                continue
            for sp in specs:
                if T._member_key(getattr(sp, "name", "")) == "researcher":
                    sp.instructions = list(getattr(sp, "instructions", None) or []) + [
                        f"── Skill: {name} (auto-loaded — this task matches its shape) ──", text]
    specs, ctools = T._strip_mutating(specs, coordinator_tools)     # investigation-only: ALWAYS read-only

    # Phase 1.20: the knowledge stage uses the SAME shared service as the Team. The dossier was prepared once by the caller
    # (swarm.team.run_task_async) before this engine was entered; a direct caller that passed none gets it prepared here.
    if knowledge is None:
        knowledge = await T._prepare_knowledge(task, project_id, mcp_urls, effective_mcp_url, None, "run_engineering_workflow")
    st.knowledge = knowledge
    st.emit("knowledge", status=knowledge.status, files=len(knowledge.source_files()), fallback=knowledge.fallback_required,
            brief_chars=(knowledge.stats or {}).get("brief_chars_researcher"))

    async with AsyncExitStack() as stack:
        mcp_list = []
        for url in all_mcp_urls:
            excl = (T._PROJECT_MCP_EXCLUDE_TOOLS if url == effective_mcp_url
                    else (T._LIGHTRAG_AGENT_EXCLUDE_TOOLS if url == T.config.lightrag_mcp_url else None))
            if not await T._mcp_reachable(url):
                print(f"[workflow] MCP unreachable, skipping ({url}) -- the run continues without it", flush=True)
                continue
            try:
                mcp_list.append(await stack.enter_async_context(
                    MCPTools(url=url, transport="streamable-http", timeout_seconds=T._MCP_TIMEOUT, exclude_tools=excl)))
            except Exception as e:  # noqa: BLE001
                print(f"[workflow] MCP unavailable, skipping ({url}): {e}", flush=True)
        if not mcp_list:
            raise RuntimeError("No MCP server available — check hive-mcp and project MCP are running")
        await model_routing.ensure_cache_loaded()
        await team_config.ensure_cache_loaded()
        hive_url = T._pick_hive_mcp_url(all_mcp_urls, effective_mcp_url)
        catalog = await T._fetch_skill_catalog(hive_url)
        ctx = {"knowledge_brief": K.render_brief(st.knowledge, "researcher"), "specs": specs, "coordinator": coordinator_model or T.config.leader_model, "ctools": ctools,
               "mcp_list": mcp_list, "catalog": catalog, "project_id": project_id, "hive_url": hive_url,
               "profile": T.execution_context.resolve_profile(None)}

        # ---- steps (never raise; they record into `st`) --------------------------------------------------
        async def s_intake(step_input):
            st.steps_run.append("intake")
            st.emit("step", name="intake", task_chars=len(task))
            return "intake-ok"

        async def s_knowledge(step_input):
            st.steps_run.append("knowledge")
            k = st.knowledge
            st.emit("step", name="knowledge", status=getattr(k, "status", None), files=len(k.source_files()) if k else 0,
                    fallback=getattr(k, "fallback_required", None), reason=getattr(k, "reason", None))
            return "knowledge-ok"

        async def s_plan(step_input):
            st.steps_run.append("plan")
            st.wf_state = "planning"
            st.plan = plan_work_items(work_items, task_id) if work_items else plan_request(task, task_id)
            if st.plan.items and st.ledger is not None:
                try:     # the plan is durable before ANY attempt can exist
                    await st.ledger._a(st.ledger.create_task, task_id, wf_run_id, task, [
                        {"work_item_id": w.work_item_id, "title": w.title,
                         "spec": {"paths": w.paths, "target": w.target, "objective": w.objective,
                                  "evidence_required": w.evidence_required, "completion_criteria": w.completion_criteria,
                                  "policy": w.policy.name}} for w in st.plan.items])
                    st.ledger_task_created = True
                except Exception as e:  # noqa: BLE001 -- fail closed: nothing is dispatched without a durable plan
                    st.plan = WorkPlan(task_id, [], f"durable ledger unavailable ({repr(e)[:120]}); request not dispatched")
            st.emit("step", name="plan", items=len(st.plan.items), rejected=st.plan.rejected_reason)
            return "plan-ok"

        async def s_execute(step_input):
            st.steps_run.append("execute")
            for wi in (st.plan.items if st.plan else []):
                c = item_controls(controls, wi.work_item_id)
                n = 1
                while n <= MAX_ATTEMPTS_PER_ITEM:
                    rem = st.remaining()
                    if st.deadline_hit or (rem is not None and rem < MIN_ATTEMPT_START_S):
                        if not st.deadline_hit:
                            st.deadline_hit = True
                        st.emit("task_deadline_no_start", work_item=wi.work_item_id, remaining_s=None if rem is None else round(rem, 1))
                        break
                    sup = c.get("supersede_after_s") if n == 1 else None
                    if sup:
                        # Control: a newer generation starts while attempt 1 is still running. The registry marks
                        # attempt 1 SUPERSEDED the moment attempt 2 starts; attempt 1 keeps running and finishes late.
                        t1 = asyncio.ensure_future(run_attempt(st, wi, ctx, c, 1))
                        done, _ = await asyncio.wait({t1}, timeout=float(sup))
                        if not done:
                            c2 = {k: v for k, v in c.items() if k not in ("supersede_after_s", "delay_tool_s", "cancel_after_s")}
                            st.emit("supersede_started", work_item=wi.work_item_id)
                            rec2 = await run_attempt(st, wi, ctx, c2, 2)
                            d2 = await assess_and_register(st, wi, rec2, c)
                            rec1 = await t1                       # the old attempt finishes LATE
                            d1 = await assess_and_register(st, wi, rec1, c)
                            st.emit("supersede_result", work_item=wi.work_item_id, new=d2.acceptance, old=d1.acceptance,
                                    old_validity=d1.validity)
                            break
                        rec = t1.result()
                    else:
                        rec = await run_attempt(st, wi, ctx, c, n)
                    d = await assess_and_register(st, wi, rec, c)
                    retry = (d.execution_status in RETRYABLE_TERMINALS and d.validity == "CURRENT"
                             and n < MAX_ATTEMPTS_PER_ITEM)
                    st.emit("retry_decision", work_item=wi.work_item_id, attempt=rec.attempt.attempt_id,
                            agent=d.execution_status, retry=retry, attempts_left=MAX_ATTEMPTS_PER_ITEM - n)
                    if not retry:
                        if d.execution_status in RETRYABLE_TERMINALS and n >= MAX_ATTEMPTS_PER_ITEM:
                            st.emit("retry_exhausted", work_item=wi.work_item_id, attempts=n)
                        # Phase 1.24 Track 5: application-controlled coverage recovery -- a
                        # SEPARATE, deterministic trigger from the execution-failure retry
                        # above, never requested by the Coordinator/Researcher. `not sup`
                        # excludes the test-controls concurrent-attempt path on purpose (see
                        # recover_and_reconcile's own docstring on sequential-only recovery).
                        if (not sup and n < MAX_ATTEMPTS_PER_ITEM and d.validity == "CURRENT"
                                and A.recovery_eligible(d)):
                            rem2 = st.remaining()
                            if st.deadline_hit or (rem2 is not None and rem2 < MIN_ATTEMPT_START_S):
                                st.emit("recovery_skipped", work_item=wi.work_item_id, reason="deadline")
                            else:
                                st.emit("recovery_triggered", work_item=wi.work_item_id,
                                        attempt=rec.attempt.attempt_id,
                                        missing=[m["id"] for m in d.gate["missing"]])
                                # Phase 1.29d Track 2.2: application-controlled, deterministic
                                # decision on whether this ALREADY-TRIGGERED recovery attempt
                                # should ALSO get a bounded task-budget extension -- computed
                                # purely from the ORIGINAL attempt's own ledger/budget facts,
                                # never from model text, and emitted either way for a durable,
                                # inspectable record of why.
                                ext = A.extension_eligible(d, rec)
                                st.emit("budget_extension_decision", work_item=wi.work_item_id,
                                        attempt=rec.attempt.attempt_id, **ext)
                                c_recovery = c
                                if ext["eligible"]:
                                    c_recovery = dict(c)
                                    if ext["new_read_ceiling"]:
                                        c_recovery["ceiling"] = ext["new_read_ceiling"]
                                        # Phase 1.30: write into the SAME task-scoped ceiling
                                        # state the just-in-time mechanism reads/writes, so
                                        # the recovery attempt's own seeded activity["read_
                                        # ceiling"] already reflects this upfront grant --
                                        # one authorized-ceiling number per work item, not
                                        # two independently-tracked ones.
                                        st.task_read_ceiling[wi.work_item_id] = ext["new_read_ceiling"]
                                    if ext["new_discovery_ceiling"]:
                                        c_recovery["discovery_ceiling"] = ext["new_discovery_ceiling"]
                                        st.task_discovery_ceiling[wi.work_item_id] = ext["new_discovery_ceiling"]
                                    st.emit("budget_extension_granted", work_item=wi.work_item_id,
                                            attempt=rec.attempt.attempt_id,
                                            read_extension=ext["read_extension"],
                                            discovery_extension=ext["discovery_extension"],
                                            new_read_ceiling=ext["new_read_ceiling"],
                                            new_discovery_ceiling=ext["new_discovery_ceiling"])
                                recovered = await recover_and_reconcile(st, wi, ctx, c_recovery, rec, d, n + 1,
                                                                       extension_info=ext)
                                if recovered is not None:
                                    rec, d = recovered
                        break
                    n += 1
            return "execute-done"

        async def s_finalize(step_input):
            st.steps_run.append("finalize")
            st.wf_state = "finalizing"
            if controls.get("hold_before_finalize_s"):       # test control: window between acceptance and finalization
                st.emit("hold", where="before_finalize", seconds=controls["hold_before_finalize_s"])
                await asyncio.sleep(float(controls["hold_before_finalize_s"]))
            if not st.plan or not st.plan.items:
                st.final = ("## Investigation result: NOT ESTABLISHED\n"
                            f"task `{task_id}` · workflow run `{wf_run_id}`\n\n"
                            f"The request was not dispatched: {st.plan.rejected_reason if st.plan else 'no plan'}.\n")
                st.final_audit = {"ok": True, "problems": []}
            else:
                final = build_final(st)
                audit = audit_final(st, final)
                st.final_audit = audit
                st.final = final if audit["ok"] else _safe_final(st, audit["problems"])
                if st.ledger is not None and st.ledger_task_created:
                    try:
                        fr = await st.ledger._a(st.ledger.finalize_task, task_id, st.final,
                                                dict(audit, deadline_hit=bool(st.deadline_hit), engine="workflow", knowledge=_knowledge_audit(st.knowledge)))
                        if not fr["finalized_now"]:
                            st.final = fr["final_text"]          # idempotent: the stored final wins
                        if controls.get("duplicate_finalize"):
                            fr2 = await st.ledger._a(st.ledger.finalize_task, task_id, st.final, audit)
                            st.emit("duplicate_finalize", reason=fr2["reason"], identical=fr2["identical"])
                        rc = await st.ledger._a(reconstruct_final, st.ledger, task_id)
                        st.emit("reconstruct", identical=(rc == st.final), chars=len(rc or ""))
                    except Exception as e:  # noqa: BLE001 -- fail closed
                        st.final = _safe_final(st, [f"durable ledger could not finalize the task ({repr(e)[:100]})"])
                        st.final_audit = {"ok": False, "problems": ["ledger finalize failed"]}
                elif controls.get("duplicate_finalize"):
                    again = build_final(st)               # control: finalize a second time, from the same registry
                    st.emit("duplicate_finalize", identical=(again == final))
            st.emit("step", name="finalize", audit_ok=st.final_audit["ok"], chars=len(st.final))
            return "finalize-ok"

        wf = Workflow(name="engineering-workflow", steps=[
            Step(name="intake", executor=s_intake, max_retries=0),
            Step(name="knowledge", executor=s_knowledge, max_retries=0),
            Step(name="plan", executor=s_plan, max_retries=0),
            Step(name="execute", executor=s_execute, max_retries=0),
            Step(name="finalize", executor=s_finalize, max_retries=0)])
        wf_status = None
        try:
            wout = await wf.arun(task, run_id=wf_run_id, session_id=session_id or f"wfs-{uuid.uuid4().hex[:8]}")
            wf_status = str(getattr(wout, "status", None))
        except BaseException as e:  # noqa: BLE001
            wf_status = f"EXC:{repr(e)[:120]}"
        st.emit("workflow_end", workflow_status=wf_status, steps=",".join(st.steps_run),
                finalized=st.final is not None)

        # A completed Workflow is not proof of anything: the answer comes only from application finalization.
        if st.final is None or st.steps_run != ["intake", "knowledge", "plan", "execute", "finalize"]:
            problems = [f"workflow did not run every step (ran: {st.steps_run}, status: {wf_status})"]
            if st.plan and st.plan.items and st.records:
                try:
                    f2 = build_final(st)
                    au = audit_final(st, f2)
                    st.final = (f2 if au["ok"] else _safe_final(st, au["problems"] + problems))
                    st.final += "\n_Note: the Workflow engine did not report a normal finish; this answer was built from the application's own registry._\n"
                except Exception as e:  # noqa: BLE001
                    st.final = _safe_final(st, problems + [repr(e)[:100]])
            else:
                st.final = _safe_final(st, problems)

    if str(wf_status).startswith("EXC") and st.final and "did not report a normal finish" not in st.final:
        st.final += ("\n_Note: the Workflow engine reported an error after the steps ran; this answer is the "
                     "application's own finalization from its registry._\n")
    st.wf_state = "done"
    if hb_task is not None:
        hb_task.cancel()
        L.write_snapshot(liveness_path, L.build_snapshot(
            nonce=liveness_nonce, state="done", identity=st.identity, tracker=st.tracker,
            task_deadline_wall=st.deadline_wall, stream_events=st.stream_events))
    tin = sum(r.tokens["in"] for r in st.records)
    tout = sum(r.tokens["out"] for r in st.records)
    print("[workflow] decisions " + "; ".join(
        f"{r.attempt.attempt_id}:{r.decision.execution_status if r.decision else 'n/a'}->"
        f"{r.decision.acceptance if r.decision else 'n/a'}" for r in st.records), flush=True)
    return st.final, {"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout}, None
