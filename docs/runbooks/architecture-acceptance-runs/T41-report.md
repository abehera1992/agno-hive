# PHASE T41 FINAL REPORT — SEMANTIC-PROGRESS DETECTION (F5) + FIFO DELEGATION-EVIDENCE CORRELATION INTEGRITY

## 1. Executive Verdicts

**Track A (F5): PARTIAL.** Contract established, gap proven baseline-first, fixed with a small, deterministic, documented subject-category mechanism, deployed and re-run against the deployed ZGX source. Live-model trigger: not attempted — `PARTIAL`, not rounded up.

**Track B (FIFO correlation): PARTIAL.** `asyncio.gather`-based concurrent tool-call execution, proven from agno's own installed source, means T40's FIFO ordering assumption is architecturally unsafe (Outcome B3-B). Fixed with `run_id`-based correlation, deployed and re-run against the deployed ZGX source. Live-model trigger of genuine out-of-order completion: not attempted — `PARTIAL`, not rounded up.

Per the mission's own final decision rule, neither track achieved a live-model trigger, so **T41 as a whole remains PARTIAL**.

## 2. F5 Contract and Evidence-Backed Impact

`_objective_names_new_entity` (`swarm/team.py`) decides, for the duplicate-delegation gate's `delegate_structured_task` branch, whether a wording-different, same-target delegation is "the same question, reworded" (stays blocked / served the prior result) or "a genuinely different question" (runs fresh). Before this fix it recognized only a new CamelCase/snake_case identifier (`Party` → `PartyRegistration`); a genuine subject change expressed in ordinary English (`list the backend endpoints` → `list the frontend hooks`) was invisible to it. The asymmetry matters: a false negative here (missing a real subject change) risks serving the *wrong* prior result for an unrelated question — the exact mechanism underlying both the original F3 incident and this phase's own F5 finding. A false positive only costs a redundant fresh delegation, informing a conservative design bias.

## 3. Baseline-First Semantic Test Results

`tests/test_t41_semantic_progress_detection.py` — 22 tests across categories A–H. Against the unmodified baseline: **6 failed** (the genuine category-A/F/H subject-change cases: endpoints↔hooks, auth↔migrations, upload↔import, routes↔config, plain-language-no-identifier, and the real caller's own decision). **16 already passed** (synonym reworded-same-subject, identifier-only changes, filler/reformulation, broad-to-narrow-within-category, one-sided/ambiguous category input, empty/generic input) and must not regress. All 22 pass post-fix.

**False-positive/false-negative analysis:** the fix fires only when *both* sides name a recognized category and those categories are disjoint — one-sided or uncategorized input stays conservative (silent) rather than guessing. Fixed vocabulary (endpoints/hooks/tables/models/migrations/auth/config/upload/import/tests/onboarding) deliberately does **not** attempt general prose similarity — this codebase's own documented history (`_normalize_delegation_task`'s docstring) already tried and ruled out SequenceMatcher/Jaccard for this exact gate, for the `Party` vs `PartyRegistration` failure shape (near-identical prose, different targets); a continuous similarity score cannot tell synonym from entity-difference without reintroducing that risk, while a fixed categorical lookup can, because project-specific entity names are simply not in this vocabulary.

## 4. FIFO Lifecycle and Ordering Analysis

Traced the complete lifecycle (B0) from `swarm/team.py` and agno's own installed source (`agno/models/base.py`, `agno/utils/events.py`):

1. **Dispatch**: `delegate_structured_task` appends a log entry and (T40) a FIFO queue entry for the dispatching member — synchronous, in dispatch order.
2. **Agno's own execution**: multiple tool calls requested in one model turn run via `asyncio.gather(*(self.arun_function_call(fc) for fc in function_calls_to_run))` — the comment in agno's own source reads *"Create and run all function calls in parallel"*. For async-generator results (confirmed: `delegate_structured_task` returns one), each gets its own background task (`process_async_generator`) feeding a **shared** `asyncio.Queue`.
3. **Completion**: whichever generator's own underlying work (tool calls, model turns) finishes first emits its `RunContentCompleted` event first — **not necessarily the one dispatched first.**

This directly contradicts T40's own FIFO-fix assumption (completion order matches dispatch order). The synchronous *dispatch-time* code (including the FIFO queue's own `append`) is unaffected — it reliably runs in scheduling order before any coroutine yields control at its first real await — so the gap is specifically between dispatch-time ordering (safe) and completion-time ordering (not guaranteed).

## 5. Reverse-Completion, Cross-Member, Exactly-Once, and Stale-Entry Results

`tests/test_t41_fifo_correlation_integrity.py` — 10 tests (Cases 1, 2, 3, 3b, 4, 5, 6, 9, 10, 11):

| Case | Result |
|---|---|
| 1 — sequential dispatch | PASS (unchanged from T40) |
| 2 — overlapping dispatch, in-order completion | PASS (unchanged from T40) |
| **3 — reverse completion order, no `run_id` claimed** | **PASS — but documents the proven misattribution** (A's repeat served B's text) when the fallback-only path is exercised |
| **3b — reverse completion order, with `run_id` claimed** | **PASS — the fix resolves it**: both A and B get their own correct text regardless of completion order |
| 4 — interleaved completion across two members | PASS — no cross-member contamination |
| 5 — F3's own incident shape (duplicate block) | PASS — reproduced and closed |
| 6 — failed/empty delegation | PASS — does not inherit a later success's text |
| 9 — exactly-once consumption | PASS — `run_id` removed from both the lookup dict and the FIFO queue on use |
| 10 — cross-team-instance isolation | PASS — no leak |
| 11 — T24 identity pin | PASS (see §8) |

Timeout/cancellation (Case 7) and a genuinely duplicated-consumption race under true parallelism (deeper Case 9 adversarial variants) were **not** separately exercised — the existing `_finalise_member_chunks` early-return on an empty buffer already covers the "no text materialized" shape (Case 6), and no distinct timeout/cancellation code path beyond that was found to exist for this specific mechanism; this is reported as a gap, not claimed as covered.

## 6. Is FIFO Sufficient? (B3 Verdict)

**No — Outcome B3-B.** FIFO alone is correct for the common case (sequential or in-order-completing dispatch) but not architecturally guaranteed under agno's own concurrent (`asyncio.gather`) execution model. Fixed with `run_id`-based correlation: agno's own `create_run_started_event`/`create_run_content_completed_event` (confirmed by direct source reading, `agno/utils/events.py`) both set `run_id=from_run_response.run_id` — a real, populated, per-invocation-unique value, not a theoretical dataclass default. Claimed at the earliest dispatch-order-safe point (`RunStarted`, confirmed as a real event type this codebase's own live journal already observed), resolved at completion via that established `run_id` regardless of completion order. Falls back to T40's own exact FIFO behavior whenever no `run_id` was claimed (an older agno, a missed event, or a test double) — never a crash, never a silent behavior change for that case.

## 7. F3 Incident-Shaped Regression

Case 5 directly reproduces T40's own incident shape: a repeated backend-endpoints ask, interleaved with a frontend-hooks delegation to the same member, correctly resolves to the backend-endpoints text — not the frontend-hooks text — under the new correlation mechanism.

## 8. Exact T24 `test_evidence_fidelity.py` Test Names and Outcomes

Per Gate C2's explicit instruction to re-verify rather than assume T40's own claim:

| Test | T40's claim | Re-verified against T40's own committed end state (git HEAD before T41) | Re-verified with T41's Track A fix applied |
|---|---|---|---|
| `test_delegated_researcher_evidence_resolves_to_researcher_not_coordinator` | passing | **FAILING** (3 consecutive identical full-suite runs) | PASSING |
| `test_multiple_delegated_members_each_resolve_to_their_own_identity` | passing | **FAILING** (3 consecutive identical full-suite runs) | PASSING |
| `test_relay_retention_no_longer_forced_to_zero_for_delegated_evidence` | passing | **FAILING** (3 consecutive identical full-suite runs) | PASSING |

**T40's own claim does not hold under re-verification.** A clean, reproducible (3× confirmed, full-suite-to-full-suite, not isolated) re-measurement found these 3 tests still failing against T40's own fully-committed end state (`b0e8760`+`d4246db`). They pass once T41's own Track A fix (`c5d7440`) is additionally applied. **No code path connects `_objective_names_new_entity` to `_evidence_fidelity_report`/`_fidelity_agent_name`** (confirmed by direct inspection — neither calls the other, no shared module-level state) — this correlation is reported honestly as reproducible but **causally unexplained** (finding F6), not claimed as an understood side effect of this phase's own fix.

## 9. Pre-Existing vs. Newly-Introduced Failures

Full suite (both tracks applied): **54 failed, 2649 passed.** A precise failing-test-**name** diff (not count) against the Track-A-only checkpoint is **empty** — Track B introduces zero new failures. The 54 are the same pre-existing set confirmed throughout T38–T40 (10 from `test_duplicate_delegation_gate_hook.py`'s own unrelated `UnboundLocalError` bug, 2 from `test_team_read_cache_hook.py`'s stale hook-count assertion, etc.), **minus** the 3 `test_evidence_fidelity.py` tests this phase's own fix resolves (§8) — i.e., the *true* pre-T41 baseline (git HEAD, no T41 changes) is **57**, not the "54" T40 itself reported; see §8 for the reproducible re-measurement.

## 10. Commits, Deployments, Runtime Health

| Component | Commit | Runtime | Method | Verified |
|---|---|---|---|---|
| Track A fix | `c5d7440` | ZGX `agno-api.service` (9001) | `git pull --ff-only` + `systemctl --user restart` | clean restart; 1 "T41 (F5)" marker; 22/22 tests re-run against deployed source via its own production Python env |
| Track B fix | `3e1ce58` | ZGX `agno-api.service` (9001) | `git pull --ff-only` + `systemctl --user restart` | clean restart; 3 "T41 (Track B)" markers; 10/10 + 22/22 (Track A re-confirmed together) tests re-run against deployed source |
| `hive-mcp` (9003) | — | untouched | n/a | not implicated by either track |
| `hive-mcp-agno` (9004) | — | untouched | n/a | explicitly out of scope, not touched |

## 11. Whether Each Intended Path Was Actually Exercised

- Track A's new subject-category signal: **exercised** (22 tests against the real production function/hook, both locally and on deployed source).
- Track B's `run_id`-based correlation, including under genuine reverse completion order: **exercised** (10 tests against the real production functions, both locally and on deployed source).
- Live-model triggers for either track: **NOT_EXERCISED** — not claimed, not attempted this phase. Forcing a model to (a) rephrase a question in a way that crosses this phase's exact subject-category boundary, or (b) dispatch 2+ concurrent delegations to the same member that complete in reverse order, is harder to force deterministically than even T39's/T40's own live scenarios, which already proved unreliable to reproduce on demand.

## 12. T24 and Unrelated Dirty-File Preservation Confirmation

`git diff --stat` on T24's own files, identical before this phase and after:

```
docs/runbooks/groundedness-battery.md |  39 ++
swarm/execution_context.py            |  32 ++-
swarm/team.py                         |  33 ++-   (T24's own hunks only -- both tracks' own hunks committed separately)
tests/test_evidence_fidelity.py       | 153 ++++
training/train.py                    | 416 ++++++++++---
zgx-ai-setup/litellm-config.yaml      |   6 +-
```

Staged `swarm/team.py` via hunk-by-hunk `git add -p` for both Track A (2 of 7 hunks) and Track B (5 of 10 hunks) commits; `git diff --cached | grep -c "T24:"` returned 0 both times. No `git add -A`/`git add .` used. Stray 0-byte artifacts (`'list`, `hooks)`, `None`) found and removed before staging, matching this session's long-documented shell-heredoc-artifact pattern.

## 13. Telemetry/Runbook Records Added

`docs/runbooks/architecture-acceptance-runs/runs.jsonl` — 4 new records this phase (1 `phase_baseline`, 2 `task_attempt` — `SUBJECT-CATEGORY-ENTITY-FIX1`, `RUN-ID-CORRELATION-FIX1` — 1 `phase_summary`), bringing the file to 30 total lines, all validated as parseable JSON. Same schema and six-value guard-outcome vocabulary as T36.1–T40, no schema changes. This file.

## 14. Remaining Risks and Smallest Justified Next Action

- **F6** (the unexplained `test_evidence_fidelity.py` correlation, §8) is the clearest open item — reproducible, high-confidence as an *observation*, zero confidence on *mechanism*. Worth a focused, narrow investigation (likely: whether import side effects, module-level caching, or `PYTHONHASHSEED`-sensitive iteration somewhere in `_evidence_fidelity_report`'s own call chain is the true cause) before the NEXT phase assumes anything about it.
- Neither track achieved a live-model trigger. Timeout/cancellation (Case 7) was not separately exercised for Track B — flagged, not fabricated.
- Track A's fixed vocabulary (11 categories) is scoped to this codebase's own observed delegation-objective domain; a genuinely novel subject area outside that vocabulary would still fall through to the conservative (silent) path, same as any uncategorized input today.

**Next recommended phase:** investigate F6 (the evidence-fidelity correlation) as its own narrowly-scoped question — specifically whether it is genuinely caused by code content/size rather than semantics, or is masking a separate, real flake — before treating either T40's or this phase's own counts as the stable baseline going forward.
