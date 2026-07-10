"""AgnoHive FastAPI server — accepts task requests from remote clients over Tailscale."""
import asyncio
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException

from api.models import AgentSpec, RunRequest, RunResponse, PlanResponse, ScanRequest, ScanResponse, FeedbackRequest, FeedbackResponse
from fastapi.responses import StreamingResponse
from swarm.ollama import ensure_models
from swarm.team import run_task_async, run_task_stream
from swarm.feedback import record_failure, record_success, drain_background_tasks
from config.config import config
from observability.setup import setup_telemetry
from swarm.sessions import (
    create_session, append_message, get_session,
    list_sessions as _list_sessions,
    delete_session as _delete_session,
    persist_session as _persist_session,
    compact_session, _cleanup_expired,
    get_context,
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
    agents = [AgentSpec(**a) for a in data["agents"]]
    coordinator = data.get("coordinator_model", config.leader_model)
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


@app.post("/run", response_model=RunResponse)
async def run(request: RunRequest):
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

    result, tokens = await run_task_async(
        task=request.task,
        agent_specs=agent_specs,
        coordinator_model=coordinator_model,
        coordinator_tools=coordinator_tools,
        mcp_url=mcp_url,
        mcp_urls=_resolve_mcp_urls(request.mcp_urls, config.lightrag_mcp_url),
        project_id=request.project_id,
        session_id=session_id,
        mode=team_mode,
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

    plan_text, _ = await run_task_async(
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
    )


@app.post("/stream")
async def stream_endpoint(request: RunRequest):
    """Stream coordinator output as Server-Sent Events.

    Events:
      data: {"type": "chunk",  "content": "<text>"}
      data: {"type": "done",   "session": {...}, "input_tokens": N, ...}
      data: {"type": "error",  "content": "<message>"}

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
        )
        return FeedbackResponse(
            recorded=True,
            message="Correction recorded — will be injected into next run for this project",
        )
    else:
        await record_success(request.task, request.notes or "user marked as correct", request.project_id)
        return FeedbackResponse(recorded=True, message="Success pattern recorded to memory")
