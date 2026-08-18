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

`team_role_tools` / `team_role_skills` (`swarm/db.py`) each hold `(team_name, role_name, tool_name | skill_name)` rows — one row per granted name. At read time, `_load_team()` resolves a role's tool/skill list with the same precedence `model:` already uses: **the YAML's own `tools:`/`skills:`, when present, wins outright** (DB rows for that role are simply not consulted); **when the YAML omits the field, the DB supplies the role's full list.** This changed from an initial additive-union design (DB rows layered on top of the YAML's own list) specifically so the DB would be the actual runtime source rather than a YAML-plus-extras layer — all 4 shipped teams had their `tools:`/`skills:`/`coordinator_tools:` fields removed accordingly, with `swarm/team_config.py`'s `_DEFAULT_TOOL_GRANTS`/`_DEFAULT_SKILL_GRANTS` (a static snapshot of the former YAML content) seeding these tables on a fresh deployment.

**The one case this deliberately preserves:** a role with no `tools:` field in its YAML *and* no rows in the DB is *unrestricted* — `swarm/agents.py`'s `make_agent_from_spec` treats `tools: None` as "sees every connected tool," not "sees nothing." A missing DB list must never resolve to an empty allowlist, so the fallback only replaces `a["tools"]` `if db_tools` is non-empty — a role nobody has migrated into the DB (e.g. `engineering`'s coordinator, which has never had a `coordinator_tools:` field or DB rows) stays unrestricted exactly as before. The same guard applies to `coordinator_tools`. Covered directly by `tests/test_load_team_config_overlay.py`'s `test_no_yaml_tools_and_no_db_grant_stays_unrestricted` and `test_no_coordinator_tools_yaml_field_and_no_db_grant_stays_unrestricted`.

**Write-time registry validation.** A grant is only accepted if the tool/skill name already exists in `tool_registry`/`skill_registry` — two more small tables, seeded from `_DEFAULT_TOOL_GRANTS`/`_DEFAULT_SKILL_GRANTS`'s own union of names on first run, refreshable from a live enumeration of `hive-mcp`'s registered tools and the skill catalog (never hand-maintained; see [Adding a provider](cloud-models.md#adding-a-provider) for the equivalent pattern on the model-routing side). `POST /admin/team-config/tools` with an unregistered `tool_name` returns **400**, not a silent no-op — a typo'd tool name should fail loudly at write time, not surface later as a mysteriously-missing capability at run time. `POST /admin/team-config/registry/refresh` re-syncs the registry from a fresh enumeration when new tools/skills ship.

## Tier 2 — instruction overlays (additive-only)

`team_role_instruction_overlays` holds free-text notes per `(team_name, role_name)`, each with an `active` flag and a `created_by`. Active overlays are appended, in insertion order, after the YAML's existing `instructions:` list, under one fixed header (`team_config.OVERLAY_HEADER`, currently `"-- User-added notes (unreviewed, use with care) --"`) so a role's base instructions and its user-added notes are always visually distinguishable in the assembled prompt.

**This is deliberately NOT a versioned/edit-in-place mechanism**, unlike `team_role_models`' PATCH-any-field shape. `InstructionOverlayPatch` only accepts `active: bool | None` — changing the text itself is delete-and-recreate. There's no "new version" concept here to PATCH toward; an overlay is either live or it isn't.

**Soft cap: 5 active overlays per (team, role)**, enforced at write time via an active-row `COUNT` — the 6th `POST` returns **409**. Deactivating an overlay frees a slot for a new one (`tests/test_admin_team_config_api.py::test_deactivating_an_overlay_frees_a_cap_slot`). The cap exists to keep a role's assembled instruction block from growing unboundedly through casual accretion — 5 is a starting number, not a hard architectural limit, and can be revisited if it proves too tight in practice.

**No automatic contradiction detection between overlays** — an open question, deliberately deferred. Two overlays that tell a role opposite things will both apply; catching that is a human review problem for now, not something `_load_team()` tries to solve.

## Tier 3 — gate flags

`decompose_first` and `search_before_browse` were previously ON/OFF purely by hardcoded set membership (`_GATE_ENABLED_TEAMS` / `_SEARCH_GATE_ENABLED_TEAMS` in `swarm/team.py`). `team_gate_flags` (`team_name, gate_name, enabled`) lets either be overridden per team without a code change, via `team_config.get_gate_enabled(team_name, gate_name, default=...)` — where `default` is exactly the old hardcoded boolean, so **a team with no override row behaves identically to before this feature existed**. `gate_name` is validated against a fixed set (`team_config.GATE_NAMES`) at write time — `POST /admin/team-config/gates` with an unrecognized name returns 400. Writing the same `(team_name, gate_name)` pair again upserts (updates the existing row) rather than erroring.

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
