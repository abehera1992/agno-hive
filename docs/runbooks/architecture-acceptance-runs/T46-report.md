# T46 — Post-T45 Identity-Correlation Hardening and Lifecycle Validation

## 1. Executive verdict

| Dimension | Verdict |
|---|---|
| Investigation | **PASS** |
| Context isolation (Track B) | **PASS** |
| Claim lifecycle (Track C) | **PASS** |
| Wrapper compatibility (Track D) | **PASS** |
| stdout/JSON protocol integrity (Track E) | **PASS** |
| Deterministic concurrency | **PASS** |
| Deployment | **NOT EXERCISED** |
| Live validation | **NOT EXERCISED** |

T46 was a focused correctness-hardening pass over T45's already-deployed identity-correlation fix, not a redesign. A comprehensive harness (36 checks across four tracks, run against the real production functions and a real `agno.agent.Agent` instance) found **zero defects**. Per the mission's own explicit instruction — "Do not rewrite the working T45 design without concrete evidence of a defect" — **no code was changed, and no deployment cycle was run.** ZGX remains at the exact commit (`a506f35`) this phase validated.

## 2. Exact deployed and candidate commit SHAs

Local HEAD, `origin/main`, and ZGX HEAD are all identical: `a506f35eb44f3b7d3523499f9d87590ebfd43755`. No candidate commit exists — this phase produced no changes.

## 3. Files inspected and changed

**Inspected**: `swarm/team.py` (the contextvar declaration at line 12516, the dispatch-time registration inside `_duplicate_delegation_gate_hook`, `_install_member_run_id_patch` + `_ensure_member_run_id_patch_installed`, and the exact-match branch in `_claim_pending_entry_for_run`) and `main.py` (`_run_worker_main`, `_run_worker`). **Changed**: none.

## 4. Context propagation findings (Track B)

8 checks, all pass, against the real wrapper and real hook:

- No pending identity → `run_id` stays unmodified (`None`).
- A call immediately after consumption never re-sees an already-consumed id.
- The contextvar is correctly cleared even when the wrapped call raises (consume-*then*-call-through ordering, not call-then-clear).
- The contextvar is correctly cleared before a cancellation of the awaiting task could matter — same ordering guarantee applies.
- A call with no pending identity retains completely original, unmodified behavior.
- Sequential reuse of the same member across two full dispatch-to-resolution cycles produces a new, distinct identity each time, each correctly, distinctly resolved.

**Specifically scrutinized, per the mission's own Track B point 5**: the wrapper's `.set(None)` blind-clear, rather than capturing and `.reset(token)`-restoring a prior `Token`. Confirmed functionally correct for this codebase's actual call pattern — one dispatch per `asyncio.Task`, consumed at most once per task, never genuinely nested *within* a single task in this codebase's own architecture (confirmed via a nested-call simulation: a second call after consumption correctly sees `None`, never a stale value). **Documented as a reviewed, accepted simplification, not a defect** — no scenario in this codebase's real call structure ever requires restoring a non-`None` "previous" value, since the contextvar's natural resting state already *is* `None`.

## 5. Claim-table lifecycle findings (Track C)

13 checks, all pass, via the real `_duplicate_delegation_gate_hook`/`_claim_pending_entry_for_run`/`_finalise_member_chunks`/`_stream_event_to_chunk`:

- **Three** overlapping delegations to the same member, completing in a deliberately **scrambled** order (C, A, B — not merely reversed), each resolved correctly via exact identity match, expected-id table fully drained afterward, zero cross-contamination across any pair.
- A `RunContentCompleted` carrying an identity matching **neither** the expected-id table **nor** any queue entry does not crash, and does not silently consume the genuinely-pending, unrelated entry.
- A **forced, artificial** id collision (simulating a theoretical `uuid4` collision, not a reachable real path) resolves via the dict's own last-write-wins semantics rather than crashing — documented as the actual behavior under this hypothetical, not claimed as a designed safety net.
- Finalising one delegation's claim does not disturb a second, still-pending sibling's own entry.
- A delegation whose own `RunStarted`/completion events **never arrive at all** leaves exactly one harmless, inert stale entry — a bounded, per-run-lifetime-only leak (confirmed: `team` objects do not persist across runs; every live ZGX run spawns a fresh worker subprocess) — and does **not** block or corrupt a separate, later delegation to the same member.

## 6. Wrapper compatibility findings (Track D)

10 checks, all pass, against a **real** `agno.agent.Agent` instance (the model call itself stubbed via monkeypatching `agno.agent._run.arun_dispatch`, never reached):

- Positional `input` correctly reaches `arun_dispatch` as `input=` — per `Agent.arun`'s own real signature, which re-passes `input` to `arun_dispatch` as a keyword regardless of how the caller supplied it (this was the correct layer to assert against; an earlier version of this check asserted against the wrong layer and was fixed before drawing any conclusion).
- Keyword args forwarded unchanged; `run_id` correctly injected from the pending contextvar.
- Non-streaming return value preserved exactly.
- **The streaming path** (`stream=True` — the actual shape AgnoHive's own production code uses) correctly returns an iterable async generator, with `run_id` also injected.
- Exception type and message preserved unchanged through the wrapper.
- An explicit caller-supplied `run_id` is never overridden by a pending contextvar value.
- A second call to the install function does not re-wrap `Agent.arun` — idempotent, and proven safe under asyncio's single-threaded cooperative scheduling (the entire install function body has zero `await` points, so no genuine race can interleave a "concurrent" install).

## 7. Subprocess stdout/stderr protocol evidence (Track E)

5 checks directly, empirically re-verifying the exact invariant T45's own deployment regression violated — simulating `main.py`'s own `_run_worker_main()` discipline exactly (redirect first, restore only immediately before the single final print):

- A normal run that triggers the lazy `Agent.arun` patch install produces **exactly one** clean JSON line on the real stdout-equivalent stream; the install print correctly lands on the redirected stream instead.
- A run whose body **raises** also produces exactly one clean JSON line (the caught-and-converted error dict) — zero raw traceback text leaks onto the real stream.
- Repeating a clean run a second time in the same process stays clean.

## 8. Deterministic harness methodology and results

Two temporary, scratchpad-only scripts (never tracked test files): `t46_harness.py` (31 checks across Tracks B/C/D) and `t46_track_e_protocol.py` (5 checks, Track E). Both use real `asyncio.Task`s and `asyncio.Event` barriers for genuine ordering control — no `sleep()`-based timing. The main harness was run **3 times** with identical pass results each time (deterministic, not a race won by chance). All 15 directly-relevant existing tests (`test_t41_fifo_correlation_integrity.py`, `test_t40_pending_entry_cross_delegation_leak.py`) re-run unmodified, passing.

**Two false-positive failures were found and fixed in the harness's own code, not the production implementation**, before any conclusion was drawn: a deadlock in the 3-way scrambled-completion scenario from an incomplete event-release chain, and the positional-arg assertion against the wrong layer (described in §6).

## 9. Production concurrency observed?

**No — NOT EXERCISED**, as in every prior phase since T39 (now 8 consecutive phases). No live run was attempted this phase (no code changed, so no live validation was needed or run). This is understood to be a structural property of this Coordinator's own real-world tool-dispatch behavior (T44/T45 both showed even an explicit instruction to batch delegations still produced sequential dispatch), not a gap in validation effort — correctness under genuine overlap rests on the isolated harnesses (T45's own, plus this phase's substantially expanded one).

## 10. Focused checks run / deliberately not run

**Run**: the 36-check hardening harness (3×), the 5-check protocol harness, the 15 existing correlation tests.
**Deliberately not run**: the full unit-test suite (per mission instruction); any live ZGX smoke test (no code change to validate); a true `RunCancelledException`-based cancellation test exercising agno's own internal cancellation machinery (out of this patch's own scope — confirmed in T45's own upstream research that this is a separate, much larger mechanism this codebase does not vendor); a real `uuid4` collision (not a reachable code path — simulated via a forced artificial dict overwrite instead, and reported as such, not substituted silently).

## 11. Deployment and health evidence

Not applicable — no code change, no deployment cycle. ZGX confirmed still running continuously since the T45 fix restart (2026-10-20 20:33:03 EDT), zero incidents in the interim, `/health` returns `{"status":"ok",...}`.

## 12. Remaining risks and smallest justified follow-up

1. Production concurrency remains unexercised live across 8 consecutive phases — no action justified; this is a structural, observed property of the Coordinator's own dispatch behavior, and correctness under it now rests on two independently-built, substantially overlapping isolated harnesses.
2. The `Agent.arun`-only coverage boundary (not `Team.arun`) remains as documented in T45 — revisit only if a sub-Team member is ever added to this deployment's topology.
3. A true `uuid4` collision remains theoretically unhandled beyond last-write-wins dict semantics — acceptable given cryptographically negligible probability; no action justified.
4. **Smallest justified follow-up, if ever revisited**: none identified. The implementation passed every check this phase could construct. If a future phase wants additional confidence, the highest-value next step would be observing genuine production concurrency itself (not a new isolated test), which remains outside this phase's or any prior phase's control.
