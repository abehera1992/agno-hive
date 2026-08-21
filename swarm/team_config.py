"""DB-backed team config (AGNOHive 2.3.3, 2026-08-18) — three tiers, each with
its own precedence:
  Tier 1 (tools/skills) — the DB is the actual runtime source. A team YAML's
    own tools:/skills:, when present, still wins outright (same "pin it back
    here to take it out of DB control" escape hatch model: already has), but
    all 4 shipped teams/*.yaml have had those fields removed, so the DB
    supplies them by default (see _DEFAULT_TOOL_GRANTS/_DEFAULT_SKILL_GRANTS
    below and api/server.py's _load_team()). Changed 2026-08-18 from an
    earlier additive-union design specifically to make the DB the primary
    source rather than a YAML-plus-extras layer.
  Tier 2 (instructions) — additive-only supplementary overlays, appended after
    a role's base instructions:, never replacing them (explicit user
    requirement — the base instruction set stays entirely YAML-owned).
  Tier 3 (gates) — a DB override for decompose_first/search_before_browse,
    falling back to the existing hardcoded team-membership default.
See the Notion design page "AGNOHive 2.3.3 - Moving team yaml configs to
sqlite db" for the full rationale.

Mirrors swarm/model_routing.py's shape deliberately: an in-process cache, loaded
once at startup (ensure_cache_loaded()) and refreshed only via an explicit
reload() call — the same "load-at-startup + explicit reload, no background TTL
polling" decision already made for model routing applies here for the same
reason (explicit over time-based).

Tier 1 (tools/skills) is consulted synchronously from api/server.py's
_load_team() — a request-time, not hot-path-per-agent-construction, call site,
so (unlike model_routing.get_route()) these lookups don't need to be as
allocation-free; they still stay cache-only, never touching the DB directly on
that path.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.sql import func

from swarm import db

# Soft cap on active team_role_instruction_overlays rows per (team_name,
# role_name) -- Open Question #3's resolution. Enforced at write time by
# api/server.py's admin endpoint (POST returns 409 once the cap would be
# exceeded), not here -- this module is cache/read-path only, matching
# model_routing.py's own split (api/server.py owns writes + validation, this
# module owns the in-process read cache). 5, not a range: this codebase's own
# thresholds are tuned against real usage, not picked as a range on paper (see
# the Notion page's Open Question #3 resolution) -- 5 is the conservative
# starting point pending that real-usage data.
INSTRUCTION_OVERLAY_SOFT_CAP = 5

# The only two mechanical gates Open Question #1 applies to -- matches
# swarm/team.py's _make_decompose_first_gate_hook / _make_search_before_browse_gate_hook.
GATE_NAMES = frozenset({"decompose_first", "search_before_browse"})

# Clearly-marked header a role's appended overlay instructions are grouped
# under, so a misbehaving agent's cause is immediately obvious: the tested base
# instructions, or a user addition (see the Notion design's Tier 2 rationale).
OVERLAY_HEADER = "-- User-added notes (unreviewed, use with care) --"


@dataclass(frozen=True)
class InstructionOverlay:
    id: int
    team_name: str
    role_name: str
    instruction_text: str
    active: bool


_tools_cache: dict[tuple[str, str], set[str]] = {}
_skills_cache: dict[tuple[str, str], set[str]] = {}
_overlay_cache: dict[tuple[str, str], list[InstructionOverlay]] = {}
_gate_flags_cache: dict[tuple[str, str], bool] = {}
_tool_registry_cache: set[str] = set()
_skill_registry_cache: set[str] = set()
_cache_loaded = False
_load_lock = asyncio.Lock()


def get_extra_tools(team_name: str, role_name: str) -> list[str]:
    """Sync, cache-only. The DB-granted tool list for this (team, role) —
    api/server.py's _load_team() uses this as the role's FULL tool list
    whenever the team YAML omits tools:/coordinator_tools: (see that
    function's Tier 1 comment). Name kept from the original additive-union
    design; the DB is now the primary source, not an addition on top."""
    return sorted(_tools_cache.get((team_name, role_name), set()))


def get_extra_skills(team_name: str, role_name: str) -> list[str]:
    """Sync, cache-only. Same contract as get_extra_tools()."""
    return sorted(_skills_cache.get((team_name, role_name), set()))


def get_instruction_overlays(team_name: str, role_name: str) -> list[str]:
    """Sync, cache-only. Active overlay instruction TEXT for this (team, role),
    in insertion order -- the caller (api/server.py's _load_team()) appends
    these after the role's base instructions, under OVERLAY_HEADER. Empty list
    when there are none, so a caller can always safely extend with the result
    (no None check needed)."""
    return [o.instruction_text for o in _overlay_cache.get((team_name, role_name), []) if o.active]


def get_gate_enabled(team_name: str | None, gate_name: str, default: bool) -> bool:
    """Sync, cache-only. Open Question #1's resolution: a DB row for this exact
    (team_name, gate_name) pair OVERRIDES the caller-supplied `default` (which
    is swarm/team.py's own existing _GATE_ENABLED_TEAMS/_SEARCH_GATE_ENABLED_TEAMS
    set-membership check) -- no row falls back to `default` unchanged, so every
    team with no override behaves exactly as before this feature existed."""
    if team_name is None:
        return default
    return _gate_flags_cache.get((team_name, gate_name), default)


def is_tool_registered(tool_name: str) -> bool:
    """Sync, cache-only. Used by api/server.py's admin endpoint to reject an
    unregistered tool_name at write time (Open Question #2's resolution) —
    never silently accepted, and never checked against a hand-maintained list
    that could drift from what hive-mcp actually exposes (see
    refresh_registry())."""
    return tool_name in _tool_registry_cache


def is_skill_registered(skill_name: str) -> bool:
    """Sync, cache-only. Same contract as is_tool_registered()."""
    return skill_name in _skill_registry_cache


async def ensure_cache_loaded() -> None:
    """Idempotent — safe to call from every request path. Loads the cache at
    most once per process unless reload() is called explicitly. Mirrors
    swarm/model_routing.ensure_cache_loaded() exactly."""
    global _cache_loaded
    if _cache_loaded:
        return
    async with _load_lock:
        if _cache_loaded:
            return
        await load_cache()
        _cache_loaded = True


async def load_cache() -> None:
    """(Re)populate every in-process cache from the DB, seeding defaults into
    brand-new/empty tables first — see _seed_defaults()'s own docstring for why
    this only ever runs once, against an empty team_role_tools, and never
    touches an already-populated deployment's rows."""
    await db.ensure_routing_schema()
    async with db.get_routing_engine().begin() as conn:
        existing = (await conn.execute(select(db.team_role_tools.c.team_name).limit(1))).first()
        if existing is None:
            await _seed_defaults(conn)

        tool_rows = (await conn.execute(select(db.team_role_tools))).mappings().all()
        skill_rows = (await conn.execute(select(db.team_role_skills))).mappings().all()
        overlay_rows = (
            await conn.execute(select(db.team_role_instruction_overlays).order_by(db.team_role_instruction_overlays.c.id))
        ).mappings().all()
        gate_rows = (await conn.execute(select(db.team_gate_flags))).mappings().all()
        tool_registry_rows = (await conn.execute(select(db.tool_registry.c.tool_name))).all()
        skill_registry_rows = (await conn.execute(select(db.skill_registry.c.skill_name))).all()

    _tools_cache.clear()
    for r in tool_rows:
        _tools_cache.setdefault((r["team_name"], r["role_name"]), set()).add(r["tool_name"])

    _skills_cache.clear()
    for r in skill_rows:
        _skills_cache.setdefault((r["team_name"], r["role_name"]), set()).add(r["skill_name"])

    _overlay_cache.clear()
    for r in overlay_rows:
        key = (r["team_name"], r["role_name"])
        _overlay_cache.setdefault(key, []).append(
            InstructionOverlay(
                id=r["id"], team_name=r["team_name"], role_name=r["role_name"],
                instruction_text=r["instruction_text"], active=r["active"],
            )
        )

    _gate_flags_cache.clear()
    for r in gate_rows:
        _gate_flags_cache[(r["team_name"], r["gate_name"])] = r["enabled"]

    _tool_registry_cache.clear()
    _tool_registry_cache.update(row[0] for row in tool_registry_rows)
    _skill_registry_cache.clear()
    _skill_registry_cache.update(row[0] for row in skill_registry_rows)


async def reload() -> dict:
    """Re-read the DB into the cache and return a diff, same "show what actually
    changed, not a bare 200" contract as model_routing.reload()."""
    before_overlay_count = sum(len(v) for v in _overlay_cache.values())
    before_tool_grants = {k: set(v) for k, v in _tools_cache.items()}
    before_skill_grants = {k: set(v) for k, v in _skills_cache.items()}
    before_gates = dict(_gate_flags_cache)

    await load_cache()

    after_overlay_count = sum(len(v) for v in _overlay_cache.values())
    tool_grants_added = {
        f"{t}/{r}": sorted(_tools_cache.get((t, r), set()) - before_tool_grants.get((t, r), set()))
        for (t, r) in _tools_cache
        if _tools_cache.get((t, r), set()) - before_tool_grants.get((t, r), set())
    }
    skill_grants_added = {
        f"{t}/{r}": sorted(_skills_cache.get((t, r), set()) - before_skill_grants.get((t, r), set()))
        for (t, r) in _skills_cache
        if _skills_cache.get((t, r), set()) - before_skill_grants.get((t, r), set())
    }
    gates_changed = [
        f"{t}/{g}" for (t, g) in _gate_flags_cache
        if _gate_flags_cache[(t, g)] != before_gates.get((t, g))
    ]
    return {
        "tool_grants_added": tool_grants_added,
        "skill_grants_added": skill_grants_added,
        "overlay_count_delta": after_overlay_count - before_overlay_count,
        "gates_changed": gates_changed,
        "tool_registry_size": len(_tool_registry_cache),
        "skill_registry_size": len(_skill_registry_cache),
    }


async def refresh_registry(tool_names: list[str], skill_names: list[str]) -> None:
    """Refresh tool_registry/skill_registry FROM a caller-supplied live
    enumeration (hive-mcp's real tool list, list_skills()'s real catalog) —
    deliberately NOT hand-maintained (see swarm/db.py's tool_registry comment
    for why that would drift). This module has no live MCP connection of its
    own to enumerate from directly (that only exists inside an active swarm-run
    context, not a standalone admin-API call), so the caller — an admin
    endpoint given an explicit list, or a future task that has a live MCP
    session open — supplies the current truth. Upserts (updates last_seen_at
    for names still present) rather than wiping and reinserting, so a
    momentarily-incomplete list passed by a caller doesn't spuriously
    de-register a real tool."""
    async with db.get_routing_engine().begin() as conn:
        for name in tool_names:
            existing = (
                await conn.execute(select(db.tool_registry.c.tool_name).where(db.tool_registry.c.tool_name == name))
            ).first()
            if existing is None:
                await conn.execute(db.tool_registry.insert().values(tool_name=name))
            else:
                await conn.execute(
                    db.tool_registry.update().where(db.tool_registry.c.tool_name == name).values(last_seen_at=func.now())
                )
        for name in skill_names:
            existing = (
                await conn.execute(select(db.skill_registry.c.skill_name).where(db.skill_registry.c.skill_name == name))
            ).first()
            if existing is None:
                await conn.execute(db.skill_registry.insert().values(skill_name=name))
            else:
                await conn.execute(
                    db.skill_registry.update().where(db.skill_registry.c.skill_name == name).values(last_seen_at=func.now())
                )


async def reset_cache_for_tests() -> None:
    """Test-only: mirrors swarm/model_routing.reset_cache_for_tests()."""
    global _cache_loaded
    _tools_cache.clear()
    _skills_cache.clear()
    _overlay_cache.clear()
    _gate_flags_cache.clear()
    _tool_registry_cache.clear()
    _skill_registry_cache.clear()
    _cache_loaded = False


# ── Seed data ─────────────────────────────────────────────────────────────────
# Populates team_role_tools/team_role_skills so a fresh deployment's behavior is
# byte-for-byte identical to the pre-DB-migration behavior on first run. Runs
# once, only when team_role_tools is empty (see load_cache() above) — never
# overwrites rows an admin has since added. team_role_instruction_overlays and
# team_gate_flags deliberately start EMPTY for every role (see the Notion
# design's Phase 1 — overlays are purely user-added going forward, and every
# team's gate behavior stays exactly as the hardcoded
# _GATE_ENABLED_TEAMS/_SEARCH_GATE_ENABLED_TEAMS sets already define until an
# admin explicitly overrides one).
#
# 2026-08-18 follow-up: this used to be parsed live from each teams/*.yaml's
# tools:/skills: content at seed time. Once the DB became the actual runtime
# source (api/server.py's _load_team() now reads a role's tools/skills from
# here whenever the YAML omits the field — see that function's Tier 1 comment)
# and teams/*.yaml had those fields removed accordingly, there was nothing left
# in the YAML for a fresh deployment to seed FROM — the static snapshot below
# is what the YAML content actually was at migration time, captured once and
# now the sole source of truth for a first-run seed. Editing an EXISTING
# deployment's grants goes through the admin API below, not this snapshot.
#
# tool_registry/skill_registry are seeded from the union of every name below —
# a reasonable, honest bootstrap (everything here was in active use at
# migration time, so definitely a real, valid name) — NOT a substitute for
# refresh_registry()'s live-MCP-sourced refresh, which is the real source of
# truth once available. A name granted via the admin API that isn't yet in the
# registry (a brand-new hive-mcp tool that hasn't been through a
# refresh_registry() call yet) is correctly rejected until the registry is
# refreshed — fail loud, not silently accept an unverifiable name.

_DEFAULT_TOOL_GRANTS: set[tuple[str, str, str]] = {
    ("engineering", "Coder", "apply_diff"),
    ("engineering", "Coder", "find_files"),
    ("engineering", "Coder", "get_file_content"),
    ("engineering", "Coder", "lightrag_insert"),
    ("engineering", "Coder", "lightrag_query"),
    ("engineering", "Coder", "list_directory"),
    ("engineering", "Coder", "load_skill"),
    ("engineering", "Coder", "run_command"),
    ("engineering", "Coder", "search_files"),
    ("engineering", "Coder", "search_knowledge_graph"),
    ("engineering", "Coder", "write_file"),
    ("engineering", "ContextRouter", "find_files"),
    ("engineering", "ContextRouter", "lightrag_query"),
    ("engineering", "ContextRouter", "list_directory"),
    ("engineering", "ContextRouter", "list_directory_tree"),
    ("engineering", "ContextRouter", "load_skill"),
    ("engineering", "ContextRouter", "notion_get_page"),
    ("engineering", "ContextRouter", "notion_search"),
    ("engineering", "ContextRouter", "search_files"),
    ("engineering", "ContextRouter", "search_knowledge_graph"),
    ("engineering", "ContextRouter", "web_fetch"),
    ("engineering", "ContextRouter", "web_search"),
    ("engineering", "Executor", "bash_job_kill"),
    ("engineering", "Executor", "bash_job_status"),
    ("engineering", "Executor", "bash_run"),
    ("engineering", "Executor", "bash_session_close"),
    ("engineering", "Executor", "bash_session_start"),
    ("engineering", "Executor", "check_port"),
    ("engineering", "Executor", "get_env_info"),
    ("engineering", "Executor", "get_file_content"),
    ("engineering", "Executor", "git_blame"),
    ("engineering", "Executor", "git_diff"),
    ("engineering", "Executor", "git_log"),
    ("engineering", "Executor", "git_log_file"),
    ("engineering", "Executor", "git_status"),
    ("engineering", "Executor", "list_processes"),
    ("engineering", "Executor", "load_skill"),
    ("engineering", "Executor", "run_command"),
    ("engineering", "Executor", "run_docker"),
    ("engineering", "Executor", "run_shell"),
    # db_query/db_schema granted 2026-08-20, together with engineering.yaml's
    # `coordinator_tools: []`. They had been granted to NO role in ANY team (confirmed
    # against the live data/model_routing.db), reaching the coordinator only because it
    # received everything outside _COORDINATOR_DISCOVERY_TOOLS -- so a DB question was
    # structurally un-delegatable and the coordinator always answered it alone.
    # Researcher is the right owner: it already holds get_file_content/search_files, so
    # it is the one agent that can choose between reading the source and querying the
    # live DB with both options actually in hand.
    ("engineering", "Researcher", "db_query"),
    ("engineering", "Researcher", "db_schema"),
    ("engineering", "Researcher", "find_files"),
    ("engineering", "Researcher", "get_file_content"),
    ("engineering", "Researcher", "lightrag_query"),
    ("engineering", "Researcher", "list_directory"),
    ("engineering", "Researcher", "list_directory_tree"),
    ("engineering", "Researcher", "load_skill"),
    ("engineering", "Researcher", "notion_get_page"),
    ("engineering", "Researcher", "notion_search"),
    ("engineering", "Researcher", "search_files"),
    ("engineering", "Researcher", "search_knowledge_graph"),
    ("engineering", "Researcher", "web_fetch"),
    ("engineering", "Researcher", "web_search"),
    ("engineering", "Reviewer", "find_files"),
    ("engineering", "Reviewer", "get_file_content"),
    ("engineering", "Reviewer", "git_diff"),
    ("engineering", "Reviewer", "git_status"),
    ("engineering", "Reviewer", "lightrag_insert"),
    ("engineering", "Reviewer", "lightrag_query"),
    ("engineering", "Reviewer", "load_skill"),
    ("engineering", "Reviewer", "search_files"),
    ("engineering", "Reviewer", "search_knowledge_graph"),
    ("parallel-review", "Coordinator", "find_files"),
    ("parallel-review", "Coordinator", "get_context_section"),
    ("parallel-review", "Coordinator", "get_file_content"),
    ("parallel-review", "Coordinator", "lightrag_query"),
    ("parallel-review", "Coordinator", "list_directory"),
    ("parallel-review", "Coordinator", "list_directory_tree"),
    ("parallel-review", "Coordinator", "load_skill"),
    ("parallel-review", "Coordinator", "search_files"),
    ("parallel-review", "Coordinator", "search_knowledge_graph"),
    ("parallel-review", "PerformanceReviewer", "find_files"),
    ("parallel-review", "PerformanceReviewer", "get_file_content"),
    ("parallel-review", "PerformanceReviewer", "lightrag_query"),
    ("parallel-review", "PerformanceReviewer", "load_skill"),
    ("parallel-review", "PerformanceReviewer", "search_files"),
    ("parallel-review", "Researcher", "find_files"),
    ("parallel-review", "Researcher", "get_file_content"),
    ("parallel-review", "Researcher", "lightrag_query"),
    ("parallel-review", "Researcher", "list_directory_tree"),
    ("parallel-review", "Researcher", "load_skill"),
    ("parallel-review", "Researcher", "search_files"),
    ("parallel-review", "SecurityReviewer", "find_files"),
    ("parallel-review", "SecurityReviewer", "get_file_content"),
    ("parallel-review", "SecurityReviewer", "git_diff"),
    ("parallel-review", "SecurityReviewer", "lightrag_query"),
    ("parallel-review", "SecurityReviewer", "load_skill"),
    ("parallel-review", "SecurityReviewer", "search_files"),
    ("planning", "ContextRouter", "find_files"),
    ("planning", "ContextRouter", "get_file_content"),
    ("planning", "ContextRouter", "lightrag_query"),
    ("planning", "ContextRouter", "list_directory"),
    ("planning", "ContextRouter", "list_directory_tree"),
    ("planning", "ContextRouter", "notion_get_page"),
    ("planning", "ContextRouter", "notion_search"),
    ("planning", "ContextRouter", "search_files"),
    ("planning", "ContextRouter", "search_knowledge_graph"),
    ("planning", "ContextRouter", "web_fetch"),
    ("planning", "ContextRouter", "web_search"),
    ("planning", "Coordinator", "find_files"),
    ("planning", "Coordinator", "get_context_section"),
    ("planning", "Coordinator", "get_file_content"),
    ("planning", "Coordinator", "lightrag_query"),
    ("planning", "Coordinator", "list_directory"),
    ("planning", "Coordinator", "list_directory_tree"),
    ("planning", "Coordinator", "notion_get_page"),
    ("planning", "Coordinator", "notion_search"),
    ("planning", "Coordinator", "search_files"),
    ("planning", "Coordinator", "search_knowledge_graph"),
    ("planning", "Planner", "find_files"),
    ("planning", "Planner", "get_file_content"),
    ("planning", "Planner", "lightrag_query"),
    ("planning", "Planner", "notion_get_page"),
    ("planning", "Planner", "notion_search"),
    ("planning", "Planner", "search_files"),
    ("planning", "Planner", "search_knowledge_graph"),
    ("planning", "Researcher", "find_files"),
    ("planning", "Researcher", "get_file_content"),
    ("planning", "Researcher", "lightrag_query"),
    ("planning", "Researcher", "list_directory"),
    ("planning", "Researcher", "list_directory_tree"),
    ("planning", "Researcher", "notion_get_page"),
    ("planning", "Researcher", "notion_search"),
    ("planning", "Researcher", "search_files"),
    ("planning", "Researcher", "search_knowledge_graph"),
    ("planning", "Researcher", "web_fetch"),
    ("planning", "Researcher", "web_search"),
    ("sprint-master", "BacklogResearcher", "find_files"),
    ("sprint-master", "BacklogResearcher", "get_file_content"),
    ("sprint-master", "BacklogResearcher", "load_skill"),
    ("sprint-master", "BacklogResearcher", "notion_find_work_item"),
    ("sprint-master", "BacklogResearcher", "notion_get_database_schema"),
    ("sprint-master", "BacklogResearcher", "notion_get_item_with_relations"),
    ("sprint-master", "BacklogResearcher", "notion_get_page"),
    ("sprint-master", "BacklogResearcher", "notion_items_in_sprint"),
    ("sprint-master", "BacklogResearcher", "notion_query_database"),
    ("sprint-master", "BacklogResearcher", "notion_search"),
    ("sprint-master", "Coordinator", "find_files"),
    ("sprint-master", "Coordinator", "get_file_content"),
    ("sprint-master", "Coordinator", "load_skill"),
    ("sprint-master", "Coordinator", "notion_append_blocks"),
    ("sprint-master", "Coordinator", "notion_append_markdown"),
    ("sprint-master", "Coordinator", "notion_create_page"),
    ("sprint-master", "Coordinator", "notion_delete_block"),
    ("sprint-master", "Coordinator", "notion_find_work_item"),
    ("sprint-master", "Coordinator", "notion_get_database_schema"),
    ("sprint-master", "Coordinator", "notion_get_item_with_relations"),
    ("sprint-master", "Coordinator", "notion_get_page"),
    ("sprint-master", "Coordinator", "notion_items_in_sprint"),
    ("sprint-master", "Coordinator", "notion_query_database"),
    ("sprint-master", "Coordinator", "notion_replace_section"),
    ("sprint-master", "Coordinator", "notion_search"),
    ("sprint-master", "Coordinator", "notion_trash_page"),
    ("sprint-master", "Coordinator", "notion_update_block"),
    ("sprint-master", "Coordinator", "notion_update_content"),
    ("sprint-master", "Coordinator", "notion_update_page_props"),
    ("sprint-master", "StoryWriter", "get_file_content"),
    ("sprint-master", "StoryWriter", "load_skill"),
    ("sprint-master", "StoryWriter", "notion_append_blocks"),
    ("sprint-master", "StoryWriter", "notion_append_markdown"),
    ("sprint-master", "StoryWriter", "notion_create_page"),
    ("sprint-master", "StoryWriter", "notion_delete_block"),
    ("sprint-master", "StoryWriter", "notion_get_database_schema"),
    ("sprint-master", "StoryWriter", "notion_get_page"),
    ("sprint-master", "StoryWriter", "notion_query_database"),
    ("sprint-master", "StoryWriter", "notion_replace_section"),
    ("sprint-master", "StoryWriter", "notion_search"),
    ("sprint-master", "StoryWriter", "notion_trash_page"),
    ("sprint-master", "StoryWriter", "notion_update_block"),
    ("sprint-master", "StoryWriter", "notion_update_content"),
    ("sprint-master", "StoryWriter", "notion_update_page_props"),
}

_DEFAULT_SKILL_GRANTS: set[tuple[str, str, str]] = {
    ("engineering", "Coder", "code-conventions"),
    ("engineering", "Coder", "counting-marker"),
    ("engineering", "Coder", "file-write-review"),
    ("engineering", "Coder", "verification-discipline"),
    ("engineering", "ContextRouter", "verification-discipline"),
    ("engineering", "Executor", "bash-sessions"),
    ("engineering", "Executor", "file-write-review"),
    ("engineering", "Executor", "verification-discipline"),
    ("engineering", "Researcher", "chain-tracing-discipline"),
    ("engineering", "Researcher", "codebase-enumeration-discipline"),
    ("engineering", "Researcher", "external-framework-verification"),
    ("engineering", "Researcher", "external-web-research"),
    ("engineering", "Researcher", "notion-reference-discovery"),
    ("engineering", "Researcher", "path-not-found-recovery"),
    ("engineering", "Researcher", "verification-discipline"),
    ("engineering", "Reviewer", "file-write-review"),
    ("engineering", "Reviewer", "verification-discipline"),
    ("parallel-review", "PerformanceReviewer", "verification-discipline"),
    ("parallel-review", "Researcher", "verification-discipline"),
    ("parallel-review", "SecurityReviewer", "verification-discipline"),
    ("sprint-master", "BacklogResearcher", "notion-grounding"),
    ("sprint-master", "BacklogResearcher", "verification-discipline"),
    ("sprint-master", "StoryWriter", "file-write-review"),
    ("sprint-master", "StoryWriter", "notion-grounding"),
}


async def _seed_defaults(conn) -> None:
    all_tools = {name for (_, _, name) in _DEFAULT_TOOL_GRANTS}
    all_skills = {name for (_, _, name) in _DEFAULT_SKILL_GRANTS}

    if _DEFAULT_TOOL_GRANTS:
        await conn.execute(
            db.team_role_tools.insert(),
            [{"team_name": t, "role_name": r, "tool_name": n} for (t, r, n) in _DEFAULT_TOOL_GRANTS],
        )
    if _DEFAULT_SKILL_GRANTS:
        await conn.execute(
            db.team_role_skills.insert(),
            [{"team_name": t, "role_name": r, "skill_name": n} for (t, r, n) in _DEFAULT_SKILL_GRANTS],
        )
    if all_tools:
        await conn.execute(db.tool_registry.insert(), [{"tool_name": t} for t in sorted(all_tools)])
    if all_skills:
        await conn.execute(db.skill_registry.insert(), [{"skill_name": s} for s in sorted(all_skills)])
