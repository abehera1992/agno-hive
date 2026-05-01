from pydantic import BaseModel


class AgentSpec(BaseModel):
    name: str
    role: str
    model: str
    instructions: list[str]


class RunRequest(BaseModel):
    task: str
    project_id: str = "default"             # namespace for memory and feedback loop
    team: str | None = None                 # named team from registry (teams/*.yaml)
    agents: list[AgentSpec] | None = None   # inline spec — overrides registry
    mcp_url: str | None = None              # override default MCP_URL for this request


class RunResponse(BaseModel):
    result: str
    team: str
    agents_used: list[str]
    models_pulled: list[str]
    duration_seconds: float
