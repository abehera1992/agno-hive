"""Structured Researcher Work Proposal + application-side validation (Phase 1.54; wired into
run_attempt() as an opt-in, additive step by Phase 1.54a -- see propose_from_text() below).

Researcher PROPOSES a structured work plan; the APPLICATION validates it deterministically
before anything else may act on it. Neither Researcher nor any Coordinator controls what
happens next -- this module defines the proposal's shape and a pure, side-effect-free
validator, plus (Phase 1.54a) the one function that actually produces a proposal from a
real agent call. Nothing here ever DISPATCHES Coder/Reviewer/Executor or alters execution
flow: run_attempt() (swarm/eng_workflow.py) calls `propose_from_text()` only when its own
new `propose` parameter is explicitly True (default False everywhere), strictly AFTER its
existing investigation/evidence/acceptance fields are already finalized, and a failure here
can never change any of them -- see run_attempt()'s own Phase 1.54a comment at that call site.

Schema (`WorkProposal`/`ProposedWorkItem`) is a pydantic BaseModel specifically so a future
caller CAN set it as `Agent.output_schema` (verified present on the installed Agno package:
`agent.output_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]]`) and get Agno's own
structured-output parsing -- see `researcher_output_schema()` below. Role/capability/skill
VALUES are plain `str` fields, never a pydantic `Literal`/enum: an out-of-vocabulary value
must construct successfully and be reported by `validate_proposal()` with an explicit,
readable reason, not fail construction with a generic pydantic error before the application's
own validator ever runs (Scope: "Return explicit validation errors rather than silently
repairing invalid proposals").

Known-roles/known-capabilities/authorized-skills are all INJECTED into `validate_proposal()`,
never looked up here. Two real, deterministic, zero-network facts ARE reused directly from
production code: `swarm.eng_workflow._DEFAULT_MEMBER_PREFERENCE` (the exact 4 roles
select_capable_member() can ever select -- the real role vocabulary, not a reinvented one) as
the default `known_roles`, and `swarm.team._is_mutating()` (the real tool-surface read-only
check `_strip_mutating()` itself uses) to reject any required_capability that would need write
access -- this Workflow path is read_only=True ALWAYS (unchanged by this phase), so a mutating
capability can never be authorized here regardless of what any external tool/skill registry
would otherwise say. Everything else about "is this tool/skill actually real" needs a live
MCP/DB registry this module deliberately does not reach for (no ZGX dependency): when the
caller does not inject that knowledge, the validator says so explicitly rather than guessing.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from config.config import config
from swarm.eng_workflow import _DEFAULT_MEMBER_PREFERENCE
from swarm.team import _is_mutating

KNOWN_ROLES = frozenset(_DEFAULT_MEMBER_PREFERENCE)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_ID_FORMAT_DESC = (
    "A valid identifier: letters, digits, and underscores only, must start with a letter "
    "or underscore. No hyphens, spaces, or other punctuation. Example: EK_VOUCHER_FULL_UI "
    "(not EK-VOUCHER-FULL-UI, not a bare number)."
)

# Phase 1.61e: these three constants/helpers render the SAME registries validate_proposal()
# checks against -- known_capabilities / authorized_skills_by_role / KNOWN_ROLES -- into the
# generation prompt itself. Phase 1.61d (read-only audit) found that propose_from_text() only
# ever forwarded these to validate_proposal() (post-hoc check), never into the prompt the
# planner model actually reads -- so the model had nothing authoritative to select from and
# fell back to plausible-sounding guesses (technology names, invented roles/skills). This is
# purely a generation-time disclosure: it changes what the planner is TOLD, never what
# validate_proposal() CHECKS -- that function, _NAME_RE, and every authorization rule are
# untouched by this phase.
_MAX_CATALOG_ITEMS = 300  # safety bound only; real registries today are a few dozen entries


def _render_catalog_list(items: list, *, empty_note: str) -> str:
    """Deterministic, sorted-input, newline-free rendering of a flat identifier catalog,
    bounded to _MAX_CATALOG_ITEMS so a pathologically large registry cannot cause unbounded
    prompt growth. Caller passes an already-sorted list; this function only truncates/joins."""
    if not items:
        return empty_note
    shown = items[:_MAX_CATALOG_ITEMS]
    text = ", ".join(shown)
    remaining = len(items) - len(shown)
    if remaining > 0:
        text += f", ... ({remaining} more not shown)"
    return text


def _render_known_roles(known_roles: frozenset) -> str:
    """Flat, sorted role-identifier list -- known_roles is always a small, fixed set in
    practice (the same KNOWN_ROLES validate_proposal() itself defaults to), but this is
    defensive against an empty/overridden set rather than assuming non-empty."""
    return _render_catalog_list(sorted(known_roles), empty_note="(no roles configured)")


def _render_known_capabilities(known_capabilities: Optional[frozenset]) -> str:
    """known_capabilities is a single, global (not role-scoped) catalog in
    validate_proposal() -- rendered ONCE here, never duplicated per role, to keep prompt
    growth bounded. None means the caller has no live registry to offer at all (distinct
    from an empty-but-real frozenset(), which means a real registry that's genuinely empty
    right now) -- both cases must leave required_capabilities empty, but the wording differs
    so the model is told WHY, not left to guess between 'nothing available' and 'not told'."""
    if known_capabilities is None:
        return ("No MCP capability registry was supplied for this attempt -- do not invent "
                "tool identifiers; required_capabilities MUST be an empty list for every "
                "work item.")
    return _render_catalog_list(
        sorted(known_capabilities),
        empty_note="No MCP capabilities are currently available -- required_capabilities "
                   "MUST be an empty list for every work item.")


def _render_skills_by_role(authorized_skills_by_role: Optional[dict], known_roles: frozenset) -> str:
    """One line per role, in deterministic sorted(known_roles) order -- NEVER a global union
    of every role's skills, so a skill authorized for one role is never presented as if it
    were available to another (the exact confusion a flat combined list would invite, since
    validate_proposal()'s own authorization check is strictly per-role). Only known_roles are
    rendered -- an authorized_skills_by_role key outside known_roles would never pass the
    role check anyway, so showing it here would only add noise. None (no registry supplied
    at all) renders one explicit, unambiguous instruction instead of per-role rows, since
    there is nothing real to group by role in that case."""
    if authorized_skills_by_role is None:
        return ("No skill registry was supplied for this attempt -- do not invent skill "
                "identifiers; proposed_skills MUST be an empty list for every work item.")
    lines = []
    for role in sorted(known_roles):
        role_skills = sorted(authorized_skills_by_role.get(role) or [])
        rendered = _render_catalog_list(role_skills, empty_note="(none authorized for this role)")
        lines.append(f"- {role}: {rendered}")
    return "\n".join(lines)


# Phase 1.63b: field classification by responsibility -- WHAT each property is for, WHO
# produces its value, and WHAT it actually does at execution time. Documentation only; no
# field is added, removed, or renamed, and every existing persisted proposal still parses
# exactly as before (see each field's own Field(description=...) below for the model-facing
# generation hint, unchanged by this phase).
#
# WORK DEFINITION -- ProposedWorkItem.objective/role/depends_on, WorkProposal.objective,
#   plus ProposedWorkItem.acceptance_criteria/expected_evidence:
#   Producer: the planner model (propose_from_text()'s structured-output call).
#   Execution significance: these are what plan_from_proposal() actually translates into a
#   real Workflow WorkItem (swarm/eng_workflow.py) when a proposal is approved and
#   dispatched -- the only fields that directly shape WHAT gets done and in what order
#   (topological, via depends_on).
#
# TECHNOLOGY GUIDANCE -- ProposedWorkItem.technologies:
#   Producer: the planner model, free-form (never validated against any registry).
#   Execution significance: PURELY INFORMATIONAL. Never read by validate_proposal(),
#   never reaches the executable work_items projection, never affects which MCP tools or
#   skills an agent receives. Exists only so a persisted, auditable proposal communicates
#   WHY the proposed capabilities/skills look the way they do (Phase 1.61a).
#
# ENGINEERING REASONING -- WorkProposal.implementation_rationale/alternatives_considered/
#   risks:
#   Producer: the planner model, free-form, evidence-grounded.
#   Execution significance: PURELY INFORMATIONAL, identical treatment to `technologies`
#   above -- disclosure for a human/auditor reading the recorded proposal, never consumed
#   by validation or execution (Phase 1.61a).
#
# MCP CAPABILITIES -- ProposedWorkItem.required_capabilities:
#   Producer: the planner model, constrained to EXACT MCP tool identifiers selected from
#   the authoritative catalog propose_from_text() renders into its own prompt (Phase 1.61e:
#   a role's SQLite-registry-assigned tools, or -- when none are assigned -- every globally
#   registered tool; Phase 1.63b formalizes this selection rule via
#   swarm.team_config.get_extra_tools()/get_registered_tools()).
#   Execution significance: FUNCTIONAL, not merely descriptive. validate_proposal() rejects
#   any entry that is malformed, unknown to the supplied catalog, or would need write
#   access (never relaxed by this phase). For an APPROVED, EXECUTABLE proposal,
#   swarm/eng_workflow.py's run_attempt() (Phase 1.63b) widens the acting role's own
#   per-attempt tool set with exactly these names before the agent is built, so a
#   model-selected capability is not just persisted/displayed but actually made callable --
#   see that call site's own comment for the resolution path (swarm/agents.py's
#   make_agent_from_spec(), unchanged).
#
# SKILLS -- ProposedWorkItem.proposed_skills:
#   Producer: the planner model, constrained to skill identifiers already granted to the
#   work item's OWN role (per-role catalogs, never a cross-role union -- Phase 1.61e).
#   Execution significance: FUNCTIONAL but advisory, not an authorization grant on its own
#   -- validate_proposal() reports an unauthorized (but well-formed, real) skill as a
#   DISCLOSURE (`unauthorized_skills`), never a hard error; skill content actually reaching
#   an executing agent still goes through the SAME existing skill-resolution/ownership path
#   (swarm.team._resolve_requested_skills(), Phase 1.57c) this phase does not touch.
class ProposedWorkItem(BaseModel):
    """One unit of proposed work. Field names deliberately mirror swarm.eng_workflow.WorkItem's
    own vocabulary (work_item_id/objective/required_capabilities) where the same concept
    already exists there, rather than inventing a second naming convention for it.

    Phase 1.56d: `work_item_id`'s Field description states the exact identifier format
    validate_proposal()'s own `_NAME_RE` requires (unchanged, still strict -- this is a
    schema-level hint for the model that GENERATES a proposal, not a relaxation of the
    deterministic check that VALIDATES one). Verified that pydantic Field descriptions
    survive into the JSON schema agno actually sends the model (model_json_schema()) --
    this is a real generation-time hint, not decorative Python-only documentation."""

    work_item_id: str = Field(description=_ID_FORMAT_DESC)
    objective: str
    role: str
    depends_on: list[str] = Field(
        default_factory=list,
        description="Other work items' work_item_id values this one depends on. Must use "
                    "the EXACT SAME identifier string as that other item's own work_item_id "
                    f"field -- same format rule applies: {_ID_FORMAT_DESC}")
    required_capabilities: list[str] = Field(
        default_factory=list,
        description="MCP TOOL IDENTIFIERS ONLY (e.g. get_file_content, search_files) -- the "
                    "exact names of tools this work item's role needs. Never a framework, "
                    "language, or library name -- those belong in `technologies` below.")
    proposed_skills: list[str] = Field(
        default_factory=list,
        description="Skill IDENTIFIERS ONLY, from the skill registry already granted to this "
                    "work item's role. Never a human-readable label like 'UI/UX Design' or "
                    "'Quality Assurance' -- those belong in `technologies` below. Subject to "
                    "the existing per-role authorization check; unauthorized or malformed "
                    "entries are still disclosed/rejected exactly as before.")
    acceptance_criteria: str = ""
    expected_evidence: str = ""
    # Phase 1.61a: informational only, added alongside (never instead of) the two
    # identifier-shaped fields above. None of these four fields is read by
    # validate_proposal()'s authorization logic, by Ledger.record_proposal()'s
    # proposal_work_item INSERT (which hand-picks only the pre-existing columns -- see
    # that function's own docstring), or by plan_from_proposal()/s_execute() (which only
    # ever reads Ledger.get_executable_proposal()'s work_items rows, never raw_proposal).
    # They exist purely so a persisted, auditable proposal communicates WHY the proposed
    # work items look the way they do -- they can never grant a permission, authorize a
    # tool/skill, change which role is selected, or affect whether a work item becomes
    # executable. Defaulting to "" / [] keeps every proposal recorded before this phase
    # fully parseable: WorkProposal.model_validate(old_json) still succeeds unchanged.
    technologies: list[str] = Field(
        default_factory=list,
        description="Human-readable frameworks, languages, and technologies relevant to THIS "
                    "work item (e.g. 'React', 'TypeScript', 'Formik', 'FastAPI'). Purely "
                    "descriptive -- never validated against any registry, never checked for "
                    "authorization, never affects execution.")


class WorkProposal(BaseModel):
    """The Researcher's structured output: an overall objective plus the proposed work items.
    Construction always succeeds for any well-typed JSON (lists/strings in the right shape) --
    semantic correctness (known role, no cycle, no dangling dependency, ...) is exclusively
    validate_proposal()'s job, run separately, after construction.

    Phase 1.61a: `implementation_rationale`/`alternatives_considered`/`risks` are the
    overall-strategy counterparts to ProposedWorkItem.technologies (see that field's own
    docstring for the full "informational only, never authorization-checked, never read by
    plan_from_proposal()" guarantee -- identical here). Default "" / [] so a pre-1.61a
    persisted proposal (raw_proposal_json with no such keys) still parses via
    model_validate() unchanged."""

    objective: str
    work_items: list[ProposedWorkItem] = Field(default_factory=list)
    implementation_rationale: str = Field(
        default="",
        description="Concise, evidence-grounded explanation of why the proposed approach "
                    "fits the task -- grounded in the investigation findings, not invented.")
    alternatives_considered: list[str] = Field(
        default_factory=list,
        description="Other viable approaches considered and why the proposed approach was "
                    "preferred. Concise, evidence-grounded -- not required for every task.")
    risks: list[str] = Field(
        default_factory=list,
        description="Relevant implementation risks or constraints (e.g. compatibility, "
                    "scope boundaries). Concise, evidence-grounded -- not required for every task.")


def researcher_output_schema() -> type[WorkProposal]:
    """The schema a caller passes as `Agent(output_schema=researcher_output_schema())` (or
    sets on an already-built agent before `.arun()`) to make a Researcher-role agent produce
    a structured WorkProposal instead of free-form investigative text. Verified against the
    installed Agno package: Agent.output_schema accepts `Type[BaseModel]` directly.

    Deliberately NOT called anywhere in run_attempt()/_build_team()/make_researcher() in this
    phase: forcing output_schema on every Researcher call would replace its existing free-form
    investigative answer with forced JSON on every single run, which is a real behavior change
    this phase's own compatibility requirement ("preserve existing Researcher investigation...
    maintain backward compatibility... where practical") forbids introducing blindly. A future,
    explicitly-opted-in caller (a distinct planning call, not every investigative attempt) is
    the right place to actually set this -- not implemented here (no agent dispatch change)."""
    return WorkProposal


class ValidationResult(BaseModel):
    """`valid` is False iff `errors` is non-empty -- nothing else makes a proposal invalid.
    `unauthorized_skills` and `unverified_capabilities` are disclosures, never rejections:
    skills are recommendations only (Scope: "do not authorize or load unapproved skills" is
    satisfied by recording them here, never by adding them to `errors`), and a capability this
    validator could not check against a real registry (none was injected) is reported as
    unverified rather than silently trusted or silently rejected."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    unauthorized_skills: dict[str, list[str]] = Field(default_factory=dict)
    unverified_capabilities: dict[str, list[str]] = Field(default_factory=dict)


def _find_cycle(nodes: list[str], edges: dict[str, list[str]]) -> Optional[list[str]]:
    """Standard DFS cycle detection (white/gray/black). Returns the cycle as a list of
    work_item_ids (first id repeated at the end) if one exists, else None. `edges` must
    already be restricted to in-proposal targets only -- a dangling depends_on reference is
    validate_proposal()'s own, separately-reported error, not something this function sees."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    path: list[str] = []

    def visit(n: str) -> Optional[list[str]]:
        color[n] = GRAY
        path.append(n)
        for m in edges.get(n, []):
            if color.get(m) == GRAY:
                i = path.index(m)
                return path[i:] + [m]
            if color.get(m) == WHITE:
                found = visit(m)
                if found:
                    return found
        path.pop()
        color[n] = BLACK
        return None

    for n in nodes:
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def validate_proposal(
    proposal: WorkProposal,
    *,
    known_roles: frozenset = KNOWN_ROLES,
    known_capabilities: Optional[frozenset] = None,
    authorized_skills_by_role: Optional[dict] = None,
) -> ValidationResult:
    """Deterministic, side-effect-free. Never raises for a malformed proposal -- every
    rejection reason is appended to `errors` and reported, never swallowed or silently patched.

    `known_roles` defaults to the real _DEFAULT_MEMBER_PREFERENCE set (researcher/executor/
    coder/reviewer) -- the same vocabulary select_capable_member() already uses, not a
    reinvented one. `known_capabilities` (default None) is the caller's own real tool-name
    registry, if it has one available (e.g. from a live MCP/team_config query) -- when absent,
    "is this capability name real" is NOT checked (recorded under `unverified_capabilities`
    instead of being guessed at); the MUTATING-tool check below runs regardless, since it needs
    no registry at all. `authorized_skills_by_role` (default None) is the caller's own mapping
    of role -> the skill names actually granted to it (e.g. from AgentSpec.skills); when
    absent, EVERY proposed skill is treated as unauthorized by default -- the safe direction,
    since skills are recommendations only and nothing here may authorize or load one."""
    errors: list[str] = []
    unauthorized_skills: dict[str, list[str]] = {}
    unverified_capabilities: dict[str, list[str]] = {}

    if not (proposal.objective or "").strip():
        errors.append("overall objective is empty")
    if not proposal.work_items:
        errors.append("proposal has no work items")

    seen_ids: set = set()
    duplicate_ids: set = set()
    for wi in proposal.work_items:
        if not _NAME_RE.match(wi.work_item_id or ""):
            errors.append(f"work_item_id {wi.work_item_id!r} is not a valid identifier")
            continue
        if wi.work_item_id in seen_ids:
            duplicate_ids.add(wi.work_item_id)
        seen_ids.add(wi.work_item_id)
    for dup in sorted(duplicate_ids):
        errors.append(f"duplicate work_item_id: {dup!r}")

    valid_ids = seen_ids - duplicate_ids  # a duplicated id is never a safe dependency target
    edges: dict[str, list[str]] = {}
    for wi in proposal.work_items:
        if wi.work_item_id not in valid_ids:
            continue  # already reported above (malformed or duplicate); do not compound errors
        if not (wi.objective or "").strip():
            errors.append(f"work item {wi.work_item_id!r}: objective is empty")
        if not (wi.acceptance_criteria or "").strip():
            errors.append(f"work item {wi.work_item_id!r}: acceptance_criteria is empty")
        if not (wi.expected_evidence or "").strip():
            errors.append(f"work item {wi.work_item_id!r}: expected_evidence is empty")
        if wi.role not in known_roles:
            errors.append(f"work item {wi.work_item_id!r}: unsupported role {wi.role!r} "
                          f"(known roles: {sorted(known_roles)})")

        resolved_deps: list[str] = []
        for dep in wi.depends_on:
            if dep == wi.work_item_id:
                errors.append(f"work item {wi.work_item_id!r}: depends on itself")
            elif dep not in valid_ids:
                errors.append(f"work item {wi.work_item_id!r}: depends_on references "
                              f"unknown work_item_id {dep!r}")
            else:
                resolved_deps.append(dep)
        edges[wi.work_item_id] = resolved_deps

        item_unverified: list[str] = []
        for cap in wi.required_capabilities:
            if not _NAME_RE.match(cap or ""):
                errors.append(f"work item {wi.work_item_id!r}: malformed capability name {cap!r}")
                continue
            if _is_mutating(cap):
                errors.append(
                    f"work item {wi.work_item_id!r}: required_capability {cap!r} needs write "
                    f"access, which this read-only workflow can never authorize")
                continue
            if known_capabilities is not None and cap not in known_capabilities:
                errors.append(f"work item {wi.work_item_id!r}: unsupported capability "
                              f"{cap!r} (not in the known capability registry)")
            elif known_capabilities is None:
                item_unverified.append(cap)
        if item_unverified:
            unverified_capabilities[wi.work_item_id] = item_unverified

        item_unauthorized: list[str] = []
        role_skills = (authorized_skills_by_role or {}).get(wi.role, frozenset())
        for skill in wi.proposed_skills:
            if not _NAME_RE.match((skill or "").replace("-", "_")):
                errors.append(f"work item {wi.work_item_id!r}: malformed skill name {skill!r}")
                continue
            if skill not in role_skills:
                item_unauthorized.append(skill)
        if item_unauthorized:
            unauthorized_skills[wi.work_item_id] = item_unauthorized

    cycle = _find_cycle(sorted(valid_ids), edges)
    if cycle:
        errors.append("dependency cycle detected: " + " -> ".join(cycle))

    return ValidationResult(
        valid=not errors, errors=errors,
        unauthorized_skills=unauthorized_skills,
        unverified_capabilities=unverified_capabilities,
    )


async def propose_from_text(
    investigation_text: str,
    task: str,
    model,
    *,
    known_capabilities: Optional[frozenset] = None,
    authorized_skills_by_role: Optional[dict] = None,
    skill_blocks: Optional[list] = None,
) -> tuple[Optional[WorkProposal], ValidationResult]:
    """Phase 1.54a: the SEPARATE structured-planning call run_attempt() invokes, only when
    explicitly opted into, after its own investigation call has already produced
    `investigation_text` -- never the same `.arun()` call that produced it, so the existing
    free-form investigative answer is never replaced or truncated (Scope: "Use a separate
    structured planning call if necessary to preserve the existing output contract").

    `model` is the CALLER's own already-resolved model object (e.g. the acting agent's own
    `agent.model` from the completed investigation) -- never a hardcoded model/provider name,
    so this makes no assumption about what is actually deployed. The planning agent built here
    is given NO tools: it synthesizes a structured breakdown from text already gathered, it
    does not investigate further and cannot call anything, Coder/Reviewer/Executor included.

    Never raises: any failure (bad/missing structured content, a model/network error) returns
    `(None, ValidationResult(valid=False, errors=[...]))` -- a malformed or absent proposal is
    reported as explicitly invalid, never silently treated as "nothing to validate" and never
    promoted to something executable. `known_capabilities`/`authorized_skills_by_role` are
    forwarded unchanged to validate_proposal() -- see that function's own docstring for why
    missing registry data is disclosed, never treated as authorization.

    `skill_blocks` (Phase 1.58.1, default None/empty -- every existing caller
    unaffected): pre-rendered, provenance-tagged skill content
    (swarm.team._render_skill_injection_block) for the SAME work item this
    investigation already ran under, already resolved/authorized by
    run_attempt()'s own caller (eng_workflow.s_execute) before this attempt
    started. Prepended to the planning prompt, never to `instructions=`
    (this planner Agent is built with no `instructions` concept at all --
    see below -- its only input is this one prompt string). Covers the
    mission's "Researcher... proposal-generation stages where applicable":
    the exact same per-item skill text the investigation call itself got
    (via eng_workflow.run_attempt's own AgentSpec injection) is also visible
    to the planning call that synthesizes a proposal FROM that
    investigation's findings.

    Phase 1.56b: this call needs its OWN, longer HTTP timeout (config.proposal_request_timeout_s,
    see that field's own docstring for the root cause) -- `model` is reused as a shallow copy
    with `timeout` overridden, never mutated in place, so the caller's own already-in-use model
    object (and any client it has already cached) is completely unaffected. `async_client`/
    `http_client` are explicitly reset to None on the copy so a fresh client is built with the
    new timeout on first use here, instead of reusing a client the original object may already
    have cached (and baked the OLD 120s timeout into) from the investigation call that produced
    `investigation_text`."""
    from agno.agent import Agent
    import copy as _copy

    planner_model = _copy.copy(model)
    planner_model.async_client = None
    planner_model.http_client = None
    planner_model.timeout = config.proposal_request_timeout_s

    try:
        planner = Agent(model=planner_model, tools=[], output_schema=researcher_output_schema(),
                        markdown=False)
        skill_preamble = ("\n\n".join(skill_blocks) + "\n\n") if skill_blocks else ""
        # Phase 1.61e: render the SAME registries validate_proposal() will check this
        # proposal against, so the planner is told what is actually valid instead of being
        # expected to infer it. See the _render_* helpers' own docstrings above for why each
        # is rendered the way it is (capabilities: one global list; skills: per-role, never
        # a union; roles: the same KNOWN_ROLES validate_proposal() defaults to).
        role_catalog_text = _render_known_roles(KNOWN_ROLES)
        capability_catalog_text = _render_known_capabilities(known_capabilities)
        skills_catalog_text = _render_skills_by_role(authorized_skills_by_role, KNOWN_ROLES)
        prompt = (
            skill_preamble +
            "Based ONLY on the investigation findings below (already gathered -- do not "
            "investigate further, you have no tools), propose a structured WorkProposal: "
            "the overall objective, and zero or more work items. Each work item needs a "
            "role, objective, dependencies (depends_on, by work_item_id), "
            "required_capabilities, proposed_skills, technologies, acceptance_criteria and "
            "expected_evidence. If the findings do not call for any further work, return "
            "an empty work_items list rather than inventing one.\n\n"
            f"Every work_item_id MUST be a valid identifier: {_ID_FORMAT_DESC} Use the exact "
            "same work_item_id string when another item's depends_on references it -- "
            "dependencies are matched by exact string equality, not by similarity.\n\n"
            "IMPORTANT -- these three fields are NOT interchangeable:\n"
            "- required_capabilities: MCP TOOL IDENTIFIERS ONLY -- select ONLY from the "
            "authoritative capability list below. Never a framework, language, or library "
            "name.\n"
            "- proposed_skills: SKILL IDENTIFIERS ONLY -- select ONLY from the authoritative "
            "per-role skill lists below, for the work item's OWN selected role. Never a "
            "human-readable label, and never a skill listed under a different role.\n"
            "- technologies: human-readable frameworks, languages, and technologies relevant "
            "to that work item (e.g. 'React', 'TypeScript', 'Formik', 'FastAPI'). This is "
            "where ALL human-readable labels belong -- never in required_capabilities or "
            "proposed_skills.\n\n"
            "AUTHORITATIVE CATALOGS -- the ONLY values allowed in the fields named above. Do "
            "not invent, guess, paraphrase, or substitute a plausible-sounding alternative. If "
            "nothing in a catalog fits a work item, use an empty list for that field rather "
            "than inventing an entry:\n\n"
            f"Valid roles (role field): {role_catalog_text}\n\n"
            f"Valid MCP tool identifiers (required_capabilities field): {capability_catalog_text}\n\n"
            "Valid skill identifiers (proposed_skills field), listed separately per role -- a "
            "skill listed under one role is NOT authorized for any other role:\n"
            f"{skills_catalog_text}\n\n"
            "Also provide, at the overall WorkProposal level: implementation_rationale (a "
            "concise, evidence-grounded explanation of why the proposed approach fits the "
            "task -- grounded in the investigation findings below, never invented), "
            "alternatives_considered (other viable approaches and why this one was preferred, "
            "if any were genuinely considered), and risks (relevant implementation risks or "
            "constraints, if any apply). Leave these empty rather than inventing content the "
            "investigation findings do not support.\n\n"
            f"Original task: {task}\n\n"
            f"Investigation findings:\n{investigation_text}"
        )
        run_output = await planner.arun(prompt)
    except Exception as e:  # noqa: BLE001 -- additive capability, must never raise into the caller
        return None, ValidationResult(valid=False, errors=[f"proposal generation call failed: {repr(e)[:200]}"])

    content = getattr(run_output, "content", None)
    try:
        if isinstance(content, WorkProposal):
            proposal = content
        elif isinstance(content, dict):
            proposal = WorkProposal.model_validate(content)
        elif isinstance(content, str) and content.strip():
            proposal = WorkProposal.model_validate_json(content)
        else:
            return None, ValidationResult(
                valid=False,
                errors=[f"planning call produced no usable structured output (content type: {type(content).__name__})"])
    except Exception as e:  # noqa: BLE001 -- a malformed response is an explicit validation failure, never a crash
        return None, ValidationResult(valid=False, errors=[f"could not parse structured output as WorkProposal: {repr(e)[:200]}"])

    return proposal, validate_proposal(
        proposal, known_capabilities=known_capabilities,
        authorized_skills_by_role=authorized_skills_by_role)
