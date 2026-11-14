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


def derive_policy(paths: list):
    """Deterministic evidence policy: one requirement per named file (a SUCCESS source_read of that file by the
    attempt). The known T11 doc-upload chain additionally gets its evidence-backed relationship links."""
    t11 = {A.norm_path(c): r for r in A.T11_DOC_UPLOAD_POLICY.requirements for c in r.any_of_paths}
    reqs, used = [], set()
    for i, p in enumerate(paths, 1):
        n = A.norm_path(p)
        hit = next((r for c, r in t11.items() if n.endswith(c)), None)
        if hit is not None:
            reqs.append(hit)
            used.add(hit.id)
        else:
            reqs.append(A.Requirement(f"R{i}_read_{os.path.basename(p)}", f"read source file {p}", [n]))
    links = list(A.T11_DOC_UPLOAD_POLICY.links) if used >= {r.id for r in A.T11_DOC_UPLOAD_POLICY.requirements} else []
    return A.EvidencePolicy(name="derived-" + hashlib.sha256("|".join(paths).encode()).hexdigest()[:8],
                            requirements=reqs, links=links)


def plan_request(task: str, task_id: str) -> WorkPlan:
    """Application-owned plan. Validation: the request must name at least one repository file; otherwise the
    application cannot define required evidence, and refuses to dispatch rather than let a model invent scope."""
    task = (task or "").strip()
    if not task:
        return WorkPlan(task_id, [], "empty request")
    paths = extract_paths(task)
    if not paths:
        return WorkPlan(task_id, [], "the request names no repository file path, so the application cannot define "
                                     "required source evidence (workflow path is investigation of named files)")
    wi = WorkItem(
        work_item_id="wi-1", title="Investigate the named files",
        target="Files named in the request: " + " ; ".join(paths),
        objective=task,
        evidence_required=("For each named file: the file path you read, the function/handler name, and the route path "
                           "or URL, taken from file content you actually read."),
        completion_criteria=("Done when each named file has a source-backed finding, or an explicit statement of "
                             "which file or step could not be evidenced and why."),
        paths=paths, policy=derive_policy(paths))
    return WorkPlan(task_id, [wi])


# ---------------------------------------------------------------------------- run state
@dataclass
class AttemptRecord:
    attempt: object
    decision: object = None
    summary: dict = field(default_factory=dict)
    error: Optional[str] = None
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
    t0: float = field(default_factory=time.time)

    def emit(self, kind: str, **kw):
        rec = {"t": round(time.time() - self.t0, 2), "event": kind, **kw}
        self.progress.append(rec)
        print("[workflow] " + " ".join(f"{k}={v}" for k, v in rec.items()), flush=True)


# ---------------------------------------------------------------------------- one Researcher attempt
class _Trace:
    def __init__(self, T, RunContentEvent):
        self.T, self.RCE = T, RunContentEvent
        self.parts, self.pending = [], False
        self.agent_run_id = None
        self.run_output = None
        self.completed = self.cancelled = False
        self.error = None
        self.tin = self.tout = 0

    def add(self, ev):
        name = getattr(ev, "event", None) or type(ev).__name__
        if name == "RunStarted":
            self.agent_run_id = ev.run_id
        elif name == "ModelRequestCompleted":
            self.tin += ev.input_tokens or 0
            self.tout += ev.output_tokens or 0
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
        elif name in self.T._SEGMENT_BOUNDARY_EVENTS:
            self.pending = True

    @property
    def text(self):
        return "".join(self.parts)


def _controls(raw) -> dict:
    """Fault/budget controls for controlled validation. Honoured ONLY when the process was started with
    HIVE_WORKFLOW_TEST_CONTROLS=1; otherwise ignored (and logged) so a production request cannot inject faults."""
    if not raw:
        return {}
    if os.environ.get(CONTROLS_ENV) != "1":
        print("[workflow] workflow_controls ignored: test controls are not enabled in this process", flush=True)
        return {}
    return dict(raw)


async def run_attempt(st: RunState, wi: WorkItem, ctx: dict, controls: dict, attempt_no: int):
    """Create a NEW attempt (new id + generation via the registry), run the real Researcher under it, observe the
    Agent's own terminal facts, and return the AttemptRecord. Never raises."""
    import functools
    import inspect
    import swarm.team as T
    from agno.agent import Agent
    from agno.run.agent import RunContentEvent

    att = st.registry.start(st.task_id, wi.work_item_id, workflow_run_id=st.workflow_run_id)
    rec = AttemptRecord(attempt=att)
    st.records.append(rec)
    st.emit("attempt_started", work_item=wi.work_item_id, attempt=att.attempt_id, generation=att.generation,
            workflow_run=st.workflow_run_id, attempt_no=attempt_no)
    trace = _Trace(T, RunContentEvent)
    old_ceiling = T._DELEGATION_READ_CEILING
    outcome_rec = A.OutcomeRecorder(att.attempt_id, tool_deadline=controls.get("tool_deadline"))
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
        att.preloaded = {"text": "", "echo": echo, "target": wi.target, "objective": wi.objective,
                         "evidence_required": wi.evidence_required, "task_text": echo}
        team._delegation_preloaded[A.KEY] = dict(att.preloaded)
        canon = T._build_canonical_researcher_task(
            target=wi.target, objective=wi.objective, evidence_required=wi.evidence_required,
            completion_criteria=wi.completion_criteria)
        if controls.get("break_model"):
            researcher.model.base_url = "http://127.0.0.1:9/v1"
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
                        if trace.agent_run_id and att.agent_run_id is None:
                            att.agent_run_id = trace.agent_run_id
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
                    ret = Agent.cancel_run(trace.agent_run_id) if trace.agent_run_id else None
                    st.emit("attempt_cancelled", attempt=att.attempt_id, agent_cancel_returned=ret)
            await asyncio.wait_for(asyncio.shield(task), ATTEMPT_DEADLINE_S)
        except asyncio.TimeoutError:
            timed_out = True
            st.registry.cancel(att.attempt_id)
            task.cancel()
            st.emit("attempt_timed_out", attempt=att.attempt_id, deadline_s=ATTEMPT_DEADLINE_S)
        except asyncio.CancelledError:
            task.cancel()
            raise
        except BaseException as e:  # noqa: BLE001
            exc_text = repr(e)[:300]
        if task.done() and not task.cancelled() and task.exception() is not None and exc_text is None:
            exc_text = repr(task.exception())[:300]
        att.agent_run_id = trace.agent_run_id or att.agent_run_id
        ro = trace.run_output
        att.terminal = {
            "completed_event": trace.completed,
            "run_output_status": str(getattr(ro, "status", None)) if ro is not None else None,
            "run_error": trace.error or exc_text,
            "cancelled_event": trace.cancelled,
            "timed_out": timed_out,
            "stream_closed_early": (not trace.completed) and not trace.cancelled and not (trace.error or exc_text),
        }
        att.outcomes = outcome_rec.calls
        att.text = T._strip_leaked_tool_tags(trace.text or "")
        att.source = "workflow-real"
        rec.tokens = {"in": trace.tin, "out": trace.tout}
        if att.state == "RUNNING":
            att.state = "FINISHED"
    except BaseException as e:  # noqa: BLE001 -- setup failure: recorded as an Agent-level failure, never raised
        rec.error = repr(e)[:300]
        att.terminal = {"completed_event": False, "run_output_status": None, "run_error": f"setup: {rec.error}",
                        "cancelled_event": False, "timed_out": False, "stream_closed_early": False}
        att.outcomes = outcome_rec.calls
        att.text = ""
        att.source = "workflow-real"
        st.emit("attempt_setup_error", attempt=att.attempt_id, error=rec.error)
    finally:
        T._DELEGATION_READ_CEILING = old_ceiling
    return rec


def assess_and_register(st: RunState, wi: WorkItem, rec: AttemptRecord):
    """Acceptance authority = swarm.acceptance.decide (terminal state, validity, tool-outcome integrity,
    evidence policy, assessor restricted-only, registry). Workflow status is NOT consulted here."""
    att = rec.attempt
    att.workflow_status = "RunStatus.running"       # observed separately; never an input to acceptance
    try:
        d = A.decide(att, wi.policy, st.registry)
    except BaseException as e:  # noqa: BLE001 -- the decision pipeline itself failed: nothing is accepted
        rec.error = (rec.error or "") + f" decide-error:{repr(e)[:200]}"
        d = A.Decision(identity={"task_id": att.task_id, "work_item_id": att.work_item_id,
                                 "attempt_id": att.attempt_id, "generation": att.generation,
                                 "workflow_run_id": att.workflow_run_id, "agent_run_id": att.agent_run_id,
                                 "source": att.source},
                       workflow_status=None, execution_status="UNKNOWN", execution_reason="decision pipeline error",
                       counts=A.count_outcomes(att.outcomes), gate={"applicable": False, "satisfied": [], "missing": []},
                       acceptance=A.NOT_ACCEPTED, not_accepted_cause="DECISION_ERROR", reason=rec.error)
    rec.decision = d
    rec.summary = A.summarize(d)
    st.emit("attempt_text", attempt=att.attempt_id, chars=len(att.text or ""),
            sha=A.sha(att.text), head=repr((att.text or "")[:100]))
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
            lines.append("Tool outcomes for this attempt: " + ", ".join(f"{n} {k}" for k, n in al.items() if n) )
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
                lines.append("Reason: no attempt produced a decision.")
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


def _safe_final(st: RunState, problems: list) -> str:
    return ("## Investigation result: NOT ESTABLISHED\n"
            f"task `{st.task_id}` · workflow run `{st.workflow_run_id}`\n\n"
            "No findings are reported because the application could not establish a trustworthy final answer. "
            "Problems: " + "; ".join(problems[:5]) + "\n")


# ---------------------------------------------------------------------------- the Workflow
async def run_engineering_workflow(
    task: str, agent_specs, coordinator_model, coordinator_tools, mcp_url, mcp_urls,
    project_id: str, session_id, controls_raw=None, registry=None,
):
    """Entry point called from swarm.team.run_task_async when orchestration == "workflow".
    Returns (content, tokens, clarification) exactly like the Team path."""
    from agno.tools.mcp import MCPTools
    from agno.workflow import Workflow, Step
    import swarm.team as T
    from swarm import model_routing, team_config

    controls = _controls(controls_raw)
    task_id = f"task-{uuid.uuid4().hex[:10]}"
    wf_run_id = f"wf-{uuid.uuid4().hex[:8]}"
    st = RunState(task=task, task_id=task_id, workflow_run_id=wf_run_id, registry=registry or A.Registry())
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

    async with AsyncExitStack() as stack:
        mcp_list = []
        for url in all_mcp_urls:
            excl = T._PROJECT_MCP_EXCLUDE_TOOLS if url == effective_mcp_url else None
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
        ctx = {"specs": specs, "coordinator": coordinator_model or T.config.leader_model, "ctools": ctools,
               "mcp_list": mcp_list, "catalog": catalog, "project_id": project_id, "hive_url": hive_url,
               "profile": T.execution_context.resolve_profile(None)}

        # ---- steps (never raise; they record into `st`) --------------------------------------------------
        async def s_intake(step_input):
            st.steps_run.append("intake")
            st.emit("step", name="intake", task_chars=len(task))
            return "intake-ok"

        async def s_plan(step_input):
            st.steps_run.append("plan")
            st.plan = plan_request(task, task_id)
            st.emit("step", name="plan", items=len(st.plan.items), rejected=st.plan.rejected_reason)
            return "plan-ok"

        async def s_execute(step_input):
            st.steps_run.append("execute")
            for wi in (st.plan.items if st.plan else []):
                for n in range(1, MAX_ATTEMPTS_PER_ITEM + 1):
                    rec = await run_attempt(st, wi, ctx, controls, n)
                    d = assess_and_register(st, wi, rec)
                    retry = (d.execution_status in RETRYABLE_TERMINALS and d.validity == "CURRENT"
                             and n < MAX_ATTEMPTS_PER_ITEM)
                    st.emit("retry_decision", work_item=wi.work_item_id, attempt=rec.attempt.attempt_id,
                            agent=d.execution_status, retry=retry)
                    if not retry:
                        break
            return "execute-done"

        async def s_finalize(step_input):
            st.steps_run.append("finalize")
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
            st.emit("step", name="finalize", audit_ok=st.final_audit["ok"], chars=len(st.final))
            return "finalize-ok"

        wf = Workflow(name="engineering-workflow", steps=[
            Step(name="intake", executor=s_intake, max_retries=0),
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
        if st.final is None or st.steps_run != ["intake", "plan", "execute", "finalize"]:
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
    tin = sum(r.tokens["in"] for r in st.records)
    tout = sum(r.tokens["out"] for r in st.records)
    print("[workflow] decisions " + "; ".join(
        f"{r.attempt.attempt_id}:{r.decision.execution_status if r.decision else 'n/a'}->"
        f"{r.decision.acceptance if r.decision else 'n/a'}" for r in st.records), flush=True)
    return st.final, {"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout}, None
