"""AgnoHive FastAPI server — accepts task requests from remote clients over Tailscale."""
import asyncio
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException

from api.models import AgentSpec, RunRequest, RunResponse, PlanResponse
from swarm.ollama import ensure_models
from swarm.team import run_task_async
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

    result = await run_task_async(
        task=request.task,
        agent_specs=agent_specs,
        coordinator_model=coordinator_model,
        mcp_url=mcp_url,
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

    plan_text = await run_task_async(
        task=request.task,
        agent_specs=agent_specs,
        coordinator_model=coordinator_model,
        mcp_url=mcp_url,
        project_id=request.project_id,
    )
    return PlanResponse(
        plan=plan_text,
        duration_seconds=round(time.perf_counter() - start, 2),
    )
