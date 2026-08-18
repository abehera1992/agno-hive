"""Regression test: teams/engineering.yaml no longer has a separate Planner agent --
its responsibilities were merged into Researcher (2026-08-14, see the "AgnoHive -
Engineering Team 2.0 Update" Notion plan).

Why: Phase 0's real 30-day journalctl measurement found Planner delegated in only
~10% of the runs that used Researcher at all (2 of 20 runs, or 2 of 18 excluding two
already-known/fixed over-delegation outliers) -- Planner was essentially never
reached, while the coordinator was already doing its OWN informal, un-inspectable,
one-step-at-a-time decomposition via a long sequence of narrow Researcher
delegations. Merging removes the lossy Researcher -> Planner handoff (agno's
share_member_interactions only forwards a teammate's final TEXT answer between
delegations, never raw tool results -- the same documented mechanism behind the
2026-08-07 get_files_batch 21-29x-for-2-files incident) and makes the checklist-first
decomposition step something Researcher does INTERNALLY, as an explicit, ordered
first output, rather than something that depends on a second delegation hop the
coordinator mostly skips.

teams/planning.yaml keeps its OWN, separate Planner role untouched -- this merge is
scoped to teams/engineering.yaml only, per the plan's own stated scope.
"""
from pathlib import Path

import yaml

from tests._team_config_helpers import effective_tools

_ENGINEERING_YAML = Path(__file__).parent.parent / "teams" / "engineering.yaml"
_PLANNING_YAML = Path(__file__).parent.parent / "teams" / "planning.yaml"


def _engineering_agents() -> dict:
    data = yaml.safe_load(_ENGINEERING_YAML.read_text(encoding="utf-8"))
    return {a["name"]: a for a in data["agents"]}


def _planning_agents() -> dict:
    data = yaml.safe_load(_PLANNING_YAML.read_text(encoding="utf-8"))
    return {a["name"]: a for a in data["agents"]}


def test_engineering_yaml_has_no_separate_planner_agent():
    agents = _engineering_agents()
    assert "Planner" not in agents


def test_engineering_yaml_still_has_exactly_five_agents():
    agents = _engineering_agents()
    assert set(agents.keys()) == {"ContextRouter", "Researcher", "Coder", "Executor", "Reviewer"}


def test_planning_yaml_keeps_its_own_separate_planner_untouched():
    """Scope guard: this merge is engineering.yaml-only. planning.yaml's own,
    differently-shaped Planner (DISCUSSION/ROADMAP vs IMPLEMENTATION dual mode) must
    survive unchanged."""
    agents = _planning_agents()
    assert "Planner" in agents
    assert "DISCUSSION / ROADMAP" in "\n".join(agents["Planner"]["instructions"])


def test_researcher_description_reflects_the_merged_role():
    agents = _engineering_agents()
    description = agents["Researcher"]["description"]
    assert "decomposition" in description.lower()


def test_researcher_has_a_decompose_first_rule():
    agents = _engineering_agents()
    text = "\n".join(agents["Researcher"]["instructions"])
    assert "DECOMPOSE-FIRST rule" in text


def test_decompose_first_rule_requires_artifact_resolution_before_exploration():
    agents = _engineering_agents()
    text = "\n".join(agents["Researcher"]["instructions"])
    assert "resolve every named reference" in text
    assert "ONE concrete artifact ID" in text


def test_decompose_first_rule_requires_a_literal_checklist():
    agents = _engineering_agents()
    text = "\n".join(agents["Researcher"]["instructions"])
    assert "write out a literal checklist" in text


def test_decompose_first_rule_exempts_single_bounded_tasks():
    """A task naming one already-known thing to check must not be forced through
    the full checklist ceremony -- confirmed live 2026-08-14 that a narrow,
    single-target prompt ran clean in 42s with no decomposition needed."""
    agents = _engineering_agents()
    text = "\n".join(agents["Researcher"]["instructions"])
    assert "does not need this" in text


def test_researcher_keeps_the_existing_comparison_rule_unchanged():
    """The merge must not disturb the pre-existing, already-proven COMPARISON rule
    (2026-08-05) -- the new checklist becomes its enumerated 'side A' input, it does
    not replace it."""
    agents = _engineering_agents()
    text = "\n".join(agents["Researcher"]["instructions"])
    assert "COMPARISON rule" in text
    assert "enumerate side A completely first" in text


def test_researcher_still_names_responsible_agents_for_implementation_tasks():
    """Planner's original step-list behavior (name Coder/Executor/Reviewer per step)
    must survive inside the merged role for tasks that need real implementation, not
    just research."""
    agents = _engineering_agents()
    text = "\n".join(agents["Researcher"]["instructions"])
    assert "naming the responsible agent per step" in text
    assert "Coder for changes" in text


def test_researcher_tool_list_is_unchanged_by_the_merge():
    """Researcher's own tool list was ALREADY a superset of Planner's (Planner had no
    tool Researcher lacked) -- the merge should not need to add or remove anything
    here, only fold in instructions."""
    agents = _engineering_agents()
    tools = effective_tools("engineering", "Researcher", agents["Researcher"].get("tools"))
    for tool in (
        "find_files", "search_files", "get_file_content", "list_directory",
        "list_directory_tree", "search_knowledge_graph", "lightrag_query",
        "web_search", "web_fetch", "notion_search", "notion_get_page", "load_skill",
    ):
        assert tool in tools, f"{tool} should still be on Researcher's tool list"


def test_coordinator_instructions_no_longer_list_planner_as_a_member():
    from swarm.team import _COORDINATOR_INSTRUCTIONS
    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "ContextRouter, Researcher, Planner, Coder" not in text
    assert "ContextRouter, Researcher, Coder, Executor, Reviewer" in text


def test_coordinator_instructions_tell_it_to_delegate_multi_part_tasks_whole():
    """The Phase 0 finding was that the COORDINATOR itself did the piecemeal
    decomposition via many narrow delegations -- the fix needs an explicit
    instruction telling it to stop doing that and delegate whole instead."""
    from swarm.team import _COORDINATOR_INSTRUCTIONS
    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "delegate the WHOLE thing to Researcher" in text or "WHOLE task to Researcher" in text
    assert "do NOT decompose the" in text


def test_make_planner_no_longer_exists_in_swarm_agents():
    import swarm.agents as agents_module
    assert not hasattr(agents_module, "make_planner")


def test_config_no_longer_has_planner_model():
    from config.config import Config
    assert not hasattr(Config(), "planner_model")


def test_model_routing_seed_no_longer_seeds_engineering_planner():
    from swarm.model_routing import _TEAM_ROLE_DEFAULTS
    assert ("engineering", "Planner", "qwen2.5-coder:32b") not in _TEAM_ROLE_DEFAULTS


def test_model_routing_seed_still_seeds_planning_planner():
    """Scope guard, mirrors test_planning_yaml_keeps_its_own_separate_planner_untouched
    at the DB-seed layer."""
    from swarm.model_routing import _TEAM_ROLE_DEFAULTS
    assert ("planning", "Planner", "qwen2.5-coder:32b") in _TEAM_ROLE_DEFAULTS
