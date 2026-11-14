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


async def run_attempt(st: RunState, wi: WorkItem, ctx: dict, controls: dict, attempt_no: int):
    """Create a NEW attempt (new id + generation via the registry), run the real Researcher under it, observe the
    Agent's own terminal facts, and return the AttemptRecord. Never raises."""
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
    try:
        if controls.get("ceiling") is not None:
            T._DELEGATION_READ_CEILING = controls["ceiling"]
        # A fresh team (hence a fresh Researcher, hooks and evidence state) per attempt: a retry inherits nothing.
        team = T._build_team(
            ctx["specs"], ctx["coordinator"], ctx["ctools"], "coordinate", ctx["mcp_list"], [],
            read_only=True, skill_catalog=ctx["catalog"], task=wi.objective, team_name="engineering",
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
        echo = f"{wi.target} {wi.objective} {wi.evidence_required} {wi.completion_criteria}"
        att.preloaded = {"text": ctx.get("knowledge_brief") or "", "echo": echo, "target": wi.target, "objective": wi.objective,
                         "evidence_required": wi.evidence_required, "task_text": echo}
        team._delegation_preloaded[A.KEY] = dict(att.preloaded)
        canon = T._build_canonical_researcher_task(
            target=wi.target, objective=wi.objective, evidence_required=wi.evidence_required,
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
