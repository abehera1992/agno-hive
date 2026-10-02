"""Phase B (AgnoHive JEV-like Control Box consolidation, 2026-10-02): the
first, additive-only run-scoped control object -- objective -> action ->
result -> progress, for member-level tool calls. No existing control path
(duplicate-delegation gate, tool-budget guard, repetition detector,
EvidenceLedger, Claim layer, _verified_answer's retry budget) is touched or
replaced by this phase; these tests exercise the new, standalone pieces
only: MemberAction, MemberControlState, _MemberControlBox,
_get_member_control_box, _start_member_objective, _record_member_action,
_make_member_control_box_hook, _control_box_state_fingerprint.

The critical regression (Section 18 of the Control Box phase spec) is
test_critical_regression_6331f33a146f_identical_calls_are_no_progress_not_
completion below: it replays the real shape of run 6331f33a146f (58
identical update_session_state calls, zero state growth) and proves the
box correctly classifies every repeat after the first as NO PROGRESS,
while completion_status is never touched by this phase at all (stays
"unevaluated" throughout) -- Phase B detects, it does not decide.
"""
import asyncio

import swarm.team as team_mod
from swarm.team import (
    MemberAction,
    MemberControlState,
    _control_box_state_fingerprint,
    _fingerprint_args,
    _get_member_control_box,
    _make_member_control_box_hook,
    _MemberControlBox,
    _record_member_action,
    _start_member_objective,
)


def _run(coro):
    return asyncio.run(coro)


class _Team:
    """Bare stand-in, same convention as test_comparison_reconciliation.py's
    own _Team -- nothing in this phase's code touches any attribute beyond
    getattr/setattr on whatever object is passed as `team`."""


def _start(team, member_id="researcher", delegation_key="abc123",
           objective="find gaps", evidence_required="a list of gaps",
           completion_criteria="all gaps identified"):
    _start_member_objective(team, member_id, delegation_key, objective,
                             evidence_required, completion_criteria)


# ── MemberControlState / _MemberControlBox basics ───────────────────────────


def test_objective_fields_captured_verbatim():
    """_start_member_objective stores objective/evidence_required/
    completion_criteria exactly as given -- never from an arbitrary
    model-chosen session-state key -- and completion_status starts
    'unevaluated', never inferred."""
    team = _Team()
    _start(team, objective="X", evidence_required="Y", completion_criteria="Z")
    state = _get_member_control_box(team).get("researcher", "abc123")
    assert state.objective == "X"
    assert state.evidence_required == "Y"
    assert state.completion_criteria == "Z"
    assert state.completion_status == "unevaluated"
    assert state.total_actions == 0
    assert state.no_progress_streak == 0


def test_cross_run_isolation():
    """Two independent team objects never share control-box state -- same
    isolation guarantee as _EvidenceLedger/_ClaimStore."""
    team_a = _Team()
    team_b = _Team()
    _start(team_a, member_id="researcher", delegation_key="a1")
    assert _get_member_control_box(team_b).list() == []
    assert _get_member_control_box(team_b).get("researcher", "a1") is None
    assert _get_member_control_box(team_a).get("researcher", "a1") is not None


def test_latest_for_member_finds_most_recent_delegation():
    team = _Team()
    _start(team, member_id="researcher", delegation_key="first")
    _start(team, member_id="researcher", delegation_key="second")
    latest = _get_member_control_box(team).latest_for_member("researcher")
    assert latest.delegation_key == "second"


# ── Progress semantics (Section 6: never "call count increased") ───────────


def test_first_action_is_always_progress():
    """The very first observed action for a delegation has nothing prior to
    compare against -- it is progress by definition, not a repeat."""
    team = _Team()
    _start(team)
    fp = (0, 0, 0, 0)
    _record_member_action(team, "researcher", "get_file_content",
                          {"relative_path": "a.py"}, fp, fp)
    state = _get_member_control_box(team).get("researcher", "abc123")
    assert state.total_actions == 1
    assert state.no_progress_streak == 0
    assert state.current_action.tool_name == "get_file_content"


def test_different_args_is_always_progress_even_with_no_state_growth():
    """Section 6 + Section 20 rule 4/6: a repeated LEGITIMATE poll (same
    tool, genuinely different arguments each time) must remain legal and
    must never be flagged as no-progress merely for repeating the tool
    name. State growth is irrelevant here -- the action itself differs."""
    team = _Team()
    _start(team)
    fp = (0, 0, 0, 0)  # state never grows across any of these calls
    for i in range(5):
        _record_member_action(team, "researcher", "get_file_content",
                              {"relative_path": f"file_{i}.py"}, fp, fp)
    state = _get_member_control_box(team).get("researcher", "abc123")
    assert state.total_actions == 5
    assert state.no_progress_streak == 0
    assert state.max_no_progress_streak == 0


def test_identical_call_with_state_growth_is_progress():
    """An EXACT repeat (same tool, same args) is still progress if the
    run's own existing counters actually grew as a result -- e.g. a
    redundant-looking call that nonetheless advanced real state."""
    team = _Team()
    _start(team)
    args = {"relative_path": "a.py"}
    _record_member_action(team, "researcher", "get_file_content", args,
                          (0, 0, 0, 0), (0, 0, 1, 0))
    _record_member_action(team, "researcher", "get_file_content", args,
                          (0, 0, 1, 0), (0, 0, 2, 0))
    state = _get_member_control_box(team).get("researcher", "abc123")
    assert state.total_actions == 2
    assert state.no_progress_streak == 0


def test_identical_call_with_no_state_growth_is_no_progress():
    """The exact 6331f33a146f shape, minimal case: same tool, same args,
    counters never move -> NO PROGRESS, streak climbs."""
    team = _Team()
    _start(team)
    args = {"session_state_updates": {"task_completed": "same string"}}
    flat = (3, 1, 2, 0)
    _record_member_action(team, "researcher", "update_session_state", args,
                          flat, flat)  # first call: still "progress" (nothing prior)
    _record_member_action(team, "researcher", "update_session_state", args,
                          flat, flat)  # second: identical repeat, no growth
    _record_member_action(team, "researcher", "update_session_state", args,
                          flat, flat)  # third: identical repeat, no growth
    state = _get_member_control_box(team).get("researcher", "abc123")
    assert state.total_actions == 3
    assert state.no_progress_streak == 2
    assert state.max_no_progress_streak == 2


def test_repetition_is_not_completion():
    """However long the no-progress streak grows, completion_status is
    never touched by this phase -- Phase B detects, it does not decide.
    (Section 12: 'Do not let repetition detection fabricate completion.')"""
    team = _Team()
    _start(team)
    args = {"session_state_updates": {"task_completed": "x"}}
    flat = (0, 0, 0, 0)
    for _ in range(58):
        _record_member_action(team, "researcher", "update_session_state",
                              args, flat, flat)
    state = _get_member_control_box(team).get("researcher", "abc123")
    assert state.completion_status == "unevaluated"


def test_critical_regression_6331f33a146f_identical_calls_are_no_progress_not_completion():
    """The actual production shape from run 6331f33a146f: 58 identical
    update_session_state({'task_completed': '<fixed string>'}) calls, state
    counters never moving (no new file read, no new evidence, no new claim,
    no new member result -- confirmed from the preserved journal: read_log
    held exactly one file the entire loop). The box must recognize every
    call after the first as NO PROGRESS, while NEVER treating the streak as
    completion and NEVER refusing/blocking any of the 58 calls (no
    duplicate-call kill switch -- Section 10's explicit prohibition)."""
    team = _Team()
    _start(team, member_id="researcher", delegation_key="run_6331f33a146f",
           objective="Identify any gaps between the two sides.",
           evidence_required="A list of endpoints with no corresponding hook.",
           completion_criteria="All gaps have been identified and listed.")
    args = {"session_state_updates": {
        "task_completed": "Endpoints listed: GET /vouchers, GET /vouchers/"
                          "{voucher_id}, POST /vouchers, PUT /vouchers/"
                          "{voucher_id}/post, PUT /vouchers/{voucher_id}/cancel, "
                          "POST /vouchers/grn/{po_id}, POST /vouchers/credit-note/"
                          "{invoice_id}, POST /vouchers/stock-adjustment, "
                          "POST /vouchers/stock-transfer."}}
    flat = (0, 0, 1, 0)  # one file already read; never changes across the loop
    for _ in range(58):
        _record_member_action(team, "researcher", "update_session_state",
                              args, flat, flat)
    state = _get_member_control_box(team).get("researcher", "run_6331f33a146f")
    assert state.total_actions == 58
    # First call had nothing prior -> progress; the other 57 are exact
    # repeats with zero state growth -> no progress.
    assert state.no_progress_streak == 57
    assert state.max_no_progress_streak == 57
    assert state.completion_status == "unevaluated"
    assert state.objective == "Identify any gaps between the two sides."


# ── Hook behavior: purely observational, never blocks/alters ───────────────


def test_hook_returns_real_result_unchanged_on_progress():
    team = _Team()
    _start(team)
    hook = _make_member_control_box_hook(role="researcher")

    async def real_tool(**kwargs):
        return "real result unaffected"

    result = _run(hook("get_file_content", real_tool, {"relative_path": "a.py"},
                        agent=None, team=team, run_context=None))
    assert result == "real result unaffected"


def test_hook_returns_real_result_unchanged_on_no_progress():
    """Even when the hook classifies a call as NO PROGRESS, it must still
    call through and return the tool's real, unmodified result -- the hook
    has no authority to block, stub, or decorate anything in this phase."""
    team = _Team()
    _start(team)
    hook = _make_member_control_box_hook(role="researcher")
    calls = {"n": 0}

    async def real_tool(**kwargs):
        calls["n"] += 1
        return f"result #{calls['n']}"

    args = {"session_state_updates": {"task_completed": "same"}}
    r1 = _run(hook("update_session_state", real_tool, args,
                    agent=None, team=team, run_context=None))
    r2 = _run(hook("update_session_state", real_tool, args,
                    agent=None, team=team, run_context=None))
    assert r1 == "result #1"
    assert r2 == "result #2"
    assert calls["n"] == 2  # both calls genuinely executed -- nothing was skipped
    state = _get_member_control_box(team).get("researcher", "abc123")
    assert state.no_progress_streak == 1


def test_hook_is_a_noop_when_no_delegation_was_started():
    """A tool call from a member with no started delegation (e.g. the
    Coordinator's own direct tool calls, which never go through
    delegate_structured_task) must not raise or affect the result -- the
    box silently has nothing to record."""
    team = _Team()
    hook = _make_member_control_box_hook(role="Coordinator")

    async def real_tool(**kwargs):
        return "coordinator result"

    result = _run(hook("some_tool", real_tool, {}, agent=None, team=team,
                        run_context=None))
    assert result == "coordinator result"
    assert _get_member_control_box(team).list() == []


def test_hook_handles_team_none_without_raising():
    """agno's own _build_hook_args only supplies `team` when the hook
    signature names it and agno can resolve it -- defensive, never assume
    it is always present."""
    hook = _make_member_control_box_hook(role="researcher")

    async def real_tool(**kwargs):
        return "ok"

    result = _run(hook("get_file_content", real_tool, {"relative_path": "a.py"},
                        agent=None, team=None, run_context=None))
    assert result == "ok"


# ── Fingerprint helpers ──────────────────────────────────────────────────────


def test_fingerprint_args_is_order_independent():
    a = _fingerprint_args({"b": 2, "a": 1})
    b = _fingerprint_args({"a": 1, "b": 2})
    assert a == b


def test_fingerprint_args_differs_for_different_values():
    a = _fingerprint_args({"relative_path": "a.py"})
    b = _fingerprint_args({"relative_path": "b.py"})
    assert a != b


def test_control_box_state_fingerprint_reads_existing_counters_only():
    """Must never lazily create an EvidenceLedger/ClaimStore for a team that
    never used comparison -- only read what already exists (Coder/Reviewer/
    Executor have no reason to ever get one just from being observed)."""
    team = _Team()
    fp = _control_box_state_fingerprint(team)
    assert fp == (0, 0, 0, 0)
    assert not hasattr(team, "_evidence_ledger")
    assert not hasattr(team, "_claim_store")


# ── Phase B.1 regression: display-cased vs. delegation-cased member_id ─────
#
# Live root cause (2026-10-02): delegate_structured_task's own member_id
# argument arrives lowercase ("researcher" -- the Coordinator's own tool-call
# argument, confirmed verbatim in the production journal), while the
# tool-call observer hook's own `who` resolves from agent.name/role, which
# is display-cased ("Researcher"). Before the fix, _start_member_objective
# stored the MemberControlState under the lowercase key while the hook
# looked it up under the capitalized one -- latest_for_member() never
# matched, and _record_member_action's own `if state is None: return` fired
# silently on EVERY member tool call, in every live trial, with no
# exception and no telemetry. These tests reproduce that exact mismatch
# directly (bypassing the hook, which now normalizes `who` itself via
# _member_id()) to prove the stores/lookups are consistent regardless of
# which casing a caller uses on either side.


def test_phase_b1_regression_display_cased_lookup_matches_lowercase_start():
    """The exact live mismatch: _start_member_objective is called with the
    Coordinator's own lowercase delegation argument, but the lookup (as the
    fixed hook now does internally via _member_id()) uses the display-cased
    agent name. Both must resolve to the SAME stored state."""
    team = _Team()
    _start_member_objective(
        team, "researcher", "delegation-key-1", "find gaps",
        "a list of gaps", "all gaps identified")
    # Simulates what the FIXED hook does: normalize the display name before
    # looking up, exactly as _member_id("Researcher") would.
    normalized_who = team_mod._member_id("Researcher")
    assert normalized_who == "researcher"
    state = _get_member_control_box(team).get("researcher", "delegation-key-1")
    assert state is not None
    assert _get_member_control_box(team).latest_for_member(normalized_who) is state


def test_phase_b1_regression_action_recorded_fires_with_display_cased_role(capsys):
    """End-to-end through the real hook: role bound at hook-construction
    time is display-cased ("Researcher", matching agent.name/spec.name in
    production), while the delegation was started with the Coordinator's
    own lowercase argument ("researcher") -- CONTROL_BOX_ACTION_RECORDED
    must fire, proving the hook's internal _member_id() normalization
    closes the exact live gap."""
    team = _Team()
    _start_member_objective(
        team, "researcher", "delegation-key-1", "find gaps",
        "a list of gaps", "all gaps identified")
    hook = _make_member_control_box_hook(role="Researcher")  # display-cased, like agent.name

    async def real_tool(**kwargs):
        return "real result"

    result = _run(hook("get_file_content", real_tool, {"relative_path": "a.py"},
                        agent=None, team=team, run_context=None))
    assert result == "real result"
    out = capsys.readouterr().out
    assert "CONTROL_BOX_ACTION_RECORDED" in out
    state = _get_member_control_box(team).get("researcher", "delegation-key-1")
    assert state.total_actions == 1
