"""DB-backed team config additions (AGNOHive 2.3.3, 2026-08-18) — per-role tool/
skill allowlist ADDITIONS, additive-only supplementary instructions, and per-team
gate on/off flags, layered on top of teams/*.yaml, never replacing it. See the
Notion design page "AGNOHive 2.3.3 - Moving team yaml configs to sqlite db" for
the full three-tier rationale this module implements.

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
    """Sync, cache-only. Additional tool names granted via the DB for this
    (team, role) -- to be UNIONED with the team YAML's own tools: list, never
    replacing it (see swarm/db.py's team_role_tools comment)."""
    return sorted(_tools_cache.get((team_name, role_name), set()))


def get_extra_skills(team_name: str, role_name: str) -> list[str]:
    """Sync, cache-only. Same additive-union contract as get_extra_tools()."""
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
# Populates team_role_tools/team_role_skills from each shipped team YAML's
# CURRENT tools:/skills: content, so a fresh deployment's behavior is byte-for-
# byte identical to today's pre-DB-migration behavior on first run (the DB
# grants are a union superset that happens to equal the YAML content exactly at
# seed time — see swarm/db.py's team_role_tools comment). Runs once, only when
# team_role_tools is empty (see load_cache() above) — never overwrites rows an
# admin has since added. team_role_instruction_overlays and team_gate_flags
# deliberately start EMPTY for every role (see the Notion design's Phase 1 —
# overlays are purely user-added going forward, and every team's gate behavior
# stays exactly as the hardcoded _GATE_ENABLED_TEAMS/_SEARCH_GATE_ENABLED_TEAMS
# sets already define until an admin explicitly overrides one).
#
# tool_registry/skill_registry are seeded from the STATIC union of every
# tool:/skill: name that appears across all shipped teams/*.yaml — a reasonable,
# honest bootstrap (everything currently in active use is definitely a real,
# valid name) — NOT a substitute for refresh_registry()'s live-MCP-sourced
# refresh, which is the real source of truth once available. A name granted via
# the admin API that isn't yet in the registry (a brand-new hive-mcp tool that
# hasn't been through a refresh_registry() call yet) is correctly rejected until
# the registry is refreshed — fail loud, not silently accept an unverifiable name.

import yaml
from pathlib import Path

_TEAMS_DIR = Path(__file__).resolve().parent.parent / "teams"


def _read_team_yaml_tools_and_skills() -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """Parses every teams/*.yaml directly (not via api/server.py's _load_team(),
    which requires an event loop / DB-resolved model and is request-shaped, not
    seed-shaped) — returns two sets of (team_name, role_name, name) triples,
    one for tools and one for skills. Sets, not lists: a coordinator_tools entry
    and a per-agent tools entry could name the same tool for the same (team,
    role) in principle, and team_role_tools' primary key would reject a literal
    duplicate insert — de-duplicating here is simpler than catching that at
    insert time."""
    tool_triples: set[tuple[str, str, str]] = set()
    skill_triples: set[tuple[str, str, str]] = set()
    for path in sorted(_TEAMS_DIR.glob("*.yaml")):
        team_name = path.stem
        data = yaml.safe_load(path.read_text())
        for tool_name in (data.get("coordinator_tools") or []):
            tool_triples.add((team_name, "Coordinator", tool_name))
        for agent in data.get("agents", []):
            role_name = agent["name"]
            for tool_name in (agent.get("tools") or []):
                tool_triples.add((team_name, role_name, tool_name))
            for skill_name in (agent.get("skills") or []):
                skill_triples.add((team_name, role_name, skill_name))
    return tool_triples, skill_triples


async def _seed_defaults(conn) -> None:
    tool_triples, skill_triples = _read_team_yaml_tools_and_skills()
    all_tools = {name for (_, _, name) in tool_triples}
    all_skills = {name for (_, _, name) in skill_triples}

    if tool_triples:
        await conn.execute(
            db.team_role_tools.insert(),
            [{"team_name": t, "role_name": r, "tool_name": n} for (t, r, n) in tool_triples],
        )
    if skill_triples:
        await conn.execute(
            db.team_role_skills.insert(),
            [{"team_name": t, "role_name": r, "skill_name": n} for (t, r, n) in skill_triples],
        )
    if all_tools:
        await conn.execute(db.tool_registry.insert(), [{"tool_name": t} for t in sorted(all_tools)])
    if all_skills:
        await conn.execute(db.skill_registry.insert(), [{"skill_name": s} for s in sorted(all_skills)])
