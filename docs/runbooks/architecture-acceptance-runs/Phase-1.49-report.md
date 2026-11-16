# Phase 1.49 — Claim Attribution and Scope Precision

## Summary

Fixed the three P3 accuracy defects identified in the post-Phase-1.48 battery
(`T1-T13-POST-PHASE-1.48-BATTERY.md`): T3's function-to-file misattribution, T4's
`default=`/`server_default=` conflation, and T13b's table-ownership-vs-reference
scope gap. All three fixes are narrow, diagnostic-only, and leave Phase
1.46/1.47/1.48 behavior untouched. A fourth, smaller defect was found and fixed
**in this phase's own new code** during the full-battery validation run (see
Section 5) before the phase was considered complete.

## 1. Baseline and deployed SHAs

| | SHA | Note |
|---|---|---|
| Baseline before this phase | `e3d2db2` | Phase 1.48, confirmed deployed on ZGX at mission start |
| First Phase 1.49 commit | `74d0756` | The three named fixes |
| Follow-up fix (this same phase) | `84362cb` | **Final deployed state** — corrects a bug in `74d0756`'s own new wiring, found during this phase's own validation |

Both commits pushed to `origin/main` and fast-forwarded onto ZGX's `~/agno-hive`
checkout (no merge conflicts, no force-push). `agno-api.service` restarted after
each deploy (runtime code, not just docs); both restarts confirmed via a changed
`MainPID`/`ActiveEnterTimestamp` and a clean `/health` (`200 ok`) + `/runs` (`[]`)
immediately after.

## 2. Changed files

- `swarm/team.py` — three new functions (`_misattributed_function_citation`,
  `_server_default_conflation`, `_column_decl_span`), a new per-path symbol index
  (`ev["path_symbols"]`, populated in `_record_call_in_evidence`), two new regex
  constants (`_FUNC_FILE_CITATION_RE`, `_PLAIN_DEFAULT_CLAIM_RE`, `_DEF_DECL_RE`),
  and a one-line scoping fix to `_module_table_ownership_scope`'s existing
  bail-out check. Both new checks wired into `_assess_member_result`.
- `swarm/eng_workflow.py` — `_module_table_ownership_scope()` called from
  `run_attempt()`'s existing `att.terminal[...]` stash pattern (same convention as
  `capability_gap`, Phase 1.42 Track C) and rendered in `build_final()`. One
  follow-up edit (commit `84362cb`) changes which task text is passed to this
  call.

No other files changed. No tests added or run (scratch harness only, per
constraint). `.hive_pending_actions/` confirmed empty before and after every run
in this phase; EkamApp's and agno-hive's own pre-existing dirty/untracked working
trees (32 entries on ZGX, same count before and after) were never touched.

## 3. Exact source-level fixes

### 3.1 T3 — function-to-file attribution

**Cause, confirmed by inspection first:** nothing in the existing acceptance
pipeline checked WHICH file a claimed function was attributed to — only whether
the function name existed somewhere in evidence at all (`_claim_support`). T3's
original defect (`get_seller_status` attributed to `business_api.py`, when the
real function there is `get_seller_status_by_user_id`, and `get_seller_status` is
a distinct, real function in `business_service_client.py`) passed every existing
check because the symbol genuinely exists — just not where claimed.

**Fix:** `ev["path_symbols"]: dict[str, set[str]]`, a normalized-path → declared-
function-names index, populated in `_record_call_in_evidence` for single-path
`.py` reads only (via the new `_DEF_DECL_RE` applied after stripping
`get_file_content`'s own line-number prefix). `_misattributed_function_citation(text, ev)`
matches the citation shape `` the `X` function in `Y.py` `` and flags it only
when `Y` was genuinely read AND `X` is a real symbol seen declared in some OTHER
read file — never when `Y` was never read (a different, already-covered gap) or
`X` was never seen anywhere (plain fabrication, also already covered).

### 3.2 T4 — `default=` vs `server_default=`

**Cause, confirmed by inspection first:** same gap as above — no check compared
the attempt's own *wording* of a field's default against the real SQLAlchemy
keyword actually used in the declaration it read.

**Fix:** `_server_default_conflation(text, ev)` scans the attempt's own merged
read corpus (`ev["corpus"]`, already populated from every successful read) for
the claimed field's own `<field> = Column(...)` declaration, using a new
balanced-paren scanner (`_column_decl_span`) — not a flat fixed-width window.
**The flat-window version was wrong and was caught by the scratch harness before
ever reaching the real codebase**: with two short field declarations close
together, a fixed 300-char window bled past the first field's own closing paren
into the second field's own (genuine) `default=`, producing a false negative.
Fixed before commit by switching to depth-tracked paren matching, which correctly
isolates exactly one field's own declaration regardless of length or line count.

### 3.3 T13b — table ownership vs. reference

**Cause, confirmed by inspection first — two independent causes, not one:**

1. `_module_table_ownership_scope()` (an existing, already-correct Phase 0.28
   guard) had a bail-out — `if re.search(r"\.(?:py|ts|tsx)\b", task or ""):
   ...return ""` — scoped to the **whole task string**. T13b's own prompt names
   three files as reading hints *after* the real "the vouchers module[']s ...
   database tables" question (`_MODULE_TABLE_ASK_RE`'s own match, which cannot
   cross a sentence boundary by construction). The old bail-out scanned past that
   boundary and incorrectly went silent. Fixed: scoped to `task[:m.end()]`
   instead — the matched ask's own span, never a later sentence.
2. **This guard was Team-engine-only code — never called anywhere in
   `eng_workflow.py` at all.** The post-1.48 battery ran on the Workflow engine
   (the default when `read_only=True` and no `orchestration` is given), so the
   guard never had a chance to fire for T13a/T13b regardless of bug #1. Fixed by
   wiring a new call site into `run_attempt()`'s existing `finally` block
   (parallel to the existing `capability_gap` pattern) and a new render block in
   `build_final()`.

## 4. Validation evidence

### 4.1 Scratch harness (not unit tests, per constraint)

`p149_precision_scratch.py` — 20/20 checks against the real functions (not
reimplementations), including the balanced-paren bug found and fixed mid-session,
and the recovery-task regression found and fixed in Section 5 below.

### 4.2 Targeted T3/T4/T13b re-run (exact unmodified prompts, commit `74d0756`)

| Test | Result | Finding |
|---|---|---|
| T3 | ACCEPTED_COMPLETE, 270s | Correctly attributes `get_seller_status` to `business_service_client.py` — verified against the real file. The live defect did not recur this run (model phrasing variance), so there was nothing for the new guard to catch; the fix's correctness rests on the scratch harness's direct reproduction of the exact defect text. |
| T4 | ACCEPTED_COMPLETE, 154s | Made no `default=` claims this run (different phrasing) — same "nothing to catch" situation as T3. Field/line-range claims independently verified accurate. |
| T13b | ACCEPTED_COMPLETE, 1320s (two generations) | **The TABLE OWNERSHIP SCOPE CHECK fired live, correctly, for the first time** — distinguishing `Voucher, VoucherSeries, VoucherVersion` (owned) from `StockLedger`/`StockLevel` (referenced), matching the mission's own stated ground truth exactly. |

### 4.3 Full T1–T13b battery (exact unmodified prompts, commit `74d0756`, before the Section 5 follow-up fix)

All 14 tests run sequentially, `engineering`, `read_only=True`, fresh session
each. Service never restarted mid-battery (confirmed same PID throughout).
`.hive_pending_actions/` confirmed empty before and after.

| # | Result | Duration | Note |
|---|---|---:|---|
| T1 | ACCEPTED_COMPLETE | 150s | `models.py:116`/`:129` exact (re-confirmed) |
| T2 | ACCEPTED_COMPLETE | 309s | Same 13-endpoint/16-hook/6-gap enumeration as the prior battery, consistent |
| T3 | ACCEPTED_COMPLETE | 259s | `get_seller_status` correctly attributed to `business_service_client.py` again |
| T4 | ACCEPTED_COMPLETE | 189s | **Every field uses `server_default=` correctly this run** — matches real declarations exactly |
| T5 | ACCEPTED_COMPLETE | 335s | Zero staged writes confirmed |
| T6 | ACCEPTED_COMPLETE | 168s | 24/24 files |
| T7 | ACCEPTED_COMPLETE | 123s | Correctly reports 6 files |
| T8 | ACCEPTED_COMPLETE | 187s | Real query, 0 rows |
| T9 | ACCEPTED_COMPLETE | 265s | All 3 fields correct, project root kept distinct from cwd |
| T10 | ACCEPTED_COMPLETE | 118s | Exact citations again |
| T11 | **ACCEPTED_PARTIAL** | 488s | Completed naturally (no supervisor kill), but honestly self-disclosed two missing R5 link-citations (`register_documents`, `verify_seller`). **Counted as partial, not pass, per the mission's explicit scoring rule.** |
| T12 | ACCEPTED_COMPLETE | 346s | Full real router/model enumeration, improved from the prior battery's self-scored partial |
| T13a | ACCEPTED_COMPLETE | 485s | Table-ownership note fired correctly (lowercase table-name variant this run). **Separate finding, out of this phase's scope**: the final "Analysis" section claims "all backend endpoints have corresponding frontend hooks" despite only ever naming 5 of 9 — a wrong gap-analysis conclusion built on correct enumeration (the long-standing "reasoning over correct evidence" gap this file has named before, not new, not caused by this phase) |
| T13b | ACCEPTED_COMPLETE | 836s | **Table-ownership note MISSING on the accepted (recovery, generation-1) attempt** — see Section 5 |

**Per the mission's scoring rule (count `ACCEPTED_PARTIAL` as partial, not pass,
never infer causation from correlation):** 12 pass, 2 partial-by-this-rubric
(T11 self-disclosed; T13a's wrong final conclusion, scored partial-accuracy here
despite its own `ACCEPTED_COMPLETE` system verdict, because the substantive
"gap" claim is factually wrong), 0 fail. **T13b's own fix was incomplete at this
point in the phase** — see Section 5.

### 4.4 Section 5 fix + re-validation

See below. Confirmed working via a standalone T13b re-run after the follow-up
deploy: the TABLE OWNERSHIP SCOPE CHECK note now correctly appears on the
accepted (recovery) generation, with the same correct direct/indirect
classification as before (`voucher_series, voucher_versions, vouchers` owned;
every other real table, including `stock_ledger`/`stock_levels`, correctly
excluded).

## 5. A defect found in this phase's own new code, fixed before completion

The full-battery run (Section 4.3) showed T13b's table-ownership note present on
the ORIGINAL (generation-0) attempt but **missing on the RECOVERY (generation-1)
attempt** — the one that actually got `ACCEPTED_COMPLETE` and therefore the one
`build_final()` renders. Traced directly (not guessed): `run_attempt()`'s new
call site passed `task_override if task_override is not None else wi.objective`
to the guard. For a recovery attempt, `task_override` is `build_recovery_task()`'s
own generated text, which embeds the ORIGINAL attempt's own prose verbatim as
"ESTABLISHED FINDINGS" — and that prose, this run, happened to say "...especially
those **related to** vouchers," spuriously matching
`_REQUESTS_WORKFLOW_RELATED_RE`'s `\brelated\s+to\b` and making the guard
wrongly conclude the TASK ITSELF had asked for a broader relationship scope.

This is why the earlier targeted run (Section 4.2) did NOT show this bug: that
run's own generation-0 text happened to say "voucher-related" (a hyphenated
compound, not "related to") instead — a pure, confirmed case of model-wording
variance exposing a latent bug non-deterministically.

**Fix (commit `84362cb`):** the call site now always passes `wi.objective` —
the stable, original question, identical across every generation of a work item
and never containing an echoed prior-attempt quote — never `task_override`.
Confirmed in the scratch harness (new checks T17a/T17b) and live: redeployed,
re-ran T13b standalone once more (618s, two generations again), and the note now
correctly appears on the accepted generation with the right classification.

## 6. Deployment and restart details

| Step | Detail |
|---|---|
| Commit `74d0756` | Pushed to `origin/main`, fast-forwarded on ZGX, `agno-api.service` restarted (new PID `286421`, confirmed `/health`+`/runs` clean) |
| Commit `84362cb` | Pushed to `origin/main`, fast-forwarded on ZGX, `agno-api.service` restarted again (new PID `587093`, confirmed `/health`+`/runs` clean) |
| Dirty/untracked preservation | ZGX's pre-existing 32 dirty/untracked entries (stale `.env.bak*`, training artifacts, log files — none touched by this phase) unchanged in count before and after both restarts |

## 7. Remaining limitations (not in this phase's scope; flagged, not fixed)

1. **T13a's "all endpoints covered" false conclusion** (Section 4.3) — correct
   enumeration, correct table-ownership distinction, wrong final gap-analysis
   sentence. This is the long-standing "reasoning over correct evidence" gap this
   repository's own history has named repeatedly (2026-08-23, 2026-09-22
   entries); Phase 1.49 did not touch gap-analysis-conclusion checking and this
   defect is unrelated to the three named targets.
2. **`_misattributed_function_citation`/`_server_default_conflation` were never
   observed firing on a genuine live recurrence of their own named defect** in
   either live re-run this phase performed — both T3 and T4 happened to phrase
   their answers differently enough (correctly, as it turned out) that the
   original defect shape simply didn't recur. Their correctness rests on the
   scratch harness's direct reproduction of the exact original defect text, not
   on a live catch. This is an inherent limitation of validating a precision fix
   against a non-deterministic model's live output, not a gap in the fix itself.
3. **`_misattributed_function_citation` is scoped to single-path `.py` reads
   only** — `.ts`/`.tsx` export forms use a different grammar and were
   deliberately left out of this phase's scope (matches the T3 defect's own
   shape, which was Python-only).
4. **The Phase 1.48 report's own two flagged, deliberately-unfixed findings
   remain open**: `requirement_source_paths()` never resolves `policy.links`-
   derived obligations (affecting the JIT extension mechanism's ability to match
   R5-type obligations — directly visible again in this battery's own T11
   result), and per-member tool-surface counts remain log-only. Neither is in
   this phase's scope.
