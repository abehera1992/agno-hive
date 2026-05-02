from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel


class AgentSpec(BaseModel):
    name: str
    role: str
    model: str
    instructions: list[str]


class RunRequest(BaseModel):
    task: str
    project_id: str = "default"
    team: str | None = None
    agents: list[AgentSpec] | None = None
    mcp_url: str | None = None            # primary MCP — project context + app-specific tools
    mcp_urls: list[str] | None = None     # secondary MCPs — e.g. hive-mcp for host actions
    session_id: str | None = None         # resume existing session
    persist: bool = False                 # mark new session as permanent


class SessionMeta(BaseModel):
    session_id: str
    turn: int         # message pairs completed (message_count // 2)
    context_size: int # verbatim messages injected into coordinator
    compacted: bool   # True if a summary was also injected
    persist: bool
    expires_at: str | None  # ISO 8601 or None when persist=True


class RunResponse(BaseModel):
    result: str
    team: str
    agents_used: list[str]
    models_pulled: list[str]
    duration_seconds: float
    session: SessionMeta
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class PlanResponse(BaseModel):
    plan: str
    duration_seconds: float


# ── Session endpoint models ────────────────────────────────────────────────────

class SessionListItem(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    persist: bool
    message_count: int


class SessionMessage(BaseModel):
    role: str
    content: str
    created_at: datetime


class SessionDetail(BaseModel):
    id: str
    project_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    persist: bool
    summary: str | None
    message_count: int
    messages: list[SessionMessage]
