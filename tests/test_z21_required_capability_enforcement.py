"""Phase Z21 (2026-09-30) -- implements and proves the production enforcement
boundary Z20 identified but did not build: delegate_structured_task now accepts
an optional `required_capabilities: list[str] | None = None` and, when populated,
deterministically rejects a delegation to a member whose real `.tools` do not
intersect it -- BEFORE the original delegated entrypoint (and therefore the
member's task execution) ever runs.

These tests exercise the REAL production call shape -- `agno.team._default_tools.
_get_delegate_task_function(team, ...)`, the exact free-function call agno's own
`_tools.py`/`team.py` use, with only the underlying agno delegation engine
(`_ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION`) stubbed. This is the identical
harness `test_phase_s_structural_delegation.py` established and Z19/Z20 reused;
duplicated here in full (not imported cross-module -- this pytest config does not
put `tests/` on `sys.path` for plain module imports) rather than reimplemented
differently. Team/member construction reuses the exact real AgentSpec/Agent/Team
path Z19/Z20 already validated. The only thing genuinely new here is the
capability field and its validation; nothing about member resolution, team
construction, or the underlying agno delegation engine is mocked differently
than those existing, already-trusted tests.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import swarm.team as team_mod
from swarm.team import _DB_TOOLS, _build_team, _member_capability_tools

import agno.team._default_tools as agno_default_tools
from agno.tools.function import Function as AgnoFunction

from api.models import AgentSpec


# ── Harness duplicated verbatim from test_phase_s_structural_delegation.py ───

class _FakeFunction:
    def __init__(self, entrypoint):
        self.entrypoint = entrypoint
        self.stop_after_tool_call = False
        self.show_result = True


def _fake_original_entrypoint(calls):
    async def entrypoint(member_id, task):
        calls.append({"member_id": member_id, "task": task})
        yield f"[delegating to {member_id}]"
        yield f"Agent {member_id}: fake result"
    return entrypoint


def _fake_agno_original(calls):
    def original(team, **kwargs):
        return _FakeFunction(_fake_original_entrypoint(calls))
    return original


def _call_production_path(team, calls=None):
    if calls is None:
        calls = []
    with patch.object(
        team_mod, "_ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION", _fake_agno_original(calls),
    ):
        result = agno_default_tools._get_delegate_task_function(
            team, run_response=None, run_context=None, session=None,
            team_run_context={},
        )
    return result, calls


async def _run_entrypoint(new_function, **kwargs):
    chunks = []
    async for item in new_function.entrypoint(**kwargs):
        chunks.append(item)
    return chunks


# ── Real team construction (same pattern as Z19/Z20) ─────────────────────────

def _fake_tool(name: str) -> AgnoFunction:
    def _entrypoint():
        return None
    return AgnoFunction.from_callable(_entrypoint, name=name)


def _fake_mcp(tool_names):
    return SimpleNamespace(functions={n: _fake_tool(n) for n in tool_names})


def _z21_team(researcher_tools, executor_tools):
    """Two real members, via the real _build_team/make_agent_from_spec path,
    with CALLER-CHOSEN tool grants -- lets each test case set up exactly the
    tool surface its assertion needs (e.g. Assertion B needs a member with
    ONLY db_schema, not the full Researcher surface)."""
    all_tools = sorted(set(researcher_tools) | set(executor_tools) | {"unrelated_tool"})
    fake_mcp = _fake_mcp(all_tools)
    agent_specs = [
        AgentSpec(
            name="Researcher", role="Codebase investigator", model="qwen2.5-coder:32b",
            instructions=["Investigate the codebase."], tools=list(researcher_tools),
        ),
        AgentSpec(
            name="Executor", role="Command runner", model="qwen2.5-coder:32b",
            instructions=["Run commands."], tools=list(executor_tools),
        ),
    ]
    return _build_team(
        agent_specs=agent_specs, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate", mcp_list=[fake_mcp], instructions=[],
    )


_BASE_KWARGS = dict(
    target="the live database", objective="count the number of rows in the parties table",
    evidence_required="the exact row count returned by the query",
    completion_criteria="the row count has been returned",
)


async def _delegate(new_function, member_id, required_capabilities=None, **overrides):
    kwargs = dict(_BASE_KWARGS)
    kwargs.update(overrides)
    return await _run_entrypoint(
        new_function, member_id=member_id,
        required_capabilities=required_capabilities, **kwargs,
    )


# ── Assertion A: db_query satisfies DB capability, entrypoint executes once ──

@pytest.mark.asyncio
async def test_a_db_query_satisfies_db_capability_and_executes_once():
    team = _z21_team(researcher_tools=["db_query", "get_file_content"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(new_function, "researcher", required_capabilities=["db_query", "db_schema"])

    assert len(calls) == 1
    assert calls[0]["member_id"] == "researcher"
    assert not any(c.startswith("DELEGATION REJECTED") for c in chunks)


# ── Assertion B: db_schema ALONE satisfies DB capability -- the Z20 case ─────

@pytest.mark.asyncio
async def test_b_db_schema_alone_satisfies_db_capability_and_executes_once():
    """Mandatory per Z20: a member with ONLY db_schema (no db_query) must still
    be ACCEPTED when required_capabilities=["db_query","db_schema"] -- proving
    the list is OR/set-intersection semantics, not "must have the first one"."""
    team = _z21_team(researcher_tools=["db_schema", "get_file_content"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(new_function, "researcher", required_capabilities=["db_query", "db_schema"])

    assert len(calls) == 1
    assert calls[0]["member_id"] == "researcher"
    assert not any(c.startswith("DELEGATION REJECTED") for c in chunks)


# ── Assertion C: neither DB tool -> reject, zero executions ──────────────────

@pytest.mark.asyncio
async def test_c_neither_db_tool_rejects_with_zero_executions():
    team = _z21_team(researcher_tools=["db_query"], executor_tools=["unrelated_tool"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(new_function, "executor", required_capabilities=["db_query", "db_schema"])

    assert calls == []
    assert len(chunks) == 1
    assert chunks[0].startswith("DELEGATION REJECTED")
    assert "executor" in chunks[0]


# ── Assertion D: single-capability mismatch rejects ──────────────────────────

@pytest.mark.asyncio
async def test_d_single_capability_mismatch_rejects():
    """required_capabilities=["db_query"] (singular list) against a member with
    only db_schema must REJECT -- proves the list names alternative acceptable
    tools explicitly, rather than treating "any DB-shaped tool" as equivalent."""
    team = _z21_team(researcher_tools=["db_schema"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(new_function, "researcher", required_capabilities=["db_query"])

    assert calls == []
    assert chunks[0].startswith("DELEGATION REJECTED")


# ── Assertion E: single-capability match succeeds, executes exactly once ─────

@pytest.mark.asyncio
async def test_e_single_capability_match_executes_exactly_once():
    team = _z21_team(researcher_tools=["db_query"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(new_function, "researcher", required_capabilities=["db_query"])

    assert len(calls) == 1
    assert not any(c.startswith("DELEGATION REJECTED") for c in chunks)


# ── Assertion F: required_capabilities=None preserves existing behavior ─────

@pytest.mark.asyncio
async def test_f_none_preserves_existing_behavior_even_for_an_incapable_member():
    """No requirement declared -- a member with NO matching capability at all
    must still execute normally. The new field must never become an implicit
    requirement."""
    team = _z21_team(researcher_tools=["db_query"], executor_tools=["unrelated_tool"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(new_function, "executor", required_capabilities=None)

    assert len(calls) == 1
    assert calls[0]["member_id"] == "executor"
    assert not any(c.startswith("DELEGATION REJECTED") for c in chunks)


@pytest.mark.asyncio
async def test_f_omitting_the_argument_entirely_also_preserves_existing_behavior():
    """Byte-for-byte: a caller that doesn't even pass the new kwarg (every
    pre-Z21 call shape) must behave exactly as before Z21."""
    team = _z21_team(researcher_tools=["db_query"], executor_tools=["unrelated_tool"])
    new_function, calls = _call_production_path(team)

    kwargs = dict(_BASE_KWARGS)
    chunks = await _run_entrypoint(new_function, member_id="executor", **kwargs)

    assert len(calls) == 1
    assert not any(c.startswith("DELEGATION REJECTED") for c in chunks)


# ── Assertion G: rejection prevents execution (entrypoint invocation count = 0) ──

@pytest.mark.asyncio
async def test_g_rejection_prevents_execution_entrypoint_count_zero():
    team = _z21_team(researcher_tools=["get_file_content"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    await _delegate(new_function, "researcher", required_capabilities=["db_query", "db_schema"])

    assert len(calls) == 0


# ── Assertion H: successful validation executes exactly once (no double-exec) ──

@pytest.mark.asyncio
async def test_h_successful_validation_executes_exactly_once_not_twice():
    team = _z21_team(researcher_tools=["db_query", "db_schema"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    await _delegate(new_function, "researcher", required_capabilities=["db_query", "db_schema"])

    assert len(calls) == 1


# ── Assertion I: validation is independent of delegation wording ────────────

@pytest.mark.asyncio
async def test_i_validation_result_identical_across_completely_different_wording():
    team = _z21_team(researcher_tools=["run_command"], executor_tools=["run_command"])
    new_function, _ = _call_production_path(team)

    chunks_1 = await _delegate(
        new_function, "researcher", required_capabilities=["db_query"],
        target="the live database", objective="count the number of rows in the parties table",
        evidence_required="the exact row count returned by the query",
        completion_criteria="the row count has been returned",
    )
    new_function2, _ = _call_production_path(team)
    chunks_2 = await _delegate(
        new_function2, "researcher", required_capabilities=["db_query"],
        target="an entirely different unrelated area",
        objective="do something that has nothing to do with rows or tables",
        evidence_required="literally anything", completion_criteria="whenever",
    )

    assert chunks_1[0].startswith("DELEGATION REJECTED")
    assert chunks_2[0].startswith("DELEGATION REJECTED")


@pytest.mark.asyncio
async def test_i_ambiguous_wording_with_no_db_words_still_rejects():
    """Objective/target deliberately avoid "database", "query", "DB", "row",
    "table" -- the exact words _DB_TASK_RE (unmodified, untouched by Z21) looks
    for. The structured field still produces the correct result because
    nothing in the validation path reads this text at all."""
    team = _z21_team(researcher_tools=["run_command"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(
        new_function, "researcher", required_capabilities=["db_query", "db_schema"],
        target="parties", objective="find out how many there are right now",
        evidence_required="a number", completion_criteria="a number was obtained",
    )

    for forbidden in ("database", "query", " db ", "row", "table"):
        assert forbidden not in "find out how many there are right now"
        assert forbidden not in "parties"
    assert calls == []
    assert chunks[0].startswith("DELEGATION REJECTED")


# ── Assertion J: multiple required capability values -- OR semantics ────────

@pytest.mark.asyncio
async def test_j_multiple_required_capabilities_satisfied_by_only_one_match():
    """required_capabilities=["db_query","db_schema"], member has ONLY
    db_query -> ACCEPT. Establishes OR/set-intersection, not AND/all-required."""
    team = _z21_team(researcher_tools=["db_query"], executor_tools=["run_command"])
    new_function, calls = _call_production_path(team)

    chunks = await _delegate(new_function, "researcher", required_capabilities=["db_query", "db_schema"])

    assert len(calls) == 1
    assert not any(c.startswith("DELEGATION REJECTED") for c in chunks)


def test_j_helper_function_directly_confirms_or_semantics():
    """The primary proof above exercises the real structured-delegation path;
    this additionally checks the extracted helper (_member_capability_tools)
    directly, confirming it reflects _DB_TOOLS' own existing plural grouping."""
    team = _z21_team(researcher_tools=["db_schema"], executor_tools=["run_command"])
    available = _member_capability_tools(team, "researcher")
    assert available is not None
    assert bool(available & set(_DB_TOOLS))  # db_schema is a real member of _DB_TOOLS
    assert not (available & {"db_query"})    # but NOT db_query specifically


# ── Member resolution: real team.members, not a fabricated registry ─────────

def test_member_resolution_uses_the_real_team_members_not_a_fabricated_registry():
    team = _z21_team(researcher_tools=["db_query", "db_schema"], executor_tools=["run_command"])
    researcher_tools = _member_capability_tools(team, "researcher")
    executor_tools = _member_capability_tools(team, "executor")
    assert researcher_tools == {"db_query", "db_schema", "update_session_state"}
    assert executor_tools == {"run_command", "update_session_state"}
    assert _member_capability_tools(team, "nonexistent-member") is None


def test_member_resolution_returns_none_when_team_is_none():
    """An unresolvable team must fail closed (None), not silently permit
    everything -- the caller in delegate_structured_task treats None as
    REJECT when required_capabilities is populated."""
    assert _member_capability_tools(None, "researcher") is None
