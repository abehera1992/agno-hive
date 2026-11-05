# Phase 0.38 — Reviewer Finding Preservation Across Correction and Repetition Recovery

Investigation into the November 5, 2026 T13a incident (final answer shipped imprecise frontend hook names and a false "full CRUD coverage" conclusion despite a real 4-item backend/frontend gap), reconstruction of the exact failure mechanism from direct journal evidence, a narrowly-scoped fix, controlled validation against the real production function, and live ZGX deployment with validation.

## Section 1 — Starting and Ending State

| | Start of phase | End of phase |
|---|---|---|
| `agno-hive` local | `a5d74f1` | `ba8744e` |
| `agno-hive` origin | `a5d74f1` | `ba8744e` |
| `agno-hive` ZGX | `75d8d7a` (code-identical to `a5d74f1`) | `ba8744e` — pulled via fast-forward `git pull`, never reset/checkout/clean |
| ZGX working tree | 35 dirty/untracked entries | 35 dirty/untracked entries — exact count unchanged before and after, none touched |
| `agno-api.service` | PID `1557931` | restarted (team.py changed) — new PID `2812255`, active since `2026-11-05 11:54:19 EST`, health 200, no errors in post-restart journal |
| `hive-mcp` container | image `237a88da3701d...`, healthy | **unchanged this phase** — no `hive-mcp`/`verify.py` edit was required |

**Files changed:** `swarm/team.py` only — 40 insertions, 1 deletion, commit `ba8744e`. No other file was staged or touched; all 35 pre-existing dirty/untracked entries (deleted legacy `T36–T66` reports, modified `groundedness-battery.md`/`test_evidence_fidelity.py`/`train.py`/`litellm-config.yaml`, and several stray single-token files) were explicitly excluded from every `git add` and remain exactly as found.

## Section 2 — Failure Reconstruction

Reconstructed directly from the `agno-api.service` journal for PID `2646213` (run `c7223a84b865`, the actual November 5, 2026 T13a incident), read in full — not inferred from the earlier report's summary.

**B1 — exact event sequence.**

1. **10:40:39** — Coordinator delegates to Researcher: "Audit the vouchers module: list its endpoints, database tables, and frontend hooks." The Researcher does real, grounded work: `search_files('voucher')`, `search_files('vouchers')`, `get_file_content(vouchers_api.py)` (45,338 chars, 30 declarations indexed), `get_file_content(models.py)` (38,172 chars, 36 declarations indexed).
2. **10:41:17** — The Researcher's own text generation hits the in-process repetition detector at only **664 chars** — pure announcement prose ("I'll audit the vouchers module... Let me start by searching..."), containing **zero factual claims** at the point of truncation.
3. **10:41:58–10:43:49** — The resulting repetition-completion pass generates a **wholly fabricated** continuation from scratch, not grounded in the Researcher's own already-gathered evidence at all: `POST /vouchers/create`, `DELETE /vouchers/{id}`, a `VoucherRedemptions` table, `useVoucher`/`useVoucherRedemptions` hooks, a fictional `POST /vouchers/validate` endpoint, and a fictional "Voucher Expiry Management" feature.
4. **10:43:49–10:43:57** — Phase 0.35's `new-claim-check` gate correctly fires: `verify_claims` reports "3 claim(s) could NOT be found," and the completion's fabricated content is rejected — "keeping the pre-repeat draft, disclosed as incomplete." **The "pre-repeat draft" here is the 664-char announcement stub from step 2 — it contains no facts at all.**
5. **10:44:03–10:44:05** — A module-hook/"two-sided comparison" mechanism recognizes the near-empty stub despite 36 real declarations being in hand, and renders the raw read lines, bringing content to 3,238 chars.
6. **10:44:28–10:53:35 (547.9s)** — `_attempt_coverage_correction` runs. Its traversal finds `inventoryApi.ts` — a real dependency the first draft never examined — via a "blind category search." `missing_cats` is **empty** at this point (every category had a file read shaped like it). Despite that, because new evidence (`resolved`) was found, the function dispatches **one retry**, explicitly prompting: *"Before concluding any gap/absence... Reconcile your conclusion against this evidence."*
7. **Inside that single retry (10:45:15–10:49:07):** a **second, full** Researcher delegation runs (the read-budget counter is reset to 0 first). It genuinely re-reads all 3 real files from scratch and produces a draft with the correct 9 endpoints, correct 3 tables, but **imprecisely-named** hooks (`getVouchers`/`getVoucher`/etc. instead of the real `useGet...Query`/`use...Mutation` symbols) and a **false conclusion**: "the frontend hooks provide full CRUD operations for the vouchers." This draft is recorded via `forward_member_answer` at **10:49:10**.
8. **10:49:10–10:50:59 (~110s stall)** — the Coordinator does nothing for over a minute (`WORKFLOW_STALLED`).
9. **10:50:59** — the Coordinator correctly delegates a **comparison-discipline Reviewer check** on that exact draft: *"Re-derive both enumerated lists from the answer below and check the conclusion against them one item at a time. Flag any item that should be marked as a gap but is missing from the summary..."* — the system correctly recognized this conclusion needed independent verification.
10. **10:51:29–10:53:05** — the Reviewer genuinely re-reads all 3 real files again (fully grounded tool calls, confirmed) and begins re-deriving the real endpoint list to compare against the summary.
11. **10:53:05–10:53:35** — the Reviewer's **own text generation** enters a repetition loop while **re-stating** the endpoint list it had just re-derived — verbatim repeats of "GET /vouchers... PUT .../post... POST .../grn..." — and is truncated by the repetition detector **before it ever states a single word of comparison or names a single gap**.
12. **10:53:35** — "coverage-correction retry produced nothing usable" fires (the retry's own `_complete_repetition_truncated_answer` recovery attempt also failed to rescue it, leaving the `_INCOMPLETE_ANSWER_MARKER`). The function correctly discards this unusable draft and returns `content + disclosure`, where `content` is the pre-retry 3,238-char stub and `disclosure = _mandatory_coverage_disclosure(missing_cats=[], ...)` — which returns **`""`**, since no file-level category was ever found missing.
13. **10:53:35 and again 10:53:37** — `_with_forwarded_evidence` (called inside the guard chain, and again as a documented "idempotent final re-application," see below) detects that `content` does not carry the Researcher's `forward_member_answer`-recorded draft and **appends it verbatim** — logged both times as *"forwarded answer(s) not carried by the final answer -- appending verbatim: ['researcher']"*. This is the mechanism that actually ships the imprecise hook names and the false "full CRUD" claim.

**Answering B1's specific questions directly:**
- Was the Reviewer actually invoked? **Yes.**
- Did it complete successfully? **No** — its generation was truncated before producing any conclusion.
- What findings did it return? **None.** It never reached the comparison/gap-naming step.
- Did its (non-existent) findings identify the four missing hooks? **N/A — no findings were ever produced.**
- Were findings recorded anywhere? **N/A.**
- Did the Coordinator receive a complete Reviewer response? **No** — the retry result was itself incomplete.
- Were findings in the coverage-correction input? **N/A — nothing to carry forward.**
- Did correction receive findings but fail to incorporate them? **No** — there were no findings to incorporate; the correction attempt itself never completed.
- Were findings lost through context truncation, state replacement, or prompt reconstruction? **No — nothing was lost. Nothing was ever produced to lose.**
- When repetition detection intervened, what draft was retained? The Reviewer's own cut-off, conclusion-less re-derivation was discarded entirely; the function fell back to `content`, the pre-retry stub.
- Was the pre-review draft restored, adopted, or left unchanged? **Restored unchanged**, by the existing "nothing usable" fallback.
- Why did the false full-CRUD conclusion ship? Because `_with_forwarded_evidence`'s loss-prevention append (a *separate* mechanism from the coverage-correction fallback) reattached the Researcher's earlier, never-reconciled draft verbatim, and that append is — by its own documented design — never re-verified.

**B2 — `answer_retention=0.017`, traced to its actual implementation (`_evidence_fidelity_report`, `swarm/team.py` ~line 27978):**
- Compares the salient-token set of **every durable `EvidenceRecord`** this run captured (every real tool-call result, across *both* Researcher delegations *and* the Reviewer's reads) against the salient-token set of `content` **as it stood at the diagnostic call site** — which is *before* the final idempotent `_with_forwarded_evidence` re-application (confirmed by reading the call-site ordering directly: the diagnostic call is at line ~31508, the final re-application at line ~31546).
- It measures pure **textual/shape-based token overlap**, nothing semantic.
- It **does** include tokens from the Reviewer's tool calls, but has **no concept of "finding"** at all — a Reviewer that read three files and produced zero prose contributes evidence tokens with no distinction from any other agent's reads.
- It does **not** distinguish material findings from ordinary prose.
- A low value triggers **no action whatsoever** — the function's own docstring states it is "Deliberately NOT wired into the live answer-generation pipeline," is "log-only, proven unsafe to gate on directly," and is "wrapped in its own try/except so a diagnostic can never affect the answer."
- **`0.017` is pure telemetry, not an enforcement signal, and is not the mechanism responsible for this incident.** It correlates with the incident (the final content really was mostly disconnected from the evidence gathered) but does not explain *why* — that is the `_attempt_coverage_correction`/`_with_forwarded_evidence` interaction documented above.

**B3 — failure category:**
- **Primary cause:** the Reviewer's delegation genuinely began grounded, correct work, but its own text generation stalled in a repetition loop **before producing any finding at all.** This is not "a finding was lost" — no finding was ever generated.
- **Contributing cause #1:** `_attempt_coverage_correction`'s "retry produced nothing usable" fallback discloses only file-level category coverage (`_mandatory_coverage_disclosure`) — which was already complete — and had no mechanism to disclose that a reconciliation *specifically targeting a gap/absence conclusion* was dispatched and failed.
- **Contributing cause #2:** `_with_forwarded_evidence`, a separate, correctness-blind-by-design loss-prevention mechanism, is what actually re-attached the stale, never-reconciled claim to the shipped answer, with no re-verification.

## Section 3 — Root Cause

All of Section 2 above is a **proven fact**, read directly from the live journal for the exact incident run (PID `2646213`, run `c7223a84b865`) and cross-referenced against the exact source lines of `_attempt_coverage_correction`, `_mandatory_coverage_disclosure`, `_with_forwarded_evidence`, and `_evidence_fidelity_report`. No part of this reconstruction rests on inference or assumption about what "must have" happened — every transition is a specific, timestamped, named journal event or a specific source-code branch.

The root cause is **not** "a Reviewer's finding was silently discarded during correction." It is: **when a coverage-correction reconciliation retry is dispatched because new evidence was found, and that retry fails to produce any usable result, the system discloses only file-category coverage status — never the fact that the specific conclusion the retry was dispatched to check remains unreconciled — so a stale, never-verified conclusion can still ship via the separate, correctness-blind `_with_forwarded_evidence` append.**

## Section 4 — Implementation

**Exact change** (`swarm/team.py`, 40 insertions / 1 deletion):

1. A new constant, `_COVERAGE_RECONCILIATION_UNRESOLVED_NOTE`, placed immediately after `_mandatory_coverage_disclosure` (its sibling/companion disclosure):
   ```
   "\n\n---\n**A CONCLUSION ABOVE WAS FLAGGED FOR RECONCILIATION, NOT "
   "CONFIRMED — this run found evidence it had not previously examined and "
   "asked itself to reconcile any gap/absence/completeness conclusion "
   "against it before finalizing. That reconciliation attempt did not "
   "produce a usable result (see this run's own log). Any claim above that "
   "something is fully covered, complete, or has no gap should be treated "
   "as UNVERIFIED, not confirmed, until it is actually checked against the "
   "evidence this run already has in hand.**"
   ```
2. One line changed inside `_attempt_coverage_correction`'s existing "retry produced nothing usable" branch:
   ```diff
   -        return content + disclosure
   +        return content + disclosure + _COVERAGE_RECONCILIATION_UNRESOLVED_NOTE
   ```

**Why this is the narrowest safe fix.** It reuses the *exact same* append-a-disclosure pattern this same function already uses for its sibling failure mode (`_mandatory_coverage_disclosure`, used when traversal finds *no* new evidence) — no new framework, no parallel finding-tracker, no new state. It fires on exactly one branch: a reconciliation retry was dispatched (new evidence existed) **and** that retry produced nothing usable. It never fires on the `not resolved` early return (no retry is ever dispatched there — nothing to disclose), never on a successful retry, and never on the retry's separate `verify_claims`-fabrication-rejection path a few lines below (which already has its own, different, already-correct handling via `_new_flagged`/`_salvage_resolved_category_section`).

**Existing mechanisms reused:** the function's own pre-existing "append a disclosure on this exit path" pattern.

**Safeguards deliberately preserved (all confirmed untouched):** repetition detection, thresholds, retry budgets, watchdogs, timeouts, and liveness mechanisms; `compare_enumerations` and T13b's tool grants; Phase 0.35's completion-adoption gate and final-draft verification invariant; Phase 0.36's citation pairing, entity attribution, and bold-symbol extraction; the `51e6398` sibling-list hotfix; Phase 0.37's P0 evidence-scoped route verification and P1's full-topic-phrase discovery recipe. None of these files or functions were opened for editing this phase.

**Known limitation, deliberately not fixed:** a few lines above the fixed branch, the retry's `except Exception as exc:` path has the exact same `return content + disclosure` shape and is **not** covered by this fix. It was deliberately left out: the demonstrated incident exercised only the "retry produced nothing usable" shape (a repetition-loop truncation), never the exception shape, and the mission explicitly instructs against manufacturing a fix beyond what the reconstructed evidence demonstrates.

## Section 5 — Controlled Validation

No unit tests were added, modified, or run; the unit-test suite was not run. Validation used a scratch harness (`test_p38_coverage_reconciliation.py`) that imports `swarm.team` directly and calls the **real, unmodified-elsewhere** `_attempt_coverage_correction`, monkey-patching only the three pieces that require a live model (`_comparison_coverage_gap`, `_stream_team_run`, `_complete_repetition_truncated_answer`) — the function's own control flow, disclosure logic, and string concatenation all run for real.

| Scenario | Setup | Result |
|---|---|---|
| **D2** (live-incident shape) | `resolved` non-empty (reconciliation dispatched), retry text carries `_INCOMPLETE_ANSWER_MARKER` | New note **present**, original content preserved, no spurious category disclosure — **PASS** |
| **D3** | Same, but retry text is completely empty | Same correct behavior — **PASS** |
| **D6** (regression) | `resolved` non-empty, retry **succeeds** with a clean, reconciled draft | New note correctly **absent**; reconciled content adopted normally, unaffected by the change — **PASS** |
| **D4** (regression) | `resolved` empty (the `not resolved` early-return path) | New note correctly **absent**; the pre-existing category disclosure still fires exactly as before — **PASS** |
| D1 (success path) | — | Untouched code path; confirmed by direct code reading, not separately scripted |
| D5 (partial salvage, multiple findings) | — | Handled entirely by the pre-existing, untouched `_salvage_resolved_category_section`; this phase's change only affects the *total*-failure fallback, never the partial-salvage path |
| D7 (cross-run isolation) | — | Trivially satisfied: the change is a pure local return-value computation with zero persisted state, no caching, nothing written to disk or a shared store |
| D8 (existing safeguards) | — | Confirmed via `grep`: `_attempt_coverage_correction` has exactly one caller in the entire file, which receives a plain `str` — unaffected by the change in shape. Phase 0.35/0.36/`51e6398`/0.37 files were never opened this phase |

All four scripted scenarios pass.

## Section 6 — Live Validation

**E1 — primary T13a acceptance.** Exact mission prompt: *"Audit the vouchers module: enumerate its backend API endpoints, the database tables/models it owns, the frontend API hooks, and identify any backend endpoints that lack corresponding frontend hooks."* Run: PID `2812813`, run `daae806be03e`, 319.04s.

Ground truth independently re-verified against current source immediately before this run: 9 endpoints at `vouchers_api.py` lines 212/233/248/398/491/589/738/807/843; exactly 5 real hooks in `inventoryApi.ts` (`useGetVouchersQuery`/`useGetVoucherQuery`/`useCreateVoucherMutation`/`usePostVoucherMutation`/`useCancelVoucherMutation`, confirmed no `useDeleteVoucher`/`useUpdateVoucher` exist anywhere); 3 key tables (`VoucherSeries:326`/`Voucher:350`/`VoucherVersion:402`).

The Researcher's own generation was cut by repetition detection at 1016 chars. The resulting repetition-completion pass fabricated a **second, more severe** wave of invented content than even the original incident: a nonexistent `VoucherType`/`VoucherStatus` table at fabricated lines 51–60, and **six wholly invented** frontend hooks (`useVouchers`/`useVoucher`/`useCreateVoucher`/`usePostVoucher`/`useCancelVoucher`/`useCreateGRNFromPO`) at fabricated line numbers, concluding "all backend endpoints have corresponding frontend hooks... ensuring full coverage" — the same false-full-coverage *shape* as the original incident, but this time built on entirely invented symbol names rather than merely-imprecise naming of real ones.

**This was caught.** Phase 0.35's `new-claim-check` gate correctly rejected the fabricated continuation (`verify_claims` flagged it; content dropped from 1,542 to 1,196 chars), falling back to the genuinely-grounded partial draft: the first 6 real endpoints, correctly named with correct line ranges, truncated before reaching endpoints 7–9, tables, hooks, or any gap conclusion — with the standard incompleteness disclosure correctly attached.

**Explicit statement of whether the new mechanism fired: it did not.** Confirmed via journal: `[guard-trace] _attempt_coverage_correction run=daae806be03e ENTER` / `EXIT after 0.0s outcome=skipped out_chars=1,196` — the function never dispatched a reconciliation retry at all, because the content it received (after Phase 0.35's gate had already stripped the fabricated tables/hooks/conclusion) contained nothing to reconcile in the first place. **A different, earlier existing safeguard caught the problem before the task ever reached this phase's own fix's trigger condition.**

**E2 — T13b positive control.** Same prompt with explicit file paths named. Run: PID `2822961`, run `559a9220c5bb`, 246.43s. The Researcher's generation was cut by repetition detection at 1652 chars; the one-shot completion attempt did not produce a more grounded result, and the content shipped with the generic `**INCOMPLETE ANSWER —` marker. The content shown (the same 6 real endpoints, with full, accurate function signatures) is correct, but the run never reached the tables, hooks, or gap-analysis stage either. Journal confirms: `_attempt_coverage_correction` ENTER/EXIT in 0.0s, `outcome=skipped` — same as E1. `compare_enumerations` was never invoked (absent from the entire journal for this PID) because the run never reached the comparison stage.

**Per the mission's own explicit Section E3 instruction, neither run was repeated.** Both outcomes are reported exactly as observed.

## Section 7 — Regression Assessment

- **Phase 0.35's completion-adoption gate:** confirmed **still firing correctly**, and in E1's case catching an even *more* severe fabrication (wholly-invented hooks/table, not just a wrong HTTP verb) than any prior incident — a positive, if incidental, finding.
- **Phase 0.35's final-draft verification invariant, Phase 0.36 (citation pairing/entity attribution/bold-symbol extraction), the `51e6398` hotfix, Phase 0.37 P0/P1:** no file in any of these areas was touched this phase; no evidence of regression surfaced in either live run, though — matching the same honest caveat Phase 0.37's own report carried — neither live run this phase independently re-exercised most of this machinery either.
- **T13b / `compare_enumerations`:** could not be assessed this phase — E2 never reached the point where `compare_enumerations` would be selected, due to an early repetition stall unrelated to this phase's change.

## Section 8 — Limitations and Deferred Issues

- **Confirmed defects:** none outstanding — the one demonstrated defect (the undisclosed reconciliation-retry failure) was fixed.
- **Controlled-only findings:** the fix's own correctness is demonstrated only in the scratch harness against the real production function with the model-dependent pieces monkey-patched — **not yet directly observed live**, since neither E1 nor E2 reached the trigger condition (a reconciliation retry dispatched and then failing).
- **Live-unobserved behavior:** the exact live trigger for this phase's new code — `_attempt_coverage_correction` dispatching a reconciliation retry that subsequently fails to produce anything usable — did not recur naturally in either live run this session. This is explicitly **not** labeled as live-validated, per the mission's own instruction not to claim confirmation of an unexercised mechanism.
- **Known gap, not fixed this phase:** the retry's `except Exception` path (same `return content + disclosure` shape, a few lines above the fixed branch) remains undisclosed in the same way if it is ever the actual failure mode — deliberately deferred, since no evidence this phase demonstrated that shape specifically.
- **Model-level variance observed, unrelated to this phase's change:** both E1 and E2 hit repetition stalls substantially earlier in their own generation than the original incident did (664–1016–1652 chars vs. further along), preventing either run from reaching the comparison/gap stage at all. This is consistent with ordinary run-to-run model variance already documented elsewhere in this project's history, not a new or worsened condition caused by this phase's change (which touches nothing upstream of where these stalls occurred).
- **Speculative concerns:** none added without evidence this phase.

## Section 9 — Verdicts

- **Investigation: PASS.** The full event chronology, the exact point of non-reconciliation, the precise responsible code path, and the exact explanation of `answer_retention=0.017` are all established directly from journal and source evidence — no part rests on an unverified hypothesis.
- **Implementation: PASS.** The fix is narrow, directly justified by the reconstructed evidence, reuses an existing disclosure pattern rather than inventing new machinery, introduces no overlapping protections, and leaves every other guard/mechanism in this file untouched.
- **Controlled validation: PASS.** All four scripted scenarios pass against the real, unmodified-elsewhere production function.
- **Live validation: PARTIAL.** Both E1 and E2 are deployed, healthy, and produced safe, accurate-as-far-as-they-went, honestly-disclosed-incomplete answers — but neither naturally exercised this phase's new code path (`_attempt_coverage_correction`'s reconciliation-retry-failure branch), so the new disclosure's live behavior remains unconfirmed. Per the mission's own explicit rule, a correct final answer and a healthy deployment are not sufficient to claim live confirmation of an unexercised mechanism.
- **Overall Phase 0.38: PARTIAL.** Applying "do not round up": the investigation and fix are sound, narrowly scoped, and controlled-validated, but direct live confirmation of the new mechanism actually firing was not achieved this session.
