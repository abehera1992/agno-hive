← [Back to guide index](README.md) · [Main README](../../README.md)

# 🧩 DB-Backed Team Config (AGNOHive 2.3.3)

## Contents
- [What this is](#what-this-is)
- [The three tiers](#the-three-tiers)
- [Tier 1 — tools/skills (DB-primary, override-with-fallback)](#tier-1--toolsskills-db-primary-override-with-fallback)
- [Tier 2 — instruction overlays (additive-only)](#tier-2--instruction-overlays-additive-only)
- [Tier 3 — gate flags](#tier-3--gate-flags)
- [Admin API](#admin-api)
- [Reload semantics](#reload-semantics)
- [What this deliberately does NOT do](#what-this-deliberately-does-not-do)

---

## What this is

Every `teams/*.yaml` file used to hardcode each role's `tools:`, `skills:`, and `instructions:` — the same pattern `model_catalog`/`team_role_models` ([Cloud Model Providers](cloud-models.md)) replaced for model routing. AGNOHive 2.3.3 does the same for tools/skills/instructions/gates: three new SQLite/Postgres tables let a user grant a role a tool, add a supplementary instruction, or flip a gate on/off **without editing or redeploying a YAML file** — a prerequisite for a future command-center UI wrapper where these are user-configurable settings, not code changes.

**Tools/skills (Tier 1) and instructions (Tier 2) take deliberately different precedence, by explicit design choice:**
- **Tier 1 (tools/skills) — the DB is the actual runtime source.** All 4 shipped `teams/*.yaml` have had their `tools:`/`skills:`/`coordinator_tools:` fields removed entirely; `_load_team()` resolves each role's list from the DB instead. A YAML can still declare an explicit list to pin a role and take it out of DB control — same escape hatch `model:` already has — but nothing ships that way by default anymore.
- **Tier 2 (instructions) stays additive-only.** A role's base `instructions:` in the YAML are never touched; DB overlays are appended after them under a clear header. This was an explicit, separate requirement — the tested base instruction set is not something this feature is allowed to replace or reorder.

## The three tiers

| Tier | What | Mechanism | Mutable via |
|---|---|---|---|
| 1 | Tools/skills per (team, role) | DB is the source; YAML value, if present, overrides it outright (same precedence as `model:`) | `POST/DELETE /admin/team-config/tools`, `/skills` |
| 2 | Supplementary instructions per (team, role) | Appended after the YAML's base `instructions:`, under a clear header — additive only | `POST/PATCH/DELETE /admin/team-config/instruction-overlays` |
| 3 | Per-gate on/off (`decompose_first`, `search_before_browse`) | DB override falling back to the existing hardcoded team-membership default | `POST/DELETE /admin/team-config/gates` |

All three read through one in-process cache module, `swarm/team_config.py` — same pattern as `swarm/model_routing.py`: loaded once at FastAPI startup via `ensure_cache_loaded()`, refreshed only via an explicit `POST /admin/team-config/reload`. No background TTL polling.

## Tier 1 — tools/skills (DB-primary, override-with-fallback)

`team_role_tools` / `team_role_skills` (`swarm/db.py`) each hold `(team_name, role_name, tool_name | skill_name)` rows — one row per granted name. At read time, `_load_team()` resolves a role's tool/skill list with the same precedence `model:` already uses: **the YAML's own `tools:`/`skills:`, when present, wins outright** (DB rows for that role are simply not consulted); **when the YAML omits the field, the DB supplies the role's full list.** This changed from an initial additive-union design (DB rows layered on top of the YAML's own list) specifically so the DB would be the actual runtime source rather than a YAML-plus-extras layer — all 4 shipped teams had their `tools:`/`skills:`/`coordinator_tools:` fields removed accordingly, with `seeds/team_config.yaml` (a static snapshot of the former YAML content) seeding these tables on a fresh deployment. That seed is read **once**, only against a completely empty `team_role_tools` — editing it never changes an already-seeded deployment, which is why it lives outside `swarm/` rather than beside the live cache.

**The one case this deliberately preserves:** a role with no `tools:` field in its YAML *and* no rows in the DB is *unrestricted* — `swarm/agents.py`'s `make_agent_from_spec` treats `tools: None` as "sees every connected tool," not "sees nothing." A missing DB list must never resolve to an empty allowlist, so the fallback only replaces `a["tools"]` `if db_tools` is non-empty — a role nobody has migrated into the DB (e.g. `engineering`'s coordinator, which has never had a `coordinator_tools:` field or DB rows) stays unrestricted exactly as before. The same guard applies to `coordinator_tools`. Covered directly by `tests/test_load_team_config_overlay.py`'s `test_no_yaml_tools_and_no_db_grant_stays_unrestricted` and `test_no_coordinator_tools_yaml_field_and_no_db_grant_stays_unrestricted`.

**Write-time registry validation.** A grant is only accepted if the tool/skill name already exists in `tool_registry`/`skill_registry` — two more small tables, seeded from `seeds/team_config.yaml`'s own union of names on first run, refreshable from a live enumeration of `hive-mcp`'s registered tools and the skill catalog (never hand-maintained; see [Adding a provider](cloud-models.md#adding-a-provider) for the equivalent pattern on the model-routing side). `POST /admin/team-config/tools` with an unregistered `tool_name` returns **400**, not a silent no-op — a typo'd tool name should fail loudly at write time, not surface later as a mysteriously-missing capability at run time. `POST /admin/team-config/registry/refresh` re-syncs the registry from a fresh enumeration when new tools/skills ship.

## Tier 2 — instruction overlays (additive-only)

`team_role_instruction_overlays` holds free-text notes per `(team_name, role_name)`, each with an `active` flag and a `created_by`. Active overlays are appended, in insertion order, after the YAML's existing `instructions:` list, under one fixed header (`team_config.OVERLAY_HEADER`, currently `"-- User-added notes (unreviewed, use with care) --"`) so a role's base instructions and its user-added notes are always visually distinguishable in the assembled prompt.

**This is deliberately NOT a versioned/edit-in-place mechanism**, unlike `team_role_models`' PATCH-any-field shape. `InstructionOverlayPatch` only accepts `active: bool | None` — changing the text itself is delete-and-recreate. There's no "new version" concept here to PATCH toward; an overlay is either live or it isn't.

**Soft cap: 5 active overlays per (team, role)**, enforced at write time via an active-row `COUNT` — the 6th `POST` returns **409**. Deactivating an overlay frees a slot for a new one (`tests/test_admin_team_config_api.py::test_deactivating_an_overlay_frees_a_cap_slot`). The cap exists to keep a role's assembled instruction block from growing unboundedly through casual accretion — 5 is a starting number, not a hard architectural limit, and can be revisited if it proves too tight in practice.

**No automatic contradiction detection between overlays** — an open question, deliberately deferred. Two overlays that tell a role opposite things will both apply; catching that is a human review problem for now, not something `_load_team()` tries to solve.

## Tier 3 — gate flags

`decompose_first` and `search_before_browse` were previously ON/OFF purely by hardcoded set membership (`_GATE_ENABLED_TEAMS` / `_SEARCH_GATE_ENABLED_TEAMS` in `swarm/team.py`). `team_gate_flags` (`team_name, gate_name, enabled`) lets either be overridden per team without a code change, via `team_config.get_gate_enabled(team_name, gate_name, default=...)` — where `default` is exactly the old hardcoded boolean, so **a team with no override row behaves identically to before this feature existed**. `gate_name` is validated against a fixed set (`team_config.GATE_NAMES`) at write time — `POST /admin/team-config/gates` with an unrecognized name returns 400. Writing the same `(team_name, gate_name)` pair again upserts (updates the existing row) rather than erroring.

## Beyond the three tiers — the complete tool-access model

The three tiers above answer "does SQLite govern this tool's grant?" They do not, by
themselves, answer "where does a given tool actually come from?" Phase 0.63.1's audit
found that question needed a separate, explicit answer — reading `team_role_tools`
alone gives a falsely complete picture of what a role actually ends up holding. Every
tool a Coordinator or member agent can call falls into exactly one of these:

1. **SQLite-governed (Tier 1, above).** The overwhelming majority — every member
   role's full tool list in all 4 shipped teams, and the Coordinator's own MCP-tool
   allowlist wherever its YAML doesn't explicitly override it.
2. **YAML overrides.** An explicit `tools:`/`coordinator_tools:` in a team's YAML wins
   outright over any DB row for that (team, role) — including an explicit empty list,
   which is a deliberate disarm, not an omission (see Tier 1 above).
3. **Hardcoded application tools.** `request_clarification` and `update_session_state`
   are appended to every Coordinator's tool list unconditionally, in `swarm/team.py`'s
   `_build_team()`, regardless of SQLite or YAML — both are local, side-effect-free
   tools the code's own comments justify as safe for every team, including read-only
   ones.
4. **Locally implemented, SQLite-gated tools.** `forward_member_answer` is a local
   Python function (not MCP-surfaced), but its grant to a Coordinator role IS read
   from `team_role_tools` — via `team_config.get_extra_tools(team_name, "Coordinator")`,
   checked directly in `_build_team()`, bypassing the Tier-1 YAML/DB-fallback path
   entirely (that path is for the *MCP-tool* allowlist only). This is the same pattern
   `delegate_structured_task` now uses — see point 7.
5. **Capability-policy filtering (Phase P, 2026-10-04).** `agent_capability_policy` can
   additionally *remove* (never add) a tool from the Coordinator's resolved list, via
   `team_config.resolve_effective_policy()`, wrapped in try/except so a bug here fails
   OPEN to the pre-Phase-P surface, never silently closed. Only one row is seeded today
   (`engineering`/`Coordinator`/`member.forwarding`=REQUIRED, a REQUIRE row, not a
   FORBID row) — in current practice this layer changes nothing. **It cannot affect
   `delegate_structured_task` even if a FORBID row existed for it**: this filtering
   runs inside `_build_team()`, against `coordinator_tools_list`, before the `Team`
   object exists — but `delegate_structured_task` is injected later, during an actual
   run, by point 7's own mechanism. The two layers never see each other.
6. **Agno-native framework tools.** Agno's own built-ins (e.g. `get_member_information`)
   follow Agno's own rules, outside this codebase's tool-grant machinery entirely.
7. **The Coordinator-only delegation tool (`delegate_structured_task`).** This tool
   is *not* attached via `coordinator_tools_list` at all — Agno 2.5.17 calls
   `agno.team._default_tools._get_delegate_task_function(team, run_response=...,
   session=..., run_context=..., team_run_context=..., ...)` itself, fresh, on every
   single model generation (confirmed by source read: `agno/team/_tools.py`'s
   `_determine_tools_for_model`, called from many sites in `agno/team/_run.py`,
   re-imports this free function by name on every call rather than caching a
   reference). Those per-call arguments don't exist before a run starts, so the
   result cannot be pre-built once at `_build_team()`-time and cached as a static
   `Function` object. `swarm/team.py` intercepts this exact module-level name
   (`_patched_agno_get_delegate_task_function`) — the only extension point this Agno
   version offers; an instance-method override on a `Team` subclass was tried first
   and found to be dead code, since Agno's real call sites never use polymorphic
   dispatch (Phase S.1/S.2). As of Phase 0.63.2, the interception checks
   `team_config.get_extra_tools(team_name, "Coordinator")` for an explicit
   `delegate_structured_task` grant (the same lookup `forward_member_answer` uses)
   before wrapping Agno's real tool into the structured contract — SQLite is now the
   actual authority over *who* receives it, though the interception *mechanism*
   itself remains, since no alternative exists in this Agno version without either
   reimplementing Agno's own delegation internals or reverting to its differently-
   named native tool. A team/role combination with no matching grant gets Agno's
   original, unwrapped tool — fails closed, never silently broadens access.
8. **How ordinary members return findings without delegation authority.** A member
   agent (Researcher, Coder, Reviewer, …) is a plain `Agent`, never itself a `Team` —
   Agno's `_get_delegate_task_function` only ever runs for a `Team`'s own leader, so a
   member structurally never reaches point 7's interception at all, regardless of
   SQLite. A member's own findings reach the Coordinator exclusively through its
   return value / the result-capture machinery (`_capture_member_result` →
   `member_answers`), which the Coordinator must then explicitly relay via
   `forward_member_answer` (point 4) — there is no path by which a member can itself
   initiate a further delegation.

## Admin API

Same FastAPI app, same trust boundary as every other endpoint (unauthenticated, reached only over Tailscale) — no dedicated admin service, matching [model routing's admin API](cloud-models.md#admin-api--editing-routes-at-runtime):

| Endpoint | Purpose |
|---|---|
| `GET /admin/team-config/tools` | List every `team_role_tools` grant |
| `POST /admin/team-config/tools` | Grant a tool to a (team, role) — 400 if unregistered, 409 on duplicate |
| `DELETE /admin/team-config/tools/{team_name}/{role_name}/{tool_name}` | Revoke a grant — 404 if unknown |
| `GET /admin/team-config/skills` | List every `team_role_skills` grant |
| `POST /admin/team-config/skills` | Grant a skill (mirrors tools) — 400 if unregistered, 409 on duplicate |
| `DELETE /admin/team-config/skills/{team_name}/{role_name}/{skill_name}` | Revoke a grant — 404 if unknown |
| `GET /admin/team-config/instruction-overlays?team_name=&role_name=` | List overlays, optionally filtered |
| `POST /admin/team-config/instruction-overlays` | Create an overlay — 409 if the 5-per-role soft cap is exceeded |
| `PATCH /admin/team-config/instruction-overlays/{id}` | Toggle `active` — 400 if no field supplied, 404 if unknown, re-checks the cap when reactivating |
| `DELETE /admin/team-config/instruction-overlays/{id}` | Delete an overlay — 404 if unknown |
| `GET /admin/team-config/gates` | List every gate-flag override |
| `POST /admin/team-config/gates` | Upsert a gate override — 400 if `gate_name` isn't recognized |
| `DELETE /admin/team-config/gates/{team_name}/{gate_name}` | Remove an override, reverting to the hardcoded default |
| `POST /admin/team-config/registry/refresh` | Re-sync `tool_registry`/`skill_registry` from a live enumeration |
| `POST /admin/team-config/reload` | **Required after any write above** — re-reads the DB into the in-process cache, returns the actual diff |

## Reload semantics

Identical philosophy to model routing: a write that isn't followed by `POST /admin/team-config/reload` has no effect on any agent — deliberate, not a bug. `TeamConfigReloadResponse` reports `tool_grants_added`/`skill_grants_added` (per `team/role` key), `overlay_count_delta`, `gates_changed`, and the current registry sizes, so a reload's effect is visible immediately instead of a bare 200.

## What this deliberately does NOT do

- **Never overrides or replaces a YAML's existing `instructions:`** — Tier 2 is additive-only, by design (see [Tier 2](#tier-2--instruction-overlays-additive-only) above). This does NOT apply to Tier 1: an explicit `tools:`/`skills:` in the YAML *does* override the DB outright (see [Tier 1](#tier-1--toolsskills-db-primary-override-with-fallback) above) — the two tiers use intentionally different precedence.
- **Tier 3 mechanics (which gates exist, how a gate task is computed) stay in code** — only the on/off decision per team is DB-overridable. Adding a genuinely new gate is still a code change in `swarm/team.py`.
- **No contradiction detection across overlays** — deferred, see [Tier 2](#tier-2--instruction-overlays-additive-only).
- **No versioning/audit trail on overlay edits** beyond `active`/`created_by`/`created_at` — deleting and recreating is the only way to change text.

---

**Next:** [🤖 Agents & Teams](agents-and-teams.md) · [☁️ Cloud Model Providers](cloud-models.md)
