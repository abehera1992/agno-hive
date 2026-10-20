# T44 — Native Agno Identity Propagation, Pre-RunStarted Race Resolution, and Targeted ZGX Validation

## 1. Executive verdict

| Dimension | Verdict |
|---|---|
| Investigation | **PASS** |
| Implementation | **BLOCKED** |
| Deployment | **NOT EXERCISED** |
| Live validation | **PARTIAL** |

T44's mission was to reconcile Agno's documented `Team.arun(run_id=, run_context=,
metadata=)` capabilities with T43's findings about the actual internal delegation
entrypoints, and to implement a native fix if the evidence supported one. The
investigation was thorough and conclusive: **`run_id` is a real, native, documented
parameter on `Agent.run()`/`Agent.arun()`, but the installed Agno version's own
delegation closures never forward it and offer no extension point to do so.**
`metadata`/`dependencies` genuinely do reach the member's own run before the race
window, but are **conclusively confirmed, by reading the exact event-construction
functions this codebase consumes from, to never reach the events** the correlation
mechanism observes — ruling this path out directly, not speculatively. Upstream Agno
has since added exactly the needed mechanism, but only as part of a major version
line (2.x → 3.x) this codebase cannot safely adopt within this phase's scope.

**No production code change was made.** This is reported as **Implementation: BLOCKED**
— every option in the mission's own ranking was evaluated on evidence and correctly
rejected, landing on "Option 5: retain current behavior" as a deliberate, fully
documented architecture decision, not an unexamined non-action.

## 2. Verified Agno version and source/API findings

- Installed version (ZGX and local inspection venv, confirmed identical): **2.5.17**.
- Latest PyPI release: **3.1.7** — a major version jump. Agno's own v3 changelog
  documents breaking `Team`/`Workflow` constructor changes.
- `Agent.run()`/`Agent.arun()` (`agno/agent/agent.py` ~1332-1482) both accept
  `run_id: Optional[str] = None` as a genuine, documented, forwarded parameter
  (`_run.run_dispatch`/`arun_dispatch`, which do `run_id = run_id or str(uuid4())`).
- `Team.arun()`'s own documented `run_id`/`run_context`/`metadata` parameters
  (`docs.agno.com/reference/teams/team`) apply to the **outer Team run**, not
  automatically to child member delegations — the documentation itself does not say
  how these propagate to members, and source inspection was required to resolve this.

## 3. Actual Coordinator-to-member delegation call chain

Verified directly, not assumed:

```
AgnoHive Coordinator (Team, running async via team.arun())
  -> agno/team/_tools.py: resolve_team_tools(..., async_mode=True)
  -> agno/team/_default_tools.py: _get_delegate_task_function(..., async_mode=True)
       selects adelegate_task_to_member (async def, AsyncIterator)
       -- NOT the sync delegate_task_to_member sibling
  -> swarm/team.py: delegate_structured_task (this repo's own wrapper)
       calls: async for item in original_entrypoint(member_id=.., task=..)
  -> agno's adelegate_task_to_member closure:
       member_agent.arun(stream=True, metadata=run_context.metadata,
                          dependencies=run_context.dependencies, ...)
       -- confirmed: no run_id= passed
  -> agno/agent/_run.py: arun_dispatch -- run_id = run_id or str(uuid4()) (fresh)
       -- awaits aregister_run, aread_or_create_session BEFORE RunStarted (T43's
          own finding, re-confirmed)
```

A separate hypothesis was tested and ruled out this phase: whether the *synchronous*
`delegate_task_to_member`/`member_agent.run()` path (which exists in Agno's source
alongside the async one) might be the one actually used, and whether it would block
the event loop (via `asyncio.to_thread` in `agno/models/base.py:2452`) in a way that
would change the whole concurrency analysis. Confirmed: AgnoHive's own Coordinator
runs in async mode, so `async_mode=True` always selects the async closure
(`adelegate_task_to_member`) and the async delegation path (`member_agent.arun`),
never the sync one — T43's original race analysis was reading the correct branch.

## 4. Resolution of the `Team.arun(run_id=...)` vs. internal delegation discrepancy

The documented `Team.arun(run_id=...)` parameter is real but answers a different
question than the one this investigation needed: it identifies the **outer team run**,
not the **child member run** a delegation tool call creates. `RunContext.run_id: str`
(a required field, `agno/run/base.py:17`) confirms every run — team or member — has
its own distinct run context. Agno's own internal closures construct the member's
`RunContext` fresh, with a fresh `run_id`, every single delegation — this is why
`run_id` is unique per delegation (confirming, not contradicting, T41's own premise)
but also why nothing at the `Team.arun()` level can set it: the closure that creates
the member's run (`adelegate_task_to_member`) is the only code that could pass one
through, and its own fixed `(member_id: str, task: str)` signature offers no way for
a caller — including this codebase's own wrapper — to supply one.

## 5. Identity propagation mechanism selected, or why none is sufficient

| Mechanism | Reaches child before race? | Unique per delegation? | Recoverable at result consumption? | Supported/public? | Verdict |
|---|---|---|---|---|---|
| `Agent.run()/arun(run_id=...)` | Yes (if called) | Would be, if we generated it | Yes (copied onto events) | Yes, public | **Not reachable** — Agno's own closure never passes it, and offers no extension point |
| `run_context.metadata` | Yes (confirmed, pre-race) | Would be, if set per-call | **No** — `create_run_started_event`/`create_run_content_completed_event` never copy `metadata` onto the events consumed | Yes, public | **Ruled out** — direct evidence, not speculation |
| `run_context.dependencies` | Yes (confirmed, pre-race) | Same as metadata | Same as metadata — not copied onto events either | Yes, public | **Ruled out**, same reason |
| `tool.child_run_id` (Agno's own native bookkeeping, `_process_delegate_task_to_member`) | N/A (post-completion, not real-time) | Yes, if name-matched | Gated by `tool.tool_name.lower() == "delegate_task_to_member"` — **never matches**, since this codebase's wrapper renames the tool to `delegate_structured_task` | Internal | **Broken by this codebase's own tool rename** — noted, not fixed (fixing it wouldn't help: it's post-hoc team-level bookkeeping, not available to the real-time event-stream correlation this codebase relies on) |
| Upstream `member_run_id` + `register_member_run` | Yes (confirmed on upstream `main`) | Yes | Yes | Yes, public (upstream) | **Not available in installed 2.5.17** — requires a major version upgrade |

**No native mechanism in the installed Agno version is both reachable and
recoverable.** Option 5 (retain current behavior) was selected per Track C's own
decision tree, with the full rejection chain documented in the telemetry (Track
C/D record `OPTION5-RETAIN-CURRENT-BEHAVIOR`).

## 6. Exact production-code changes and commit

**None.** No file under `swarm/` or any other production path was modified this
phase. Per the mission's own explicit instruction ("If no code change is justified,
do not force a commit or deploy an unnecessary change"), only this report and the
telemetry file were committed (docs-only).

## 7. Targeted ZGX validation evidence

Two live checks were performed against the unchanged deployed commit (`c032e73`):

1. **Track A re-verification**: `journalctl` grep for both the T42 and T43 ambiguity
   diagnostics across the full window since T43's deployment — **zero matches**.
   Classified "no ambiguous events observed," not "proof of race-free operation."
2. **Deliberate concurrency-induction attempt**: a live task explicitly instructed
   the Coordinator to issue two `delegate_structured_task` calls to the same member
   "in the same turn, not one after another." Result: dispatched **strictly
   sequentially** anyway (delegation 1 fully completed at 19:51:16; delegation 2 not
   dispatched until 19:51:35 — 19 seconds later, zero overlap). Both delegations'
   own results were correctly, distinctly correlated via `run_id` with zero
   cross-contamination — the existing mechanism continues to work correctly under
   normal operation.

## 8. Whether genuine out-of-order completion was exercised

**No — NOT EXERCISED**, now across 6 consecutive phases (T39 through T44), including
this phase's own explicit, deliberate attempt to induce it via direct prompt
instruction. This is reported honestly as a limitation of what this Coordinator's
own real-world dispatch behavior produces, not as evidence that the underlying race
(proven via direct source reading of `asyncio.gather` and the pre-RunStarted awaits)
is absent or resolved.

## 9. Deployment status and deployed commit

No deployment cycle was needed (no code change). ZGX remains at `c032e73`
(identical to local HEAD), `agno-api.service` confirmed healthy and running
continuously throughout this phase's investigation (same main process, same start
time, zero unexpected restarts). The telemetry/report commit for this phase (made
after this report is finalized) is docs-only and will be pulled onto ZGX for
consistency, same as T43's own practice.

## 10. Confirmation no unit-test files were added or modified

Confirmed via `git status --short -uall`, byte-for-byte identical to the pre-T44
baseline. No test file was touched. The full unit-test suite was **not run** this
phase (per the mission's own explicit instruction); the three protected T42
scenarios were inspected read-only only insofar as their already-known behavior
(from T42/T43) was relied upon, not re-executed.

## 11. Remaining risks and next decision

1. **The pre-RunStarted race window remains open**, now confirmed to have no safe
   fix available in the installed Agno version. It is real but narrow (bounded by
   two specific awaits: `aregister_run`, `aread_or_create_session`), unexercised by
   any test or live run.
2. **Closing it natively requires one of**: (a) confirming a 2.x backport of
   upstream's `member_run_id`/`register_member_run` mechanism exists (not confirmed
   this phase — the search budget available did not pin down the exact version),
   or (b) a deliberately-scoped major Agno version upgrade (2.5.17 → 3.x) with full
   compatibility verification across this codebase's extensive Agno integration
   surface (`Function.from_callable` usage, custom hooks, event-type handling,
   `_get_delegate_task_function` assumptions) — a decision and effort beyond this
   phase's own scope, requiring explicit sign-off given the operational risk.
3. **If this boundary is revisited**: the next phase should either (a) do a focused
   search specifically for a 2.x Agno release that backports `member_run_id`/
   `register_member_run` without the 3.x breaking changes, or (b) if none exists,
   scope a dedicated Agno-3.x-upgrade compatibility phase before attempting this
   fix again — not re-attempt metadata/dependencies propagation, which this phase
   conclusively ruled out.
4. Genuine concurrent dispatch remains NOT_EXERCISED live; correctness there
   continues to rest on the unit test suite alone (unmodified, not re-run this
   phase, confirmed passing as of T43's own most recent verification).
