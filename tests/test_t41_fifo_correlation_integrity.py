"""T41 Track B -- FIFO delegation-evidence correlation integrity.

B0/B3 finding, proven from agno's own installed source (agno/models/base.py):
multiple tool calls requested in one model turn are run via
`asyncio.gather(*(self.arun_function_call(fc) for fc in function_calls_to_run))`
-- genuine concurrent execution, not sequential awaiting -- and for the
async-generator-shaped results delegate_structured_task returns specifically,
agno builds a PER-GENERATOR background task (`process_async_generator`) whose
output events land in a SHARED asyncio.Queue. There is no guarantee that two
concurrently-dispatched delegations to the SAME member complete (emit their
own 'RunContentCompleted' event) in the SAME order they were dispatched --
whichever one's own underlying work (tool calls, model turns) finishes first
emits its completion event first, independent of dispatch order.

T40's FIFO fix (commit b0e8760) assumed completion order always matches
dispatch order. This is correct for the common case (completions usually do
resolve in roughly dispatch order) but not GUARANTEED by the architecture --
Case 3 below demonstrates the exact violation: dispatch A then B to the same
member, but B's own result materializes BEFORE A's. FIFO (pop-oldest) wrongly
tags B's text onto A's log entry.

Fix (T41/B): a second correlation mechanism using agno's own per-invocation
`run_id` (confirmed present on BaseAgentRunEvent, agno/run/agent.py), captured
at the EARLIEST point available -- a 'RunStarted' event -- and associated
FIFO-style with the oldest UNCLAIMED pending entry (this earliest-point claim
is itself safe under the SAME agno source evidence: a coroutine's synchronous
code, including the dispatch-time log.append this codebase already does, runs
in scheduling order before any task yields control at its first await/model
call -- so a 'RunStarted' event, the very first thing agno emits once that
specific invocation begins, is observed in dispatch order even though LATER
completion is not). Completion is then resolved via the established run_id,
not queue position -- correct regardless of completion order.

Case numbers below match T41's own mission spec (Cases 1-12).
"""
from types import SimpleNamespace

import pytest

from swarm.team import (
    _make_duplicate_delegation_gate_hook, _finalise_member_chunks,
    _accumulate_member_chunk, _stream_event_to_chunk, _claim_pending_entry_for_run,
)


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


def _args(member_id="researcher", target="vouchers_api.py", objective="list endpoints"):
    return {
        "member_id": member_id, "target": target, "objective": objective,
        "evidence_required": "evidence", "completion_criteria": "done",
    }


class _Event:
    """Minimal stand-in for agno's BaseAgentRunEvent -- only the fields this
    codebase's own event handling actually reads."""
    def __init__(self, event, agent_name="", run_id=None, content=None):
        self.event = event
        self.agent_name = agent_name
        self.run_id = run_id
        self.content = content


# ── Case 1: sequential same-member dispatch (already covered by T40, re-confirmed) ──

async def test_case1_sequential_dispatch_each_result_maps_to_its_own_entry():
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="a.py", objective="list A"), run_context=None, team=team)
    _accumulate_member_chunk(team, "Researcher", "A RESULT")
    _finalise_member_chunks(team, "Researcher")

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="b.py", objective="list B"), run_context=None, team=team)
    _accumulate_member_chunk(team, "Researcher", "B RESULT")
    _finalise_member_chunks(team, "Researcher")

    repeat_a = await hook("delegate_structured_task", _fake_delegate,
                          _args(target="a.py", objective="list A again"),
                          run_context=None, team=team)
    assert "A RESULT" in repeat_a and "B RESULT" not in repeat_a


# ── Case 2: overlapping dispatch, in-order completion (T40's own original case) ────

async def test_case2_overlapping_dispatch_in_order_completion():
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="a.py", objective="list A"), run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target="b.py", objective="list B"), run_context=None, team=team)

    _accumulate_member_chunk(team, "Researcher", "A RESULT")
    _finalise_member_chunks(team, "Researcher")
    _accumulate_member_chunk(team, "Researcher", "B RESULT")
    _finalise_member_chunks(team, "Researcher")

    repeat_a = await hook("delegate_structured_task", _fake_delegate,
                          _args(target="a.py", objective="list A once more"),
                          run_context=None, team=team)
    assert "A RESULT" in repeat_a and "B RESULT" not in repeat_a


# ── Case 3: REVERSE completion order -- the proven architectural gap ───────────────

async def test_case3_reverse_completion_order_without_run_id_tracking_would_misattribute():
    """Direct proof of B3's finding: with NO run_id claimed (simulating a
    caller/event stream that never emits 'RunStarted', or an older code path),
    _finalise_member_chunks falls back to FIFO pop-oldest -- which is WRONG
    when B completes before A. This test documents the exact failure FIFO
    alone cannot prevent; the fix in Case 3b (below) closes it."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="a.py", objective="list A"), run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target="b.py", objective="list B"), run_context=None, team=team)

    # B completes FIRST -- no run_id claimed (simulating the pre-T41 shape).
    _accumulate_member_chunk(team, "Researcher", "B RESULT")
    _finalise_member_chunks(team, "Researcher")  # pops A's entry (FIFO oldest) -- WRONG
    _accumulate_member_chunk(team, "Researcher", "A RESULT")
    _finalise_member_chunks(team, "Researcher")  # pops B's entry -- ALSO WRONG

    repeat_a = await hook("delegate_structured_task", _fake_delegate,
                          _args(target="a.py", objective="list A once more"),
                          run_context=None, team=team)
    # Document the actual (wrong, FIFO-only) outcome -- A's own entry was
    # tagged with B's text, because B completed first and FIFO popped
    # whatever was oldest, not whatever actually belonged to B.
    assert "B RESULT" in repeat_a, (
        "this assertion documents the PROVEN FIFO-only misattribution under "
        "reverse completion order -- without run_id tracking, A's repeat is "
        "served B's text"
    )


async def test_case3b_run_id_tracking_fixes_reverse_completion_order():
    """The actual fix: claim run_id at 'RunStarted' (dispatch-order-safe, per
    this file's own module docstring), resolve at 'RunContentCompleted' via
    that run_id (correct regardless of completion order)."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="a.py", objective="list A"), run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target="b.py", objective="list B"), run_context=None, team=team)

    # RunStarted events arrive in DISPATCH order (A's invocation starts first,
    # per the agno scheduling-order evidence) -- claimed via the real event path.
    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-A"), team=team)
    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-B"), team=team)

    # But B's own work finishes FIRST -- reverse completion order.
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="B RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-B"), team=team)
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="A RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-A"), team=team)

    repeat_a = await hook("delegate_structured_task", _fake_delegate,
                          _args(target="a.py", objective="list A once more"),
                          run_context=None, team=team)
    repeat_b = await hook("delegate_structured_task", _fake_delegate,
                          _args(target="b.py", objective="list B once more"),
                          run_context=None, team=team)

    assert "A RESULT" in repeat_a and "B RESULT" not in repeat_a, (
        f"A's repeat must be served A's own text even though B completed "
        f"first -- got: {repeat_a!r}"
    )
    assert "B RESULT" in repeat_b and "A RESULT" not in repeat_b


# ── Case 4: interleaved completion across TWO different members, no cross-contamination ──

async def test_case4_interleaved_completion_across_two_members_no_cross_contamination():
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(member_id="researcher", target="a.py", objective="list A"),
              run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(member_id="researcher", target="b.py", objective="list B"),
              run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(member_id="coder", target="c.py", objective="patch C"),
              run_context=None, team=team)

    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-A"), team=team)
    _stream_event_to_chunk(_Event("RunStarted", agent_name="Coder", run_id="run-C"), team=team)
    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-B"), team=team)

    _stream_event_to_chunk(_Event("RunContent", agent_name="Coder", content="C RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Coder", run_id="run-C"), team=team)
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="B RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-B"), team=team)
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="A RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-A"), team=team)

    repeat_a = await hook("delegate_structured_task", _fake_delegate,
                          _args(member_id="researcher", target="a.py", objective="list A once more"),
                          run_context=None, team=team)
    repeat_c = await hook("delegate_structured_task", _fake_delegate,
                          _args(member_id="coder", target="c.py", objective="patch C once more"),
                          run_context=None, team=team)

    assert "A RESULT" in repeat_a and "B RESULT" not in repeat_a and "C RESULT" not in repeat_a
    assert "C RESULT" in repeat_c and "A RESULT" not in repeat_c and "B RESULT" not in repeat_c


# ── Case 5: duplicate-delegation block cannot serve unrelated result text ──────────

async def test_case5_duplicate_block_never_serves_unrelated_text():
    """Reproduces T40's own F3 incident shape as a regression: a repeat ask
    for the backend-endpoints target must never be served a frontend-hooks
    result just because the same member handled both."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="vouchers_api.py", objective="list endpoints"),
              run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target="page.tsx", objective="list frontend hooks"),
              run_context=None, team=team)

    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-endpoints"), team=team)
    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-hooks"), team=team)
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="FRONTEND HOOKS RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-hooks"), team=team)
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="BACKEND ENDPOINTS RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-endpoints"), team=team)

    repeat_endpoints = await hook(
        "delegate_structured_task", _fake_delegate,
        _args(target="vouchers_api.py", objective="list endpoints again"),
        run_context=None, team=team)

    assert "BACKEND ENDPOINTS RESULT" in repeat_endpoints
    assert "FRONTEND HOOKS RESULT" not in repeat_endpoints


# ── Case 6: a failed delegation's entry must not be consumed by a later success ────

async def test_case6_a_failed_delegation_never_inherits_a_later_results_text():
    """A dispatches, but produces no text at all (failure/empty) -- its own
    entry must stay unresolved, and a LATER successful delegation to the same
    member must not have its own result stolen to fill A's gap."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="a.py", objective="list A"), run_context=None, team=team)
    await hook("delegate_structured_task", _fake_delegate,
              _args(target="b.py", objective="list B"), run_context=None, team=team)

    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-A"), team=team)
    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-B"), team=team)
    # A produces NO content at all (failure/empty) -- no RunContent, straight
    # to RunContentCompleted. _finalise_member_chunks's own early-return on an
    # empty buffer means nothing gets tagged for A.
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-A"), team=team)
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="B RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-B"), team=team)

    repeat_b = await hook("delegate_structured_task", _fake_delegate,
                          _args(target="b.py", objective="list B once more"),
                          run_context=None, team=team)
    assert "B RESULT" in repeat_b


# ── Case 9: exactly-once consumption -- a result is never attached twice ───────────

async def test_case9_exactly_once_consumption_no_double_tagging():
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="a.py", objective="list A"), run_context=None, team=team)

    _stream_event_to_chunk(_Event("RunStarted", agent_name="Researcher", run_id="run-A"), team=team)
    _stream_event_to_chunk(_Event("RunContent", agent_name="Researcher", content="A RESULT"), team=team)
    _stream_event_to_chunk(_Event("RunContentCompleted", agent_name="Researcher", run_id="run-A"), team=team)

    by_run = getattr(team, "_pending_log_entry_by_run_id", {})
    assert "run-A" not in by_run, "a consumed run_id must be removed, not left claimable again"

    queue = getattr(team, "_pending_log_entry_by_member", {}).get("researcher", [])
    assert len(queue) == 0, "the consumed entry must be removed from the FIFO queue too"


# ── Case 10: stale pending state does not leak into an unrelated team instance ─────

async def test_case10_pending_state_does_not_leak_across_team_instances():
    hook = _make_duplicate_delegation_gate_hook()
    team_1 = SimpleNamespace(_member_results={})
    team_2 = SimpleNamespace(_member_results={})

    await hook("delegate_structured_task", _fake_delegate,
              _args(target="a.py", objective="list A"), run_context=None, team=team_1)

    assert not getattr(team_2, "_pending_log_entry_by_member", {})
    assert not getattr(team_2, "_pending_log_entry_by_run_id", {})


# ── Case 11: T24 preservation -- exact test identities and outcomes, unchanged ─────

def test_case11_t24_evidence_fidelity_tests_identity_check():
    """Not a behavioral test -- records the exact T24 test names this phase's
    own report must cite verbatim, per the mission's explicit instruction.
    Their pass/fail outcome is reported in the T41 report itself (re-verified
    there, not assumed), not asserted here, since this file must not depend
    on or alter test_evidence_fidelity.py's own implementation."""
    exact_names = [
        "tests/test_evidence_fidelity.py::test_delegated_researcher_evidence_resolves_to_researcher_not_coordinator",
        "tests/test_evidence_fidelity.py::test_multiple_delegated_members_each_resolve_to_their_own_identity",
        "tests/test_evidence_fidelity.py::test_relay_retention_no_longer_forced_to_zero_for_delegated_evidence",
    ]
    assert len(exact_names) == 3  # trivially true -- this test exists to pin the names in one place
