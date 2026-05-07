"""AgnoHive FastAPI server — accepts task requests from remote clients over Tailscale."""
import asyncio
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException

from api.models import AgentSpec, RunRequest, RunResponse, PlanResponse, ScanRequest, ScanResponse
from fastapi.responses import StreamingResponse
from swarm.ollama import ensure_models
from swarm.team import run_task_async, run_task_stream
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

app = FastAPI(title="AgnoHive", version="1.0.0")

# Auto-instrument FastAPI — adds spans for every HTTP request
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
except ImportError:
    pass

_TEAMS_DIR = Path(__file__).parent.parent / "teams"


def _load_team(name: str) -> tuple[list[AgentSpec], str]:
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
    return agents, coordinator


@app.on_event("startup")
async def _start_cleanup_loop():
    asyncio.create_task(_session_cleanup_loop())


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
        team_name = request.team or "custom"
    elif request.team:
        agent_specs, coordinator_model = _load_team(request.team)
        team_name = request.team
    else:
        agent_specs, coordinator_model = _load_team("engineering")
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
    _, prior_messages = await get_context(session_id)
    context_size = len(prior_messages)
    session_before = await get_session(session_id)
    has_summary = bool(session_before and session_before.get("summary"))

    result, tokens = await run_task_async(
        task=request.task,
        agent_specs=agent_specs,
        coordinator_model=coordinator_model,
        mcp_url=mcp_url,
        mcp_urls=request.mcp_urls,
        project_id=request.project_id,
        session_id=session_id,
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
    agent_specs, coordinator_model = _load_team("planning")
    mcp_url = request.mcp_url or config.mcp_url

    all_models = list({coordinator_model} | {a.model for a in agent_specs})
    await ensure_models(all_models, config.ollama_host)

    plan_text, _ = await run_task_async(
        task=request.task,
        agent_specs=agent_specs,
        coordinator_model=coordinator_model,
        mcp_url=mcp_url,
        mcp_urls=request.mcp_urls,
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
    elif request.team:
        agent_specs, coordinator_model = _load_team(request.team)
    else:
        agent_specs, coordinator_model = _load_team("engineering")

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

    _, prior_messages = await get_context(session_id)
    context_size = len(prior_messages)
    session_before = await get_session(session_id)
    has_summary = bool(session_before and session_before.get("summary"))

    async def generate():
        try:
            async for chunk in run_task_stream(
                task=request.task,
                agent_specs=agent_specs,
                coordinator_model=coordinator_model,
                mcp_url=mcp_url,
                mcp_urls=request.mcp_urls,
                project_id=request.project_id,
                session_id=session_id,
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
