"""Tests for the 2026-08-15 parallel-review / planning groundedness fixes.

Both teams were found, by reading their real YAMLs and the relevant runtime code
directly (not assumed), to have concrete gaps relative to engineering.yaml's own
already-hardened setup:

1. `teams/parallel-review.yaml` declared `mode: collaborate` -- not a real agno
   TeamMode (confirmed against the installed agno/team/mode.py: only coordinate/
   route/broadcast/tasks exist). agno/team/_init.py's mode-resolution if/elif chain
   never matched the unrecognized string, so `respond_directly`/
   `delegate_to_all_members` silently stayed at their coordinate-equivalent
   defaults -- this team was NEVER actually broadcasting to all 3 reviewers
   simultaneously as its own description claimed. Fixed to the real `broadcast`
   TeamMode, which agno/team/_default_tools.py confirms dispatches via
   `delegate_task_to_members` (plural) -- the exact tool Phase 2's
   `_make_decompose_first_gate_hook` was already built to never block (see its own
   docstring), so no gate-logic changes were needed alongside this fix.

2. `teams/planning.yaml` had no Notion read tools at all (engineering.yaml got
   them 2026-08-14; planning.yaml was never updated) -- directly blocking a
   "read a spec, map it to our project" workflow. Added notion_search/
   notion_get_page to ContextRouter/Researcher/Planner, mirroring engineering's
   grant + instruction wording exactly.

3. `teams/planning.yaml`'s Researcher/Planner still carried the stale
   "If lightrag_query is available via MCP, call it..." hedge -- already fixed
   in engineering.yaml (lightrag is always connected) but never ported here.

4. `_COORDINATOR_INSTRUCTIONS` (swarm/team.py, shared verbatim across ALL 4 teams)
   hardcodes engineering's own roster in its "Delegate to team members
   (ContextRouter, Researcher, Coder, Executor, Reviewer)" line and several
   scenario blocks -- factually wrong for parallel-review (no ContextRouter),
   sprint-master (BacklogResearcher/StoryWriter), and planning (no Coder/
   Executor/Reviewer). Rather than editing that hard-won, 8-phase-validated
   prose (real regression risk), `_team_roster_preamble()` prepends a small,
   dynamically-generated, per-run block naming each team's REAL members ahead
   of it -- purely additive.
"""
import inspect
from pathlib import Path

import yaml

from swarm.team import _team_roster_preamble, run_task_async, run_task_stream

_PARALLEL_REVIEW_YAML = Path(__file__).parent.parent / "teams" / "parallel-review.yaml"
_PLANNING_YAML = Path(__file__).parent.parent / "teams" / "planning.yaml"
_ENGINEERING_YAML = Path(__file__).parent.parent / "teams" / "engineering.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _agent(data: dict, name: str) -> dict:
    return next(a for a in data["agents"] if a["name"] == name)


# ── parallel-review.yaml: mode fix ──────────────────────────────────────────────

def test_parallel_review_mode_is_a_real_agno_teammode():
    data = _load(_PARALLEL_REVIEW_YAML)
    from agno.team.mode import TeamMode

    assert data["mode"] in {m.value for m in TeamMode}


def test_parallel_review_mode_is_broadcast_not_collaborate():
    data = _load(_PARALLEL_REVIEW_YAML)
    assert data["mode"] == "broadcast"


# ── parallel-review.yaml: lightrag-empty fallback (2026-08-15, live-validated gap) ──
#
# Live-confirmed 2026-08-15, after the broadcast mode fix above: with broadcast
# correctly dispatching to all 3 reviewers (delegate_task_to_members WAS called,
# confirmed via the run's own session_state log), SecurityReviewer and
# PerformanceReviewer each tried lightrag_query('config.py'...) exactly once, got
# back "[no-context]", and gave up -- calling request_clarification instead of
# falling back to find_files/search_files, tools both agents already had on their
# own tool list the whole time. config/config.py is a real, existing file. Fixed
# with an explicit FALLBACK rule on all 3 review agents.

def test_researcher_has_lightrag_fallback_rule():
    instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), "Researcher")["instructions"])
    assert "fallback rule" in instructions.lower()
    assert "find_files" in instructions and "search_files" in instructions


def test_security_reviewer_has_lightrag_fallback_rule():
    instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), "SecurityReviewer")["instructions"])
    assert "fallback rule" in instructions.lower()
    assert "find_files" in instructions and "search_files" in instructions


def test_performance_reviewer_has_lightrag_fallback_rule():
    instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), "PerformanceReviewer")["instructions"])
    assert "fallback rule" in instructions.lower()
    assert "find_files" in instructions and "search_files" in instructions


def test_fallback_rule_tells_agents_not_to_stop_after_one_attempt():
    """The core behavioral fix, not just tool-name presence -- the live failure was
    stopping/asking for clarification after exactly one lightrag_query miss."""
    for agent_name in ("Researcher", "SecurityReviewer", "PerformanceReviewer"):
        instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), agent_name)["instructions"]).lower()
        assert "never stop" in instructions or "immediately fall back" in instructions


# ── planning.yaml: Notion tools ─────────────────────────────────────────────────

def test_planning_context_router_has_notion_read_tools():
    data = _load(_PLANNING_YAML)
    tools = _agent(data, "ContextRouter")["tools"]
    assert "notion_search" in tools
    assert "notion_get_page" in tools


def test_planning_researcher_has_notion_read_tools():
    data = _load(_PLANNING_YAML)
    tools = _agent(data, "Researcher")["tools"]
    assert "notion_search" in tools
    assert "notion_get_page" in tools


def test_planning_planner_has_notion_read_tools():
    data = _load(_PLANNING_YAML)
    tools = _agent(data, "Planner")["tools"]
    assert "notion_search" in tools
    assert "notion_get_page" in tools


def test_planning_researcher_notion_grant_matches_engineering_tool_set():
    """Not just present -- the SAME two read tools engineering.yaml grants, so
    planning's Researcher can't drift onto a write tool (notion_create_page etc.)
    by copy-paste error."""
    planning_tools = set(_agent(_load(_PLANNING_YAML), "Researcher")["tools"])
    engineering_tools = set(_agent(_load(_ENGINEERING_YAML), "Researcher")["tools"])
    notion_tools_planning = {t for t in planning_tools if t.startswith("notion_")}
    notion_tools_engineering = {t for t in engineering_tools if t.startswith("notion_")}
    assert notion_tools_planning == notion_tools_engineering == {"notion_search", "notion_get_page"}


# ── planning.yaml: stale lightrag hedge removed ─────────────────────────────────

def test_planning_researcher_no_longer_hedges_on_lightrag_availability():
    data = _load(_PLANNING_YAML)
    instructions = " ".join(_agent(data, "Researcher")["instructions"])
    assert "if lightrag_query is available" not in instructions.lower()


def test_planning_planner_no_longer_hedges_on_lightrag_availability():
    data = _load(_PLANNING_YAML)
    instructions = " ".join(_agent(data, "Planner")["instructions"])
    assert "if lightrag_query is available" not in instructions.lower()


# ── _team_roster_preamble ────────────────────────────────────────────────────────

def _spec(name, description=None, role="a role"):
    from api.models import AgentSpec
    return AgentSpec(name=name, role=role, model="qwen2.5-coder:32b", instructions=[], description=description)


def test_empty_agent_specs_returns_empty_list():
    assert _team_roster_preamble(None) == []
    assert _team_roster_preamble([]) == []


def test_lists_every_real_agent_by_exact_name():
    specs = [_spec("BacklogResearcher", "Delivery-board reader."), _spec("StoryWriter", "Delivery-board writer.")]

    result = _team_roster_preamble(specs)
    joined = "\n".join(result)

    assert "BacklogResearcher" in joined
    assert "StoryWriter" in joined
    assert "ContextRouter" not in joined  # sprint-master has no such member


def test_falls_back_to_role_when_description_is_none():
    specs = [_spec("Researcher", description=None, role="Senior engineer who investigates the codebase.")]

    result = _team_roster_preamble(specs)

    assert "Senior engineer who investigates the codebase." in "\n".join(result)


def test_result_is_prepended_ahead_of_coordinator_instructions_in_both_functions():
    """Source-inspection check (same convention as test_gate_team_scoping.py's
    team_name wiring checks) -- neither run_task_async nor run_task_stream is
    unit-tested directly elsewhere (heavy MCP/session setup); confirms the
    preamble is prepended, not appended or omitted, in both places it's built."""
    async_source = inspect.getsource(run_task_async)
    stream_source = inspect.getsource(run_task_stream)

    assert "_team_roster_preamble(agent_specs) + list(_COORDINATOR_INSTRUCTIONS)" in async_source
    assert "_team_roster_preamble(agent_specs) + list(_COORDINATOR_INSTRUCTIONS)" in stream_source
