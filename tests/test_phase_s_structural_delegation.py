"""Phase S (2026-09-24) + Phase S.2 (2026-09-25): structural delegation contract.

Phase R/R.1 established that telling the Coordinator, in prose, to PHRASE
delegations with TARGET/OBJECTIVE/EVIDENCE REQUIRED/COMPLETION CRITERIA did not
change what the model actually generated for a well-rehearsed task -- the
delegation text was byte-identical to the pre-instruction baseline. The
instruction reached the Coordinator's effective prompt; the model's completion
for that one slot was simply unaffected.

Phase S moved the same four fields out of prose into a structured tool-call
schema instead: `delegate_structured_task(member_id, target, objective,
evidence_required, completion_criteria)`, replacing agno's own free-form
`delegate_task_to_member(member_id, task)`, while reusing agno's real
underlying delegation engine (session storage, member execution, results
storage) completely unchanged.

Phase S's FIRST interception attempt overrode the instance method
`Team._get_delegate_task_function` on a `_StructuredDelegationTeam` subclass.
The Phase S.1 live run proved this was dead code in production: agno's real
tool-assembly path (`agno/team/_tools.py`'s `_determine_tools_for_model`,
called from many sites in `agno/team/_run.py`) does a function-body-LOCAL
`from agno.team._default_tools import _get_delegate_task_function` on every
call and invokes it as a free function -- `_get_delegate_task_function(team,
...)`, never `team._get_delegate_task_function(...)`. A subclass method
override is only reachable through polymorphic dispatch, which that call shape
never uses. Zero `delegate_structured_task` calls occurred in the Phase S.1
live run; every delegation used the old free-form schema.

Phase S.2's fix moves the interception to the ONE place both of agno's own
call sites actually resolve at call time: `agno.team._default_tools.
_get_delegate_task_function` itself (the module attribute). Both
`Team._get_delegate_task_function` (module-attribute access) and
`_tools.py`'s `_determine_tools_for_model` (a fresh per-call `from ... import`)
re-resolve this name from `_default_tools`'s live namespace on every call,
so replacing the attribute reaches both call shapes with one patch, scoped by
`isinstance(team, _StructuredDelegationTeam)`.

These tests exercise the REAL production call shape directly:
`agno.team._default_tools._get_delegate_task_function(team, ...)`, called as a
free function exactly as `_tools.py` calls it -- not `team._get_delegate_task_
function()`, which is precisely the call shape Phase S's original tests used
and which turned out not to prove anything about production. Only the
underlying agno delegation engine (`_ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION`)
is stubbed; everything downstream of that (isinstance routing, validation,
canonical task construction, hashing, pass-through) is the real Phase S/S.2
code.
"""
from unittest.mock import patch

import pytest

import swarm.team as team_mod
from swarm.team import (
    _STRUCTURED_DELEGATION_REQUIRED_FIELDS, _StructuredDelegationTeam,
    _build_canonical_researcher_task, _build_team,
)

import agno.team._default_tools as agno_default_tools


class _FakeFunction:
    """Minimal stand-in for agno's real Function object -- the wrapper only
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


def _fake_agno_original(calls):
    """Stands in for the TRUE, unwrapped agno `_get_delegate_task_function` --
    the thing `_ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION` is patched to during
    these tests. Matches its real call shape: `team` first, then a pile of
    required kwargs (run_response, run_context, session, team_run_context) this
    fake ignores, same as the real one would use to build agno's stock
    `delegate_task_to_member` Function."""

    def original(team, **kwargs):
        return _FakeFunction(_fake_original_entrypoint(calls))

    return original


def _call_production_path(team, calls=None):
    """Calls `agno.team._default_tools._get_delegate_task_function(team, ...)`
    -- the EXACT call shape agno's real `_tools.py:276` and `team.py:1455` both
    use (free-function-style, `team` as the first positional/keyword arg,
    required kwargs supplied) -- with the underlying agno engine stubbed via
    `_ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION`. This is the production call
    shape Phase S.1 found was never actually exercised by the old tests."""
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


def _structured_team():
    return _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate", mcp_list=[], instructions=[],
    )


# ── Test H: the constructed team is the marker type, and the module patch is live ──

def test_build_team_constructs_the_structured_delegation_team():
    team = _structured_team()
    assert type(team) is _StructuredDelegationTeam


def test_default_tools_get_delegate_task_function_is_actually_patched():
    # THE central Phase S.2 proof: the live module attribute agno's own code
    # calls (agno.team._default_tools._get_delegate_task_function) really has
    # been replaced by our wrapper -- not merely that a subclass CAN be
    # overridden, which is what Phase S's original (insufficient) test proved.
    assert (agno_default_tools._get_delegate_task_function
            is team_mod._patched_agno_get_delegate_task_function)
    assert getattr(agno_default_tools._get_delegate_task_function,
                   team_mod._STRUCTURED_DELEGATION_PATCH_MARKER, False) is True


# ── Test 4 (spec Section 4): the REAL production call shape reaches the structured tool ──

def test_production_call_shape_returns_structured_tool_for_structured_team():
    """Reproduces agno/team/_tools.py:276's exact call:
    `_get_delegate_task_function(team, run_response=..., run_context=..., session=...,
    team_run_context=...)` -- called as a free function on the module attribute,
    never as `team._get_delegate_task_function(...)`. This is the test that would
    have caught the Phase S.1 defect: if the interception point regresses back to
    an instance-method-only override, `agno_default_tools._get_delegate_task_function`
    stays agno's true original, this call returns agno's stock Function (name
    delegate_task_to_member, no five-field schema), and this test fails."""
    team = _structured_team()
    new_function, calls = _call_production_path(team)
    assert new_function.name == "delegate_structured_task"
    assert calls == []  # not invoked yet, only built


def test_production_call_shape_schema_has_five_required_fields_no_task_param():
    team = _structured_team()
    new_function, _ = _call_production_path(team)
    param_names = set(new_function.parameters.get("properties", {}).keys())
    assert param_names == {
        "member_id", "target", "objective", "evidence_required", "completion_criteria",
    }
    assert "task" not in param_names
    required = set(new_function.parameters.get("required", []))
    for field in ("member_id",) + _STRUCTURED_DELEGATION_REQUIRED_FIELDS:
        assert field in required, f"{field!r} must be a required parameter"


# ── Test 7 (spec Section 7): ordinary/non-structured Team is unaffected ──

def test_ordinary_team_through_the_same_production_call_shape_is_unaffected():
    """Proves the isinstance scoping: an object that is NOT a
    _StructuredDelegationTeam, run through the exact same patched module
    function, gets agno's ORIGINAL tool back untouched -- the wrapper must not
    globally replace agno behavior for every Team instance, only this
    codebase's own Coordinator team."""

    class _OrdinaryTeam:
        pass

    ordinary = _OrdinaryTeam()
    calls = []
    with patch.object(
        team_mod, "_ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION", _fake_agno_original(calls),
    ):
        result = agno_default_tools._get_delegate_task_function(
            ordinary, run_response=None, run_context=None, session=None,
            team_run_context={},
        )
    # The fake original's own Function is returned completely unwrapped --
    # not a delegate_structured_task, no five-field schema grafted on.
    assert isinstance(result, _FakeFunction)
    assert not hasattr(result, "parameters")


# ── Test 8 (spec Section 8): installation lifecycle safety ──

def test_installation_is_idempotent_and_never_stacks_a_second_wrapper():
    before_wrapper = agno_default_tools._get_delegate_task_function
    before_original = team_mod._ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION

    team_mod._install_structured_delegation_interception()
    team_mod._install_structured_delegation_interception()
    team_mod._install_structured_delegation_interception()

    assert agno_default_tools._get_delegate_task_function is before_wrapper
    assert team_mod._ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION is before_original


def test_reinstalling_does_not_recapture_an_already_wrapped_function_as_original():
    # If installation ever re-ran after the patch was already live and failed to
    # recognize its own marker, it would capture the WRAPPER itself as
    # "_ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION", and every subsequent call
    # would double-wrap. The marker check must prevent this.
    team_mod._install_structured_delegation_interception()
    assert team_mod._ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION is not (
        team_mod._patched_agno_get_delegate_task_function)
    assert not getattr(team_mod._ORIGINAL_AGNO_GET_DELEGATE_TASK_FUNCTION,
                       team_mod._STRUCTURED_DELEGATION_PATCH_MARKER, False)


# ── Test A: structured intent accepted -- schema has the five separate fields ──
# (kept, now sourced through the production call path above rather than direct
# method invocation -- test_production_call_shape_schema_has_five_required_fields_no_task_param)


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
# (now exercised through the production call path's resulting Function)

@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", _STRUCTURED_DELEGATION_REQUIRED_FIELDS)
async def test_missing_required_field_is_rejected_cleanly(missing_field):
    team = _structured_team()
    new_function, calls = _call_production_path(team)
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
    team = _structured_team()
    new_function, calls = _call_production_path(team)
    chunks = await _run_entrypoint(
        new_function, member_id="researcher", target="a.py", objective="find X",
        evidence_required="   ", completion_criteria="done",
    )
    assert chunks[0].startswith("DELEGATION REJECTED")
    assert calls == []


# ── Test E / Section 6: with all fields present, through the production call
#    path, the existing delegation engine runs and receives the canonical task ──

@pytest.mark.asyncio
async def test_complete_delegation_reaches_the_existing_delegation_engine():
    team = _structured_team()
    new_function, calls = _call_production_path(team)

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
    team = _structured_team()
    new_function, _ = _call_production_path(team)
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
