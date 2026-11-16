"""Structured Researcher Work Proposal + application-side validation (Phase 1.54).

Researcher PROPOSES a structured work plan; the APPLICATION validates it deterministically
before anything else may act on it. Neither Researcher nor any Coordinator controls what
happens next -- this module defines the proposal's shape and a pure, side-effect-free
validator. It does NOT dispatch agents, does NOT alter execution flow, and is NOT wired into
run_attempt()/run_engineering_workflow() in this phase: nothing here changes how the existing
Researcher-only read-only Workflow path behaves today. A future phase that wires this into
actual multi-stage dispatch is explicitly out of this phase's scope.

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

from swarm.eng_workflow import _DEFAULT_MEMBER_PREFERENCE
from swarm.team import _is_mutating

KNOWN_ROLES = frozenset(_DEFAULT_MEMBER_PREFERENCE)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProposedWorkItem(BaseModel):
    """One unit of proposed work. Field names deliberately mirror swarm.eng_workflow.WorkItem's
    own vocabulary (work_item_id/objective/required_capabilities) where the same concept
    already exists there, rather than inventing a second naming convention for it."""

    work_item_id: str
    objective: str
    role: str
    depends_on: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    proposed_skills: list[str] = Field(default_factory=list)
    acceptance_criteria: str = ""
    expected_evidence: str = ""


class WorkProposal(BaseModel):
    """The Researcher's structured output: an overall objective plus the proposed work items.
    Construction always succeeds for any well-typed JSON (lists/strings in the right shape) --
    semantic correctness (known role, no cycle, no dangling dependency, ...) is exclusively
    validate_proposal()'s job, run separately, after construction."""

    objective: str
    work_items: list[ProposedWorkItem] = Field(default_factory=list)


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
