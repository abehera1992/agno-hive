"""Phase Z20 (2026-09-30) -- local contract-shape proof only. No production file is
imported for modification and none is touched; swarm/team.py is read here only to
reuse its (unmodified) `_member_id` resolver and to cite `_DB_TOOLS` as existing
architectural evidence for Section 5's single-vs-list question.

Z19 (unmodified, all 4 tests still pass as of this phase) proved the supply side:
the delegation boundary can resolve a member and read its actual `.tools`, and this
distinguishes a Researcher-equivalent (has db_query/db_schema) from an
Executor-equivalent (does not) -- reusing the same real AgentSpec/Agent/Team
construction path and the same `_member_id` resolver production's own "[team]
member surface" log line trusts.

Z20 asks the remaining, demand-side question: can a required-capability field be
added to the delegation *contract* and validated deterministically against that
same supply-side information? This file answers that with a LOCAL representation
only -- a plain dataclass standing in for what `delegate_structured_task`'s
signature could look like, and a pure validation function next to it. Neither is
wired into `swarm/team.py`; `delegate_structured_task` itself is not called here at
all. This is intentionally narrower than Z19, which did exercise the real
production delegation-construction call shape -- Z20 has nothing new to prove about
that call shape, only about a hypothetical additional field's shape and semantics.
"""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agno.tools.function import Function as AgnoFunction

from api.models import AgentSpec
from swarm.team import _DB_TOOLS, _build_team, _member_id


# ── Local test doubles (same construction pattern Z19 already validated) ────────

def _fake_tool(name: str) -> AgnoFunction:
    def _entrypoint():
        return None
    return AgnoFunction.from_callable(_entrypoint, name=name)


def _fake_mcp(tool_names):
    return SimpleNamespace(functions={n: _fake_tool(n) for n in tool_names})


def _z20_team():
    fake_mcp = _fake_mcp([
        "db_query", "db_schema", "get_file_content", "search_files",
        "get_env_info", "run_command", "check_port",
    ])
    agent_specs = [
        AgentSpec(
            name="Researcher", role="Codebase investigator", model="qwen2.5-coder:32b",
            instructions=["Investigate the codebase."],
            tools=["db_query", "db_schema", "get_file_content", "search_files"],
        ),
        AgentSpec(
            name="Executor", role="Command runner", model="qwen2.5-coder:32b",
            instructions=["Run commands."],
            tools=["get_env_info", "run_command", "check_port"],
        ),
    ]
    return _build_team(
        agent_specs=agent_specs, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate", mcp_list=[fake_mcp], instructions=[],
    )


def _resolve_member(team, member_id: str):
    """member_id -> real member object, same resolver production's own "member
    surface" log line uses (_member_id), applied here purely locally -- not a
    change to any production function."""
    target = _member_id(member_id)
    for member in team.members:
        if _member_id(getattr(member, "name", "")) == target:
            return member
    return None


def _member_tool_names(member) -> set[str]:
    return {getattr(t, "name", type(t).__name__) for t in (getattr(member, "tools", []) or [])}


# ── Section 2/3: the candidate contract shape, LOCAL to this test file only ─────
# Deliberately not added to delegate_structured_task's real signature
# (swarm/team.py:14161-14167) -- this dataclass exists only in this test module.

@dataclass
class _CandidateStructuredDelegation:
    member_id: str
    target: str
    objective: str
    evidence_required: str
    completion_criteria: str
    required_capability: str | None = None  # Option A shape under test


def _validate_capability(team, delegation: _CandidateStructuredDelegation) -> str:
    """The pure validation function Section 3 asks for: structured requirement ->
    selected member -> actual member.tools -> deterministic comparison ->
    ACCEPT/REJECT. Depends on nothing but `delegation.required_capability` and the
    resolved member's real `.tools` -- no access to `.objective`/`.target`/
    `.evidence_required`/`.completion_criteria` anywhere in this function body,
    which is what Assertion E/F require."""
    if delegation.required_capability is None:
        return "ACCEPT"  # no requirement declared -- nothing to validate against
    member = _resolve_member(team, delegation.member_id)
    if member is None:
        return "REJECT"
    return "ACCEPT" if delegation.required_capability in _member_tool_names(member) else "REJECT"


# ── Assertion A: contract representation ─────────────────────────────────────

def test_a_required_capability_is_representable_as_structured_data():
    delegation = _CandidateStructuredDelegation(
        member_id="researcher", target="the live database",
        objective="count the number of rows in the parties table",
        evidence_required="the exact row count returned by the query",
        completion_criteria="the row count has been returned",
        required_capability="db_query",
    )
    # Structured field, not text embedded in objective/target/etc.
    assert delegation.required_capability == "db_query"
    assert "db_query" not in delegation.objective
    assert "db_query" not in delegation.target


# ── Assertions B/C: Researcher resolution + capability match ────────────────

def test_b_c_researcher_resolves_and_matches_db_query():
    team = _z20_team()
    member = _resolve_member(team, "researcher")
    assert member is not None
    assert "db_query" in _member_tool_names(member)


# ── Assertions D: Executor resolution + capability mismatch ─────────────────

def test_d_executor_resolves_and_lacks_db_query():
    team = _z20_team()
    member = _resolve_member(team, "executor")
    assert member is not None
    assert "db_query" not in _member_tool_names(member)


# ── Assertion E: deterministic validation, ACCEPT/REJECT ────────────────────

def test_e_researcher_with_db_query_requirement_is_accepted():
    team = _z20_team()
    delegation = _CandidateStructuredDelegation(
        member_id="researcher", target="the live database",
        objective="count the number of rows in the parties table",
        evidence_required="the exact row count returned by the query",
        completion_criteria="the row count has been returned",
        required_capability="db_query",
    )
    assert _validate_capability(team, delegation) == "ACCEPT"


def test_e_executor_with_db_query_requirement_is_rejected():
    team = _z20_team()
    delegation = _CandidateStructuredDelegation(
        member_id="executor", target="the live database",
        objective="count the number of rows in the parties table",
        evidence_required="the exact row count returned by the query",
        completion_criteria="the row count has been returned",
        required_capability="db_query",
    )
    assert _validate_capability(team, delegation) == "REJECT"


def test_e_validation_depends_only_on_member_and_capability_not_wording():
    """Same member_id and required_capability, completely different
    objective/target/evidence_required/completion_criteria text -- the result
    must be identical, proving the check reads only the structured field."""
    team = _z20_team()
    d1 = _CandidateStructuredDelegation(
        member_id="executor", target="the live database",
        objective="count the number of rows in the parties table",
        evidence_required="the exact row count returned by the query",
        completion_criteria="the row count has been returned",
        required_capability="db_query",
    )
    d2 = _CandidateStructuredDelegation(
        member_id="executor", target="an entirely different unrelated area",
        objective="do something that has nothing to do with rows or tables",
        evidence_required="literally anything", completion_criteria="whenever",
        required_capability="db_query",
    )
    assert _validate_capability(team, d1) == _validate_capability(team, d2) == "REJECT"


# ── Assertion F: no false inference -- ambiguous/DB-word-free text, same result ──

def test_f_ambiguous_wording_with_no_db_words_still_rejects_executor():
    """Objective/target deliberately avoid "database", "query", "DB", "row",
    "table" -- exactly the words _DB_TASK_RE (swarm/team.py, out of scope,
    unmodified) looks for. A free-text heuristic would very plausibly miss
    this; the structured field does not, because nothing here reads the text."""
    team = _z20_team()
    delegation = _CandidateStructuredDelegation(
        member_id="executor", target="parties",
        objective="find out how many there are right now",
        evidence_required="a number", completion_criteria="a number was obtained",
        required_capability="db_query",
    )
    for forbidden in ("database", "query", " db ", "row", "table"):
        assert forbidden not in delegation.objective.lower()
        assert forbidden not in delegation.target.lower()
    assert _validate_capability(team, delegation) == "REJECT"


def test_f_same_ambiguous_wording_accepts_researcher():
    team = _z20_team()
    delegation = _CandidateStructuredDelegation(
        member_id="researcher", target="parties",
        objective="find out how many there are right now",
        evidence_required="a number", completion_criteria="a number was obtained",
        required_capability="db_query",
    )
    assert _validate_capability(team, delegation) == "ACCEPT"


# ── Section 5/7 evidence: _DB_TOOLS is a pre-existing PLURAL grouping ────────
# Not a new finding manufactured for this phase -- cited from the already-shipped,
# unmodified guard constant this codebase uses today for DB-evidence satisfaction.

def test_existing_db_tools_constant_is_already_a_plural_set_not_a_single_name():
    assert _DB_TOOLS == {"db_query", "db_schema"}
    assert len(_DB_TOOLS) > 1


def test_single_capability_check_would_wrongly_reject_a_db_schema_only_member():
    """Concrete demonstration for Section 7: if T8's requirement were represented
    as the single string "db_query" and a member legitimately satisfies the DB-
    evidence guard via db_schema alone (production's OWN _DB_TOOLS treats the two
    as interchangeable evidence -- swarm/team.py's db-evidence guard computes
    db_reads via `tool_names=_DB_TOOLS`, either tool counting), a bare single-
    string required_capability check produces a FALSE REJECT for a capable member."""
    team = _z20_team()
    schema_only_specs = [
        AgentSpec(
            name="SchemaOnlyResearcher", role="Schema inspector", model="qwen2.5-coder:32b",
            instructions=["Inspect schema."], tools=["db_schema", "get_file_content"],
        ),
    ]
    schema_only_team = _build_team(
        agent_specs=schema_only_specs, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate",
        mcp_list=[_fake_mcp(["db_schema", "get_file_content"])], instructions=[],
    )
    delegation = _CandidateStructuredDelegation(
        member_id="schema-only-researcher", target="x", objective="y",
        evidence_required="z", completion_criteria="w",
        required_capability="db_query",  # singular -- names only ONE of the two
    )
    # A member whose only DB tool is db_schema is wrongly rejected by a
    # singular-string check, even though production's own guard would have
    # accepted db_schema evidence as satisfying the DB-evidence requirement.
    assert _validate_capability(schema_only_team, delegation) == "REJECT"
    assert "db_schema" in _DB_TOOLS  # the tool this member DOES have is a real DB tool
