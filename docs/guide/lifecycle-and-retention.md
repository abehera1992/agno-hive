# Durable-state lifecycle, retention, recovery & backup boundary

Phase L (Operational Durability, Retention & Recovery) of the durable
execution/evidence/memory architecture (Phases A–L, `swarm/execution_store.py`,
`swarm/db.py`, `swarm/sessions.py`). This is the reference this whole effort's
own code comments point at (`see docs/guide/lifecycle-and-retention.md`).

Core invariant this document exists to make checkable:

> Durable state has explicit lifecycle, retention, recovery, integrity, and
> backup/restore semantics; cleanup must never destroy state that is still
> authoritative or required for recovery.

## 1. Lifecycle matrix

Classification key: `SESSION` (dies with its session) · `RUN_HISTORY` (dies
with its run, which dies with its session) · `RECOVERY_STATE` (checkpoints)
· `PROJECT_MEMORY` (project-owned, outlives every session) · `GLOBAL`
(project-scoped but not session-scoped) · `EPHEMERAL` (not in this database
at all).

| Table | Class | Owner | Lifetime | Deletion trigger | Cascade | Recovery requirement | Backup requirement |
|---|---|---|---|---|---|---|---|
| `chat_sessions` | SESSION | project (bare string) | `config.session_ttl_days` unless `persist=True` | `expires_at` passed **and** `persist=False` **and** no run under it is still `"running"` (Phase K/L guard — see §3) | `ON DELETE CASCADE` → `session_messages`, `runs` (and everything under `runs`, transitively) | none — ephemeral by design | operator DB backup only |
| `session_messages` | SESSION | `chat_sessions` | = owning session | cascades with session | leaf | none | operator DB backup |
| `runs` | RUN_HISTORY | `chat_sessions` | = owning session | session deletion (cascade) | → `executions`, `checkpoints`, `claims` | Phase H `rehydrate_run` (judgment only, never acts) | operator DB backup |
| `executions` | RUN_HISTORY | `runs` | = owning run | run deletion (cascade) | → `tool_calls` | covered by `rehydrate_run` at the run level | operator DB backup |
| `tool_calls` | RUN_HISTORY | `executions` | = owning execution | execution deletion (cascade) | → `evidence` | Phase H: a non-terminal `tool_calls.status` blocks resumability absolutely | operator DB backup |
| `evidence` | RUN_HISTORY | `tool_calls` | = owning tool call | tool_call deletion (cascade) | leaf (`claim_evidence` cascades FROM here) | none directly; referenced (never copied) by `claims` | operator DB backup |
| `claims` | RUN_HISTORY | `runs` (execution ref is `SET NULL`) | = owning run | run deletion (cascade) | → `claim_evidence` | none | operator DB backup |
| `claim_evidence` | RUN_HISTORY | `claims` + `evidence` | = shorter-lived side | either side's deletion | leaf | none | operator DB backup |
| `checkpoints` | RECOVERY_STATE | `runs` | = owning run | run deletion (cascade) | leaf (`last_execution_id` is `SET NULL`, not cascaded) | Phase H `rehydrate_run` reads it; Phase L `list_stale_runs` surfaces stale ones | operator DB backup |
| `project_memory_promotions` | PROJECT_MEMORY | project (bare string; optional `projects.tenant_id`) | **indefinite — no TTL, no deletion path exists anywhere in this codebase** | none (append-only by design, Phase F) | **none** — deliberately no FK into the session-owned tree | none — durable by design | **highest-priority table to back up**: it is the only durable record of validated, human-approved knowledge that survives nothing else |
| `projects` | PROJECT_MEMORY (registry) | global | indefinite | none | none | none | operator DB backup |
| `failure_log` | GLOBAL | project (bare string) | **indefinite — no TTL, no cleanup path exists** (Phase L forensic finding, see ledger #16) | none currently | none | none | operator DB backup |
| `task_outcome_queue` | GLOBAL | project (bare string) | naturally bounded — `UniqueConstraint(project_id, task_hash)`; a re-verified task **replaces** its row via `supersedes`, never adds a second one | replaced on re-verification, or drained/marked `done`/`failed` | none | `swarm/outcomes.py`'s own `requeue_stuck()` (pre-Phase-A mechanism, unrelated to Phase H checkpoints) | operator DB backup |
| `model_catalog` / `team_role_models` | GLOBAL | global, **separate routing database** (`get_routing_engine()`, deliberately decoupled — see `swarm/db.py`'s own "TWO separate engines" note) | indefinite, admin-managed | explicit `/admin/model-routes` DELETE only | `team_role_models → model_catalog` FK | none | operator DB backup, **as a separate target** from the primary database |
| hive-mcp `.hive_proposed` / `.hive_scratch/` | EPHEMERAL | hive-mcp process, **not this database at all** | until `WRITE_REVIEW` confirm/reject, or TTL-swept | explicit confirm/reject, or scratch TTL sweep | filesystem, not FK | none — a lost proposal is simply redone | **not this document's concern** — filesystem, not durable DB state; see §4 |

### Explicit answers

- **What survives session expiry?** Nothing in the session-owned tree, unless
  the session has a run still `status == "running"` against it (the Phase
  K/L cleanup guard defers deletion until that run reaches a terminal
  state) or `persist=True` was set.
- **What survives session *deletion*** (an explicit `DELETE`, not merely
  expiry — e.g. `DELETE /sessions/{id}`)? Only `project_memory_promotions`
  (no FK into the tree at all) and anything genuinely project-scoped that
  was never tied to that session (`failure_log`, `task_outcome_queue`,
  `projects`).
- **What must never be automatically deleted?** `project_memory_promotions`
  (Phase F's own founding principle: validated, human-approved knowledge),
  and a session/run tree with a currently-`"running"` run (Phase K/L).
- **What belongs to project memory rather than session history?**
  `project_memory_promotions` exclusively. Everything else under a session
  — `runs` through `claim_evidence`, plus `checkpoints` — is `RUN_HISTORY`/
  `RECOVERY_STATE`, tied to that session's own lifecycle.

## 2. Recovery decision

**Detection + explicit operator recovery — not automatic resume.**

Justification, from forensics across Phases H, K, and L:

1. No code path today lets a second worker resume an *existing* `run_id`
   (Phase K forensic finding, re-verified this phase: `run_task_async`/
   `run_task_stream` still never call the ownership primitives).
2. A `ToolCall` with an ambiguous real-world side effect must never be
   auto-replayed (Phase H's absolute rule; re-confirmed by this phase's own
   regression test).
3. `rehydrate_run` (Phase H) is deliberately read-only/judgment-only — it
   has never launched, driven, or continued execution, and does not gain
   that ability this phase.
4. `list_stale_runs` (Phase L, new) is detection-only — it surfaces
   candidates, never acts on them.

The composed policy an operator actually uses:
`list_stale_runs()` (which runs look abandoned) → `rehydrate_run(run_id)`
(is *this specific one* safe to continue from) → a human decides whether to
start a **new** run informed by that context, or leave it for manual
inspection. Nothing in this lineage is ever automatic.

Accounted for explicitly: worker/process death (SIGKILL is unobservable
in-process, Phase H) · DB restart (readiness surfaces this immediately, see
`GET /health/db`, Phase I) · stale `"running"` state (`list_stale_runs`,
this phase) · invalid checkpoints (`rehydrate_run`'s integrity check, Phase
H; `check_storage_integrity`'s hash-mismatch count, Phase I) · partial
ToolCall/Evidence persistence (the non-terminal-ToolCall absolute block,
Phase H) · cleanup vs. recovery races (the active-run cleanup guard, Phase
K/L — see §3).

## 3. Cleanup semantics

`swarm/sessions._cleanup_expired(dry_run: bool = False)`:

- **Deterministic**: a plain `WHERE expires_at < now() AND persist=False AND
  NOT EXISTS(an active run)` — same inputs, same outcome, every time.
- **Idempotent**: a session already deleted (by an earlier or concurrent
  call) simply isn't matched a second time; repeated calls are always safe.
- **Observable**: returns the count affected; every failure is printed.
- **Safe around active/recoverable Runs**: the `NOT EXISTS` guard (Phase K,
  reused unchanged this phase) refuses to delete a session whose run is
  still `"running"`, however long it has been expired.
- **Dry-run capable** (Phase L, new): `dry_run=True` runs the identical
  `WHERE` clause as a `SELECT COUNT` instead of a `DELETE` — nothing is
  removed, so an operator can preview impact first.
- **Fail-safe on DB errors**: returns `0` (no visible effect), never raises
  — this runs from a background loop (`api/server.py`'s
  `_session_cleanup_loop`) that must never crash its host process.

## 4. Backup / restore boundary

> **Hive defines durable-state backup/restore requirements. PostgreSQL (or
> the operator's chosen backup tooling) performs the backup.**

Hive does **not** wrap `pg_dump`, does **not** integrate any cloud storage
vendor, and does **not** run scheduled backup jobs — deliberately, per this
phase's own non-goal.

- **What must be backed up together**: every table from `chat_sessions`
  through `project_memory_promotions` and `projects` lives in ONE logical
  database (`config.database_url`). Every FK relationship in this whole
  architecture is *within* that one database, so a single consistent
  point-in-time snapshot of it (`pg_dump`/`pg_basebackup`/a file-level
  SQLite copy) is self-consistent on its own.
- **Restore consistency requirement**: restoring to *any* single snapshot of
  the primary database is internally consistent by construction — there is
  no cross-database consistency requirement, because `model_catalog`/
  `team_role_models` deliberately live in a **separate** routing database
  (see the lifecycle matrix) and can be backed up/restored independently
  without affecting execution history.
- **Schema/version requirement**: after any restore, the restored
  `alembic_version` must match a revision the *currently running* code
  recognizes.
- **What Hive can validate after a restore** (all pre-existing, reused
  unchanged):
  - `db.check_storage_readiness()` (Phase I) — is the restored schema at
    the expected head? (`GET /health/db`)
  - `execution_store.check_storage_integrity()` (Phase I, extended this
    phase) — stuck states, checkpoint hash mismatches, impossible
    lifecycle states, invalid promotion rows, duplicate checkpoint
    sequences.
  - `execution_store.list_stale_runs()` (Phase L) — which runs look
    abandoned as of the restored point in time.

  None of these three functions mutate anything — they only tell an
  operator whether the restored state looks trustworthy.

## 5. Configurable policies (this phase's own scope)

- `swarm.sessions._cleanup_expired(dry_run=...)` — new.
- `execution_store.list_stale_runs(older_than_seconds=3600)` — new,
  caller-supplied threshold, no hidden default beyond the documented one.
- Pre-existing and unchanged: `config.session_ttl_days`,
  `config.session_cleanup_interval` (Phase A-era), `lease_seconds` on
  `acquire_run_ownership` (Phase K).
- **Deliberately not added**: any retention policy for `failure_log`/
  `task_outcome_queue` — see the deferred-limitations ledger, item 16, for
  why ("do not invent retention defaults without forensic justification" —
  no evidence of operational harm was found).
