# PHASE T37 — EVIDENCE-TO-ANSWER INTEGRITY REPORT

## 1. Executive Verdict

**One defect proven and fixed; two others found and deliberately left for the next phase.**

Traced a real, documented incident (T36.1's own T13a-LIVE1 live replay) through the full evidence-to-answer pipeline and found its earliest provable failure boundary is **evidence gathering**, not transport (Priority A is clean for this incident). The next boundary — **final-answer completeness** (Priority B) — had a real, demonstrated defect in a pre-existing, generic, evidence-based mechanism (`_under_reported_exports`), fixed with a narrow, backward-compatible change, deployed to ZGX. A live validation attempt became orphaned (client-side timeout) before producing a final answer to re-check the fix against, but it surfaced an independently valuable Priority C/B finding (a brute-force tool-call pattern) that is documented, not implemented, per this phase's own "one defect per phase" discipline. No Priority C cache/dedup defect was found; the existing mechanism is sound.

## 2. T36.1 Baseline

`docs/runbooks/architecture-acceptance-runs/runs.jsonl` holds structured records for **only T10, T11, T13a** (all from phase T36.1). **T3, T6, T9, T12, and T13b have zero JSONL records** — their only documented results are prose in phase T36's final chat report, which was never persisted to a file anywhere in this repository (confirmed by a repo-wide search). This is itself a telemetry-quality finding (F3 below), reported honestly rather than papered over by inventing structured data that does not exist.

| Task | Accuracy | Containment | Duration | Source | Confidence |
|---|---|---|---|---|---|
| T3 | PASS | PASS | 1093.4s | T36 prose (chat only) | MEDIUM |
| T6 | PASS | PASS | 123.3s | T36 prose | MEDIUM |
| T9 | PASS | PASS | 83.2s | T36 prose | MEDIUM |
| T10 | PARTIAL | NOT_APPLICABLE | 204.3s | **JSONL: T10-LIVE1** | HIGH |
| T11 | PASS (live, prose) / controlled PASS | controlled PASS, live not exercised | 348.8s (live) | **JSONL: T11-INTEGRATION1** + T36 prose | HIGH (controlled) / MEDIUM (live) |
| T12 | FAIL (3 defects, all caught) | PASS | 1013.9s | T36 prose | MEDIUM |
| T13a | FAIL → PASS across attempts | PASS (confirmed live) | 775–992s (4 attempts total) | **JSONL: T13a-LIVE1, T13a-LIVE2** | HIGH |
| T13b | PARTIAL (by design) | PASS | 605.9s | T36 prose | MEDIUM |

## 3. Priority A — Evidence Lifecycle Findings

Traced T13a-LIVE1 (the clearest available incident) through all seven pipeline boundaries the mission names. The member's own text ("The frontend has a vouchers module... page.tsx (contains 2 top-level definitions)") shows it performed a **directory listing**, never a `get_file_content` read of the real API-slice file (`inventoryApi.ts`) — so the loss boundary is **source tool invocation itself**, before member reasoning, before capture, before `forward_member_answer` ever has anything to transport.

Confirmed this is not a transport bypass: the same run's journal shows `forward_member_answer`'s own coverage-ratio fallback (`relay coverage: members named 1, answer kept 0, missing 1` → `forwarded answer(s) not carried by the final answer -- appending verbatim: ['researcher']`) correctly fired for the **endpoints** content the member *did* gather. The mechanism works when there is something to forward; it cannot forward evidence that was never gathered. A separate, pre-existing, already-running experiment (`MINIMAL_COORDINATOR_GATE`) independently measures `forward_member_answer`'s own invocation reliability — not re-litigated here.

**No new evidence-transport/persistence defect was found.** Per Decision Gate A, this closes Priority A for the traced incident and moves the investigation to Priority B.

## 4. Priority B — Completeness and Accuracy Findings

Found a pre-existing, generic, **evidence-based** mechanism (`_under_reported_exports`, 2026-08-28, predates T36) built explicitly for this exact T13 shape — its own docstring: *"symbol_index knows inventoryApi.ts declares [5 real hooks]... so 'the answer names 1 of 5 siblings' is a fact, not a judgement."*

Its silencing condition, `if not named or len(named) == len(members): continue`, conflates two cases:
- **(a)** the answer never discusses the topic at all — correctly silent (the author's own stated intent).
- **(b)** the answer discusses the topic but names zero real identifiers — T13a-LIVE1's exact shape, incorrectly silent too.

**Fixed**: added an optional `task` parameter (backward compatible). Zero-named now fires only when the task explicitly asked about this family's domain (hooks) *and* the file was read with real exports on record — closing case (b) without touching case (a) or the already-working "named some but not all" path.

## 5. Priority C — Efficiency Findings

The existing read-cache (`_make_read_cache_tool_hook`, per-(tool,args) network cache + per-agent serve budget) is sound, already measured, and already documented with its own live incident history. No new cache defect was found.

A live validation attempt (see §9) surfaced a genuinely new, directly-logged finding: a Researcher delegation made **79 tool calls against a 25-call budget**, 77 of them `search_files` calls sequentially guessing roughly 60 speculative, nonexistent RTK-hook name patterns (`useReactQueryCancelProvider`, `useReactQueryAbort`, `useReactQuerySkeleton`, …) instead of reading the real file directly. This is a real, measurable cost pattern — not implemented this phase, recorded as the top candidate for the next one.

## 6. Ranked Findings Register

| ID | Priority | Status | Confidence |
|---|---|---|---|
| F1 — `_under_reported_exports` zero-named blind spot | B/A | **SELECTED, IMPLEMENTED, DEPLOYED** | HIGH |
| F2 — brute-force hook-name guessing (79/25 tool calls) | C/B | Documented, not implemented — next experiment | MEDIUM-HIGH (cost pattern) / LOW (downstream impact, unconfirmed) |
| F3 — T36's results never persisted to a file | observability | Documented, not implemented | HIGH |

## 7. Controlled Experiment

- **Baseline**: `_under_reported_exports(content, read_state)` — no `task` parameter exists; 6 new tests raise `TypeError` against this exact baseline, proving the defect's boundary precisely.
- **Hypothesis**: the "named==0" silencing conflates two distinct, distinguishable cases.
- **Reproduction**: constructed the exact T13a-LIVE1 shape (file read, 5 real hooks on record, task asks about hooks, answer names zero) as a controlled fixture.
- **Intervention**: optional `task` param; zero-named fires only when task asks about "hooks" (regex, narrow, matches this function's own existing hook-only scope).
- **Result**: 7/7 tests pass post-fix, including 5 negative controls proving no loosening.

## 8. Test Results

- New: 7 tests (`tests/test_t37_under_reported_exports_zero_named.py`), all passing.
- Full root suite: **54 pre-existing failures unchanged** (T36/T36.1's own confirmed baseline), 2591 passed.
- Production-path validation: call site (`_verified_answer`, line ~9003) updated to pass `task=task`; confirmed via direct code reading that `task` is in scope there (it is `_verified_answer`'s own parameter).

## 9. Before/After Comparison

| | Before | After |
|---|---|---|
| T13a-LIVE1 shape (0/5 named, task asks hooks, file read) | Silent (confirmed via failing test against baseline) | Fires with real export count and missing names |
| Off-topic task, same read_state | Silent | Silent (unchanged) |
| Partial/full naming | Unchanged behavior | Unchanged behavior |

A live end-to-end replay (T13a-T37-LIVE1) was attempted post-deployment but ended **ORPHANED**: the `agno_run` MCP call exceeded its 1800s client timeout; `ps aux` on ZGX subsequently found no worker process, but the server-side final disposition could not be confirmed. Per this phase's own rule, this is recorded as INCONCLUSIVE for end-to-end re-confirmation, not silently discarded or retried under the same attempt ID.

## 10. Deployment Evidence

- Commit `d380975` pushed to `origin/main`.
- ZGX `agno-api.service`: `git pull --ff-only` → `d380975`, `systemctl --user restart`, clean journal startup confirmed, 2 "T37" source markers grep-confirmed in the running checkout.
- `hive-mcp` (port 9003): **untouched** — this fix is `swarm/team.py` only, not verifier-relevant.
- `hive-mcp-agno` (port 9004): untouched, confirmed out of scope throughout.

## 11. Telemetry Artifacts

- `docs/runbooks/architecture-acceptance-runs/runs.jsonl` — 12 total lines across T36.1 (8) and T37 (4: 1 baseline, 2 task_attempt, 1 phase_summary).
- This file.
- No schema changes this phase — T37 reused T36.1's schema exactly, as instructed.

## 12. Preservation Statement

T24's uncommitted work (`swarm/execution_context.py`, partial `swarm/team.py` hunks) and all pre-existing dirty/untracked files remain exactly as they were at phase start, verified via hunk-level `git add -p` staging (confirmed zero T24 content in the committed diff) and `git status` before and after every commit this phase.

## 13. Remaining Risks

- F1's fix has no live end-to-end re-confirmation yet (the one attempt orphaned) — unit-level proof against real incident data stands, live proof is open.
- F2 (brute-force hook guessing) is documented but unimplemented; its downstream accuracy impact on the specific orphaned run is unconfirmed (the final answer was never observed).
- F3 (T36's results never persisted) remains a process gap for future phases to close, not attempted here to avoid scope creep.

## 14. Next Recommendation

**Investigate F2 next**: reproduce the 79-tool-call brute-force hook-guessing pattern deliberately (a no-files-named T13a-shaped task), confirm whether it recurs, and — if it does — implement the smallest correction (most likely: instructing the member to list the directory or call a targeted glob before attempting name-guessing, mirroring the existing "scan-first" convention already used elsewhere in this codebase) rather than widening any verifier guard further.
