"""AgnoHive FastAPI server — accepts task requests from remote clients over Tailscale."""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request

from api.models import (
    AgentSpec, RunRequest, RunResponse, PlanResponse, ScanRequest, ScanResponse,
    FeedbackRequest, FeedbackResponse, BranchRequest, ForkRequest,
    ModelCatalogEntry, ModelCatalogPatch, TeamRoleModelEntry, ModelRoutesReloadResponse,
    ClarificationRequest,
    TeamRoleToolEntry, TeamRoleSkillEntry, InstructionOverlayCreate, InstructionOverlayPatch,
    InstructionOverlayOut, TeamGateFlagEntry, RegistryRefreshRequest, TeamConfigReloadResponse,
)
from fastapi.responses import StreamingResponse
from swarm.ollama import ensure_models
from swarm.team import run_task_async, run_task_stream
from swarm.feedback import record_failure, record_success, drain_background_tasks
from swarm import db, model_routing, team_config
from config.config import config
from observability.setup import setup_telemetry
from swarm.sessions import (
    create_session, append_message, get_session,
    list_sessions as _list_sessions,
    delete_session as _delete_session,
    persist_session as _persist_session,
    compact_session, _cleanup_expired,
    get_context, list_session_tree, set_current_leaf, fork_session,
)

setup_telemetry()


def _resolve_mcp_urls(request_urls, lightrag_url: str) -> list[str]:
    """Always include LightRAG MCP so agents get lightrag_query without explicit opt-in."""
    urls = list(request_urls or [])
    if lightrag_url and lightrag_url not in urls:
        urls.append(lightrag_url)
    return urls or None

app = FastAPI(title="AgnoHive", version="1.0.0")

# Auto-instrument FastAPI — adds spans for every HTTP request
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
except ImportError:
    pass

_TEAMS_DIR = Path(__file__).parent.parent / "teams"


def _load_team(name: str) -> tuple[list[AgentSpec], str, str, list[str] | None]:
    path = _TEAMS_DIR / f"{name}.yaml"
    if not path.exists():
        available = [f.stem for f in _TEAMS_DIR.glob("*.yaml")]
        raise HTTPException(
            status_code=404,
            detail=f"Team '{name}' not found. Available: {available}",
        )
    data = yaml.safe_load(path.read_text())
    # AGNOHive 2.3.2 addendum (2026-08-08): a team YAML MAY omit an agent's model:
    # field and let model_routing's DB-backed default (team_role_models) fill it in
    # — the YAML value always wins when present. Resolved here, before AgentSpec
    # construction, so AgentSpec.model stays a required str for every other caller
    # (e.g. RunRequest.agents posted directly with no team/DB context to fall back
    # to). Raises the same "fail loudly" way a missing YAML value always has if
    # NEITHER the YAML nor the DB has a value for this role.
    agents = []
    for a in data["agents"]:
        a = dict(a)
        if not a.get("model"):
            default = model_routing.get_default_model(name, a["name"])
            if not default:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Team '{name}' agent '{a['name']}' has no model: in the YAML and "
                        f"no default in team_role_models — set one via /admin/model-routes."
                    ),
                )
            a["model"] = default
        # Declarative per-role policy (Recommendation #4, 2026-08-13, see DOCS.md
        # "Declarative Per-Role Policy") — identical precedence to model: above,
        # one field at a time: the YAML's own value always wins; a field the YAML
        # never set gets filled from the SAME team_role_models row model: already
        # consults, if that row sets it. A field neither the YAML nor the DB sets
        # stays None on the AgentSpec, and make_agent_from_spec() falls back to
        # config.py's global default — unchanged behavior for every role that
        # never opts in.
        policy = model_routing.get_role_policy(name, a["name"])
        if policy is not None:
            for field in ("temperature", "max_tokens", "tool_call_limit"):
                if a.get(field) is None:
                    db_value = getattr(policy, field)
                    if db_value is not None:
                        a[field] = db_value
        # AGNOHive 2.3.3 (2026-08-18) -- Tier 1 (tools/skills): same override-with-
        # DB-fallback precedence as model:/policy above (2026-08-18 follow-up --
        # shipped first as an additive union that kept YAML as the primary list;
        # changed to replace-or-fallback per explicit follow-up request to make
        # the DB the actual runtime source, not a YAML-plus-extras layer). A
        # role's own tools:/skills: in the YAML, when present, always wins
        # outright -- the same "pin it back here to take it out of DB control"
        # escape hatch model: already has (all 4 shipped teams/*.yaml have had
        # their tools:/skills: fields removed, since team_role_tools/
        # team_role_skills was already fully seeded from those exact values).
        # When the YAML omits the field, the DB supplies the full list; if the
        # DB has no rows either, the field stays None/absent, meaning
        # "unrestricted, sees every connected tool" (make_agent_from_spec's own
        # `if spec.tools:` truthy check) -- unchanged for a role that was never
        # migrated into the DB (e.g. engineering's coordinator, below).
        #
        # `is None`, NOT falsiness (fixed 2026-08-21): an explicitly empty
        # `tools: []` is a deliberate disarm and must WIN, exactly like a
        # non-empty list does. Under the old truthy check it was
        # indistinguishable from an omitted field, so the DB silently overrode
        # it and re-armed the role -- the same empty-vs-absent conflation
        # _scope_coordinator_tools() had to grow an early return for.
        if a.get("tools") is None:
            db_tools = team_config.get_extra_tools(name, a["name"])
            if db_tools:
                a["tools"] = db_tools
        if a.get("skills") is None:
            db_skills = team_config.get_extra_skills(name, a["name"])
            if db_skills:
                a["skills"] = db_skills
        # Tier 2 (additive-only supplementary instructions): appended AFTER the
        # role's base instructions (YAML/hardcoded, untouched), under a clearly-
        # marked header -- never merged into or replacing the base list. Empty
        # when no active overlay rows exist for this (team, role), so this is a
        # no-op for every role until an admin explicitly adds one.
        overlays = team_config.get_instruction_overlays(name, a["name"])
        if overlays:
            a["instructions"] = list(a.get("instructions") or []) + [team_config.OVERLAY_HEADER] + overlays
        agents.append(AgentSpec(**a))
    coordinator = data.get("coordinator_model") or model_routing.get_default_model(name, "Coordinator") or config.leader_model
    mode = data.get("mode", "coordinate")
    # Optional read-only allowlist scoping the coordinator's direct MCP tool surface
    # (mirrors per-agent `tools:` scoping). None means "no scoping — full surface" —
    # the existing behavior, preserved for teams like `engineering` that need write access.
    coordinator_tools = data.get("coordinator_tools")
    # Same override-with-DB-fallback rule as per-agent tools above, applied to
    # the coordinator's own allowlist -- a team with NO coordinator_tools: is
    # left alone; a team whose YAML omitted coordinator_tools: but has DB rows
    # (sprint-master/planning/parallel-review, all migrated 2026-08-18) gets
    # its allowlist from the DB instead.
    #
    # `is None`, NOT falsiness (fixed 2026-08-21) -- and this one was load-
    # bearing, not hypothetical: engineering.yaml's `coordinator_tools: []`
    # (2026-08-20, the fix that stopped the coordinator answering db_schema
    # questions itself instead of delegating) is falsy, so the old check fell
    # through to the DB lookup. It survived only because no (engineering,
    # Coordinator) rows happen to exist -- one admin POST or a re-seed would
    # have silently re-armed the coordinator with no error anywhere.
    if coordinator_tools is None:
        db_coordinator_tools = team_config.get_extra_tools(name, "Coordinator")
        if db_coordinator_tools:
            coordinator_tools = db_coordinator_tools
    return agents, coordinator, mode, coordinator_tools


# EK-88 router-of-teams: the child teams the "router" virtual team delegates to, each with the
# description the router leader reads to pick exactly one.
ROUTABLE_TEAMS = {
    "engineering":   "Choose for (1) any task that WRITES or CHANGES code — implement a feature, edit/create files, fix a bug, refactor, run tests; OR (2) a read-only task whose answer must be GROUNDED in the actual codebase — a verification/groundedness probe, 'what does file X contain', 'confirm how Y works'. This is the only team that reliably opens and quotes real files, so pick it whenever the answer must be accurate to the code, even if the task is read-only.",
    "parallel-review": "Choose for read-only multi-perspective REVIEW of existing code: code review before merge, security audit (auth / secrets / OWASP), or performance review (N+1 queries, missing indexes). Use for 'review / audit / critique this code'. Do NOT use it to verify specific facts about the code — that is engineering.",
    "sprint-master": "Choose for PLANNING and DESIGN: produce an implementation plan, break a feature into sub-tasks, read a spec / Notion page and a codebase to plan (no code is written), or do delivery-board (epic / feature / task / bug) CRUD. ANY task that says 'plan', 'design', 'break into sub-tasks', or 'planning only' belongs here.",
    "planning":      "Choose ONLY for quick read-only conceptual Q&A or reasoning that needs NO codebase or spec grounding (it does not reliably read files). If the answer must be grounded in real code, pick engineering instead.",
}


async def _route_to_team(task: str, choices: dict, model: str, ollama_host: str) -> str:
    """Classifier-then-dispatch routing (EK-88). One cheap LLM call picks the single best team name
    for `task`; the caller then runs that team via the normal path. This deliberately avoids agno
    route-mode nested delegation — delegate_task_to_member is unreliable over ollama (intermittently
    emitted as text). Returns a key of `choices`; falls back to the first key on any failure or
    unrecognised answer."""
    import httpx
    menu = "\n".join(f"- {name}: {desc}" for name, desc in choices.items())
    prompt = (
        "You are a task router. Pick the SINGLE best team for the task below. "
        "Reply with ONLY the team name (exactly one of the listed names) and nothing else.\n\n"
        f"Teams:\n{menu}\n\nTask:\n{task[:1500]}\n\nTeam name:"
    )
    host = ollama_host or "http://localhost:11434"
    answer = ""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if config.inference_backend == "vllm":
                # llama-swap OpenAI gateway (':' -> '-' served name)
                resp = await client.post(
                    f"{config.vllm_gateway_url}/chat/completions",
                    json={"model": model.replace(":", "-"),
                          "messages": [{"role": "user", "content": prompt}],
                          "stream": False, "temperature": 0, "max_tokens": 16},
                )
                resp.raise_for_status()
                answer = (resp.json()["choices"][0]["message"]["content"] or "").strip().lower()
            else:
                resp = await client.post(
                    f"{host}/api/generate",
                    json={"model": model, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0, "num_predict": 16}},
                )
                resp.raise_for_status()
                answer = (resp.json().get("response") or "").strip().lower()
    except Exception as e:
        print(f"[router] classify failed ({e}); falling back to first team")
    for name in choices:
        if name.lower() in answer:
            return name
    return next(iter(choices))  # fallback: first team (engineering)


@app.on_event("startup")
async def _load_model_routing_cache():
    # Guarantees the cache is populated before ANY request handler runs (Uvicorn
    # doesn't route requests until startup events complete) — in particular before
    # _load_team() below can consult model_routing.get_default_model() for a team
    # YAML that omits a role's model: field.
    await db.ensure_schema()
    await model_routing.ensure_cache_loaded()
    await team_config.ensure_cache_loaded()
    # Non-blocking diagnostic (2026-08-16) — never delays or fails startup, see
    # check_coordinator_readiness()'s own docstring for the gap this closes.
    warning = await model_routing.check_coordinator_readiness()
    if warning:
        print(f"[readiness] WARNING: {warning}")
    # Same contract, for team config (2026-08-21). Answers "is this deployment
    # actually configured, or is it silently fail-open?" at the one moment
    # someone is watching the log — see check_config_health()'s docstring for
    # why it reports broken states rather than seed drift.
    for finding in team_config.check_config_health():
        print(f"[readiness] WARNING: {finding}")


@app.on_event("startup")
async def _start_cleanup_loop():
    asyncio.create_task(_session_cleanup_loop())


@app.on_event("shutdown")
async def _drain_feedback_tasks():
    # Await any in-flight fire-and-forget experience-indexing tasks so a graceful
    # shutdown doesn't drop an outcome that returned just before exit.
    await drain_background_tasks()


async def _session_cleanup_loop():
    while True:
        await asyncio.sleep(config.session_cleanup_interval)
        count = await _cleanup_expired()
        if count:
            print(f"[sessions] cleaned up {count} expired session(s)")


@app.get("/health")
async def health():
    return {"status": "ok", "mcp_url": config.mcp_url}


@app.get("/teams")
async def list_teams():
    teams = []
    for f in sorted(_TEAMS_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        teams.append({
            "name": data["name"],
            "description": data.get("description", ""),
            "agents": [a["name"] for a in data["agents"]],
        })
    return {"teams": teams}


_WORKER_POLL_S = 2.0
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _liveness_kill_reason(snapshot: dict) -> str | None:
    """Given a liveness snapshot dict (written each tick by swarm.team._run_heartbeat,
    read from disk by the worker-subprocess poll loops below), return a human-readable
    reason to kill the run, or None if it's still healthy. Pure function, no I/O -- the
    decision logic is testable independent of file-reading/subprocess mechanics. See
    DOCS.md "Liveness-Based Auto-Kill" for the full design and why these signals
    (not a single generic timeout) were chosen. Three tiers: silence (Tier 1,
    backstop), a single key's repeated-identical-call count (Tier 2), and the SUM
    of stub serves across every key in the run (Tier 3, 2026-08-14 -- catches a
    model that rotates its non-convergence across several different files instead
    of hammering one, which can keep every individual Tier-2 count just under
    threshold; see swarm/team.py's _make_read_cache_tool_hook docstring for the
    live incident)."""
    stagnant = snapshot.get("stagnant_seconds", 0)
    if stagnant > config.liveness_silence_threshold_s:
        return f"no tool call or new stream content for over {config.liveness_silence_threshold_s:.0f}s"
    stub_count = snapshot.get("max_stub_serve_count", 0)
    if stub_count > config.liveness_stub_serve_threshold:
        return f"repeated an identical call {stub_count} times despite being told to stop"
    total_stub_count = snapshot.get("total_stub_serve_count", 0)
    if total_stub_count > config.liveness_aggregate_stub_threshold:
        return (
            f"served {total_stub_count} duplicate-read stubs across the run despite "
            f"repeated stop instructions"
        )
    # Tier 4 (2026-08-26): the model is WORKING but nothing it asks for is running.
    #
    # Distinct from Tier 1, which waits for silence. Here the stream is not silent at
    # all -- model requests keep completing, ~1.2s apart -- while zero tools execute,
    # because agno refused them all once tool_call_limit was hit. agno emits no event
    # for a refused call (see swarm/team.py's _tools_refused_for_limit), so from the
    # outside this looks like healthy activity, and Tier 1 only fires after the full
    # silence threshold has elapsed on a run that will never recover.
    #
    # Live case (T2, 13:52:38 -> 13:58:20): the Researcher spent its 50-call budget in
    # 75s, then looped for 5m42s emitting the same refused call before Tier 1 killed
    # it. The 25,000 characters of real findings it had already gathered were
    # discarded and the caller got a bare 504. Catching it on this signature ends the
    # run while that draft is still returnable.
    #
    # Requires THREE conditions, not two (corrected 2026-08-27). The original pair --
    # requests advancing AND no tool executing -- rested on the assumption written into
    # the old comment here, that "a long single generation moves neither counter". It
    # moves the first one: content chunks ARE stream events, so a model writing a long
    # final answer looks identical to one emitting refused calls on both axes this was
    # checking.
    #
    # Measured on the chained T12 leg: 100 seconds of pure generation, 6,303 chars of a
    # correct 10-router answer, and the run was killed with "tool budget is spent and
    # every further call is being refused" while sitting at 10 of 50 calls. Nothing was
    # refused. At ~100 chars/sec that condemned any final answer over roughly 9,000
    # characters -- which is most thorough ones, and explains truncations across several
    # runs that were being attributed to budget exhaustion.
    #
    # `stagnant_seconds` is the discriminator, and it was already in the snapshot:
    # swarm/team.py's heartbeat derives it from activity["last_progress_at"], which
    # advances on REAL content as well as tool events. A generating model keeps it near
    # zero; the refused-call loop this tier exists for produces contentless turns and
    # lets it climb. Tier 1 already relies on exactly that signal -- Tier 4 ignored it.
    #
    # So Tier 4 is now Tier 1's signal at a third of the wait, gated on the extra
    # evidence that tools are being refused rather than merely unneeded.
    # Tier 5 (2026-08-27): the repetition detector has fired repeatedly.
    #
    # _looks_like_repetition_loop has existed and worked for a while -- 217 firings
    # across the retained journal. Its only response was to withhold progress credit,
    # which reaches Tier 1 solely when repetition is CONTINUOUS: one genuine segment in
    # between refreshes last_progress_at and resets the stall clock entirely.
    #
    # Measured: a run emitted the SAME "Researcher's Final Report" block seven times
    # over 736 seconds. The detector fired 21 times and stopped nothing, because real
    # new text landed between the duplicates. Detection was never the gap.
    #
    # Counting, like the stub-serve tiers, is what closes it. 6 is past any plausible
    # incidental echo (a templated document repeating section headers trips this once
    # or twice, per _REPETITION_PREFIX_CHARS' own calibration notes) and well short of
    # the 21 that run reached -- it would have stopped it around the second duplicate
    # block instead of the seventh.
    repeats = snapshot.get("repetition_count", 0)
    if repeats >= config.liveness_repetition_threshold:
        return (
            f"the same content has been regenerated {repeats} times — the model is "
            f"looping rather than making progress"
        )
    no_tool = snapshot.get("no_tool_progress_seconds", 0)
    if (no_tool > config.liveness_refused_call_threshold_s
            and stagnant > config.liveness_refused_call_threshold_s
            and snapshot.get("requests_advancing")):
        return (
            f"model kept issuing calls for {no_tool:.0f}s but none executed, and "
            f"produced no content in that time — tool budget is spent and every "
            f"further call is being refused"
        )
    return None


def _read_liveness_snapshot(path: "Path") -> dict | None:
    """Best-effort read of a liveness snapshot -- missing file (run hasn't reached its
    first heartbeat tick yet, or liveness is disabled for this build), a torn/partial
    write, or any other OSError/JSONDecodeError all just mean "nothing to act on yet,"
    never a crash. The write side is already atomic (temp file + os.replace in
    _run_heartbeat), so a torn read should not happen in practice; this stays
    defensive anyway, matching every other piece of this mechanism's own posture."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# Active swarm worker subprocesses, pid -> metadata. Populated on spawn, removed in
# the same finally that cleans up the liveness file.
#
# Exists because there was NO way to stop a running swarm from outside (2026-08-23).
# The disconnect poller below is correct and fires on a real client disconnect -- but
# stopping the CALLER does not necessarily produce one: an agno_run issued through the
# project MCP leaves ekamapp-mcp-server's own HTTP request to this service open, so
# is_disconnected() stays False and the worker runs on. Observed live: after the client
# task was stopped, heartbeats kept climbing (960s, 990s, 1020s, 1050s) with the GPU
# pinned at 96% and 87 degrees, and only a full service restart freed it.
#
# This is a direct control path, deliberately NOT a second disconnect poller -- an
# uncoordinated second poller previously corrupted an anyio cancel scope, and that
# lesson stands.
_ACTIVE_WORKERS: dict[int, dict] = {}


@app.get("/runs")
async def list_active_runs():
    """Swarm workers running right now, newest first."""
    now = time.time()
    return {"runs": [
        {"pid": pid, "elapsed_s": round(now - m["started_at"], 1),
         "team": m.get("team"), "task": m.get("task")}
        for pid, m in sorted(_ACTIVE_WORKERS.items(),
                             key=lambda kv: kv[1]["started_at"], reverse=True)
    ]}


@app.post("/runs/{pid}/cancel")
async def cancel_run(pid: int):
    """Kill one running swarm worker. Frees the GPU immediately."""
    meta = _ACTIVE_WORKERS.get(pid)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"no active run with pid {pid}")
    proc = meta.get("proc")
    if proc is not None and proc.returncode is None:
        proc.kill()
        print(f"[api] run {pid} cancelled by request — worker killed, GPU work aborted",
              flush=True)
    return {"cancelled": pid, "elapsed_s": round(time.time() - meta["started_at"], 1)}


async def _run_worker_subprocess(
    http_request, payload: dict, argv: list[str] | None = None
) -> tuple[str, dict, dict | None]:
    """Runs run_task_async() in an isolated child process (`python main.py
    --run-worker`, see main.py's _run_worker()) instead of in-process. See
    DOCS.md "Process-Boundary Cancellation" for the full design.

    Why: four rounds of cooperative cancellation this codebase used to have --
    agno's own acancel_run/araise_if_cancelled, generic asyncio task.cancel(), a
    shared claimed-flag between two independent checkers, and tracking every
    observed run_id instead of just the first -- each closed one specific way
    cancellation could land wrong inside agno + MCP + anyio's nested async call
    graph, and each was followed by a NEW way it still didn't (most recently:
    cancelling several run_ids from the same event in rapid succession still
    corrupted anyio's cancel-scope bookkeeping when their generators shared
    overlapping async context managers). That pattern -- a new interaction
    breaking after every fix -- was itself the signal: cooperative cancellation
    across a framework we don't control had run out of ROI. This sidesteps the
    whole bug class rather than continuing to look for the next fix: on
    disconnect the child process is SIGKILLed outright. The OS reclaims every
    open socket and every anyio scope unconditionally when a process dies -- no
    cooperation from agno's internals required, because none of that cleanup
    code needs to run at all for the kernel to free the resources. Validated
    live 2026-08-12: three deliberate mid-flight kills (two /run, one /stream,
    one specifically mid-write), zero anyio errors in any of them -- the first
    clean kills this whole investigation produced. The old cooperative path
    (DisconnectSignal, _run_cancel_on_disconnect, _make_disconnect_checker) was
    removed the same day once both endpoints had this replacement validated;
    see DOCS.md "Process-Boundary Cancellation" for the full incident history
    and validation record.

    `payload` is the exact kwargs run_task_async() would take, JSON-encoded --
    see main.py's _run_worker() for the receiving side. `argv` defaults to the
    real worker command; tests override it to spawn a small fixture script
    instead, so this can be exercised without the real agno/MCP/vLLM stack.

    Liveness-based auto-kill (Recommendation #2, 2026-08-13, see DOCS.md
    "Liveness-Based Auto-Kill"): when config.enable_liveness_autokill is set, this
    same poll loop ALSO reads a small JSON snapshot swarm/team.py's heartbeat
    writes each tick to a path keyed by this child's own pid, and kills the
    process the identical way a disconnect does -- same actuator, second trigger
    condition -- if _liveness_kill_reason judges it stale. Distinguished from a
    disconnect via a 504 (vs. 499) so a caller can tell "gave up because it looked
    stuck" apart from "you left."
    """
    argv = argv or [sys.executable, "main.py", "--run-worker"]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # inherit -- child's stderr (every [team]/[api] print, unchanged)
                      # flows straight into this process's own stdout/journald
        cwd=str(_REPO_ROOT),
    )
    _ACTIVE_WORKERS[proc.pid] = {
        "proc": proc,
        "started_at": time.time(),
        "team": payload.get("team"),
        "task": (payload.get("task") or "")[:120],
    }
    liveness_path = Path(tempfile.gettempdir()) / f"agnohive-liveness-{proc.pid}.json"
    worker_payload = {**payload, "liveness_path": str(liveness_path)}
    proc.stdin.write(json.dumps(worker_payload).encode())
    proc.stdin.write_eof()
    await proc.stdin.drain()

    task = asyncio.ensure_future(proc.communicate())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_WORKER_POLL_S)
            if done:
                stdout_data, _ = task.result()
                break
            if await http_request.is_disconnected():
                proc.kill()
                await proc.wait()
                print("[api] client disconnected — worker process killed, GPU work aborted")
                raise HTTPException(status_code=499, detail="client disconnected")
            if config.enable_liveness_autokill:
                snapshot = _read_liveness_snapshot(liveness_path)
                if snapshot is not None:
                    reason = _liveness_kill_reason(snapshot)
                    if reason is not None:
                        proc.kill()
                        await proc.wait()
                        print(f"[api] run auto-terminated (liveness): {reason}")
                        # Hand back whatever the run actually produced (2026-08-24).
                        # The kill is right; throwing away a finished answer to
                        # report it is not. T13 lost 10,594 characters of a correct
                        # vouchers audit this way -- the main pass was fine, a
                        # guard-triggered retry stalled, and the process-level kill
                        # discarded a draft no guard had objected to.
                        #
                        # The worker writes its longest draft into this same snapshot
                        # each heartbeat (see _run_heartbeat), so it survives SIGKILL
                        # by virtue of already being on disk. Up to one heartbeat
                        # stale, which beats a bare 504 every time.
                        draft = (snapshot.get("draft") or "").strip()
                        if draft:
                            print(f"[api] returning the {len(draft):,}-char draft the "
                                  f"run had already produced")
                            # Token counts are unavailable -- they are assembled by the
                            # worker at normal completion and this run never got there.
                            # Zeros, not estimates: a fabricated count would be worse
                            # than an obviously-absent one.
                            return (
                                f"{draft}\n\n---\n**RUN STOPPED EARLY — {reason}. The "
                                f"answer above is what had been produced when the run "
                                f"was stopped, and may be incomplete or unreviewed. "
                                f"Nothing after this point was generated.**",
                                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                                None,
                            )
                        raise HTTPException(status_code=504, detail=f"run auto-terminated: {reason}")
    except asyncio.CancelledError:
        proc.kill()
        raise
    finally:
        _ACTIVE_WORKERS.pop(proc.pid, None)
        try:
            liveness_path.unlink(missing_ok=True)
        except OSError:
            pass

    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"worker process exited with code {proc.returncode}",
        )

    try:
        result = json.loads(stdout_data.decode())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500, detail=f"worker process produced unparseable output: {exc}"
        )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result["content"], result["tokens"], result["clarification"]


async def _stream_worker_subprocess(http_request, payload: dict, argv: list[str] | None = None):
    """Streaming counterpart to _run_worker_subprocess (Phase 3 of process-
    boundary cancellation, see DOCS.md "Process-Boundary Cancellation") --
    spawns `python main.py --stream-worker` (main.py's _run_stream_worker())
    and yields each chunk as it arrives over NDJSON on stdout, instead of
    waiting for one final result the way /run does. Uses the same
    SIGKILL-on-disconnect approach Phase 1/2 already validated live for /run
    (three clean kills, zero anyio errors, including one mid-write) — see
    DOCS.md for the full validation record.

    Each NDJSON line is {"ok": true, "v": <chunk>} (yielded as-is -- exactly
    what run_task_stream() itself would have yielded, str or dict, since
    json round-trips both without an explicit type tag) or {"ok": false,
    "error": "..."} (raised as a RuntimeError, letting it propagate into
    /stream's existing `except Exception` SSE-error handling unchanged).

    Explicit is_disconnected() polling, not left to Starlette's own
    StreamingResponse machinery alone: if Starlette merely stops calling
    this generator's __anext__() on disconnect rather than actually
    cancelling it, an abandoned subprocess would only get cleaned up
    whenever Python's GC eventually collects the generator and sends it
    GeneratorExit -- unbounded, and exactly the "orphaned worker keeps
    running" failure this whole design exists to close. Polling explicitly
    means the kill happens within one poll interval regardless of what
    Starlette does with the generator itself; the `finally` block below is
    the belt-and-suspenders backstop for the GeneratorExit path too.
    """
    argv = argv or [sys.executable, "main.py", "--stream-worker"]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # inherit -- same rationale as _run_worker_subprocess
        cwd=str(_REPO_ROOT),
    )
    proc.stdin.write(json.dumps(payload).encode())
    proc.stdin.write_eof()
    await proc.stdin.drain()

    try:
        while True:
            readline_task = asyncio.ensure_future(proc.stdout.readline())
            raw_line = None
            while True:
                done, _ = await asyncio.wait({readline_task}, timeout=_WORKER_POLL_S)
                if done:
                    raw_line = readline_task.result()
                    break
                if await http_request.is_disconnected():
                    readline_task.cancel()
                    proc.kill()
                    await proc.wait()
                    return
            if not raw_line:
                break  # EOF -- worker process finished
            line = json.loads(raw_line.decode())
            if not line["ok"]:
                raise RuntimeError(line["error"])
            yield line["v"]
    except asyncio.CancelledError:
        proc.kill()
        raise
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@app.post("/run", response_model=RunResponse)
async def run(request: RunRequest, http_request: Request):
    from api.models import SessionMeta
    start = time.perf_counter()

    # Resolve team spec
    # gate_team_name (2026-08-15 gate-scope extension, see swarm/team.py's
    # _GATE_ENABLED_TEAMS) is the underlying team identity _build_team needs to
    # decide gate policy -- distinct from team_name below, which is the DISPLAY
    # string returned in RunResponse.team (e.g. "router:engineering", not
    # "engineering"). request.agents (raw AgentSpecs posted directly, no team
    # YAML) gets gate_team_name=None -- an arbitrary roster has no guaranteed
    # "Researcher"-shaped member, so the gates correctly stay off (see
    # _GATE_ENABLED_TEAMS' fail-safe-off default for any unrecognized name).
    if request.agents:
        agent_specs = request.agents
        coordinator_model = config.leader_model
        team_mode = request.mode or "coordinate"
        team_name = request.team or "custom"
        coordinator_tools = None
        gate_team_name = None
    elif request.team == "router":
        # EK-88 classifier-then-dispatch: one LLM call picks the team, then run it normally.
        chosen = await _route_to_team(request.task, ROUTABLE_TEAMS, config.router_classifier_model, config.ollama_host)
        agent_specs, coordinator_model, team_mode, coordinator_tools = _load_team(chosen)
        team_mode = request.mode or team_mode
        team_name = f"router:{chosen}"
        gate_team_name = chosen
    elif request.team:
        agent_specs, coordinator_model, team_mode, coordinator_tools = _load_team(request.team)
        team_mode = request.mode or team_mode  # request-level override wins
        team_name = request.team
        gate_team_name = request.team
    else:
        agent_specs, coordinator_model, team_mode, coordinator_tools = _load_team("engineering")
        team_mode = request.mode or team_mode
        team_name = "engineering"
        gate_team_name = "engineering"

    all_models = list({coordinator_model} | {a.model for a in agent_specs})
    models_pulled = await ensure_models(all_models, config.ollama_host)

    mcp_url = request.mcp_url or config.mcp_url

    # Resolve session — create new one if none provided
    session_id = request.session_id
    if not session_id:
        session_id = await create_session(
            project_id=request.project_id,
            title=request.task,
            persist=request.persist,
        )
    elif request.persist:
        await _persist_session(session_id)

    # Capture context size before run (for footer metadata)
    session_summary, prior_messages = await get_context(session_id)
    session_before = await get_session(session_id)
    has_summary = bool(session_before and session_before.get("summary"))
    is_chain_handoff = session_summary.startswith("── Chain handoff")
    # When a chain-handoff digest is active, team.py skips injecting the message
    # history — report 0 injected context rather than the (unenforced) message count.
    context_size = 0 if is_chain_handoff else len(prior_messages)

    resolved_mcp_urls = _resolve_mcp_urls(request.mcp_urls, config.lightrag_mcp_url)

    # Process-boundary cancellation (see DOCS.md "Process-Boundary Cancellation") --
    # runs in an isolated child process, SIGKILLed outright on disconnect. Replaced
    # the original in-process cooperative-cancellation path (DisconnectSignal,
    # _run_cancel_on_disconnect) on 2026-08-12 once validated live: three clean
    # kills, zero anyio errors, including one mid-write.
    worker_payload = {
        "task": request.task,
        "agent_specs": [a.model_dump() for a in agent_specs] if agent_specs else None,
        "coordinator_model": coordinator_model,
        "coordinator_tools": coordinator_tools,
        "mcp_url": mcp_url,
        "mcp_urls": resolved_mcp_urls,
        "project_id": request.project_id,
        "session_id": session_id,
        "mode": team_mode,
        "read_only": request.read_only,
        "team_name": gate_team_name,
    }
    result, tokens, clarification = await _run_worker_subprocess(http_request, worker_payload)

    # Append this turn to session
    await append_message(session_id, "user", request.task)
    await append_message(session_id, "assistant", result)

    # Refresh session metadata for response
    session_after = await get_session(session_id)
    message_count = session_after["message_count"] if session_after else 0
    turn = message_count // 2

    # Trigger compaction async if threshold crossed (fire-and-forget)
    if message_count >= config.compact_threshold:
        asyncio.create_task(compact_session(session_id))

    session_meta = SessionMeta(
        session_id=session_id,
        turn=turn,
        context_size=context_size,
        compacted=has_summary,
        persist=session_after["persist"] if session_after else request.persist,
        expires_at=(
            session_after["expires_at"].isoformat()
            if session_after and session_after.get("expires_at")
            else None
        ),
    )

    return RunResponse(
        result=result,
        team=team_name,
        agents_used=[a.name for a in agent_specs],
        models_pulled=models_pulled,
        duration_seconds=round(time.perf_counter() - start, 2),
        session=session_meta,
        input_tokens=tokens.get("input_tokens", 0),
        output_tokens=tokens.get("output_tokens", 0),
        total_tokens=tokens.get("total_tokens", 0),
        needs_clarification=ClarificationRequest(**clarification) if clarification else None,
    )


@app.post("/plan", response_model=PlanResponse)
async def plan(request: RunRequest):
    """Run the planning team only — returns a step-by-step plan without executing.
    Used by the hive CLI --review flag for human-in-the-loop approval before execution.
    """
    start = time.perf_counter()
    agent_specs, coordinator_model, _, coordinator_tools = _load_team("planning")
    mcp_url = request.mcp_url or config.mcp_url

    all_models = list({coordinator_model} | {a.model for a in agent_specs})
    await ensure_models(all_models, config.ollama_host)

    plan_text, _, clarification = await run_task_async(
        task=request.task,
        agent_specs=agent_specs,
        coordinator_model=coordinator_model,
        coordinator_tools=coordinator_tools,
        mcp_url=mcp_url,
        mcp_urls=_resolve_mcp_urls(request.mcp_urls, config.lightrag_mcp_url),
        project_id=request.project_id,
        # Explicit, not the team_name=None default -- /plan always runs the
        # "planning" team, which the 2026-08-15 gate-scope extension explicitly
        # excludes (its own Researcher agent must stay ungated; see
        # swarm/team.py's _GATE_ENABLED_TEAMS). Passing team_name=None here would
        # silently leave the OLD unconditional-gate leak in place for this one
        # remaining call site.
        team_name="planning",
    )
    return PlanResponse(
        plan=plan_text,
        duration_seconds=round(time.perf_counter() - start, 2),
        needs_clarification=ClarificationRequest(**clarification) if clarification else None,
    )


def _tool_event_to_sse(chunk: dict) -> str | None:
    """Turn a run_task_stream tool-event dict (see swarm.team._stream_event_to_chunk)
    into an SSE data line, or None if `chunk` isn't a recognized tool-event shape."""
    kind = chunk.get("__tool_event__")
    if kind == "start":
        payload = {"type": "tool_start", "name": chunk["name"], "args": chunk["args"]}
    elif kind == "end":
        payload = {"type": "tool_end", "name": chunk["name"], "result_preview": chunk["result_preview"]}
    else:
        return None
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/stream")
async def stream_endpoint(request: RunRequest, http_request: Request):
    """Stream coordinator output as Server-Sent Events.

    Events:
      data: {"type": "chunk",      "content": "<text>"}
      data: {"type": "tool_start", "name": "<tool>", "args": {...}}
      data: {"type": "tool_end",   "name": "<tool>", "result_preview": "<text>" | null}
      data: {"type": "done",       "session": {...}, "input_tokens": N, ...,
             "needs_clarification": {"question": str, "options": [...]} | absent}
      data: {"type": "error",      "content": "<message>"}

    Keeps the HTTP connection alive the whole time — no 300s timeout risk.
    """
    import json as _json

    # gate_team_name -- see /run's identical comment above (2026-08-15 gate-scope
    # extension). /stream has no router branch, so this mirrors /run's other 3.
    if request.agents:
        agent_specs = request.agents
        coordinator_model = config.leader_model
        stream_mode = request.mode or "coordinate"
        coordinator_tools = None
        gate_team_name = None
    elif request.team:
        agent_specs, coordinator_model, stream_mode, coordinator_tools = _load_team(request.team)
        stream_mode = request.mode or stream_mode
        gate_team_name = request.team
    else:
        agent_specs, coordinator_model, stream_mode, coordinator_tools = _load_team("engineering")
        stream_mode = request.mode or stream_mode
        gate_team_name = "engineering"

    mcp_url = request.mcp_url or config.mcp_url

    session_id = request.session_id
    if not session_id:
        session_id = await create_session(
            project_id=request.project_id,
            title=request.task,
            persist=request.persist,
        )
    elif request.persist:
        await _persist_session(session_id)

    session_summary, prior_messages = await get_context(session_id)
    session_before = await get_session(session_id)
    has_summary = bool(session_before and session_before.get("summary"))
    is_chain_handoff = session_summary.startswith("── Chain handoff")
    context_size = 0 if is_chain_handoff else len(prior_messages)

    resolved_mcp_urls = _resolve_mcp_urls(request.mcp_urls, config.lightrag_mcp_url)

    async def generate():
        try:
            # Process-boundary cancellation (see DOCS.md "Process-Boundary
            # Cancellation") -- same SIGKILL-on-disconnect mechanism validated live
            # for /run, extended to /stream's incremental delivery via
            # _stream_worker_subprocess's NDJSON-over-stdout protocol. Replaced the
            # original in-process cooperative path (DisconnectSignal + plain
            # run_task_stream(is_disconnected=...)) on 2026-08-12 once both endpoints
            # had a validated replacement.
            stream_source = _stream_worker_subprocess(http_request, {
                "task": request.task,
                "agent_specs": [a.model_dump() for a in agent_specs] if agent_specs else None,
                "coordinator_model": coordinator_model,
                "coordinator_tools": coordinator_tools,
                "mcp_url": mcp_url,
                "mcp_urls": resolved_mcp_urls,
                "project_id": request.project_id,
                "session_id": session_id,
                "mode": stream_mode,
                "read_only": request.read_only,
                "team_name": gate_team_name,
            })

            async for chunk in stream_source:
                if isinstance(chunk, str):
                    yield f"data: {_json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

                elif isinstance(chunk, dict) and "__tool_event__" in chunk:
                    sse_line = _tool_event_to_sse(chunk)
                    if sse_line:
                        yield sse_line

                elif isinstance(chunk, dict) and chunk.get("__done__"):
                    await append_message(session_id, "user", request.task)
                    await append_message(session_id, "assistant", chunk["content"])

                    session_after = await get_session(session_id)
                    msg_count = session_after["message_count"] if session_after else 0
                    if msg_count >= config.compact_threshold:
                        asyncio.create_task(compact_session(session_id))

                    tokens = chunk.get("tokens", {})
                    session_meta = {
                        "session_id": session_id,
                        "turn": msg_count // 2,
                        "context_size": context_size,
                        "compacted": has_summary,
                        "persist": session_after["persist"] if session_after else request.persist,
                        "expires_at": (
                            session_after["expires_at"].isoformat()
                            if session_after and session_after.get("expires_at")
                            else None
                        ),
                    }
                    done_event = {
                        "type": "done",
                        "session": session_meta,
                        "input_tokens": tokens.get("input_tokens", 0),
                        "output_tokens": tokens.get("output_tokens", 0),
                        "total_tokens": tokens.get("total_tokens", 0),
                    }
                    clarification = chunk.get("clarification")
                    if clarification:
                        done_event["needs_clarification"] = clarification
                    yield f"data: {_json.dumps(done_event)}\n\n"

        except Exception as exc:
            yield f"data: {_json.dumps({'type': 'error', 'content': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/scan", response_model=ScanResponse)
async def scan(request: ScanRequest):
    """Call scan_project_context on hive-mcp directly — no coordinator model, no team overhead.

    Uses a raw ClientSession (same pattern as bootstrap) so the scan can take as
    long as it needs without hitting the MCPTools 60s cap. Timeout: 240s.
    """
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    start = time.perf_counter()

    def _extract(result) -> str:
        if not result or not result.content:
            return "(no output)"
        return "\n".join(
            item.text for item in result.content
            if hasattr(item, "text") and item.text
        ) or "(no output)"

    async def _do_scan() -> str:
        async with streamablehttp_client(request.mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "scan_project_context",
                    {"force": request.force},
                )
                return _extract(result)

    try:
        output = await asyncio.wait_for(_do_scan(), timeout=240)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="scan_project_context timed out after 240s")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return ScanResponse(
        result=output,
        duration_seconds=round(time.perf_counter() - start, 2),
    )


@app.get("/sessions")
async def list_sessions_endpoint(project_id: str = "default", limit: int = 20):
    from api.models import SessionListItem
    sessions = await _list_sessions(project_id, limit=limit)
    return {
        "sessions": [
            SessionListItem(
                id=s["id"],
                title=s["title"],
                created_at=s["created_at"],
                updated_at=s["updated_at"],
                expires_at=s.get("expires_at"),
                persist=s["persist"],
                message_count=s["message_count"],
            )
            for s in sessions
        ]
    }


@app.get("/sessions/{session_id}")
async def get_session_endpoint(session_id: str):
    from api.models import SessionDetail, SessionMessage
    import psycopg
    from config.config import config as _config

    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    try:
        async with await psycopg.AsyncConnection.connect(_config.postgres_uri) as conn:
            rows = await conn.execute(
                "SELECT role, content, created_at FROM session_messages"
                " WHERE session_id = %s ORDER BY created_at ASC",
                (session_id,),
            )
            messages = [
                SessionMessage(role=r[0], content=r[1], created_at=r[2])
                for r in await rows.fetchall()
            ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return SessionDetail(
        id=session["id"],
        project_id=session["project_id"],
        title=session["title"],
        created_at=session["created_at"],
        updated_at=session["updated_at"],
        expires_at=session.get("expires_at"),
        persist=session["persist"],
        summary=session.get("summary"),
        message_count=session["message_count"],
        messages=messages,
    )


@app.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    deleted = await _delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}


@app.patch("/sessions/{session_id}/persist")
async def persist_session_endpoint(session_id: str):
    updated = await _persist_session(session_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"persisted": session_id}


@app.get("/sessions/{session_id}/tree")
async def get_session_tree_endpoint(session_id: str):
    messages = await list_session_tree(session_id)
    return {"messages": messages}


@app.post("/sessions/{session_id}/branch")
async def branch_session_endpoint(session_id: str, request: BranchRequest):
    messages = await list_session_tree(session_id)
    target = next((m for m in messages if m["id"] == request.message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="message not found in this session")
    new_leaf_id = target["parent_message_id"]  # rewind to the SELECTED message's PARENT
    await set_current_leaf(session_id, new_leaf_id)
    return {"new_leaf_id": new_leaf_id, "editable_content": target["content"]}


@app.post("/sessions/{session_id}/fork")
async def fork_session_endpoint(session_id: str, request: ForkRequest):
    new_session_id = await fork_session(session_id, request.project_id, request.title)
    if new_session_id is None:
        raise HTTPException(status_code=404, detail="source session has no messages to fork")
    return {"session_id": new_session_id}


@app.post("/feedback", response_model=FeedbackResponse)
async def feedback(request: FeedbackRequest):
    """Record human feedback on a hive output. Bad ratings inject the correction
    into the coordinator instructions on the next run for this project."""
    if request.rating == "bad":
        await record_failure(
            request.task,
            f"[USER FEEDBACK] {request.notes}",
            request.project_id,
            agent="output_quality",
            rejected_output=request.rejected_output,
            corrected_output=request.corrected_output,
        )
        paired = bool(request.rejected_output and request.corrected_output)
        return FeedbackResponse(
            recorded=True,
            message=(
                "Correction recorded — will be injected into next run for this project"
                + (" (usable as a DPO preference pair)" if paired
                   else " (note: pass rejected_output + corrected_output to make this a training pair)")
            ),
        )
    else:
        await record_success(request.task, request.notes or "user marked as correct", request.project_id)
        return FeedbackResponse(recorded=True, message="Success pattern recorded to memory")


# ── Admin: DB-backed model routing (AGNOHive 2.3.2 addendum, 2026-08-08) ──────
# CRUD on model_catalog/team_role_models (swarm/db.py) — reuses THIS app rather
# than a dedicated service (see docs/guide/cloud-models.md's "Admin CRUD path"
# section for the reasoning: same unauthenticated-over-Tailscale trust boundary
# every other endpoint above already relies on). Deliberately stricter than this
# app's other writes: a bad row here breaks every future task that resolves to
# that model, not just one task/session — PATCH/DELETE validate against
# model_catalog (the FK on team_role_models.model_id already enforces "can't
# delete a model still in use" at the DB layer) and /reload returns the actual
# diff instead of a bare 200, so a mistake is visible immediately. Writes here
# do NOT take effect for already-running agents until /admin/model-routes/reload
# is called (or the process restarts) — see swarm/model_routing.py's
# ensure_cache_loaded()/reload() docstrings for why get_model() only ever reads
# the in-process cache, never the DB directly.

import sqlalchemy as sa


@app.get("/admin/model-routes")
async def list_model_routes():
    async with db.get_routing_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.model_catalog))).mappings().all()
    return {"models": [dict(r) for r in rows]}


@app.post("/admin/model-routes", status_code=201)
async def create_model_route(entry: ModelCatalogEntry):
    try:
        async with db.get_routing_engine().begin() as conn:
            await conn.execute(db.model_catalog.insert().values(**entry.model_dump()))
    except sa.exc.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"model_id '{entry.model_id}' already exists: {exc}")
    return entry


@app.patch("/admin/model-routes/{model_id}")
async def update_model_route(model_id: str, patch: ModelCatalogPatch):
    values = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not values:
        raise HTTPException(status_code=400, detail="no fields supplied to update")
    async with db.get_routing_engine().begin() as conn:
        result = await conn.execute(
            sa.update(db.model_catalog).where(db.model_catalog.c.model_id == model_id).values(**values)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"model_id '{model_id}' not found")
        row = (
            await conn.execute(sa.select(db.model_catalog).where(db.model_catalog.c.model_id == model_id))
        ).mappings().first()
    return dict(row)


@app.delete("/admin/model-routes/{model_id}")
async def delete_model_route(model_id: str):
    try:
        async with db.get_routing_engine().begin() as conn:
            result = await conn.execute(sa.delete(db.model_catalog).where(db.model_catalog.c.model_id == model_id))
    except sa.exc.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"'{model_id}' is still referenced by one or more team_role_models rows — remove those first: {exc}",
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"model_id '{model_id}' not found")
    return {"deleted": model_id}


@app.get("/admin/model-routes/teams")
async def list_team_role_models():
    async with db.get_routing_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.team_role_models))).mappings().all()
    return {"defaults": [dict(r) for r in rows]}


@app.post("/admin/model-routes/teams", status_code=201)
async def upsert_team_role_model(entry: TeamRoleModelEntry):
    """Create or replace the DEFAULT model AND declarative policy (Recommendation
    #4, 2026-08-13 — temperature/max_tokens/tool_call_limit) for one (team, role)
    pair — see swarm/model_routing.py's get_default_model()/get_role_policy(): a
    team YAML's own fields, when present, always override these. A full
    replace, not a partial patch: the UPDATE path writes every field on `entry`,
    including a None left as None — re-upserting with a policy field omitted
    clears any previous override for that field back to "use config.py's global
    default," not "leave whatever was there before."""
    async with db.get_routing_engine().begin() as conn:
        existing = (
            await conn.execute(
                sa.select(db.team_role_models.c.team_name).where(
                    db.team_role_models.c.team_name == entry.team_name,
                    db.team_role_models.c.role_name == entry.role_name,
                )
            )
        ).first()
        try:
            if existing:
                await conn.execute(
                    sa.update(db.team_role_models)
                    .where(
                        db.team_role_models.c.team_name == entry.team_name,
                        db.team_role_models.c.role_name == entry.role_name,
                    )
                    .values(
                        model_id=entry.model_id, temperature=entry.temperature,
                        max_tokens=entry.max_tokens, tool_call_limit=entry.tool_call_limit,
                    )
                )
            else:
                await conn.execute(db.team_role_models.insert().values(**entry.model_dump()))
        except sa.exc.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"model_id '{entry.model_id}' not in model_catalog: {exc}")
    return entry


@app.delete("/admin/model-routes/teams/{team_name}/{role_name}")
async def delete_team_role_model(team_name: str, role_name: str):
    async with db.get_routing_engine().begin() as conn:
        result = await conn.execute(
            sa.delete(db.team_role_models).where(
                db.team_role_models.c.team_name == team_name, db.team_role_models.c.role_name == role_name,
            )
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"no default for {team_name}/{role_name}")
    return {"deleted": f"{team_name}/{role_name}"}


@app.post("/admin/model-routes/reload", response_model=ModelRoutesReloadResponse)
async def reload_model_routes():
    """Re-read model_catalog/team_role_models into get_model()'s in-process cache
    and return what actually changed — the ONLY way an admin edit above takes
    effect for already-running agents (see swarm/model_routing.reload())."""
    diff = await model_routing.reload()
    return ModelRoutesReloadResponse(**diff)


# ── Admin: AGNOHive 2.3.3 team config additions (2026-08-18) ─────────────────
# CRUD on team_role_tools/team_role_skills/team_role_instruction_overlays/
# team_gate_flags — same unauthenticated-over-Tailscale trust boundary and same
# "writes need an explicit /reload to take effect for already-running agents"
# contract as /admin/model-routes above. See the Notion design page "AGNOHive
# 2.3.3 - Moving team yaml configs to sqlite db" for the full three-tier
# rationale; this section is Phase 3 of that plan.
#
# Registry validation (tools/skills, Open Question #2's resolution) queries the
# DB directly, NOT team_config's in-process cache — an admin write must never
# spuriously fail just because nobody happened to call
# team_config.ensure_cache_loaded() yet in this process; a one-off admin call
# reading the DB directly is cheap and always correct, unlike the hot per-agent-
# construction read path (_load_team(), which stays cache-only by design).

@app.get("/admin/team-config/tools")
async def list_team_role_tools():
    async with db.get_routing_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.team_role_tools))).mappings().all()
    return {"grants": [dict(r) for r in rows]}


@app.post("/admin/team-config/tools", status_code=201)
async def create_team_role_tool(entry: TeamRoleToolEntry):
    async with db.get_routing_engine().begin() as conn:
        registered = (
            await conn.execute(sa.select(db.tool_registry.c.tool_name).where(db.tool_registry.c.tool_name == entry.tool_name))
        ).first()
        if registered is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"tool_name '{entry.tool_name}' is not in the tool registry — it must be seen on a "
                    f"live MCP connection first (POST /admin/team-config/registry/refresh) before it can "
                    f"be granted. This is deliberate: a hand-typed name that doesn't exist would otherwise "
                    f"be silently inert (see the Notion design's Open Question #2 resolution)."
                ),
            )
        try:
            await conn.execute(db.team_role_tools.insert().values(**entry.model_dump()))
        except sa.exc.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"{entry.team_name}/{entry.role_name} already has tool '{entry.tool_name}' granted: {exc}",
            )
    return entry


@app.delete("/admin/team-config/tools/{team_name}/{role_name}/{tool_name}")
async def delete_team_role_tool(team_name: str, role_name: str, tool_name: str):
    async with db.get_routing_engine().begin() as conn:
        result = await conn.execute(
            sa.delete(db.team_role_tools).where(
                db.team_role_tools.c.team_name == team_name,
                db.team_role_tools.c.role_name == role_name,
                db.team_role_tools.c.tool_name == tool_name,
            )
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"no such grant: {team_name}/{role_name}/{tool_name}")
    return {"deleted": f"{team_name}/{role_name}/{tool_name}"}


@app.get("/admin/team-config/skills")
async def list_team_role_skills():
    async with db.get_routing_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.team_role_skills))).mappings().all()
    return {"grants": [dict(r) for r in rows]}


@app.post("/admin/team-config/skills", status_code=201)
async def create_team_role_skill(entry: TeamRoleSkillEntry):
    async with db.get_routing_engine().begin() as conn:
        registered = (
            await conn.execute(sa.select(db.skill_registry.c.skill_name).where(db.skill_registry.c.skill_name == entry.skill_name))
        ).first()
        if registered is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"skill_name '{entry.skill_name}' is not in the skill registry — it must be seen via "
                    f"list_skills() first (POST /admin/team-config/registry/refresh) before it can be "
                    f"granted. Same reject-at-write-not-silently rule as tools above."
                ),
            )
        try:
            await conn.execute(db.team_role_skills.insert().values(**entry.model_dump()))
        except sa.exc.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"{entry.team_name}/{entry.role_name} already has skill '{entry.skill_name}' granted: {exc}",
            )
    return entry


@app.delete("/admin/team-config/skills/{team_name}/{role_name}/{skill_name}")
async def delete_team_role_skill(team_name: str, role_name: str, skill_name: str):
    async with db.get_routing_engine().begin() as conn:
        result = await conn.execute(
            sa.delete(db.team_role_skills).where(
                db.team_role_skills.c.team_name == team_name,
                db.team_role_skills.c.role_name == role_name,
                db.team_role_skills.c.skill_name == skill_name,
            )
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"no such grant: {team_name}/{role_name}/{skill_name}")
    return {"deleted": f"{team_name}/{role_name}/{skill_name}"}


@app.get("/admin/team-config/instruction-overlays", response_model=list[InstructionOverlayOut])
async def list_instruction_overlays(team_name: str | None = None, role_name: str | None = None):
    query = sa.select(db.team_role_instruction_overlays)
    if team_name is not None:
        query = query.where(db.team_role_instruction_overlays.c.team_name == team_name)
    if role_name is not None:
        query = query.where(db.team_role_instruction_overlays.c.role_name == role_name)
    async with db.get_routing_engine().begin() as conn:
        rows = (await conn.execute(query)).mappings().all()
    return [InstructionOverlayOut(**dict(r)) for r in rows]


@app.post("/admin/team-config/instruction-overlays", status_code=201, response_model=InstructionOverlayOut)
async def create_instruction_overlay(entry: InstructionOverlayCreate):
    """Rejects with 409 once the (team_name, role_name) pair already has
    team_config.INSTRUCTION_OVERLAY_SOFT_CAP ACTIVE rows — Open Question #3's
    resolution. Counts active rows only, so deactivating (PATCH active=false)
    an existing overlay frees a slot without needing to delete it outright."""
    async with db.get_routing_engine().begin() as conn:
        active_count = (
            await conn.execute(
                sa.select(sa.func.count()).select_from(db.team_role_instruction_overlays).where(
                    db.team_role_instruction_overlays.c.team_name == entry.team_name,
                    db.team_role_instruction_overlays.c.role_name == entry.role_name,
                    db.team_role_instruction_overlays.c.active.is_(True),
                )
            )
        ).scalar_one()
        if active_count >= team_config.INSTRUCTION_OVERLAY_SOFT_CAP:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{entry.team_name}/{entry.role_name} already has {active_count} active overlay "
                    f"instructions — the soft cap ({team_config.INSTRUCTION_OVERLAY_SOFT_CAP}) would be "
                    f"exceeded. Deactivate or delete an existing one first (see the Notion design's Open "
                    f"Question #3 resolution — this cap exists because an unbounded user-editable "
                    f"instruction list already caused a measured instruction-bloat regression once, "
                    f"Engineering Team 2.0's own Phase 5)."
                ),
            )
        result = await conn.execute(
            db.team_role_instruction_overlays.insert().values(**entry.model_dump(), active=True)
        )
        # inserted_primary_key (not .returning()) -- portable across SQLite
        # versions without depending on native RETURNING-clause support, which
        # only shipped in SQLite 3.35+ and isn't guaranteed present in every
        # Python's bundled sqlite3.
        new_id = result.inserted_primary_key[0]
        row = (
            await conn.execute(sa.select(db.team_role_instruction_overlays).where(db.team_role_instruction_overlays.c.id == new_id))
        ).mappings().first()
    return InstructionOverlayOut(**dict(row))


@app.patch("/admin/team-config/instruction-overlays/{overlay_id}", response_model=InstructionOverlayOut)
async def patch_instruction_overlay(overlay_id: int, patch: InstructionOverlayPatch):
    values = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not values:
        raise HTTPException(status_code=400, detail="no fields supplied to update")
    async with db.get_routing_engine().begin() as conn:
        if values.get("active") is True:
            row = (
                await conn.execute(
                    sa.select(
                        db.team_role_instruction_overlays.c.team_name, db.team_role_instruction_overlays.c.role_name
                    ).where(db.team_role_instruction_overlays.c.id == overlay_id)
                )
            ).mappings().first()
            if row is not None:
                active_count = (
                    await conn.execute(
                        sa.select(sa.func.count()).select_from(db.team_role_instruction_overlays).where(
                            db.team_role_instruction_overlays.c.team_name == row["team_name"],
                            db.team_role_instruction_overlays.c.role_name == row["role_name"],
                            db.team_role_instruction_overlays.c.active.is_(True),
                            db.team_role_instruction_overlays.c.id != overlay_id,
                        )
                    )
                ).scalar_one()
                if active_count >= team_config.INSTRUCTION_OVERLAY_SOFT_CAP:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"re-activating this overlay would exceed the soft cap "
                            f"({team_config.INSTRUCTION_OVERLAY_SOFT_CAP}) for {row['team_name']}/{row['role_name']}"
                        ),
                    )
        result = await conn.execute(
            sa.update(db.team_role_instruction_overlays)
            .where(db.team_role_instruction_overlays.c.id == overlay_id)
            .values(**values)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"overlay id {overlay_id} not found")
        row = (
            await conn.execute(sa.select(db.team_role_instruction_overlays).where(db.team_role_instruction_overlays.c.id == overlay_id))
        ).mappings().first()
    return InstructionOverlayOut(**dict(row))


@app.delete("/admin/team-config/instruction-overlays/{overlay_id}")
async def delete_instruction_overlay(overlay_id: int):
    async with db.get_routing_engine().begin() as conn:
        result = await conn.execute(
            sa.delete(db.team_role_instruction_overlays).where(db.team_role_instruction_overlays.c.id == overlay_id)
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"overlay id {overlay_id} not found")
    return {"deleted": overlay_id}


@app.get("/admin/team-config/gates")
async def list_team_gate_flags():
    async with db.get_routing_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.team_gate_flags))).mappings().all()
    return {"flags": [dict(r) for r in rows]}


@app.post("/admin/team-config/gates", status_code=201)
async def upsert_team_gate_flag(entry: TeamGateFlagEntry):
    if entry.gate_name not in team_config.GATE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"gate_name must be one of {sorted(team_config.GATE_NAMES)}, got {entry.gate_name!r}",
        )
    async with db.get_routing_engine().begin() as conn:
        existing = (
            await conn.execute(
                sa.select(db.team_gate_flags.c.team_name).where(
                    db.team_gate_flags.c.team_name == entry.team_name,
                    db.team_gate_flags.c.gate_name == entry.gate_name,
                )
            )
        ).first()
        if existing:
            await conn.execute(
                sa.update(db.team_gate_flags)
                .where(
                    db.team_gate_flags.c.team_name == entry.team_name,
                    db.team_gate_flags.c.gate_name == entry.gate_name,
                )
                .values(enabled=entry.enabled)
            )
        else:
            await conn.execute(db.team_gate_flags.insert().values(**entry.model_dump()))
    return entry


@app.delete("/admin/team-config/gates/{team_name}/{gate_name}")
async def delete_team_gate_flag(team_name: str, gate_name: str):
    """Removing a flag row reverts that (team, gate) to swarm/team.py's
    hardcoded _GATE_ENABLED_TEAMS/_SEARCH_GATE_ENABLED_TEAMS default — not the
    same as setting enabled=false, which would force it OFF even for a team the
    hardcoded set already enables."""
    async with db.get_routing_engine().begin() as conn:
        result = await conn.execute(
            sa.delete(db.team_gate_flags).where(
                db.team_gate_flags.c.team_name == team_name, db.team_gate_flags.c.gate_name == gate_name,
            )
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"no flag for {team_name}/{gate_name}")
    return {"deleted": f"{team_name}/{gate_name}"}


@app.post("/admin/team-config/registry/refresh")
async def refresh_team_config_registry(body: RegistryRefreshRequest):
    """Refreshes tool_registry/skill_registry FROM a caller-supplied live
    enumeration — see swarm/team_config.refresh_registry()'s own docstring for
    why this module has no live MCP connection of its own to enumerate from.
    A future swarm run (which DOES hold a live MCP session) can call this with
    its own connected tool/skill list; for now this is also callable directly
    with an explicit list for bootstrapping/testing."""
    await team_config.refresh_registry(body.tool_names, body.skill_names)
    return {"tool_names_refreshed": len(body.tool_names), "skill_names_refreshed": len(body.skill_names)}


@app.get("/admin/team-config/health")
async def team_config_health():
    """What is actually misconfigured about this deployment's team config —
    the same findings printed once at startup, queryable at any time.

    Exists because the startup print is a single line in a log nobody scrolls
    back to, and because the interesting states here appear AFTER startup: an
    admin write that empties a role's grants, a tool renamed out from under a
    live grant. `healthy: true` with an empty list means clean, so this is
    usable as an actual check rather than something to eyeball.

    Cache-only and non-authoritative by the same rule as the check itself: it
    reports on what the running process currently believes, which is what the
    agents will actually get. If someone changed the DB without calling
    /admin/team-config/reload, this correctly reflects the stale cache the
    agents are running on — that IS the honest answer, not a bug."""
    findings = team_config.check_config_health()
    return {"healthy": not findings, "findings": findings, "finding_count": len(findings)}


@app.post("/admin/team-config/reload", response_model=TeamConfigReloadResponse)
async def reload_team_config():
    """Re-read team_role_tools/team_role_skills/team_role_instruction_overlays/
    team_gate_flags into the in-process cache and return what actually changed —
    the ONLY way an admin edit above takes effect for already-running agents
    (see swarm/team_config.reload())."""
    diff = await team_config.reload()
    return TeamConfigReloadResponse(**diff)
