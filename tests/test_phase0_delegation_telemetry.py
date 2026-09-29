"""Phase Z30 (2026-09-29) -- phase0 delegation telemetry recognized only the two
obsolete agno-native tool names (`delegate_task_to_member`, `delegate_task_to_members`,
matched via `function_name.startswith("delegate_task_to_member")` in
_tool_interception_hook) and never the current production delegation tool,
`delegate_structured_task`. Live-confirmed during Z29's own validation run: two real
`delegate_structured_task` calls executed, but the run's final phase0 summary reported
`"delegations": 0`.

Root cause: the `.startswith()` check itself never matches "delegate_structured_task"
(different prefix entirely), and even if it did, the call immediately below it reads
`(args or {}).get("task")` -- a key that does not exist on delegate_structured_task's
args (member_id/target/objective/evidence_required/completion_criteria). A name-only
fix would still pass empty text into Phase0Run.record_delegation.

Driven through the REAL _tool_interception_hook, not a reimplementation of its
predicate -- same discipline test_team_mechverify.py and
test_phase0_write_action_telemetry.py already established for this file. `_emit` (the
best-effort stdout+JSONL side effect inside record_delegation) is monkeypatched to a
no-op so these tests never touch the filesystem.
"""
import pytest

from swarm import phase0
from swarm.phase0 import Phase0Run
from swarm.team import _make_tool_interception_hook


class _Agent:
    def __init__(self, name="Researcher"):
        self.name = name


class _Team:
    """Same shape test_team_mechverify.py's _Team already uses: production sets
    _phase0 on the team object AFTER _build_team() returns."""

    def __init__(self, phase0_run=None):
        self._phase0 = phase0_run


@pytest.fixture(autouse=True)
def _no_emit(monkeypatch):
    monkeypatch.setattr(phase0, "_emit", lambda event: None)


def _real_phase0_run():
    return Phase0Run(project_id="ekam", session_id="sess-1", team_name="engineering",
                     read_only=True)


STRUCTURED_ARGS = {
    "member_id": "researcher",
    "target": "API/business-service/router/business_api.py",
    "objective": "list its endpoints",
    "evidence_required": "the endpoint list",
    "completion_criteria": "all endpoints found",
}


# ── Case A -- structured delegation is counted ───────────────────────────────────

@pytest.mark.asyncio
async def test_structured_delegation_is_recorded_in_phase0_telemetry():
    run = _real_phase0_run()
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member finished the subtask"

    await hook("delegate_structured_task", fake_delegate, dict(STRUCTURED_ARGS),
              agent=None, team=_Team(run))

    assert len(run.delegations) == 1


@pytest.mark.asyncio
async def test_two_structured_delegations_both_recorded():
    run = _real_phase0_run()
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "done"

    await hook("delegate_structured_task", fake_delegate,
              {**STRUCTURED_ARGS, "target": "a.py"}, agent=None, team=_Team(run))
    await hook("delegate_structured_task", fake_delegate,
              {**STRUCTURED_ARGS, "member_id": "reviewer", "target": "b.py"},
              agent=None, team=_Team(run))

    assert len(run.delegations) == 2


# ── Case D -- exact telemetry surface: the recorded fields are real, not blank ───

@pytest.mark.asyncio
async def test_structured_delegation_record_carries_the_real_member_and_target():
    run = _real_phase0_run()
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member finished the subtask"

    await hook("delegate_structured_task", fake_delegate, dict(STRUCTURED_ARGS),
              agent=None, team=_Team(run))

    record = run.delegations[0]
    assert record["member"] == "researcher"
    # The real `target` field is used directly -- not derived by guessing a path out
    # of free-form text, and not left as the "none" source _raw_audit_target would
    # produce for a call it cannot parse.
    assert record["target"] == "API/business-service/router/business_api.py"
    assert record["target_source"] == "audit"
    assert record["member_input_chars"] > 0  # objective text was recorded, not blank


# ── Case B -- a non-delegation tool call is never counted ────────────────────────

@pytest.mark.asyncio
async def test_non_delegation_tool_call_is_not_recorded_as_a_delegation():
    run = _real_phase0_run()
    hook = _make_tool_interception_hook()

    async def fake_read(**kwargs):
        return "file bytes"

    await hook("get_file_content", fake_read, {"relative_path": "x.py"},
              agent=_Agent("Researcher"), team=_Team(run))

    assert run.delegations == []


# ── Case C -- the intentionally-supported legacy forms remain intact ─────────────
# Confirmed from the code (not assumed from naming): the original
# `function_name.startswith("delegate_task_to_member")` check was a deliberate
# single-condition match for BOTH agno-native forms -- "delegate_task_to_member"
# (singular) and "delegate_task_to_members" (plural broadcast), since the plural
# name literally has the singular name as a prefix. Both remain real, still-
# registered agno tool names, so both stay supported.

@pytest.mark.asyncio
async def test_legacy_singular_delegate_task_to_member_is_still_recorded():
    run = _real_phase0_run()
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "member finished the subtask"

    await hook("delegate_task_to_member", fake_delegate,
              {"member_id": "researcher", "task": "go look"},
              agent=None, team=_Team(run))

    assert len(run.delegations) == 1
    assert run.delegations[0]["member"] == "researcher"


@pytest.mark.asyncio
async def test_legacy_plural_broadcast_delegate_task_to_members_is_still_recorded():
    run = _real_phase0_run()
    hook = _make_tool_interception_hook()

    async def fake_broadcast(**kwargs):
        return "broadcast done"

    await hook("delegate_task_to_members", fake_broadcast, {"task": "re-verify everything"},
              agent=None, team=_Team(run))

    assert len(run.delegations) == 1


@pytest.mark.asyncio
async def test_no_phase0_run_on_the_team_never_raises():
    """A team built without telemetry attached (or a test double) must not crash --
    _p0 is None and the whole block is skipped, same as before this fix."""
    hook = _make_tool_interception_hook()

    async def fake_delegate(**kwargs):
        return "done"

    out = await hook("delegate_structured_task", fake_delegate, dict(STRUCTURED_ARGS),
                     agent=None, team=_Team(None))

    assert out == "done"
