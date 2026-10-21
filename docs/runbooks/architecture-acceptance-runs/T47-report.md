# T47 — Passive Production Telemetry for Genuine Concurrent Member Delegations

## 1. Baseline

Branch `main`, starting HEAD `06d9030` (T46's own end state), working tree identical to every prior phase's baseline (T24's 4 uncommitted hunks + unrelated training/docker files, confirmed before any edit). Deployed T45/T46 revision confirmed identical on local and ZGX (`06d9030`), service healthy, continuously running since T45's own fix restart.

## 2. Root-cause context — why T45/T46 did not establish organic concurrency

T45's own harness and T46's 36-check hardening pass both exercise correlation correctness under **simulated** overlap — two or three delegations held behind explicit `asyncio.Event` barriers, in a process that never actually drives the real Coordinator model. Neither phase, nor any phase back to T39, ever observed the real, model-driven Coordinator naturally dispatch two `delegate_structured_task` calls to the same member within one turn — even T44's own explicit instruction to batch two calls still produced sequential dispatch. The gap is not a weakness in the isolated harnesses; it is that **no instrument existed to notice an organic occurrence if one ever happened**, so the question could only be answered by inference from dispatch/completion timestamps in a specific test run, never from passive, always-on observation.

## 3. Implementation

**`swarm/team.py`**:
- `_check_member_concurrency_episode(team, agent_name)` — called from both claim-success sites inside `_claim_pending_entry_for_run` (the exact-match path and the oldest-unclaimed fallback), immediately after `entry["run_id"]` is set. Counts entries in `team._pending_log_entry_by_member[member]` that already carry a `"run_id"` key; if that count reaches 2+ and this member isn't already in an active episode, calls `phase0.record_member_concurrency` and marks the episode active.
- `_clear_member_concurrency_episode_if_resolved(team, agent_name)` — called once from `_finalise_member_chunks`, after either of its two removal paths (run_id-based or FIFO fallback), re-checking the count and clearing the active-episode flag once it drops back below 2.
- Episode state: `team._concurrency_episode_active_members`, a `set` bounded by the small, fixed number of distinct team members — never grows unboundedly.

**`swarm/phase0.py`**: `Phase0Run.record_member_concurrency`, following the exact existing `record_delegation`/`record_member_result` pattern — appends to a new `self.concurrency_events` list, calls the existing `_emit()` (stdout `[phase0] {...}` line + durable JSONL append, both best-effort, wrapped in `try/except` so a telemetry failure can never affect the run). Reuses the `PHASE0_TELEMETRY`-gated channel already confirmed active on ZGX (`PHASE0_TELEMETRY=1` in the deployed `.env`) rather than inventing a parallel logging system.

## 4. Evidence quality — how genuine overlap is distinguished from false signals

The authoritative signal is **not** raw membership in any registry — it is `entry["run_id"]` being set, which happens in exactly one place (`_claim_pending_entry_for_run`), at the exact moment Agno's own `RunStarted` event is observed for that specific delegation. This directly avoids the pitfall the mission explicitly named:

- A dispatch-time pre-registration (`_pending_entry_by_expected_run_id`) proves a delegation was *dispatched*, not that Agno has begun *executing* it — rejected as the signal, and the rejection is demonstrated, not merely asserted: Scenario E dispatches two delegations that never receive a `RunStarted` at all, and confirms zero events fire despite two raw registry entries existing.
- Two delegations to *different* members never produce a false same-member event (Scenario D) — the count is checked per-member-key.
- A rise from 2 to 3 outstanding delegations does not re-fire the event (Scenario C) — dedup is per-episode, not per-observation.
- A full drain back to 0, followed by a later, separate overlap, correctly produces a **second**, distinctly-`episode_id`'d event — the flag is cleared, not permanently consumed.

## 5. Validation matrix

| Scenario | Description | Result |
|---|---|---|
| A — Sequential | Two delegations to the same member, sequential | **PASS** — no event, no stale entry |
| B — Genuine overlap | Two delegations held outstanding via a real `asyncio.Event` barrier | **PASS** — exactly one event; correct member; `outstanding_count≥2`; distinct, correct `run_id`s; `confirmed_overlap=true`; episode_id present; correlation results unaffected |
| C — Three-way overlap | Three delegations outstanding simultaneously, scrambled completion | **PASS** — no re-fire on 2→3; correct cleanup; fresh episode after a later, separate overlap |
| D — Different members | Overlapping delegations to two different members | **PASS** — zero false positive |
| E — Lifecycle exits | Two delegations dispatched but never granted `RunStarted` | **PASS** — zero events, confirming the chosen signal resists registration-count false positives |
| F — Correlation integrity | Out-of-order completion through the real T45 path, during an active episode | **PASS** — covered within Scenario B's own completion check; results stay correctly associated, no FIFO substitute introduced |
| G — Output protocol | stdout-only-JSON contract under the new code path | **PASS** — the event's own print lands on the redirected stream in a worker-protocol simulation; exactly one clean JSON line reaches the real stdout |

21 checks locally (3× for determinism), 12 checks re-run directly against the deployed ZGX source (identical results). All 15 directly-relevant existing tests, and T45's/T46's own prior harnesses, re-run unmodified and pass.

## 6. Deployment

Commit `f016752`, pushed, pulled fast-forward onto ZGX (unrelated dirty files preserved). Service restarted; `/health` → `{"status":"ok",...}`; confirmed Main PID active continuously since restart, zero further incidents. The deployed **source**, not merely the commit hash, was independently re-verified by re-running the condensed harness directly against the ZGX checkout via its own production Python environment — identical pass results.

## 7. Journal evidence

Controlled-validation event (from this phase's own re-run against the deployed source, not organic production):

```
[phase0] {"type": "member_delegation_concurrency_observed", "run_id": "17376148aae7",
"seq": 1, "member": "researcher", "episode_id": "ba014add-ac5e-4d79-ab18-9f215b415680",
"outstanding_run_ids": ["67260ddf-15c0-457c-b5dc-815fb77fff3d",
"e815a930-b193-4af8-82f7-1d972cb86a3a"], "outstanding_count": 2, "confirmed_overlap": true}
```

This is a **controlled harness observation**, explicitly not presented as an organic production event.

A single, narrow live smoke check (one delegation, deliberately not engineered to force overlap) completed cleanly (`outcome: ok`, 182.0s), with **zero** occurrences of `member_delegation_concurrency_observed` in the journal — correct and expected, since only one delegation was dispatched.

## 8. Production observation status

- **Telemetry implemented and deployed**: yes.
- **Controlled overlap successfully detected**: yes (§5, §7).
- **Organic model-driven production overlap observed**: **NOT OBSERVED** this phase. No multi-delegation live run was attempted, per the mission's own explicit instruction not to force one. The telemetry is now live and will capture the first organic occurrence, whenever it happens, from reliable journal evidence going forward.

## 9. Regression and safety

- T45's correlation semantics (dispatch order, claim matching, fallback policy, cleanup) are provably unchanged — all 15 existing tests and both prior harnesses pass unmodified.
- The stdout-only-JSON contract remains intact, verified both in a direct worker-protocol simulation and in the live smoke check's own successful round-trip.
- No unit-test file was added, modified, deleted, or renamed.
- T24's own 4 uncommitted hunks and all unrelated ZGX state (training scripts, `.env.bak-*`, docker configs) remain byte-identical to the established baseline throughout.

## 10. Limitations and next action

The open question from T46 — does genuine concurrent member delegation ever actually occur in a live, model-driven production run — **remains unanswered**, by design: this phase builds the instrument, not the observation. The only remaining action is passive: watch the journal (or the durable `data/phase0/delegations.jsonl` artifact) for the first naturally-occurring `member_delegation_concurrency_observed` line from real production traffic. No further code change is recommended; the telemetry's own authoritative signal (keyed on Agno's real `RunStarted`, not a registry count) has been directly validated to resist every false-positive scenario the mission specified.
