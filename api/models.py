from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel


class AgentSpec(BaseModel):
    name: str
    role: str
    # Required here (this is also the shape a caller can POST directly via
    # RunRequest.agents, with no team YAML / DB context to fall back to). A team
    # YAML MAY omit model: and let api/server.py's _load_team() resolve it from
    # model_routing.get_default_model(team_name, role_name) BEFORE constructing
    # this AgentSpec (AGNOHive 2.3.2 addendum, 2026-08-08) — by the time an
    # AgentSpec exists, model is always populated one way or the other.
    model: str
    instructions: list[str]
    description: str | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None       # names from the skill catalog this role should
                                          # see in its L1 index. None means "no skills
                                          # advertised" — the agent can still call
                                          # load_skill(name) directly if it knows the
                                          # name, but nothing is proactively listed.


class RunRequest(BaseModel):
    task: str
    project_id: str = "default"
    team: str | None = None
    mode: str | None = None               # team mode override: "coordinate" | "collaborate" | "route"
    agents: list[AgentSpec] | None = None
    mcp_url: str | None = None            # primary MCP — project context + app-specific tools
    mcp_urls: list[str] | None = None     # secondary MCPs — e.g. hive-mcp for host actions
    session_id: str | None = None         # resume existing session
    persist: bool = False                 # mark new session as permanent
    read_only: bool = False               # strip every mutating tool (write_file, apply_diff,
                                          # run_shell/docker, notion_create/update/delete, ...)
                                          # from the coordinator AND all agents for this run.
                                          # Enforced at the TOOL SURFACE, not by instruction:
                                          # a prompt saying "do NOT call write_file" was
                                          # observed calling it anyway. Lets a write-capable
                                          # team be used read-only per request instead of
                                          # duplicating the team YAML.


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


class ScanRequest(BaseModel):
    mcp_url: str          # hive-mcp URL — the scan tool lives here
    force: bool = False   # True = full rescan; False = incremental


class ScanResponse(BaseModel):
    result: str
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


class BranchRequest(BaseModel):
    message_id: int


class ForkRequest(BaseModel):
    title: str
    project_id: str


class FeedbackRequest(BaseModel):
    session_id: str = ""
    task: str
    project_id: str = "default"
    rating: str  # "good" or "bad"
    notes: str = ""  # specific correction or praise
    # Preference-pair capture (training Phase 1). Supplying both turns the row into
    # a usable DPO/ORPO triple: (task, rejected_output, corrected_output).
    # `notes` stays the human-readable WHY; these two are the verbatim outputs.
    rejected_output: str = ""   # what the model actually produced (the bad answer)
    corrected_output: str = ""  # what it should have produced instead


class FeedbackResponse(BaseModel):
    recorded: bool
    message: str


# ── Admin: DB-backed model routing (AGNOHive 2.3.2 addendum, 2026-08-08) ──────
# swarm/model_routing.py / swarm/db.py's model_catalog + team_role_models tables.

class ModelCatalogEntry(BaseModel):
    model_id: str
    kind: str                          # "local" | "cloud"
    provider: str
    vllm_served_as: str | None = None
    requires_cloud_gate: bool = False
    active: bool = True


class ModelCatalogPatch(BaseModel):
    # Every field optional — PATCH applies only the fields actually supplied.
    kind: str | None = None
    provider: str | None = None
    vllm_served_as: str | None = None
    requires_cloud_gate: bool | None = None
    active: bool | None = None


class TeamRoleModelEntry(BaseModel):
    team_name: str
    role_name: str
    model_id: str


class ModelRoutesReloadResponse(BaseModel):
    model_catalog: dict[str, list[str]]      # {"added": [...], "removed": [...], "changed": [...]}
    team_role_models: dict[str, list[str]]
