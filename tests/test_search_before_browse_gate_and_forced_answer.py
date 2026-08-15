"""Tests for the 2026-08-15 mechanical backstops built after prose-only fixes were
measured insufficient on live tests:

1. _make_search_before_browse_gate_hook -- blocks Researcher's find_files/
   get_file_content/list_directory_tree calls on a multi-part task until Researcher's
   own first search_files/lightrag_query call this run. Prose alone (Step 3a in
   teams/engineering.yaml's DECOMPOSE-FIRST rule) failed THREE separate live tests
   (2026-08-14, 2026-08-15 x2) even after being shortened, moved earlier, and
   reworded "ALWAYS, NO EXCEPTIONS" -- Researcher never called search_files/
   lightrag_query on any of the three runs, going straight to directory/file
   browsing and landing on the wrong service twice.

2. _forced_answer_nudge / _FORCED_ANSWER_AGGREGATE_THRESHOLD -- a third stub-
   escalation tier, keyed on the aggregate total_stub_serve_count (not any single
   file's own serve count), for a DIFFERENT failure confirmed live on a run that
   swapped Researcher onto qwen3-coder:30b: it correctly landed on the right files
   (search-before-browse's own target problem, fixed) but then cycled through
   re-requesting the SAME ~5 already-fully-read files instead of ever converging to
   an answer, until the Tier-3 liveness auto-kill (total_stub_serve_count > 15)
   killed the run having produced nothing. The existing per-key escalated stub only
   ever addresses the ONE file just re-requested, so the model could "route around"
   it by cycling to a different already-stubbed file. This tier addresses the
   rotation as a whole, well before the hard kill.
"""
from types import SimpleNamespace

import pytest

from swarm.team import (
    _FORCED_ANSWER_AGGREGATE_THRESHOLD,
    _duplicate_read_stub,
    _forced_answer_nudge,
    _make_read_cache_tool_hook,
    _make_search_before_browse_gate_hook,
)


# ── _make_search_before_browse_gate_hook ─────────────────────────────────────────

_MULTI_PART_TASK = (
    "Compare Phase 1 requirements against the actual implementation. "
    "What's already covered vs what's still missing?"
)
_SINGLE_SHOT_TASK = "Is section X present in file Y, yes or no?"


class _FakeAgent:
    def __init__(self, name):
        self.name = name


async def _fake_browse(**kwargs):
    return f"browsed: {kwargs}"


async def _fake_search(**kwargs):
    return f"searched: {kwargs}"


@pytest.mark.asyncio
async def test_task_none_is_a_permanent_passthrough():
    hook = _make_search_before_browse_gate_hook(task=None)

    result = await hook(
        "find_files", _fake_browse, {"glob_pattern": "**/*"}, agent=_FakeAgent("Researcher")
    )

    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_non_multi_part_task_never_blocks_researcher():
    hook = _make_search_before_browse_gate_hook(task=_SINGLE_SHOT_TASK)

    result = await hook(
        "get_file_content", _fake_browse, {"relative_path": "x.py"}, agent=_FakeAgent("Researcher")
    )

    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_multi_part_task_blocks_researchers_first_browse_call():
    calls = []

    async def tracking_browse(**kwargs):
        calls.append(kwargs)
        return "real content"

    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    result = await hook(
        "find_files", tracking_browse, {"glob_pattern": "API/business-service/**/*"},
        agent=_FakeAgent("Researcher"),
    )

    assert calls == []  # the real browse call NEVER happened
    assert "REDIRECTED" in result
    assert "search_files" in result


@pytest.mark.asyncio
async def test_all_three_browse_tool_names_are_blocked():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)
    for tool_name in ("find_files", "get_file_content", "list_directory_tree"):
        result = await hook(tool_name, _fake_browse, {}, agent=_FakeAgent("Researcher"))
        assert "REDIRECTED" in result, tool_name


@pytest.mark.asyncio
async def test_search_call_unblocks_every_subsequent_browse_call():
    """Unlike the decompose-first gate's one-time nudge, this gate stays shut until
    genuinely unblocked -- but once Researcher searches even once, ALL later browse
    calls this run pass through freely (reading the search's own top hits is the
    whole point of Step 3a)."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    blocked = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert "REDIRECTED" in blocked

    searched = await hook("search_files", _fake_search, {"pattern": "Party"}, agent=_FakeAgent("Researcher"))
    assert searched.startswith("searched:")

    # Every subsequent browse call now passes through, not just the next one.
    for _ in range(3):
        result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
        assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_lightrag_query_also_unblocks():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)
    await hook("lightrag_query", _fake_search, {"query": "Party GSTIN"}, agent=_FakeAgent("Researcher"))

    result = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_context_router_is_never_blocked():
    """ContextRouter's entire job is fast retrieval via these same tool names --
    blocking it would be a regression, not a fix."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    result = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("ContextRouter"))

    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_coder_is_never_blocked():
    """Coder legitimately needs get_file_content before editing -- blocking it on a
    multi-part-classified task text would be a real regression."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Coder"))

    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_coordinator_own_calls_agent_none_are_never_blocked():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    result = await hook("find_files", _fake_browse, {}, agent=None)

    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_non_browse_non_search_tool_is_untouched():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    async def fake_write(**kwargs):
        return "written"

    result = await hook("apply_diff", fake_write, {}, agent=_FakeAgent("Researcher"))

    assert result == "written"


# ── _forced_answer_nudge / aggregate escalation tier ─────────────────────────────

def test_forced_answer_nudge_names_every_browse_tool_and_demands_an_answer():
    text = _forced_answer_nudge("Researcher", 6)
    for tool in ("get_file_content", "find_files", "search_files", "list_directory_tree"):
        assert tool in text
    assert "Write the final answer NOW" in text
    assert "Researcher" in text
    assert "6" in text


def test_forced_answer_nudge_falls_back_to_the_coordinator_label():
    text = _forced_answer_nudge("", 6)
    assert "the coordinator" in text


@pytest.mark.asyncio
async def test_read_cache_hook_switches_to_forced_answer_at_the_aggregate_threshold():
    """Reproduces the live incident directly: cycling through several DIFFERENT
    already-fully-read files never lets any single one cross the per-key escalation
    threshold by much, but the aggregate crosses _FORCED_ANSWER_AGGREGATE_THRESHOLD
    well before the Tier-3 kill (15) -- and once it does, the returned message must
    be the aggregate one, not the per-key "STOP calling THIS file" wording."""
    activity: dict = {}
    hook = _make_read_cache_tool_hook(activity=activity)

    async def fake_get_file_content(**kwargs):
        return "x" * 100

    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    last_result = None
    # 3 passes through 5 distinct files = each file served 3 times (2 over budget
    # each, _MAX_FULL_SERVES_PER_AGENT=2) -- total_stub_serve_count climbs to 5 by
    # the end of the 3rd pass without any SINGLE file's own count reaching
    # _STUB_ESCALATION_SERVE (5).
    for _pass in range(4):
        for f in files:
            last_result = await hook(
                "get_file_content", fake_get_file_content, {"relative_path": f},
                agent=_FakeAgent("Researcher"),
            )

    assert activity["total_stub_serve_count"] >= _FORCED_ANSWER_AGGREGATE_THRESHOLD
    assert "FORCED STOP" in last_result
    assert "Write the final answer NOW" in last_result


@pytest.mark.asyncio
async def test_read_cache_hook_keeps_normal_stub_wording_below_the_aggregate_threshold():
    """A single file repeated a few times, alone, must not trip the aggregate
    tier prematurely -- only the per-key escalated wording should show."""
    activity: dict = {}
    hook = _make_read_cache_tool_hook(activity=activity)

    async def fake_get_file_content(**kwargs):
        return "x" * 100

    last_result = None
    for _ in range(4):
        last_result = await hook(
            "get_file_content", fake_get_file_content, {"relative_path": "a.py"},
            agent=_FakeAgent("Researcher"),
        )

    assert activity["total_stub_serve_count"] < _FORCED_ANSWER_AGGREGATE_THRESHOLD
    assert "FORCED STOP" not in last_result


@pytest.mark.asyncio
async def test_forced_answer_tier_keeps_firing_on_every_call_past_threshold():
    """Not a one-shot -- if the model ignores the forced nudge and keeps reading
    duplicates anyway, it must keep seeing the strong message, not revert to the
    softer per-key wording that already failed to land."""
    activity: dict = {}
    hook = _make_read_cache_tool_hook(activity=activity)

    async def fake_get_file_content(**kwargs):
        return "x" * 100

    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    results = []
    for _pass in range(6):
        for f in files:
            results.append(await hook(
                "get_file_content", fake_get_file_content, {"relative_path": f},
                agent=_FakeAgent("Researcher"),
            ))

    forced_count = sum(1 for r in results if "FORCED STOP" in r)
    assert forced_count > 1
