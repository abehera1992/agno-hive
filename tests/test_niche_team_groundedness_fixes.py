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

5. Root-caused live during parallel-review validation (2026-08-15): `project_id`
   (the real LightRAG namespace, e.g. "ekam") was NEVER surfaced into any agent's
   instructions -- confirmed by reading lightrag_mcp/server.py's own tool
   signatures, `project_id` is a free-form argument the CALLING AGENT chooses.
   With nothing telling it the real value, agents guessed -- sometimes a
   plausible-but-wrong directory name, once an outright-fabricated UUID that made
   lightrag_query hard-error with "graph name is invalid". Confirmed via direct
   postgres query (agno.lightrag_doc_status.workspace, ZGX): the real indexed
   namespace is "ekam" (2,646 docs); "default" (the request-level fallback when a
   caller omits project_id) has only 2. `_project_id_preamble()` now states the
   real value explicitly, universally (all 4 teams, not team-scoped).

6. Same day, 3rd/4th live failures on `parallel-review`: even with a WORKING
   project_id (#5), lightrag_query kept "succeeding" with real but purely
   DESCRIPTIVE/summary content about a named file's role -- never its literal
   source -- so the earlier FALLBACK rule's failure-triggered condition (#3-shaped
   wording) never fired, and separately the model repeated the identical failing
   call 9+ times before the run's own budget ran out. Fixed with two changes:
   `lightrag_query` added to `_CACHEABLE_READ_TOOLS` (swarm/team.py) so the
   already-proven duplicate-serve escalation applies to it too, and a new
   KNOWN-PATH rule on all 3 review agents (parallel-review.yaml) requiring
   get_file_content(path) as the FIRST call whenever the task already names a
   specific path -- lightrag_query reserved for genuine discovery only.
"""
import inspect
from pathlib import Path

import pytest
import yaml

from swarm.team import _member_id, _project_id_preamble, _team_roster_preamble, run_task_async, run_task_stream

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


def test_fallback_rule_widened_to_cover_errors_not_just_empty_results():
    """2nd live failure, same day: the original wording ('no results, [no-context],
    or an unhelpful/empty answer') did not cover an outright ERROR string
    ('Query failed: graph name is invalid') -- the model kept retrying the SAME
    broken call across all 3 mode values (~40+ times) instead of ever falling
    back, because the instruction's trigger condition never matched a hard error.
    Must now explicitly cover error strings AND cap retries at one attempt."""
    for agent_name in ("Researcher", "SecurityReviewer", "PerformanceReviewer"):
        instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), agent_name)["instructions"]).lower()
        assert "error" in instructions
        assert "at most once" in instructions
        assert "do not retry" in instructions


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


def test_shows_the_real_member_id_form_not_just_the_display_name(monkeypatch):
    """2026-08-15 correction -- the original version showed ONLY the display name
    (e.g. "ContextRouter") as "the name to use", which is factually wrong for
    delegate_task_to_member's own member_id argument on a multi-word name (agno's
    real lookup key is "context-router", confirmed by reading
    agno.utils.string.url_safe_string directly). Live-confirmed as the actual root
    cause of a planning-team incident: the coordinator used the display name
    verbatim, failed, and concluded the whole delegation system was broken."""
    specs = [_spec("ContextRouter", "Lightweight query router.")]

    result = _team_roster_preamble(specs)
    joined = "\n".join(result)

    assert "context-router" in joined
    assert "ContextRouter" in joined  # display name still shown, for readability


def test_falls_back_to_role_when_description_is_none():
    specs = [_spec("Researcher", description=None, role="Senior engineer who investigates the codebase.")]

    result = _team_roster_preamble(specs)

    assert "Senior engineer who investigates the codebase." in "\n".join(result)


def test_result_is_prepended_ahead_of_coordinator_instructions_in_both_functions():
    """Source-inspection check (same convention as test_gate_team_scoping.py's
    team_name wiring checks) -- neither run_task_async nor run_task_stream is
    unit-tested directly elsewhere (heavy MCP/session setup); confirms BOTH
    preambles are prepended, not appended or omitted, in both places they're
    built, and that _project_id_preamble comes first (project namespace is more
    fundamental than the roster -- order between the two doesn't functionally
    matter, but this locks the actual shipped order so a future edit that
    silently drops one is caught)."""
    async_source = inspect.getsource(run_task_async)
    stream_source = inspect.getsource(run_task_stream)

    expected = (
        "_project_id_preamble(project_id) + _team_roster_preamble(agent_specs)\n"
        "        + list(_COORDINATOR_INSTRUCTIONS)"
    )
    assert expected in async_source
    assert expected in stream_source


# ── _project_id_preamble ─────────────────────────────────────────────────────────

def test_states_the_real_project_id_explicitly():
    result = _project_id_preamble("ekam")
    joined = "\n".join(result)

    assert "'ekam'" in joined
    assert "lightrag_query" in joined


def test_default_project_id_is_stated_too_not_special_cased():
    """"default" is a real, if near-empty, namespace -- the preamble must state
    whatever value it's given, not silently skip or special-case it. Guessing
    is the failure this exists to remove, for every value, not just the common
    ones."""
    result = _project_id_preamble("default")

    assert "'default'" in "\n".join(result)


def test_warns_against_guessing_or_inventing_a_value():
    result = _project_id_preamble("ekam")
    joined = "\n".join(result).lower()

    assert "never guess" in joined or "never" in joined and "guess" in joined


# ── project_id also reaching MEMBER agents (2026-08-15, 2nd live failure) ────────
#
# _project_id_preamble above is real but INSUFFICIENT alone: confirmed by reading
# agno/team/team.py directly, Team(instructions=...) only reaches the team
# LEADER/coordinator's own prompt -- member agents are separate Agent objects with
# their own instructions built purely from spec.instructions. Live-confirmed:
# SecurityReviewer/PerformanceReviewer kept fabricating a NEW UUID-shaped
# project_id on a run AFTER the coordinator-level fix had already deployed.
# make_agent_from_spec() now takes project_id directly and prepends the same
# instruction into each MEMBER agent's own prompt.

def test_make_agent_from_spec_prepends_project_id_instruction(monkeypatch):
    from api.models import AgentSpec
    from swarm.agents import make_agent_from_spec

    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    spec = AgentSpec(
        name="SecurityReviewer", role="reviewer", model="qwen2.5-coder:32b",
        instructions=["base instruction"],
    )

    agent = make_agent_from_spec(spec, project_id="ekam")

    joined = "\n".join(agent.instructions)
    assert "'ekam'" in joined
    assert "lightrag_query" in joined
    assert "base instruction" in joined


def test_make_agent_from_spec_without_project_id_is_unchanged(monkeypatch):
    monkeypatch.setattr("swarm.agents.config.inference_backend", "ollama")
    from api.models import AgentSpec
    from swarm.agents import make_agent_from_spec

    spec = AgentSpec(
        name="SecurityReviewer", role="reviewer", model="qwen2.5-coder:32b",
        instructions=["base instruction"],
    )

    agent = make_agent_from_spec(spec)

    assert agent.instructions == ["base instruction"]


def test_build_team_forwards_project_id_to_each_member_agent(monkeypatch):
    from api.models import AgentSpec
    from swarm.team import _build_team

    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    specs = [
        AgentSpec(name="Researcher", role="r", model="qwen2.5-coder:32b", instructions=["do research"]),
        AgentSpec(name="SecurityReviewer", role="r", model="qwen2.5-coder:32b", instructions=["do security"]),
    ]

    team = _build_team(
        specs, "qwen2.5-coder:32b", None, "broadcast", [], [],
        project_id="ekam",
    )

    for member in team.members:
        joined = "\n".join(member.instructions)
        assert "'ekam'" in joined


def test_run_task_async_and_stream_forward_project_id_to_build_team():
    """Source-inspection check, same convention as the other wiring tests in this
    file -- neither function is unit-tested directly (heavy MCP/session setup)."""
    async_source = inspect.getsource(run_task_async)
    stream_source = inspect.getsource(run_task_stream)

    assert "project_id=project_id" in async_source
    assert "project_id=project_id" in stream_source


# ── lightrag_query added to the duplicate-read cache (2026-08-15, 3rd live failure) ─
#
# Even with a WORKING project_id (both fixes above deployed), a parallel-review
# run had SecurityReviewer call lightrag_query with the IDENTICAL args 9+ times in
# a row -- each call genuinely succeeded (real content came back every time), but
# the agent wrote "failed, falling back to find_files" into shared state anyway
# and immediately repeated the SAME call instead of ever actually falling back.
# This is the exact self-reinforcing duplicate-read loop _make_read_cache_tool_hook
# already exists to stop for get_file_content/search_files/etc -- lightrag_query
# was simply never added to the set it watches. No new mechanism, just widening
# the existing, already-proven one.

def test_lightrag_query_is_in_the_cacheable_read_tools_set():
    from swarm.team import _CACHEABLE_READ_TOOLS

    assert "lightrag_query" in _CACHEABLE_READ_TOOLS


def test_search_knowledge_graph_is_in_the_cacheable_read_tools_set():
    """Same day, same pattern, different tool: a planning-team run cycled the
    SAME ~6 search_knowledge_graph queries twice in a row, each one succeeding,
    then went silent until the 300s liveness auto-kill fired."""
    from swarm.team import _CACHEABLE_READ_TOOLS

    assert "search_knowledge_graph" in _CACHEABLE_READ_TOOLS


def test_researcher_has_known_path_rule():
    instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), "Researcher")["instructions"]).lower()
    assert "known-path rule" in instructions
    assert "get_file_content" in instructions


def test_security_reviewer_has_known_path_rule():
    instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), "SecurityReviewer")["instructions"]).lower()
    assert "known-path rule" in instructions
    assert "get_file_content" in instructions


def test_performance_reviewer_has_known_path_rule():
    instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), "PerformanceReviewer")["instructions"]).lower()
    assert "known-path rule" in instructions
    assert "get_file_content" in instructions


def test_known_path_rule_explains_lightrag_is_summary_not_literal_source():
    """The actual root cause, not just tool-name presence: lightrag_query kept
    'succeeding' with real but purely descriptive content -- the model needs to
    understand WHY that's insufficient, not just be told a different tool name."""
    for agent_name in ("Researcher", "SecurityReviewer", "PerformanceReviewer"):
        instructions = " ".join(_agent(_load(_PARALLEL_REVIEW_YAML), agent_name)["instructions"]).lower()
        assert "summary" in instructions or "literal source" in instructions


@pytest.mark.asyncio
async def test_read_cache_hook_stubs_a_repeated_identical_lightrag_query_call():
    from swarm.team import _make_read_cache_tool_hook

    hook = _make_read_cache_tool_hook()

    async def fake_lightrag_query(**kwargs):
        return "── Project (ekam) ──\nThe file config/config.py is a central config file..."

    args = {"query": "Find config/config.py", "project_id": "ekam", "mode": "local"}
    first = await hook("lightrag_query", fake_lightrag_query, args)
    second = await hook("lightrag_query", fake_lightrag_query, args)
    third = await hook("lightrag_query", fake_lightrag_query, args)

    assert "central config file" in first
    assert "central config file" in second  # tolerated (serve 1-2 get real content)
    assert "Already returned this exact" in third  # serve 3+ gets the stub, not the real content again


# ── _member_id: the real agno delegate_task_to_member key (2026-08-15, root cause) ──
#
# Root-caused live during planning validation: agno's own `Team.get_member_id()`
# (confirmed by reading agno/utils/team.py + agno/utils/string.py directly) runs a
# member's display name through `url_safe_string`, which inserts a dash at every
# camelCase boundary before lowercasing -- "ContextRouter" -> "context-router".
# `agno/team/_tools.py`'s `_find_member_by_id` compares against this transformed
# value with a plain `==`, no normalization on its side. agno-hive's own
# _team_roster_preamble and _COORDINATOR_INSTRUCTIONS examples showed the raw
# display name as "the name to use" -- factually wrong for any multi-word agent.
# Live-confirmed: a planning-team coordinator tried
# delegate_task_to_member(member_id='ContextRouter', ...), failed, and concluded
# "a fundamental failure in the team member resolution system" instead of
# recognizing its own format mistake -- then abandoned delegation for the rest of
# the run (silently losing access to planning's Notion tools, which only the
# member agents have).

def test_member_id_single_word_names_are_just_lowercased():
    assert _member_id("Researcher") == "researcher"
    assert _member_id("Planner") == "planner"
    assert _member_id("Coder") == "coder"


def test_member_id_inserts_a_dash_at_camelcase_boundaries():
    assert _member_id("ContextRouter") == "context-router"
    assert _member_id("BacklogResearcher") == "backlog-researcher"
    assert _member_id("StoryWriter") == "story-writer"
    assert _member_id("SecurityReviewer") == "security-reviewer"
    assert _member_id("PerformanceReviewer") == "performance-reviewer"


# ── _COORDINATOR_DISCOVERY_TOOLS: lightrag_query/get_context_section excluded too ──
#
# The coordinator's direct tool surface always excludes _COORDINATOR_DISCOVERY_TOOLS
# regardless of a team's own coordinator_tools: YAML allowlist -- confirmed by
# reading _scope_coordinator_tools directly, this is a hard code-level filter, the
# same "tool surface constrains behavior, not just instructions" mechanism already
# proven live for find_files/search_files/etc (2026-08-11). lightrag_query and
# get_context_section were missed when that set was first built. Live-confirmed gaps,
# same day: a planning run had the coordinator call lightrag_query 3 times directly
# instead of delegating to Researcher (never reaching planning's Notion tools, which
# only member agents have); a parallel-review run had the coordinator call
# get_context_section ~25 times directly across topics with zero relevance to the
# task instead of ever delegating.

def _fake_mcp(functions: dict):
    from types import SimpleNamespace
    return SimpleNamespace(functions=functions)


def test_lightrag_query_and_get_context_section_are_coordinator_discovery_tools():
    from swarm.team import _COORDINATOR_DISCOVERY_TOOLS

    assert "lightrag_query" in _COORDINATOR_DISCOVERY_TOOLS
    assert "get_context_section" in _COORDINATOR_DISCOVERY_TOOLS
    assert "get_graph_report" in _COORDINATOR_DISCOVERY_TOOLS


def test_scope_coordinator_tools_excludes_lightrag_query_even_when_allowlisted():
    from swarm.team import _scope_coordinator_tools

    mcp = _fake_mcp({
        "lightrag_query": "lightrag_query_fn",
        "get_context_section": "get_context_section_fn",
        "get_file_content": "get_file_content_fn",
    })

    # Even though the allowlist explicitly names both discovery tools (matching
    # planning.yaml's/parallel-review.yaml's own real coordinator_tools: lists),
    # the hard exclusion still wins.
    scoped = _scope_coordinator_tools(
        ["lightrag_query", "get_context_section", "get_file_content"], [mcp], read_only=True,
    )
    assert "lightrag_query_fn" not in scoped
    assert "get_context_section_fn" not in scoped
    assert "get_file_content_fn" in scoped


# ── Notion discovery tools: engineering coordinator forced to delegate, ──────────
# ── sprint-master exempted (deliberate, tested prior design) ─────────────────────
#
# Live incident (2026-08-15, T10b engineering-team groundedness retest):
# teams/engineering.yaml has NO coordinator_tools: allowlist at all, so
# _scope_coordinator_tools' no-allowlist branch handed its coordinator every tool
# from every connected MCP except _COORDINATOR_DISCOVERY_TOOLS -- Notion included.
# Asked a sprint-lookup question, the coordinator hand-built raw
# notion_query_database relation filters itself (5 failed attempts) instead of
# delegating to a member with real Notion-usage instructions, and never
# synthesized a final answer. sprint-master.yaml is the deliberate exception --
# its own coordinator_tools comment documents that direct coordinator access to
# these same read tools was a tested fix for a different, earlier incident.

def test_notion_read_tools_are_in_the_notion_discovery_set():
    """Notion discovery tools are deliberately kept in their OWN set, not unioned
    into _COORDINATOR_DISCOVERY_TOOLS itself -- _scope_coordinator_tools' _keep()
    checks both sets together, but _COORDINATOR_DISCOVERY_TOOLS stays the
    unconditional (no team exemption possible) set, while _NOTION_DISCOVERY_TOOLS
    is the one _NOTION_DISCOVERY_EXEMPT_TEAMS can opt out of."""
    from swarm.team import _NOTION_DISCOVERY_TOOLS

    for tool in (
        "notion_search", "notion_get_page", "notion_get_database_schema",
        "notion_query_database", "notion_items_in_sprint",
        "notion_get_item_with_relations", "notion_find_work_item",
    ):
        assert tool in _NOTION_DISCOVERY_TOOLS


def test_notion_write_tools_are_never_in_either_discovery_set():
    """Only read/query tools are forced through delegation -- write tools stay
    directly callable by the coordinator, gated by WRITE_REVIEW like any other
    write, consistent with the project's own delivery-board-sync workflow."""
    from swarm.team import _NOTION_DISCOVERY_TOOLS, _COORDINATOR_DISCOVERY_TOOLS

    for tool in (
        "notion_create_page", "notion_update_page_props", "notion_append_blocks",
        "notion_append_markdown", "notion_trash_page", "notion_update_content",
        "notion_update_block", "notion_delete_block", "notion_replace_section",
    ):
        assert tool not in _NOTION_DISCOVERY_TOOLS
        assert tool not in _COORDINATOR_DISCOVERY_TOOLS


def test_engineering_coordinator_no_allowlist_reaches_notion_replace_section():
    """2026-08-18 live incident: notion_replace_section is a real, registered
    hive-mcp tool but was granted to NO team's tools:/coordinator_tools: allowlist
    (confirmed via grep across teams/*.yaml at the time). engineering.yaml has no
    coordinator_tools: allowlist at all, so its coordinator lands in the
    no-allowlist branch -- confirms it already reaches notion_replace_section
    there without any YAML change, since the tool is in neither discovery-exclusion
    set (see test_notion_write_tools_are_never_in_either_discovery_set above)."""
    from swarm.team import _scope_coordinator_tools

    mcp = _fake_mcp({"notion_replace_section": "notion_replace_section_fn"})

    scoped = _scope_coordinator_tools(None, [mcp], read_only=False, team_name="engineering")
    assert "notion_replace_section_fn" in scoped


def test_engineering_coordinator_notion_reads_excluded_even_when_allowlisted():
    from swarm.team import _scope_coordinator_tools

    mcp = _fake_mcp({
        "notion_search": "notion_search_fn",
        "notion_items_in_sprint": "notion_items_in_sprint_fn",
        "get_file_content": "get_file_content_fn",
    })

    scoped = _scope_coordinator_tools(
        ["notion_search", "notion_items_in_sprint", "get_file_content"], [mcp],
        read_only=False, team_name="engineering",
    )
    assert "notion_search_fn" not in scoped
    assert "notion_items_in_sprint_fn" not in scoped
    assert "get_file_content_fn" in scoped


def test_engineering_coordinator_no_allowlist_still_excludes_notion_reads():
    """The exact live-incident shape: engineering.yaml passes NO coordinator_tools
    at all, landing in the no-allowlist branch."""
    from swarm.team import _scope_coordinator_tools

    mcp = _fake_mcp({
        "notion_query_database": "notion_query_database_fn",
        "notion_get_item_with_relations": "notion_get_item_with_relations_fn",
        "apply_diff": "apply_diff_fn",
    })

    scoped = _scope_coordinator_tools(None, [mcp], read_only=False, team_name="engineering")
    assert "notion_query_database_fn" not in scoped
    assert "notion_get_item_with_relations_fn" not in scoped
    assert "apply_diff_fn" in scoped  # writes are unaffected -- only Notion reads are gated


def test_sprint_master_coordinator_keeps_direct_notion_read_access():
    """The deliberate exemption -- sprint-master's own coordinator_tools comment
    documents this direct access as a tested fix for a prior, unrelated incident
    (delegating board reads to BacklogResearcher thrashed ~400s and gave up)."""
    from swarm.team import _scope_coordinator_tools

    mcp = _fake_mcp({
        "notion_search": "notion_search_fn",
        "notion_query_database": "notion_query_database_fn",
        "notion_items_in_sprint": "notion_items_in_sprint_fn",
        "notion_get_item_with_relations": "notion_get_item_with_relations_fn",
        "notion_find_work_item": "notion_find_work_item_fn",
        "notion_get_database_schema": "notion_get_database_schema_fn",
        "notion_get_page": "notion_get_page_fn",
    })

    scoped = _scope_coordinator_tools(
        list(mcp.functions.keys()), [mcp], read_only=False, team_name="sprint-master",
    )
    for fn in mcp.functions.values():
        assert fn in scoped


def test_default_team_name_none_preserves_prior_behavior_notion_excluded():
    """A caller that doesn't pass team_name (every pre-2026-08-15 caller) gets the
    Notion tools excluded too -- None is not accidentally treated as exempt."""
    from swarm.team import _scope_coordinator_tools

    mcp = _fake_mcp({"notion_search": "notion_search_fn", "get_file_content": "get_file_content_fn"})

    scoped = _scope_coordinator_tools(["notion_search", "get_file_content"], [mcp], read_only=True)
    assert "notion_search_fn" not in scoped
    assert "get_file_content_fn" in scoped


def test_some_other_team_name_does_not_get_the_sprint_master_exemption():
    from swarm.team import _scope_coordinator_tools

    mcp = _fake_mcp({"notion_search": "notion_search_fn"})

    scoped = _scope_coordinator_tools(
        ["notion_search"], [mcp], read_only=False, team_name="parallel-review",
    )
    assert "notion_search_fn" not in scoped


def test_coordinator_instructions_delegation_examples_use_the_real_member_id():
    """The actual live bug: _COORDINATOR_INSTRUCTIONS' own example code told the
    coordinator to type the display name verbatim. Every delegate_task_to_member
    example naming the ContextRouter role must now use its real id."""
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    delegate_examples = [
        line for line in _COORDINATOR_INSTRUCTIONS
        if "delegate_task_to_member(" in line and "ContextRouter" in line
    ]
    assert delegate_examples == []  # no code example uses the wrong display-name form
    assert "delegate_task_to_member('context-router'" in text


def test_coordinator_instructions_have_a_conflict_resolution_section():
    """T2c live incident (2026-08-15, parallel-review groundedness retest): three
    independently-cited member reports quoted the real schema content from
    API/inventory-service/models.py; a fourth, uncited report said the file "was
    not found". The coordinator's synthesis sided with the single uncited negative
    report over three cited positive ones. This section is the prose-side fix --
    the mechanical-side fix is _make_duplicate_delegation_gate_hook (see
    tests/test_duplicate_delegation_gate_hook.py), which stops the duplicate
    broadcast that produced the conflicting report in the first place."""
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "Resolving conflicting member reports" in text
    assert "the CITED, QUOTED-CONTENT answer" in text
    assert "T2c parallel-review groundedness retest" in text


def test_conflict_resolution_section_precedes_general_rules():
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    idx_conflict = text.index("Resolving conflicting member reports")
    idx_general = text.index("General rules")
    assert idx_conflict < idx_general


def test_coordinator_instructions_have_an_entity_match_discipline_section():
    """T10/T11 live incidents (2026-08-15, engineering-team groundedness retest):
    real, correctly-fetched Notion items (T10) and a real, correctly-described CSV
    import feature (T11) were both cited as answering a question about "Parties"
    when neither actually named Parties -- verify_claims cannot catch this since
    nothing was fabricated, only mis-attributed. This section is a prose-only
    discipline (no mechanical grep can verify semantic relevance the way existence
    checks work), consistent with verify.py's own documented MISATTRIBUTED SYMBOLS
    limitation ("proves existence, not the claimed relationship")."""
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "Entity-match discipline" in text
    assert "hive-mcp Notion tooling enhancements" in text
    assert "tally_import_api.py" in text
    assert "prose-only discipline" in text


def test_entity_match_discipline_section_precedes_general_rules():
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    idx_entity = text.index("Entity-match discipline")
    idx_general = text.index("General rules")
    assert idx_entity < idx_general


def test_entity_match_discipline_follows_conflict_resolution_section():
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    idx_conflict = text.index("Resolving conflicting member reports")
    idx_entity = text.index("Entity-match discipline")
    assert idx_conflict < idx_entity


def test_coordinator_instructions_have_a_no_process_narration_section():
    """T5/T7/T8/T9/T12/T13 live incidents (2026-08-15/16, engineering-team
    groundedness retest): every one of these otherwise-correct runs opened its
    delivered answer with process narration ("I'll review...", "I apologize for
    the error...", "Now I'll examine...") baked directly into agno's own
    final_run_output.content -- a genuinely different bug from the earlier
    fallback-accumulator narration leak (already fixed, see
    test_stream_narration_leak_fix.py), since final_run_output.content is
    non-empty here; the model itself wrote the narration as part of its real
    answer. This is a prose-only fix by design -- narration is "what the model
    says," the class of problem this codebase's own established lesson says
    instructions CAN shape (unlike tool-choice/action problems, which need
    mechanical enforcement)."""
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "No process narration in your final answer" in text
    assert "I apologize for the error, let me try again" in text
    assert "not a scratchpad" in text


def test_no_process_narration_section_precedes_general_rules():
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    idx_narration = text.index("No process narration")
    idx_general = text.index("General rules")
    assert idx_narration < idx_general


def test_no_process_narration_section_follows_entity_match_discipline():
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    idx_entity = text.index("Entity-match discipline")
    idx_narration = text.index("No process narration")
    assert idx_entity < idx_narration


def test_shared_state_section_covers_reworded_delegation_duplicates():
    """T2c live incident (2026-08-15, parallel-review groundedness retest): a
    reworded (not byte-identical) re-delegation of the same underlying question
    evades the mechanical duplicate-delegation gate (_make_duplicate_delegation_
    gate_hook only blocks exact/whitespace-normalized repeats, by design -- two
    empirically-tested similarity metrics, SequenceMatcher ratio and Jaccard
    word-overlap, were both found to produce unsafe false positives on
    realistic legitimate delegation sequences, e.g. asking about "Party" fields
    then legitimately asking about "PartyRegistration" fields). This is a
    prose-only strengthening of the existing session_state guidance, not a new
    mechanical gate, for the same reason entity-match discipline and
    no-process-narration are prose-only."""
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "REWORDED duplicates count too" in text
    assert "PartyRegistration have" in text
    assert "am I asking about the SAME target" in text


def test_reworded_duplicate_guidance_lives_in_the_session_state_section():
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    idx_session_state = text.index("Shared state across the whole run")
    idx_reworded = text.index("REWORDED duplicates count too")
    idx_multi_mcp = text.index("Multi-MCP tool selection")
    assert idx_session_state < idx_reworded < idx_multi_mcp


def test_no_process_narration_section_tells_coordinator_to_clean_up_member_reports():
    """The instructions must not claim they "apply to member agents too" -- they
    structurally can't, since _COORDINATOR_INSTRUCTIONS only ever reaches the
    coordinator (member agents get separate, static instructions from their own
    YAML specs). The section must instead tell the coordinator to clean up a
    member's narration when synthesising, which the coordinator genuinely can do."""
    from swarm.team import _COORDINATOR_INSTRUCTIONS

    text = "\n".join(_COORDINATOR_INSTRUCTIONS)
    assert "these instructions do not reach member agents, only you" in text
    assert "do NOT forward a" in text
