"""Phase S (2026-09-24): structural delegation contract.

Phase R/R.1 established that telling the Coordinator, in prose, to PHRASE
delegations with TARGET/OBJECTIVE/EVIDENCE REQUIRED/COMPLETION CRITERIA did not
change what the model actually generated for a well-rehearsed task -- the
delegation text was byte-identical to the pre-instruction baseline. The
instruction reached the Coordinator's effective prompt; the model's completion
for that one slot was simply unaffected.

Phase S moves the same four fields out of prose into a structured tool-call
schema instead: `_StructuredDelegationTeam` overrides agno's own
`Team._get_delegate_task_function` (the single choke point through which agno
builds the Coordinator's delegation tool every run -- confirmed by reading
agno/team/_tools.py:276, the only call site in the installed package) to
replace the free-form `delegate_task_to_member(member_id, task)` schema with
`delegate_structured_task(member_id, target, objective, evidence_required,
completion_criteria)`, while reusing agno's real underlying delegation engine
(session storage, member execution, results storage) completely unchanged by
calling straight through to the original entrypoint with a synthesized `task`
string.

These tests exercise the override in isolation, stubbing only agno's own
`Team._get_delegate_task_function` (the parent method) with a controllable
fake `Function`-like object -- everything downstream of that (validation,
canonical task construction, hashing, pass-through) is the real Phase S code.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agno.team import Team
from swarm.team import (
    _STRUCTURED_DELEGATION_REQUIRED_FIELDS, _StructuredDelegationTeam,
    _build_canonical_researcher_task, _build_team,
)


class _FakeFunction:
    """Minimal stand-in for agno's real Function object -- the override only
    ever reads .entrypoint/.stop_after_tool_call/.show_result off it."""

    def __init__(self, entrypoint):
        self.entrypoint = entrypoint
        self.stop_after_tool_call = False
        self.show_result = True


def _fake_original_entrypoint(calls):
    """Records every (member_id, task) it's called with; yields one fake
    streaming chunk then a final result string, matching agno's own
    adelegate_task_to_member's async-generator shape."""

    async def entrypoint(member_id, task):
        calls.append({"member_id": member_id, "task": task})
        yield f"[delegating to {member_id}]"
        yield f"Agent {member_id}: fake result"

    return entrypoint


def _get_structured_tool(calls=None):
    """Builds a real _StructuredDelegationTeam instance (via the same
    lightweight _build_team() path the existing test suite already uses --
    agent_specs=None triggers the Coder+Reviewer fallback, no network/DB
    needed) and, with Team._get_delegate_task_function stubbed, returns the
    NEW Function object Phase S's override produces."""
    if calls is None:
        calls = []
    team = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate", mcp_list=[], instructions=[],
    )
    with patch.object(
        Team, "_get_delegate_task_function",
        return_value=_FakeFunction(_fake_original_entrypoint(calls)),
    ):
        new_function = team._get_delegate_task_function()
    return team, new_function, calls


async def _run_entrypoint(new_function, **kwargs):
    chunks = []
    async for item in new_function.entrypoint(**kwargs):
        chunks.append(item)
    return chunks


# ── Test H: the constructed team uses the structural override, not plain Team ──

def test_build_team_constructs_the_structured_delegation_team():
    team = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate", mcp_list=[], instructions=[],
    )
    assert type(team) is _StructuredDelegationTeam
    # Confirms the override is genuinely bound on this class, not silently
    # shadowed/absent -- if this method resolved to Team's own, agno would
    # still be building the old free-form schema regardless of the class name.
    assert (_StructuredDelegationTeam._get_delegate_task_function
            is not Team._get_delegate_task_function)


# ── Test A: structured intent accepted -- schema has the five separate fields ──

def test_structured_tool_schema_has_the_five_separate_fields():
    _, new_function, _ = _get_structured_tool()
    assert new_function.name == "delegate_structured_task"
    param_names = set(new_function.parameters.get("properties", {}).keys())
    assert param_names == {
        "member_id", "target", "objective", "evidence_required", "completion_criteria",
    }
    required = set(new_function.parameters.get("required", []))
    for field in ("member_id",) + _STRUCTURED_DELEGATION_REQUIRED_FIELDS:
        assert field in required, f"{field!r} must be a required parameter"


# ── Test B: deterministic canonical task construction ────────────────────────

def test_canonical_task_construction_is_deterministic():
    args = ("a.py", "find X", "exact quote", "evidence found")
    assert _build_canonical_researcher_task(*args) == _build_canonical_researcher_task(*args)


def test_canonical_task_construction_never_calls_a_model():
    # Pure function: same process, no I/O, effectively instantaneous -- a real
    # model call would be orders of magnitude slower than this test's own budget.
    import time
    t0 = time.monotonic()
    _build_canonical_researcher_task("a.py", "find X", "quote", "criteria")
    assert time.monotonic() - t0 < 0.01


# ── Test C: every field survives into the canonical task verbatim ────────────

def test_canonical_task_preserves_every_field_verbatim():
    task = _build_canonical_researcher_task(
        target="UNIQUE_TARGET_MARKER",
        objective="UNIQUE_OBJECTIVE_MARKER",
        evidence_required="UNIQUE_EVIDENCE_MARKER",
        completion_criteria="UNIQUE_CRITERIA_MARKER",
    )
    for marker in ("UNIQUE_TARGET_MARKER", "UNIQUE_OBJECTIVE_MARKER",
                   "UNIQUE_EVIDENCE_MARKER", "UNIQUE_CRITERIA_MARKER"):
        assert marker in task


# ── Test D: missing required field fails cleanly at the boundary ─────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", _STRUCTURED_DELEGATION_REQUIRED_FIELDS)
async def test_missing_required_field_is_rejected_cleanly(missing_field):
    calls = []
    _, new_function, calls = _get_structured_tool(calls)
    kwargs = {
        "member_id": "researcher", "target": "a.py", "objective": "find X",
        "evidence_required": "a quote", "completion_criteria": "quote obtained",
    }
    kwargs[missing_field] = ""  # blank, not missing entirely -- same failure mode

    chunks = await _run_entrypoint(new_function, **kwargs)

    assert len(chunks) == 1
    assert chunks[0].startswith("DELEGATION REJECTED")
    assert missing_field in chunks[0]
    # The real delegation engine must never be reached for a rejected call.
    assert calls == []


@pytest.mark.asyncio
async def test_whitespace_only_field_is_also_rejected():
    calls = []
    _, new_function, calls = _get_structured_tool(calls)
    chunks = await _run_entrypoint(
        new_function, member_id="researcher", target="a.py", objective="find X",
        evidence_required="   ", completion_criteria="done",
    )
    assert chunks[0].startswith("DELEGATION REJECTED")
    assert calls == []


# ── Test E: with all fields present, the existing delegation engine runs ─────

@pytest.mark.asyncio
async def test_complete_delegation_reaches_the_existing_delegation_engine():
    calls = []
    _, new_function, calls = _get_structured_tool(calls)

    chunks = await _run_entrypoint(
        new_function, member_id="researcher", target="a.py",
        objective="find X", evidence_required="a quote",
        completion_criteria="quote obtained",
    )

    assert len(calls) == 1
    assert calls[0]["member_id"] == "researcher"
    canonical = calls[0]["task"]
    assert canonical == _build_canonical_researcher_task(
        target="a.py", objective="find X", evidence_required="a quote",
        completion_criteria="quote obtained",
    )
    # The fake entrypoint's own streamed chunks passed through unchanged.
    assert chunks == ["[delegating to researcher]", "Agent researcher: fake result"]


@pytest.mark.asyncio
async def test_instrumentation_logs_the_delegation_contract_and_canonical_hash(capsys):
    _, new_function, _ = _get_structured_tool()
    await _run_entrypoint(
        new_function, member_id="researcher", target="a.py",
        objective="find X", evidence_required="a quote",
        completion_criteria="quote obtained",
    )
    out = capsys.readouterr().out
    assert "delegate_structured_task" in out
    assert "target='a.py'" in out
    assert "canonical_task_hash=" in out
    assert "canonical_task_length=" in out


# ── Test F: the runtime never constructs pseudo-tool syntax ──────────────────

def test_canonical_task_builder_never_emits_pseudo_tool_syntax():
    task = _build_canonical_researcher_task("a.py", "find X", "quote", "done")
    for forbidden in ("<function_call>", "[TOOL_CALLS]", "<tool_call>"):
        assert forbidden not in task
