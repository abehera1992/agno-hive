"""Tests for _is_multi_part_task and _make_decompose_first_gate_hook (swarm/team.py) --
Phase 2 of the "AgnoHive - Engineering Team 2.0 Update" plan, a mechanical backstop
for Phase 1's prose-only coordinator instruction.

Confirmed live 2026-08-14, immediately after Phase 1 shipped: a fresh-session
re-run of the exact multi-part prompt that motivated the whole plan ("Compare the
eKam Delivery Board Phase 1 requirements... against the actual parties/inventory
module implementation... Identify what's already covered vs what's still missing")
still saw the coordinator open with a narrow delegate_task_to_member('ContextRouter',
'search_files for "party"...') call instead of delegating the whole task to
Researcher, exactly the piecemeal pattern Phase 1's new instruction was meant to
replace. ContextRouter then looped 14 identical calls before the Tier-2 liveness
auto-kill caught it -- Researcher's own DECOMPOSE-FIRST rule never even got a
chance to run, because the coordinator never delegated to it in the first place.
Phase 1's prose instruction was measured, not assumed, to be insufficient on its
own -- this is the mechanical gate the plan's own Phase 2 exists for.
"""
from types import SimpleNamespace

import pytest

from swarm.team import _is_multi_part_task, _make_decompose_first_gate_hook


# ── _is_multi_part_task: pure classifier ─────────────────────────────────────────

def test_the_real_incidents_exact_prompt_is_classified_multi_part():
    task = (
        "Compare the eKam Delivery Board Phase 1 requirements (Notion, "
        "notion_search/notion_get_page) against the actual parties/inventory module "
        "implementation in the codebase. Identify what's already covered vs what's "
        "still missing, citing real file:line for every claim."
    )
    assert _is_multi_part_task(task) is True


def test_what_covered_vs_missing_wording_is_multi_part():
    assert _is_multi_part_task("What's covered vs what's still missing in the parties module?") is True


def test_audit_all_wording_is_multi_part():
    assert _is_multi_part_task("Audit all of the API endpoints for missing auth checks.") is True


def test_which_of_these_wording_is_multi_part():
    assert _is_multi_part_task("Which of these five requirements are actually done?") is True


def test_gap_analysis_wording_is_multi_part():
    assert _is_multi_part_task("Do a gap analysis on the GST compliance module.") is True


def test_a_single_bounded_yes_no_question_is_not_multi_part():
    """Confirmed live 2026-08-14: this exact narrow prompt shape ran clean in 42s
    with no decomposition needed -- must not be swept into the gate."""
    task = (
        "Is the 'Registrations & Locations' UI section present in "
        "Client/EcommClient-Web/ekamweb/src/app/.../parties/page.tsx today, or is "
        "it still missing? Read that exact file directly to check."
    )
    assert _is_multi_part_task(task) is False


def test_a_plain_implementation_task_is_not_multi_part():
    assert _is_multi_part_task("Add pagination to GET /sellers in business-service.") is False


def test_empty_or_none_task_is_not_multi_part():
    assert _is_multi_part_task("") is False
    assert _is_multi_part_task(None) is False


# ── _make_decompose_first_gate_hook ──────────────────────────────────────────────

_MULTI_PART_TASK = (
    "Compare the Phase 1 requirements against the actual implementation. "
    "Identify what's covered vs what's still missing."
)
_SINGLE_SHOT_TASK = "Is section X present in file Y, yes or no?"


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


@pytest.mark.asyncio
async def test_task_none_is_a_permanent_passthrough():
    hook = _make_decompose_first_gate_hook(task=None)

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "ContextRouter", "task": "anything"},
    )

    assert result.startswith("delegated:")


@pytest.mark.asyncio
async def test_non_delegation_tool_calls_are_never_touched():
    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    async def fake_get_file_content(**kwargs):
        return "file content"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert result == "file content"


@pytest.mark.asyncio
async def test_non_multi_part_task_never_blocks():
    hook = _make_decompose_first_gate_hook(task=_SINGLE_SHOT_TASK)

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "ContextRouter", "task": "search_files for X"},
    )

    assert result.startswith("delegated:")


@pytest.mark.asyncio
async def test_multi_part_task_first_call_to_researcher_is_not_blocked():
    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": _MULTI_PART_TASK},
    )

    assert result.startswith("delegated:")


@pytest.mark.asyncio
async def test_multi_part_task_first_call_to_researcher_is_case_insensitive():
    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher", "task": _MULTI_PART_TASK},
    )

    assert result.startswith("delegated:")


@pytest.mark.asyncio
async def test_multi_part_task_first_call_to_a_non_researcher_member_is_blocked():
    calls = []

    async def tracking_delegate(**kwargs):
        calls.append(kwargs)
        return f"delegated: {kwargs}"

    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    result = await hook(
        "delegate_task_to_member", tracking_delegate,
        {"member_id": "ContextRouter", "task": "search_files for party"},
    )

    assert calls == []  # the real delegation NEVER happened
    assert "REDIRECTED" in result
    assert "Researcher" in result
    assert "ContextRouter" in result


@pytest.mark.asyncio
async def test_only_the_first_delegation_call_is_ever_gated():
    """A legitimate narrow follow-up delegation AFTER the first one (whether the
    first was blocked or allowed) must never be blocked -- this is a one-time nudge
    onto the right path, not a standing restriction."""
    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "ContextRouter", "task": "search_files for party"},
    )
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "ContextRouter", "task": "search_files for inventory"},
    )

    assert "REDIRECTED" in first
    assert second.startswith("delegated:")  # not blocked -- only the first call is gated


@pytest.mark.asyncio
async def test_delegate_task_to_members_plural_is_never_blocked():
    """The broadcast-mode plural tool has a different args shape (this codebase's
    only reachable modes are coordinate/route per DOCS.md -- plural is effectively
    unused) -- explicitly out of scope for the redirect logic, always passes
    through, but still counts as the 'first delegation call' for gating purposes."""
    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    async def fake_delegate_members(**kwargs):
        return "broadcast done"

    result = await hook("delegate_task_to_members", fake_delegate_members, {"tasks": []})

    assert result == "broadcast done"


@pytest.mark.asyncio
async def test_a_missing_member_id_does_not_crash_the_hook():
    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    result = await hook("delegate_task_to_member", _fake_delegate, {"task": "no member_id key"})

    assert "REDIRECTED" in result  # empty/missing member_id is never "researcher"


# ── _build_team wiring: task actually threads through to the gate hook ─────────

@pytest.mark.asyncio
async def test_build_team_forwards_task_to_the_gate_hook(monkeypatch):
    """End-to-end wiring check: _build_team's own `task` kwarg must reach the gate
    hook it constructs -- a run built with a real multi-part task string must
    produce a Team whose gate hook actually classifies that string as multi-part,
    not silently default to task=None's permanent-passthrough behavior."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    from swarm.team import _build_team

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
        task=_MULTI_PART_TASK,
    )
    gate_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    called = SimpleNamespace(hit=False)

    async def tracking_delegate(**kwargs):
        called.hit = True
        return "delegated"

    out = await gate_hook(
        "delegate_task_to_member", tracking_delegate, {"member_id": "ContextRouter", "task": "narrow"}
    )

    assert called.hit is False  # the real delegation never ran -- task reached the hook and was classified
    assert "REDIRECTED" in out


@pytest.mark.asyncio
async def test_build_team_with_no_task_leaves_the_gate_hook_a_passthrough(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    from swarm.team import _build_team

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )
    gate_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    out = await gate_hook(
        "delegate_task_to_member", _fake_delegate, {"member_id": "ContextRouter", "task": "narrow"}
    )

    assert out.startswith("delegated:")
