"""Tests for the 2026-08-15 gate-scope extension -- resolution of Engineering
Team 2.0's 4th open question ("should Phase 2/6's mechanical gates apply to
parallel-review/planning/sprint-master too, or engineering only?"), answered
by the user via AskUserQuestion as "extend to parallel-review + sprint-master"
(NOT the recommended engineering-only option; `planning` explicitly excluded,
since it has its own separate, differently-shaped Planner agent).

Two real findings drove the shape of this fix, both confirmed by reading the
actual code/YAML before writing anything:

1. `_build_team()` never had any team-identity awareness at all -- the two
   mechanical gates (decompose-first, search-before-browse) were wired
   UNCONDITIONALLY for every team that flows through it, keyed only on
   `_is_multi_part_task(task)` and a hardcoded agent-name string comparison.
   This means the gates were ALREADY structurally active for `parallel-review`
   (harmless -- its researcher-shaped agent happens to be literally named
   "Researcher", confirmed via `teams/parallel-review.yaml`) and for
   `planning` (a real, previously-unnoticed leak -- `planning`'s own
   `Researcher` agent was already silently gated, contradicting this exact
   open question's eventual "planning excluded" answer, discovered only by
   reading `/plan`'s own call to `run_task_async` directly).

2. `teams/sprint-master.yaml`'s researcher-shaped agent is named
   `BacklogResearcher`, not `Researcher` (confirmed by reading the YAML) --
   the OLD hardcoded `member_id == "researcher"` / `agent_key != "Researcher"`
   checks would never match it. Before this fix, if sprint-master's
   coordinator ever received a multi-part-shaped task, the decompose-first
   gate would BLOCK the correct first delegation and redirect the coordinator
   toward calling `delegate_task_to_member('Researcher', ...)` -- a member
   that does not exist on that team's roster. Extending "properly" therefore
   means teaching the gates each team's actual researcher-agent name, not
   just turning a switch on.

The fix: `_build_team()` gains a `team_name` kwarg (default None = the exact
pre-2026-08-15 unconditional behavior, so every existing caller/test that
doesn't pass it is untouched) used to look up (a) whether this team is in the
gate-enabled allowlist and (b) which agent name on this team plays the
Researcher role.
"""
from types import SimpleNamespace

import pytest

from swarm.team import (
    _GATE_ENABLED_TEAMS,
    _RESEARCHER_AGENT_NAME_BY_TEAM,
    _build_team,
    _make_decompose_first_gate_hook,
    _make_search_before_browse_gate_hook,
)

_MULTI_PART_TASK = (
    "Compare Phase 1 requirements against the actual implementation. "
    "What's already covered vs what's still missing?"
)


class _FakeAgent:
    def __init__(self, name):
        self.name = name


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


async def _fake_browse(**kwargs):
    return f"browsed: {kwargs}"


async def _fake_search(**kwargs):
    return f"searched: {kwargs}"


# ── allowlist / name-map contents ────────────────────────────────────────────

def test_gate_enabled_teams_is_engineering_parallel_review_sprint_master_only():
    assert _GATE_ENABLED_TEAMS == {"engineering", "parallel-review", "sprint-master"}


def test_planning_is_not_in_the_gate_enabled_teams():
    """This set now governs the decompose-first gate ONLY -- see
    _SEARCH_GATE_ENABLED_TEAMS below for the (broader) search-before-browse set."""
    assert "planning" not in _GATE_ENABLED_TEAMS


def test_search_gate_enabled_teams_includes_planning():
    from swarm.team import _SEARCH_GATE_ENABLED_TEAMS

    assert "planning" in _SEARCH_GATE_ENABLED_TEAMS


def test_search_gate_enabled_teams_is_a_strict_superset_of_decompose_gate_teams():
    from swarm.team import _SEARCH_GATE_ENABLED_TEAMS

    assert _GATE_ENABLED_TEAMS < _SEARCH_GATE_ENABLED_TEAMS


def test_sprint_master_researcher_agent_name_is_backlogresearcher():
    assert _RESEARCHER_AGENT_NAME_BY_TEAM["sprint-master"] == "BacklogResearcher"


# ── _make_decompose_first_gate_hook: researcher_member_id parameterization ──────

@pytest.mark.asyncio
async def test_decompose_gate_default_researcher_name_is_unchanged():
    """No researcher_member_id passed -- byte-for-byte the pre-2026-08-15 default,
    comparing against the literal 'researcher'."""
    hook = _make_decompose_first_gate_hook(task=_MULTI_PART_TASK)

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": _MULTI_PART_TASK},
    )

    assert result.startswith("delegated:")


@pytest.mark.asyncio
async def test_decompose_gate_accepts_backlogresearcher_as_the_target_name():
    hook = _make_decompose_first_gate_hook(
        task=_MULTI_PART_TASK, researcher_member_id="BacklogResearcher",
    )

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "BacklogResearcher", "task": _MULTI_PART_TASK},
    )

    assert result.startswith("delegated:")


@pytest.mark.asyncio
async def test_decompose_gate_with_backlogresearcher_still_blocks_other_members():
    hook = _make_decompose_first_gate_hook(
        task=_MULTI_PART_TASK, researcher_member_id="BacklogResearcher",
    )

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "StoryWriter", "task": "write a story now"},
    )

    assert "REDIRECTED" in result
    assert "BacklogResearcher" in result  # redirect names the REAL member on this team
    # Must never tell sprint-master's coordinator to call a member it doesn't have.
    assert "delegate_task_to_member('Researcher'" not in result


@pytest.mark.asyncio
async def test_decompose_gate_with_backlogresearcher_name_check_is_case_insensitive():
    hook = _make_decompose_first_gate_hook(
        task=_MULTI_PART_TASK, researcher_member_id="BacklogResearcher",
    )

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "backlogresearcher", "task": _MULTI_PART_TASK},
    )

    assert result.startswith("delegated:")


# ── _make_search_before_browse_gate_hook: researcher_agent_name parameterization ─

@pytest.mark.asyncio
async def test_search_gate_default_researcher_name_is_unchanged():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    result = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("Researcher"))

    assert "REDIRECTED" in result


@pytest.mark.asyncio
async def test_search_gate_scopes_to_backlogresearcher_when_configured():
    hook = _make_search_before_browse_gate_hook(
        task=_MULTI_PART_TASK, researcher_agent_name="BacklogResearcher",
    )

    blocked = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("BacklogResearcher"))
    assert "REDIRECTED" in blocked

    # A plain "Researcher"-named agent (not this team's real name) is NOT scoped.
    passthrough = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert passthrough.startswith("browsed:")


@pytest.mark.asyncio
async def test_search_gate_backlogresearcher_unblocks_after_its_own_search_call():
    hook = _make_search_before_browse_gate_hook(
        task=_MULTI_PART_TASK, researcher_agent_name="BacklogResearcher",
    )

    blocked = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("BacklogResearcher"))
    assert "REDIRECTED" in blocked

    await hook("search_files", _fake_search, {"pattern": "sprint"}, agent=_FakeAgent("BacklogResearcher"))

    result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("BacklogResearcher"))
    assert result.startswith("browsed:")


# ── _build_team: team_name wiring end-to-end ────────────────────────────────────

@pytest.mark.asyncio
async def test_build_team_with_no_team_name_keeps_the_exact_prior_unconditional_behavior(monkeypatch):
    """team_name omitted (the default) must behave byte-for-byte like before this
    change existed -- every existing caller that never passes team_name (tests,
    and any not-yet-updated production call site) is unaffected."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
    )
    gate_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    out = await gate_hook(
        "delegate_task_to_member", _fake_delegate, {"member_id": "ContextRouter", "task": "narrow"}
    )
    assert "REDIRECTED" in out


@pytest.mark.asyncio
async def test_build_team_engineering_keeps_gates_active(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
        team_name="engineering",
    )
    gate_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    out = await gate_hook(
        "delegate_task_to_member", _fake_delegate, {"member_id": "ContextRouter", "task": "narrow"}
    )
    assert "REDIRECTED" in out


@pytest.mark.asyncio
async def test_build_team_parallel_review_keeps_gates_active(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
        team_name="parallel-review",
    )
    gate_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    out = await gate_hook(
        "delegate_task_to_member", _fake_delegate, {"member_id": "SecurityReviewer", "task": "narrow"}
    )
    assert "REDIRECTED" in out
    assert "Researcher" in out


@pytest.mark.asyncio
async def test_build_team_sprint_master_gates_target_backlogresearcher_not_researcher(monkeypatch):
    """The core regression this whole fix exists for: sprint-master's gate must
    redirect toward the member it ACTUALLY has (BacklogResearcher), never toward
    a nonexistent 'Researcher'."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
        team_name="sprint-master",
    )
    gate_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    # The real member name must now pass through un-redirected.
    allowed = await gate_hook(
        "delegate_task_to_member", _fake_delegate, {"member_id": "BacklogResearcher", "task": _MULTI_PART_TASK}
    )
    assert allowed.startswith("delegated:")


@pytest.mark.asyncio
async def test_build_team_sprint_master_search_gate_scopes_to_backlogresearcher(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
        team_name="sprint-master",
    )
    search_gate_hook = next(h for h in result.tool_hooks if h.__name__ == "_search_before_browse_gate_hook")

    blocked = await search_gate_hook(
        "find_files", _fake_browse, {}, agent=_FakeAgent("BacklogResearcher")
    )
    assert "REDIRECTED" in blocked


@pytest.mark.asyncio
async def test_build_team_planning_disables_decompose_first_gate_only(monkeypatch):
    """`planning`'s decompose-first gate stays OFF (2026-08-15, unchanged from the
    original gate-scope extension) -- its own separate, differently-shaped Planner
    (DISCUSSION/ROADMAP dual-mode) means the redirect message's premise
    ("Researcher now also decomposes tasks internally, merged with Planner") is
    simply FALSE for this team: planning's Researcher and Planner are two distinct
    agents, not a merged role. Forcing a delegate-whole-to-Researcher redirect here
    would be actively wrong advice, not just unnecessary."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
        team_name="planning",
    )
    decompose_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    delegate_out = await decompose_hook(
        "delegate_task_to_member", _fake_delegate, {"member_id": "ContextRouter", "task": "narrow"}
    )
    assert delegate_out.startswith("delegated:")  # NOT redirected -- planning is excluded


@pytest.mark.asyncio
async def test_build_team_planning_now_enables_search_before_browse_gate(monkeypatch):
    """2026-08-15 refinement: unlike the decompose-first gate, search-before-browse's
    rationale ('search before you browse, to find the actually-owning file directly
    instead of guessing a service/directory from a domain-name association') holds
    regardless of whether Planner is merged into Researcher or stays separate --
    it's about HOW Researcher grounds one claim, not about the inter-agent shape.
    Originally excluded alongside the decompose-first gate by the first pass of this
    extension (a real, previously-unnoticed leak: planning's own Researcher was
    silently gated before ANY of this existed, then over-correctively excluded from
    BOTH gates when the leak was closed) -- this test locks in the corrected,
    narrower scope: planning's Researcher now gets real search-before-browse
    protection, the same prose-insufficiency problem Phase 5/6 already measured and
    fixed for engineering."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
        team_name="planning",
    )
    search_hook = next(h for h in result.tool_hooks if h.__name__ == "_search_before_browse_gate_hook")

    blocked = await search_hook("find_files", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert "REDIRECTED" in blocked  # NOW gated -- planning's Researcher gets real protection

    # Planner (planning's own, separate agent) must never be touched by this gate.
    planner_pass = await search_hook("find_files", _fake_browse, {}, agent=_FakeAgent("Planner"))
    assert planner_pass.startswith("browsed:")


@pytest.mark.asyncio
async def test_build_team_unknown_team_name_disables_both_gates(monkeypatch):
    """A team name that isn't in the allowlist (anything not explicitly opted in,
    e.g. a brand-new team YAML added later, or the /run 'custom' agents path)
    must fail SAFE -- gates off, not silently on with a possibly-wrong researcher
    name guess."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b", coordinator_tools=None,
        mode="coordinate", mcp_list=[], instructions=[], task=_MULTI_PART_TASK,
        team_name="some-future-team",
    )
    decompose_hook = next(h for h in result.tool_hooks if h.__name__ == "_decompose_first_gate_hook")

    out = await decompose_hook(
        "delegate_task_to_member", _fake_delegate, {"member_id": "ContextRouter", "task": "narrow"}
    )
    assert out.startswith("delegated:")


# ── run_task_async / run_task_stream: team_name forwarded to _build_team ────────
#
# Neither function is unit-tested directly anywhere in this suite (both do heavy
# setup -- MCP connections, session context, model resolution -- exercised only
# via main.py's worker functions with run_task_async/run_task_stream themselves
# mocked out, see tests/test_main_run_worker.py). Following that same convention,
# these two checks confirm the wiring via signature + source inspection rather
# than a fragile full-execution mock.

import inspect

from swarm.team import run_task_async, run_task_stream


def test_run_task_async_accepts_team_name_kwarg():
    params = inspect.signature(run_task_async).parameters
    assert "team_name" in params
    assert params["team_name"].default is None


def test_run_task_async_forwards_team_name_to_build_team():
    source = inspect.getsource(run_task_async)
    assert "team_name=team_name" in source


def test_run_task_stream_accepts_team_name_kwarg():
    params = inspect.signature(run_task_stream).parameters
    assert "team_name" in params
    assert params["team_name"].default is None


def test_run_task_stream_forwards_team_name_to_build_team():
    source = inspect.getsource(run_task_stream)
    assert "team_name=team_name" in source
