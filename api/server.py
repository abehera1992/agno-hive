"""AgnoHive FastAPI server — accepts task requests from remote clients over Tailscale."""
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException

from api.models import AgentSpec, RunRequest, RunResponse
from swarm.ollama import ensure_models
from swarm.team import run_task_async
from config.config import config

app = FastAPI(title="AgnoHive", version="1.0.0")

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
    start = time.perf_counter()

    # Resolve team spec — inline agents take priority over named team
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

    # Pull any missing Ollama models before building the team
    all_models = list({coordinator_model} | {a.model for a in agent_specs})
    models_pulled = await ensure_models(all_models, config.ollama_host)

    mcp_url = request.mcp_url or config.mcp_url

    result = await run_task_async(
        task=request.task,
        agent_specs=agent_specs,
        coordinator_model=coordinator_model,
        mcp_url=mcp_url,
        project_id=request.project_id,
    )

    return RunResponse(
        result=result,
        team=team_name,
        agents_used=[a.name for a in agent_specs],
        models_pulled=models_pulled,
        duration_seconds=round(time.perf_counter() - start, 2),
    )
