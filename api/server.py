"""AgnoHive FastAPI server — accepts task requests from remote clients over Tailscale."""
import asyncio
import json
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request

from api.models import (
    AgentSpec, RunRequest, RunResponse, PlanResponse, ScanRequest, ScanResponse,
    FeedbackRequest, FeedbackResponse, BranchRequest, ForkRequest,
    ModelCatalogEntry, ModelCatalogPatch, TeamRoleModelEntry, ModelRoutesReloadResponse,
    ClarificationRequest,
)
from fastapi.responses import StreamingResponse
from swarm.ollama import ensure_models
from swarm.team import run_task_async, run_task_stream
from swarm.feedback import record_failure, record_success, drain_background_tasks
from swarm import db, model_routing
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
        agents.append(AgentSpec(**a))
    coordinator = data.get("coordinator_model") or model_routing.get_default_model(name, "Coordinator") or config.leader_model
    mode = data.get("mode", "coordinate")
    # Optional read-only allowlist scoping the coordinator's direct MCP tool surface
    # (mirrors per-agent `tools:` scoping). None means "no scoping — full surface" —
    # the existing behavior, preserved for teams like `engineering` that need write access.
    coordinator_tools = data.get("coordinator_tools")
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


_DISCONNECT_POLL_S = 2.0


async def _run_cancel_on_disconnect(http_request, coro):
    """Await `coro`, but cancel it if the HTTP client goes away.

    Without this a client that times out, is Ctrl-C'd, or loses its connection leaves
    the run executing to completion on the GPU with nobody to receive the answer. Those
    orphans accumulate: on 2026-07-31 a handful of abandoned eval runs kept the GPU at
    96% and made every subsequent measurement look pathological — a trivial one-file
    read timed out at 600s that completes in 74s on an idle box. It also burns real
    money and thermal budget for output that is discarded.

    Cancellation propagates the whole way down: cancelling the task raises
    CancelledError at the next await, which closes the httpx connection to vLLM, and
    vLLM aborts generation when its client disconnects. So the GPU work actually stops
    rather than merely being ignored.

    The poll interval is deliberately coarse. is_disconnected() on a request whose body
    is already consumed is cheap, but this runs for the entire life of a multi-minute
    task, and detecting an abandoned run 2s late costs nothing.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_DISCONNECT_POLL_S)
            if done:
                return task.result()
            if await http_request.is_disconnected():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                print("[api] client disconnected — run cancelled, GPU work aborted")
                # 499 (nginx's "client closed request"): nobody is listening, but the
                # status distinguishes an abandoned run from a real server failure in
                # the access log.
                raise HTTPException(status_code=499, detail="client disconnected")
    except asyncio.CancelledError:
        task.cancel()
        raise


@app.post("/run", response_model=RunResponse)
async def run(request: RunRequest, http_request: Request):
    from api.models import SessionMeta
    start = time.perf_counter()

    # Resolve team spec
    if request.agents:
        agent_specs = request.agents
        coordinator_model = config.leader_model
        team_mode = request.mode or "coordinate"
        team_name = request.team or "custom"
        coordinator_tools = None
    elif request.team == "router":
        # EK-88 classifier-then-dispatch: one LLM call picks the team, then run it normally.
        chosen = await _route_to_team(request.task, ROUTABLE_TEAMS, config.router_classifier_model, config.ollama_host)
        agent_specs, coordinator_model, team_mode, coordinator_tools = _load_team(chosen)
        team_mode = request.mode or team_mode
        team_name = f"router:{chosen}"
    elif request.team:
        agent_specs, coordinator_model, team_mode, coordinator_tools = _load_team(request.team)
        team_mode = request.mode or team_mode  # request-level override wins
        team_name = request.team
    else:
        agent_specs, coordinator_model, team_mode, coordinator_tools = _load_team("engineering")
        team_mode = request.mode or team_mode
        team_name = "engineering"

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

    result, tokens, clarification = await _run_cancel_on_disconnect(
        http_request,
        run_task_async(
            task=request.task,
            agent_specs=agent_specs,
            coordinator_model=coordinator_model,
            coordinator_tools=coordinator_tools,
            mcp_url=mcp_url,
            mcp_urls=_resolve_mcp_urls(request.mcp_urls, config.lightrag_mcp_url),
            project_id=request.project_id,
            session_id=session_id,
            mode=team_mode,
            read_only=request.read_only,
        ),
    )

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
async def stream_endpoint(request: RunRequest):
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

    if request.agents:
        agent_specs = request.agents
        coordinator_model = config.leader_model
        stream_mode = request.mode or "coordinate"
        coordinator_tools = None
    elif request.team:
        agent_specs, coordinator_model, stream_mode, coordinator_tools = _load_team(request.team)
        stream_mode = request.mode or stream_mode
    else:
        agent_specs, coordinator_model, stream_mode, coordinator_tools = _load_team("engineering")
        stream_mode = request.mode or stream_mode

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

    async def generate():
        try:
            async for chunk in run_task_stream(
                task=request.task,
                agent_specs=agent_specs,
                coordinator_model=coordinator_model,
                coordinator_tools=coordinator_tools,
                mcp_url=mcp_url,
                mcp_urls=_resolve_mcp_urls(request.mcp_urls, config.lightrag_mcp_url),
                project_id=request.project_id,
                session_id=session_id,
                mode=stream_mode,
            ):
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
    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.model_catalog))).mappings().all()
    return {"models": [dict(r) for r in rows]}


@app.post("/admin/model-routes", status_code=201)
async def create_model_route(entry: ModelCatalogEntry):
    try:
        async with db.get_engine().begin() as conn:
            await conn.execute(db.model_catalog.insert().values(**entry.model_dump()))
    except sa.exc.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"model_id '{entry.model_id}' already exists: {exc}")
    return entry


@app.patch("/admin/model-routes/{model_id}")
async def update_model_route(model_id: str, patch: ModelCatalogPatch):
    values = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not values:
        raise HTTPException(status_code=400, detail="no fields supplied to update")
    async with db.get_engine().begin() as conn:
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
        async with db.get_engine().begin() as conn:
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
    async with db.get_engine().begin() as conn:
        rows = (await conn.execute(sa.select(db.team_role_models))).mappings().all()
    return {"defaults": [dict(r) for r in rows]}


@app.post("/admin/model-routes/teams", status_code=201)
async def upsert_team_role_model(entry: TeamRoleModelEntry):
    """Create or replace the DEFAULT model for one (team, role) pair — see
    swarm/model_routing.py's get_default_model(): a team YAML's own model: field,
    when present, always overrides this."""
    async with db.get_engine().begin() as conn:
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
                    .values(model_id=entry.model_id)
                )
            else:
                await conn.execute(db.team_role_models.insert().values(**entry.model_dump()))
        except sa.exc.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"model_id '{entry.model_id}' not in model_catalog: {exc}")
    return entry


@app.delete("/admin/model-routes/teams/{team_name}/{role_name}")
async def delete_team_role_model(team_name: str, role_name: str):
    async with db.get_engine().begin() as conn:
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
