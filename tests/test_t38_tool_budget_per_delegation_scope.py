"""T38 Priority B -- the T37 incident's "79 tool calls against a budget of 25" is a
METRICS-DEFINITION MISMATCH, not an enforcement bypass, confirmed by direct source
reading of both sides of the comparison:

  * `tool_call_limit` (agno's own hard cap, config.tool_call_limit /
    _resolve_tool_call_limit) is enforced via `current_function_call_count`, a LOCAL
    variable inside agno's own `response()`/`arun()` method (installed package,
    agno/models/base.py) that starts at 0 and is NEVER persisted on the Agent object
    across invocations -- confirmed by reading the installed source directly. A
    persistent Team's member Agent is invoked fresh once per delegation
    (`delegate_structured_task`), so this ceiling is a PER-DELEGATION budget, not a
    per-run one.

  * `phase0.py`'s own `tool_calls_made` (the `_actions` module-level dict) is
    explicitly "Reset per run by start_run()" -- a RUN-WIDE CUMULATIVE counter across
    every delegation to that member, by its own design and its own docstring.

`Phase0Run.record_member_result` (swarm/phase0.py) compares these two numbers
directly (`calls >= tool_call_limit`) to decide `terminal_reason == "tool_limit"`.
Reproduced here: two delegations, each genuinely WITHIN the 25-call limit on its own
(15 and 12 calls), whose CUMULATIVE total (27) exceeds 25 -- the baseline
misclassifies the SECOND delegation's own terminal_reason as "tool_limit" even
though neither delegation individually exceeded it. This is almost certainly what
actually happened in the T37 incident (session fd5bf842c52c): the Researcher was very
likely delegated to multiple times, each delegation making guesses within its own
25-call allowance, with phase0's cumulative counter (and T37's own report, which
compared the two numbers exactly like the code does) making it look like a single
79-call delegation blew through a 25-call cap.

Fix: a new per-delegation counter (`tool_calls_this_delegation`), reset at the
production delegation-start hook (`_start_member_objective`, team.py), used for the
terminal_reason classification -- the existing, unrelated-use cumulative
`tool_calls_made` is untouched and still reported alongside it.
"""
import pytest

from swarm import phase0
from swarm.phase0 import Phase0Run


@pytest.fixture(autouse=True)
def _clean():
    phase0._reset_actions()
    yield
    phase0._reset_actions()


def _run():
    return Phase0Run(project_id="ekam", session_id="sess-t38", team_name="engineering",
                     read_only=True)


# ── The exact T37-shape misclassification, reproduced and fixed ────────────────────

def test_two_delegations_each_within_budget_are_not_misclassified_as_tool_limit():
    """15 calls in delegation 1, 12 in delegation 2 -- neither exceeds 25 on its
    own, but the cumulative total (27) does. The SECOND delegation's own
    terminal_reason must not read "tool_limit"."""
    run = _run()

    phase0.note_delegation_start("researcher")
    for _ in range(15):
        phase0.note_tool_call("researcher", "search_files", "no matches")
    run.record_member_result(
        member="researcher", content="partial findings from delegation 1",
        read_delta=100, reads=[], elided=False, thin_report=False,
        tool_call_limit=25, forced_text_only=False)
    first = run.member_results[-1]
    assert first["terminal_reason"] != "tool_limit"

    phase0.note_delegation_start("researcher")
    for _ in range(12):
        phase0.note_tool_call("researcher", "search_files", "no matches")
    run.record_member_result(
        member="researcher", content="final findings from delegation 2",
        read_delta=100, reads=[], elided=False, thin_report=False,
        tool_call_limit=25, forced_text_only=False)
    second = run.member_results[-1]
    # THE FIX: delegation 2 made only 12 calls of its own 25-call budget --
    # cumulative (27) must not leak into this delegation's own classification.
    assert second["terminal_reason"] != "tool_limit"
    assert second["tool_calls_this_delegation"] == 12
    # The existing, cumulative counter is UNCHANGED and still reported -- nothing
    # that depends on it elsewhere is disturbed by this fix.
    assert second["tool_calls_made"] == 27


# ── A genuine single-delegation overshoot is still correctly classified ────────────

def test_a_real_single_delegation_overshoot_is_still_classified_tool_limit():
    run = _run()
    phase0.note_delegation_start("researcher")
    for _ in range(26):
        phase0.note_tool_call("researcher", "search_files", "no matches")
    run.record_member_result(
        member="researcher", content="forced partial answer", read_delta=100,
        reads=[], elided=False, thin_report=False, tool_call_limit=25,
        forced_text_only=False)
    result = run.member_results[-1]
    assert result["terminal_reason"] == "tool_limit"
    assert result["tool_calls_this_delegation"] == 26


# ── No delegation-start call at all (e.g. a non-delegated/direct path) is safe ──────

def test_missing_delegation_start_falls_back_to_cumulative_not_a_crash():
    """A caller that never calls note_delegation_start (an older code path, or a
    call made outside delegate_structured_task's own flow) must not crash -- it
    falls back to the cumulative count, exactly the PRE-FIX behavior, so this is
    a pure addition, never a regression for an unintegrated caller."""
    run = _run()
    for _ in range(10):
        phase0.note_tool_call("researcher", "search_files", "no matches")
    run.record_member_result(
        member="researcher", content="x", read_delta=10, reads=[], elided=False,
        thin_report=False, tool_call_limit=25, forced_text_only=False)
    result = run.member_results[-1]
    assert result["tool_calls_this_delegation"] == 10


# ── Per-member isolation: one member's delegation reset does not affect another ────

def test_delegation_reset_is_scoped_to_its_own_member():
    phase0.note_tool_call("coder", "apply_diff", "review_pending: x")
    phase0.note_delegation_start("researcher")
    for _ in range(5):
        phase0.note_tool_call("researcher", "search_files", "no matches")
    assert phase0.action_snapshot("coder")["tool_calls_made"] == 1
    assert phase0.action_snapshot("researcher")["tool_calls_this_delegation"] == 5


# ── note_delegation_start resets ONLY the per-delegation counter ───────────────────

def test_delegation_start_resets_only_the_per_delegation_counter_not_cumulative():
    phase0.note_tool_call("researcher", "search_files", "no matches")
    phase0.note_tool_call("researcher", "search_files", "no matches")
    assert phase0.action_snapshot("researcher")["tool_calls_made"] == 2
    phase0.note_delegation_start("researcher")
    snap = phase0.action_snapshot("researcher")
    assert snap["tool_calls_this_delegation"] == 0
    assert snap["tool_calls_made"] == 2, "cumulative total must survive the reset"


# ── Production wiring: _start_member_objective actually calls note_delegation_start ─

def test_production_delegation_start_hook_resets_the_per_delegation_counter(monkeypatch):
    from swarm.team import _start_member_objective

    class _Team:
        def __init__(self):
            self._member_control_box = None

    phase0.note_tool_call("researcher", "search_files", "no matches")
    phase0.note_tool_call("researcher", "search_files", "no matches")
    assert phase0.action_snapshot("researcher")["tool_calls_this_delegation"] == 2

    _start_member_objective(
        _Team(), member_id="researcher", delegation_key="dk1",
        objective="find hooks", evidence_required="hook names",
        completion_criteria="all real hooks named")

    assert phase0.action_snapshot("researcher")["tool_calls_this_delegation"] == 0
    # cumulative total is untouched by the real production call site too
    assert phase0.action_snapshot("researcher")["tool_calls_made"] == 2
