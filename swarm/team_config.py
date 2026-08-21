"""DB-backed team config (AGNOHive 2.3.3, 2026-08-18) — three tiers, each with
its own precedence:
  Tier 1 (tools/skills) — the DB is the actual runtime source. A team YAML's
    own tools:/skills:, when present, still wins outright (same "pin it back
    here to take it out of DB control" escape hatch model: already has), but
    all 4 shipped teams/*.yaml have had those fields removed, so the DB
    supplies them by default (see _load_seed_grants()/seeds/team_config.yaml
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
from pathlib import Path

import yaml
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


async def sync_registry_from_live(tool_names: list[str], skill_names: list[str]) -> dict | None:
    """Upsert the registry from a LIVE enumeration, but only when it carries a
    name the registry doesn't already have. Returns what was added, or None when
    there was nothing to do.

    This is how the registry is meant to stay current (2026-08-21).
    refresh_registry()'s admin endpoint was the bootstrapping stand-in; the real
    source is a swarm run, which already connects to every MCP server and already
    holds `MCPTools.functions` — the exact list, keyed by name — before it builds
    a single agent. Nothing else in the system has that list at a moment when it
    is known to be REACHABLE, which is the property the registry actually needs:
    it exists to validate grants, and a grant is only meaningful if the swarm can
    reach the tool. A server self-reporting at its own bootstrap would instead
    record what it HAS, which stays true right up until the moment it stops being
    reachable and the registry stops being able to tell you.

    Runs on every swarm run, so the no-change path must cost nothing: two set
    differences against the in-process cache, no connection, no query. When
    something IS new, the full lists go to refresh_registry() rather than just the
    new names — so last_seen_at is refreshed across the board exactly when the
    surface changed, which is when its value as a staleness signal matters, and
    never on the common path.

    Only ever ADDS. refresh_registry() upserts rather than wiping, deliberately
    (see its docstring), so a run connected to fewer servers than usual — or one
    whose enumeration is momentarily partial — cannot de-register a real tool.
    """
    new_tools = sorted(set(tool_names) - _tool_registry_cache)
    new_skills = sorted(set(skill_names) - _skill_registry_cache)
    if not new_tools and not new_skills:
        return None

    await refresh_registry(sorted(set(tool_names)), sorted(set(skill_names)))
    _tool_registry_cache.update(new_tools)
    _skill_registry_cache.update(new_skills)
    return {"tools_added": new_tools, "skills_added": new_skills}


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
# 2026-08-18: this used to be parsed live from each teams/*.yaml's tools:/skills:
# content at seed time. Once the DB became the actual runtime source and those
# fields were removed from the YAMLs, there was nothing left to seed FROM, so a
# static snapshot of that former YAML content became the seed — first as Python
# literals in this module, and since 2026-08-21 as seeds/team_config.yaml.
#
# The literals moved OUT of this module deliberately (see that file's own header):
# sitting in swarm/ next to the live cache, they read like editable runtime config
# and invited exactly the wrong edit. They are data, they are read once, and they
# now live somewhere that says so. What did NOT change: the seed is still
# REQUIRED — see check_config_health() for what a missing one actually costs.

_SEED_PATH = Path(__file__).resolve().parent.parent / "seeds" / "team_config.yaml"


def _load_seed_grants() -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """Read seeds/team_config.yaml into (tool_grants, skill_grants) triples.

    A missing or unreadable seed file returns empty sets rather than raising:
    seeding is best-effort by design (load_cache() runs on the startup path, and
    a broken seed must not make the service unbootable). The resulting empty-DB
    state is NOT silent — check_config_health() reports it as the highest-
    severity finding precisely because it fails OPEN, not closed.
    """
    try:
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set(), set()

    def flatten(section: str) -> set[tuple[str, str, str]]:
        out: set[tuple[str, str, str]] = set()
        for team, roles in (raw.get(section) or {}).items():
            for role, names in (roles or {}).items():
                for name in names or []:
                    out.add((team, role, name))
        return out

    return flatten("tools"), flatten("skills")


_TEAMS_DIR = Path(__file__).resolve().parent.parent / "teams"


def check_config_health() -> list[str]:
    """Sync, cache-only. Startup/admin diagnostic: what is actually WRONG with
    this deployment's team config, ordered most-severe first. Empty list = clean.

    Deliberately NOT a seed-vs-DB diff. Divergence from seeds/team_config.yaml is
    the admin API working as intended — a check that warned on it would fire on
    every healthy deployment that ever changed a grant, and a warning that is
    usually wrong gets ignored when it is finally right. So this reports only
    states that are broken on their own terms, whatever the seed says.

    Follows check_coordinator_readiness()'s contract exactly: never raises, never
    blocks startup, and stays silent when it cannot reach a conclusion. A wrong
    warning is a nuisance; a false-negative silence is acceptable; blocking on a
    diagnostic is not.

    The findings, in severity order:
      1. No grants at all — the seed never ran, or the table was wiped. Not
         "no tools": every role falls through to unrestricted (api/server.py
         _load_team()), so every agent sees apply_diff and run_command. This
         fails OPEN, which is why it outranks everything else here.
      2. A role that resolves to unrestricted — same fail-open, one role at a
         time. An explicitly empty allowlist (`tools: []`, `coordinator_tools: []`)
         is a deliberate disarm and is NOT flagged; only a genuinely absent field
         with no DB rows behind it is.
      3. A grant naming a tool missing from tool_registry — unreachable via the
         admin API's own write-time validation, so almost certainly a stale name
         left behind by a rename.
      4. A tool_registry that has never been refreshed from a live MCP
         enumeration — every name in it traces back to the seed, so the write-time
         validation in (3) is checking against a bootstrap, not against what
         hive-mcp currently exposes.
    """
    findings: list[str] = []
    try:
        if not _tools_cache:
            return [
                "No team_role_tools grants loaded — the first-run seed never ran, or the "
                "table was emptied. This does NOT disable tools: every role falls through "
                "to UNRESTRICTED and sees the full connected surface, apply_diff and "
                f"run_command included. Check that {_SEED_PATH} exists and is readable, "
                "then restart; or restore grants via POST /admin/team-config/tools."
            ]

        seed_tools, _ = _load_seed_grants()

        for path in sorted(_TEAMS_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            team = path.stem
            roles = [(a.get("name"), a.get("tools")) for a in (data.get("agents") or []) if a.get("name")]
            roles.append(("Coordinator", data.get("coordinator_tools")))
            for role, yaml_tools in roles:
                # `is None`, not falsiness: an explicitly empty list is a
                # deliberate disarm (engineering's coordinator), not an omission.
                if yaml_tools is not None:
                    continue
                if _tools_cache.get((team, role)):
                    continue
                findings.append(
                    f"{team}/{role} resolves to UNRESTRICTED — no tools: in {path.name} and no "
                    f"team_role_tools rows, so it sees every connected tool including write and "
                    f"shell ones. Grant it an explicit list via POST /admin/team-config/tools, or "
                    f"pin `tools: []` in the YAML if it is meant to hold none."
                )

        unregistered = sorted(
            {n for names in _tools_cache.values() for n in names} - _tool_registry_cache
        )
        if unregistered:
            findings.append(
                f"{len(unregistered)} granted tool(s) are absent from tool_registry and would be "
                f"rejected if re-granted today: {', '.join(unregistered[:10])}"
                f"{'...' if len(unregistered) > 10 else ''}. Usually a rename left behind. Run "
                "POST /admin/team-config/registry/refresh with a live enumeration to confirm."
            )

        if _tool_registry_cache and _tool_registry_cache <= {n for (_, _, n) in seed_tools}:
            findings.append(
                "tool_registry contains only names from the first-run seed — it appears never to "
                "have been refreshed from a live MCP enumeration, so grant validation is checking "
                "against a bootstrap rather than what hive-mcp currently exposes. Run "
                "POST /admin/team-config/registry/refresh."
            )
    except Exception as e:  # diagnostic only — never the reason a startup or request fails
        return [f"team config health check could not complete ({type(e).__name__}: {e})"]

    return findings


async def _seed_defaults(conn) -> None:
    tool_grants, skill_grants = _load_seed_grants()
    all_tools = {name for (_, _, name) in tool_grants}
    all_skills = {name for (_, _, name) in skill_grants}

    if tool_grants:
        await conn.execute(
            db.team_role_tools.insert(),
            [{"team_name": t, "role_name": r, "tool_name": n} for (t, r, n) in tool_grants],
        )
    if skill_grants:
        await conn.execute(
            db.team_role_skills.insert(),
            [{"team_name": t, "role_name": r, "skill_name": n} for (t, r, n) in skill_grants],
        )
    if all_tools:
        await conn.execute(db.tool_registry.insert(), [{"tool_name": t} for t in sorted(all_tools)])
    if all_skills:
        await conn.execute(db.skill_registry.insert(), [{"skill_name": s} for s in sorted(all_skills)])
