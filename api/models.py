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
    # Declarative per-role policy (Recommendation #4, 2026-08-13, see DOCS.md
    # "Declarative Per-Role Policy") — same optional-with-fallback shape as model:
    # above, one layer down: a team YAML MAY set these explicitly, MAY leave them
    # unset and let api/server.py's _load_team() fill a gap from
    # model_routing.get_role_policy(team_name, role_name)'s DB row, or leave both
    # unset, in which case swarm/agents.py's make_agent_from_spec() falls back to
    # config.py's existing global member_temperature/member_max_tokens/
    # tool_call_limit — today's behavior, unchanged, for any role nobody has
    # opted in for. Replaces the old hardcoded `if spec.name == "Coder"`
    # special-case in swarm/agents.py with an explicit DB row instead.
    temperature: float | None = None
    max_tokens: int | None = None
    tool_call_limit: int | None = None


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


class ClarificationOption(BaseModel):
    label: str
    description: str | None = None


class ClarificationRequest(BaseModel):
    """A genuine fork-in-the-road the coordinator cannot resolve on its own — not
    something a tool call could look up. See swarm/team.py's `_extract_clarification`
    for how this is parsed out of the coordinator's raw text, and the coordinator
    instruction (`_COORDINATOR_INSTRUCTIONS`) for when it's allowed to emit one.
    2-4 options, matching the same constraint Claude Code's own AskUserQuestion uses —
    this is the same mechanism, carried over HTTP instead of in-process."""
    question: str
    options: list[ClarificationOption]


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
    # Present only when the coordinator hit a genuine decision point it can't resolve
    # itself — the run otherwise completed normally (result/session/tokens are all
    # still meaningful) but `result` had its clarification block stripped out. The
    # caller (hive CLI, agno_run) should present `question`/`options` to the human,
    # then re-call /run with the same session_id and the chosen option folded into
    # the next task text to continue.
    needs_clarification: ClarificationRequest | None = None


class PlanResponse(BaseModel):
    plan: str
    duration_seconds: float
    needs_clarification: ClarificationRequest | None = None


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
    # Declarative per-role policy (Recommendation #4, 2026-08-13) — see
    # AgentSpec's own comment for the full precedence chain. None (the default)
    # means "no override for this field" — upsert_team_role_model's UPDATE path
    # writes these through as-is, so explicitly re-upserting with a field left at
    # None clears any previous override for that field back to "use config.py's
    # global default," the same "absence means default" contract create/read
    # already had for model_id.
    temperature: float | None = None
    max_tokens: int | None = None
    tool_call_limit: int | None = None


class ModelRoutesReloadResponse(BaseModel):
    model_catalog: dict[str, list[str]]      # {"added": [...], "removed": [...], "changed": [...]}
    team_role_models: dict[str, list[str]]


# ── AGNOHive 2.3.3 (2026-08-18) — team config additions ──────────────────────
# See swarm/db.py's team_role_tools/team_role_skills/team_role_instruction_overlays/
# team_gate_flags comments for the full three-tier design this section's request/
# response shapes serve.

class TeamRoleToolEntry(BaseModel):
    team_name: str
    role_name: str
    tool_name: str


class TeamRoleSkillEntry(BaseModel):
    team_name: str
    role_name: str
    skill_name: str


class InstructionOverlayCreate(BaseModel):
    team_name: str
    role_name: str
    instruction_text: str
    created_by: str | None = None


class InstructionOverlayPatch(BaseModel):
    # Only `active` is patchable — the text itself is small enough that changing
    # it is a delete-and-recreate, not a partial edit (see the Notion design's
    # Tier 2 rationale: this mechanism deliberately has none of team_role_models'
    # versioning ceremony, so there is no "new version" concept to PATCH toward).
    active: bool | None = None


class InstructionOverlayOut(BaseModel):
    id: int
    team_name: str
    role_name: str
    instruction_text: str
    active: bool
    created_at: datetime
    created_by: str | None = None


class TeamGateFlagEntry(BaseModel):
    team_name: str
    gate_name: str
    enabled: bool


class RegistryRefreshRequest(BaseModel):
    tool_names: list[str] = []
    skill_names: list[str] = []


class TeamConfigReloadResponse(BaseModel):
    tool_grants_added: dict[str, list[str]]
    skill_grants_added: dict[str, list[str]]
    overlay_count_delta: int
    gates_changed: list[str]
    tool_registry_size: int
    skill_registry_size: int
