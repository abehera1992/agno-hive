"""Tests for the 2026-08-15 search-before-browse + clarification-escalation fixes.

Root cause (found live testing the Parties module Phase 1 prompt via agno_run):
Researcher's very first targeted search on a GSTIN-shaped task was
find_files('API/business-service/**/*') -- guessing the wrong SERVICE by
domain-name association ("GSTIN" also exists on business-service's own seller
registration) before ever running a single content search for "Party". It then
burned ~40 near-duplicate get_file_content calls growing a read window one line
at a time on that same wrong file. Two distinct fixes:

1. Researcher's lightrag_query instruction was conditionally hedged ("if
   available via MCP, call it") even though api/server.py's _resolve_mcp_urls
   has connected it unconditionally on every run for some time -- the hedge
   gave the model an easy, plausible-sounding reason to skip it. Same stale
   hedge existed on Coder, Reviewer, and the _BASE_PREAMBLE fallback builders.
2. DECOMPOSE-FIRST's grounding step (Step 3) said "ground each checklist item"
   but never required searching BY CONTENT before browsing BY DIRECTORY NAME --
   new Step 3a closes that. New Step 3b gives Researcher a way to surface
   genuine (post-search) multi-candidate ambiguity in its own output, and a new
   coordinator instruction (_COORDINATOR_INSTRUCTIONS) treats that flagged
   ambiguity as a real request_clarification trigger -- distinct from "didn't
   search yet", which the pre-existing clarification guardrail already
   correctly excludes ("NOT a case for this: not knowing which file to edit").
"""
import yaml

from swarm.team import _COORDINATOR_INSTRUCTIONS
from swarm.agents import _BASE_PREAMBLE


def _load_engineering_yaml() -> dict:
    with open("teams/engineering.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _agent(data: dict, name: str) -> dict:
    return next(a for a in data["agents"] if a["name"] == name)


# ── lightrag_query hedge removed everywhere it appeared ─────────────────────

def test_researcher_lightrag_instruction_is_no_longer_conditionally_hedged():
    data = _load_engineering_yaml()
    researcher = _agent(data, "Researcher")
    joined = " ".join(researcher["instructions"])
    assert "if lightrag_query is available" not in joined.lower()
    assert "always connected" in joined.lower()
    assert "lightrag_query" in joined


def test_coder_lightrag_instruction_is_no_longer_conditionally_hedged():
    data = _load_engineering_yaml()
    coder = _agent(data, "Coder")
    joined = " ".join(coder["instructions"])
    assert "if lightrag_query is available" not in joined.lower()
    assert "always connected" in joined.lower()


def test_coder_namespace_consistency_step2_no_longer_hedged():
    data = _load_engineering_yaml()
    coder = _agent(data, "Coder")
    joined = " ".join(coder["instructions"])
    assert "if lightrag_query is available, call it" not in joined.lower()


def test_reviewer_lightrag_instruction_is_no_longer_conditionally_hedged():
    data = _load_engineering_yaml()
    reviewer = _agent(data, "Reviewer")
    joined = " ".join(reviewer["instructions"])
    assert "if lightrag_query is available" not in joined.lower()
    assert "always connected" in joined.lower()


def test_base_preamble_fallback_builders_lightrag_hedge_removed():
    joined = " ".join(_BASE_PREAMBLE)
    assert "if lightrag_query is available" not in joined.lower()
    assert "always connected" in joined.lower()


# ── DECOMPOSE-FIRST Step 3a (search-before-browse) ───────────────────────────

def test_researcher_has_search_before_browse_step():
    data = _load_engineering_yaml()
    researcher = _agent(data, "Researcher")
    joined = " ".join(researcher["instructions"])
    assert "Step 3a" in joined
    assert "SEARCH BEFORE YOU BROWSE" in joined
    assert "search_files" in joined
    assert "never guess which service/directory" in joined.lower()


def test_search_before_browse_step_cites_the_real_incident():
    """Grounds the rule in the actual live failure, not a hypothetical --
    matches this file's own docstring and the Notion plan page write-up."""
    data = _load_engineering_yaml()
    researcher = _agent(data, "Researcher")
    joined = " ".join(researcher["instructions"])
    assert "business-service" in joined
    assert "GSTIN" in joined


# ── DECOMPOSE-FIRST Step 3b (genuine multi-candidate ambiguity) ─────────────

def test_researcher_has_genuine_ambiguity_flagging_step():
    data = _load_engineering_yaml()
    researcher = _agent(data, "Researcher")
    joined = " ".join(researcher["instructions"])
    assert "Step 3b" in joined
    assert "GENUINE MULTI-CANDIDATE AMBIGUITY" in joined
    assert "request_clarification" in joined


def test_ambiguity_flagging_step_distinguishes_from_not_having_searched_yet():
    """The critical nuance: this must not become a license to ask before
    searching -- it explicitly excludes that case."""
    data = _load_engineering_yaml()
    researcher = _agent(data, "Researcher")
    joined = " ".join(researcher["instructions"])
    assert "not the same as simply not having searched yet" in joined.lower()


# ── SEARCH rule broadened beyond literal "how does X work" ─────────────────

def test_search_rule_covers_comparison_items_not_just_how_does_x_work():
    data = _load_engineering_yaml()
    researcher = _agent(data, "Researcher")
    joined = " ".join(researcher["instructions"])
    assert "not just literal 'how does x work' phrasing" in joined.lower()
    assert "comparison/gap-analysis items" in joined.lower()


def test_search_rule_forbids_directory_browsing_as_a_search_substitute():
    data = _load_engineering_yaml()
    researcher = _agent(data, "Researcher")
    joined = " ".join(researcher["instructions"])
    assert "do not browse by directory/service name as a substitute for searching" in joined.lower()


# ── Coordinator-side: flagged ambiguity is a real request_clarification trigger ──

def test_coordinator_instructions_treat_flagged_ambiguity_as_clarification_trigger():
    joined = " ".join(_COORDINATOR_INSTRUCTIONS)
    assert "2+ real" in joined
    assert "candidates as ambiguous ownership" in joined
    assert "request_clarification" in joined


def test_coordinator_instructions_still_exclude_not_having_searched_yet():
    """Must not regress the pre-existing guardrail: 'not knowing which file to
    edit' (i.e. not having searched) stays explicitly NOT a clarification case."""
    joined = " ".join(_COORDINATOR_INSTRUCTIONS)
    assert "not a case for this: not knowing which" in joined.lower()
    assert "not a 'didn't search yet' gap" in joined.lower()


def test_coordinator_instructions_forbid_silently_picking_one_or_redelegating_to_pick():
    joined = " ".join(_COORDINATOR_INSTRUCTIONS)
    assert "do not silently pick one on the team's behalf" in joined.lower()
    assert "just pick one" in joined.lower()
