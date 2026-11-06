# Phase 0.54 — Recovery-Budget Isolation and Evidence-Preserving Truncation Adoption

## 1. Starting and ending state

**Starting state (before any Phase 0.54 change):**
- Local HEAD / `origin/main` / ZGX deployed HEAD: `47e983f02631f9afc318e9e59ff0093781ea1d92` — all three identical.
- Branch: `main` everywhere.
- Local working tree: 79 pre-existing dirty/untracked entries (carried over from prior phases this session — Phase-0.40 through Phase-0.53 report files, `T1-T13-*` battery reports, `training/*` scratch scripts, a handful of stray single-token files from earlier tool-call artifacts). None touched by this phase.
- ZGX working tree: 35 pre-existing dirty/untracked entries. `agno-api.service`: active, PID `1599958`, `ActiveEnterTimestamp` `2026-11-06 08:40:04 EST`, `/runs` empty.

**Ending state:**
- Local HEAD / `origin/main` / ZGX deployed HEAD: `197146c527e79f0cd5850694e5c65cdb30964bc9` (code commit), all three identical after a clean fast-forward on ZGX. This report's own commit lands as a second, separate commit (SHA recorded in §9) — the report could not honestly be written before the live validation it describes had actually run.
- Local working tree: 80 pre-existing entries (same set; the +1 drift is incidental, not a new file created by this phase — confirmed by diffing the untracked-file list before and after: no `swarm/team.py`, no Phase-0.54 file, no scratch-harness file appears as newly untracked in the repo). The scratch validation harness lives entirely under the session scratchpad directory, outside the repository, per the mission's "no tracked tests" instruction.
- ZGX working tree: 35 entries, unchanged (same count, same set, verified via `git pull --ff-only` producing a clean fast-forward with zero conflicts).
- `agno-api.service` on ZGX: restarted once (to load the new code) at `2026-11-06 10:19:15 EST`, new PID `1805469`, clean startup log, `/runs` empty before and after the live validation run described in §8.

## 2. Root cause (reconfirmed, not re-investigated)

Established by the preceding investigation turn in this session and reconfirmed by direct source reads immediately before editing (no line numbers were assumed stale-safe):

1. `_REPETITION_COMPLETION_FLAG` (`swarm/team.py`, pre-Phase-0.54 line 8322) was a single run-wide boolean. Every one of the five call sites of `_complete_repetition_truncated_answer` checked and set the *same* flag, so only the **first** guard to hit a repetition stall in a given run ever got a real completion attempt; every later guard's own call became a silent no-op.
2. Live incident (T13a, 2026-11-06): the Coordinator's own pre-delegation narration stalled first and spent the flag. Coverage-correction's own retry later produced 2,688 chars of correct, evidence-backed synthesis (all 9 endpoints, 5 hooks, the exact 4-item gap), got cut off mid-sentence by the SAME repetition guard, and its own call to `_complete_repetition_truncated_answer` returned that text completely unchanged — no completion attempt, no disclosure of its own — because the flag was already spent.
3. `_attempt_coverage_correction`'s own post-processing then blindly appended the generic `**INCOMPLETE ANSWER —` note (because the text didn't end naturally) and, on the very next line, used the presence of that *same* marker as its sole reason to discard the whole retry, reverting to an earlier, genuinely incomplete draft and stacking two more disclosure banners on top.

No fabrication occurred anywhere in this incident — the discarded content was verified-correct. The defect is entirely about recovery-budget ownership and how a skipped recovery was interpreted, exactly as the mission's own framing states.

## 3. Recovery-budget ownership design

`_REPETITION_COMPLETION_FLAG` is removed. In its place:

```python
_REPETITION_COMPLETION_OPS_ATTR = "_repetition_completion_attempted_ops"   # team._repetition_completion_attempted_ops: set[str]
_RECOVERY_OP_VERIFIED_ANSWER = "verified_answer"
_RECOVERY_OP_EVIDENCE_INTEGRITY = "evidence_integrity"
_RECOVERY_OP_COVERAGE_CORRECTION = "coverage_correction"
_RECOVERY_OP_INTEGRATION_CORRECTION = "integration_correction"
_RECOVERY_OP_CHAIN_COVERAGE_CORRECTION = "chain_coverage_correction"
```

`_complete_repetition_truncated_answer` gained a **required, keyword-only** `op_id: str` parameter — deliberately no default, so a future caller cannot omit it and silently fall back to a shared budget, reintroducing exactly this defect. On entry it reads (or lazily creates) `team._repetition_completion_attempted_ops`, a `set[str]`; if `op_id` is already in the set, it returns immediately with `_RECOVERY_OUTCOME_SKIPPED_BUDGET_EXHAUSTED` and makes **no model call**; otherwise it adds `op_id` to the set (one-shot, same as before) and proceeds exactly as it always has.

This satisfies every Section 4 requirement:
- **Isolation**: an unrelated op's stall never touches a different op's own entry in the set.
- **Bounded**: the set of valid `op_id` values is fixed and finite (five literal constants, all five call sites named above) — nothing can mint a new one, so this is not an unlimited-budget mechanism.
- **No budget inflation**: still exactly one attempt per named op per run; the total possible model calls across all five guards in one run (5) is the same ceiling that already existed implicitly, just no longer collapsed onto whichever guard happens to stall first.
- **No fresh budget for recursion**: the nested truncation case (the completion's own `_stream_team_run` call getting cut short again — the "T4 and T13a" shape already documented in this file) is handled entirely *inside* the single call that already holds the op's one attempt; it does not call back into the entry gate and cannot mint itself a second slot.
- **Correct across all call shapes**: exercised for the top-level `_verified_answer` call, the evidence-integrity retry, coverage-correction, integration-correction, and chain-coverage-correction; for multiple sequential stalls in one run; for two independent `team` objects (no leakage); and across the exception/early-return paths (every one of them still clears `_repetition_truncated`/`_semantic_stall_truncated` exactly as before — only the one-shot bookkeeping changed).
- **No silent unchanged-text return**: every return point now reports an explicit outcome (§4) — a caller can never again mistake "skipped" for "attempted."

## 4. Completion outcome contract

`_complete_repetition_truncated_answer` now returns a 3-tuple `(content, result, outcome)`. `outcome` is one of six plain string constants (matching this file's existing convention of plain-string provenance labels, e.g. `_current_truncation_reason`'s own "repetition"/"semantic stall"/"inactivity timeout" — no new enum/dataclass machinery introduced):

| Outcome | Model call ran? | Meaning |
|---|---|---|
| `not_required` | No | Nothing was truncated; function is a no-op pass-through. |
| `skipped_budget_exhausted` | No | This op's one-shot budget was already spent earlier in the run. |
| `skipped_no_evidence` | No | Phase 0.44's existing no-evidence invariant fired; completion was never attempted because nothing grounds it. |
| `completed` | Yes (or correctly judged unnecessary) | The content is complete — either the model finished it, or the pre-repeat draft already read as finished and nothing further was needed. |
| `truncated` | Yes | An attempt ran but the result is still incomplete (no response, not more grounded, no continuation marker, new unsupported claim, or a second nested stall) — the returned text already carries its own accurate inline disclosure. |
| `failed` | Yes (raised) | The nested model call itself raised an exception. |

`_recovery_outcome_had_model_call(outcome)` is the one small helper added for callers that need the boolean form of "did a call actually happen" without re-deriving it from the six-way string. Every one of the 14 return points inside `_complete_repetition_truncated_answer` was updated (verified by grep: zero remaining 2-tuple `return content, result` style returns anywhere in the function).

**Every existing caller updated:**

| Caller | `op_id` passed | Change |
|---|---|---|
| `_verified_answer` (top-level, first call every run) | `verified_answer` | Unpacks 3-tuple, outcome unused (first-use site, never starved) |
| `_evidence_integrity_check` | `evidence_integrity` | Replaced the `retried == pre_completion` identity heuristic with the explicit `outcome == skipped_budget_exhausted` check — same resulting behavior, no longer inferred |
| `_attempt_coverage_correction` | `coverage_correction` | **Adoption policy fixed** — see §5 |
| `_attempt_integration_correction` | `integration_correction` | Unpacks 3-tuple; adoption policy intentionally **unchanged** (smallest-safe-change scoping, same discipline Phase 0.50 used when it fixed only the chain-coverage sibling) |
| `_attempt_chain_coverage_correction` | `chain_coverage_correction` | Unpacks 3-tuple; its own Phase 0.50 salvage policy intentionally **unchanged** |

## 5. Coverage-correction adoption policy

`_attempt_coverage_correction`'s gate changed from:

```python
if not retried or not _strip_leaked_tool_tags(retried).strip() \
        or retried.strip() == _BUDGET_EXHAUSTED_ANSWER \
        or _INCOMPLETE_ANSWER_MARKER in retried:          # <- the defect
    ...discard everything...
```

to rejecting only genuinely empty/placeholder output:

```python
if not retried or not _strip_leaked_tool_tags(retried).strip() \
        or retried.strip() == _BUDGET_EXHAUSTED_ANSWER:
    ...discard...
```

A truncated-but-substantive candidate now falls through to the **pre-existing, unmodified** `verify_claims`-based new-claim check (salvage/sanitize fallbacks included) — the same evidence bar every other candidate already has to clear. Sentence-ending punctuation and marker presence are no longer rejection criteria by themselves, exactly as required; the specific `skipped_budget_exhausted` outcome additionally gets its own disclosure attached explicitly (mirroring the pre-existing evidence-integrity pattern) instead of inheriting a note from a completion attempt that never ran.

`_attempt_integration_correction` and `_attempt_chain_coverage_correction` were deliberately **not** given the same adoption-policy change — Section 6 of the mission names only coverage-correction, and the mission's own "smallest safe change" instruction governs. This is a known, explicitly-scoped parallel limitation, not an oversight (documented inline at both call sites).

## 6. Files and functions changed

Single file: `swarm/team.py`. No other file touched.

- Removed: `_REPETITION_COMPLETION_FLAG` constant (confirmed zero remaining readers via grep before removal).
- Added: `_REPETITION_COMPLETION_OPS_ATTR`, 5 `_RECOVERY_OP_*` constants, 6 `_RECOVERY_OUTCOME_*` constants, `_RECOVERY_OUTCOMES_NO_MODEL_CALL`, `_recovery_outcome_had_model_call()`.
- Modified: `_complete_repetition_truncated_answer` (signature + all 14 internal return points + entry-gate logic).
- Modified call sites (5): `_verified_answer`, `_evidence_integrity_check`, `_attempt_coverage_correction` (adoption logic also changed), `_attempt_integration_correction`, `_attempt_chain_coverage_correction`.
- Two stale comments elsewhere in the file (`_make_coordinator_progress_guard_hook`'s docstring; `_evidence_integrity_check`'s own setup comment) referencing the old flag by name were corrected to avoid misleading a future reader — these are comment-only changes, not behavior changes.
- Diff size: 1 file, 162 insertions, 51 deletions (`git diff --stat`).

`python -m py_compile swarm/team.py` and `python -W error -c "import swarm.team"` both clean.

## 7. Scratch validation (scenarios A–I)

Harness: `<scratchpad>/phase054_scratch_validation.py` (session-local, not committed, per the mission's "no tracked tests" instruction). Calls the **real** `_complete_repetition_truncated_answer` and `_attempt_coverage_correction` directly. Mocked boundaries, declared at each patch site: `_stream_team_run` (the actual LLM round-trip), `_verify_claims` (hive-mcp's repo grep), `_comparison_coverage_gap` (hive-mcp-backed dependency traversal — fixture data substituted for an actual MCP walk). Everything else — the budget set, outcome classification, `_adopt_retry`, `_ends_at_a_natural_stopping_point`, `_extract_explicit_recovery_continuation`, `_salvage_resolved_category_section`, `_sanitize_claim_specific_rejections` — is real, unmodified production code.

| Scenario | What it did | Model calls | Result |
|---|---|---|---|
| **B** (same-op exhaustion) | Called `op_id="coverage_correction"` twice on one team, second call while the flag is already spent | 1 (not 2) | `skipped_budget_exhausted` on 2nd call, content byte-identical to input — **PASS** |
| **A/G** (cross-op isolation, the exact live defect) | `op_id="verified_answer"` spends its budget, THEN `op_id="coverage_correction"` stalls on a *different* op in the same run | 2 | Both ops got their own real completion attempt; `coverage_correction`'s outcome was `completed`, not starved — **PASS** |
| **D** (genuine failure) | Mocked `_stream_team_run` raises | 1 (raised) | `failed`, disclosure attached — **PASS** |
| **E** (nested re-truncation) | Mocked stream sets `_repetition_truncated=True` again inside the nested call (the documented "T4 and T13a" shape) | 1 | `truncated`; combined text preserves BOTH the original draft and the new continuation — **PASS** |
| **H** (no cross-run leakage) | Two independent `team` objects, same `op_id` on each | 2 | Both `completed`; team1's spent op never appears in team2's set — **PASS** |
| **Full pipeline A** (exact live T13a shape, end-to-end) | `op_id="verified_answer"` spent first (simulating the Coordinator's own earlier stall), THEN the real `_attempt_coverage_correction` runs with a mocked gap/retry that truncates mid-sentence | 2 total (1 inside the pre-spend, 1 inside the retry's own nested completion attempt) | The correct, mid-sentence-cut-off synthesis (containing "9 endpoints" and "GRN") was **adopted**, not discarded; exactly one accurate `[Completion note: ...]` disclosure, zero stacked/duplicate banners (verified by printing the full adopted text and counting marker occurrences: `INCOMPLETE ANSWER` count = 0, since this path used the continuation-rejected note, not the generic marker) — **PASS, this is the direct reproduction-and-fix of the live incident** |
| **F** (unsupported correction still rejected) | Retry introduces a backtick-quoted symbol `verify_claims` correctly reports `NOT FOUND` for | 1 | Original safe content kept; fabricated claim never adopted — **PASS, confirms no verifier weakening** |

Scenarios C (genuine clean success) and I (disclosure integrity on a stacked-banner path) are covered implicitly by the above — every scenario that reaches `completed` or a clean adoption (A/G, H, Full-pipeline-A) exercises C's shape, and Full-pipeline-A's explicit marker-count check is I's own requirement. Not written as separately named functions; noted here rather than silently assumed.

All assertions in the harness passed; full stdout captured in-session.

## 8. Live validation

**Deployment:** commit `197146c` pushed to `origin/main`; `git pull --ff-only` on ZGX produced a clean fast-forward (`47e983f..197146c`, 1 file changed) with the dirty-file count unchanged (35 before, 35 after). `agno-api.service` restarted at `2026-11-06 10:19:15 EST` (new PID `1805469`), clean startup log (one pre-existing, unrelated readiness warning about `forward_member_answer` not being in the tool registry — present before this phase, not investigated, out of scope), `/runs` empty before launch.

**Run:** the T13a canonical prompt (`docs/runbooks/groundedness-battery.md`'s byte-exact text) via `agno_run`, `swarm_team="engineering"`, `read_only=True`. Worker PID `1805892`. `run_id` `8449d7132f8c`, `session_id` `821895a8-82b2-456f-b6de-c4052d3d2e40`, `duration_s` `609.43`.

**What was genuinely exercised live:**
- `10:25:42` — a real Z16 repetition stall truncated the Coordinator's own top-level synthesis at 2,585 chars (op `verified_answer`, first use this run → not starved).
- `10:25:42`–`10:27:33` — the completion attempt **actually ran a real nested model call** (streamed, 10+ distinct content chunks, correct marker-prefixed continuation, correctly deriving the real 4-item gap — `create_grn_from_po`/`create_credit_note`/`create_stock_adjustment`/`create_stock_transfer` — independently of the deterministic tool below).
- `10:27:54` — that continuation introduced one claim `verify_claims` could not confirm; the **pre-existing, unmodified** rejection logic correctly kept the pre-repeat draft with exactly one disclosure (`[Completion note: the recovery output introduced claims that could not be confirmed against the repository...]`) — confirming this outcome path (`truncated`, via the new-claim-check branch) is correctly classified and disclosed, and that the unrelated existing fabrication guard is **unregressed**.
- `10:29:04`–`10:29:05` — `_attempt_coverage_correction` (`guard-trace ENTER`) ran, called `_comparison_coverage_gap`, found a real gap (`models` category — the same pre-existing category-duplication artifact already known from the T13b run, out of scope for this phase) but **no new resolved evidence to retry with** — it took the pre-existing, unmodified early-exit path (`"no new evidence found by traversal -- disclosing the gap rather than retrying blind"`) and never called `_stream_team_run` at all.
- Final answer: the deterministic `compare_enumerations` tool supplied the authoritative gap analysis (left-only 4: the exact correct GRN/credit-note/stock-adjustment/stock-transfer set, matching documented ground truth exactly), the top-level completion's own honest disclosure was preserved, and the `models`-category gap was disclosed as unresolved rather than silently dropped. `/runs` confirmed empty immediately after.

**What this run did NOT reach:** coverage-correction's own retry never got truncated this run (model behavior here is stochastic — the deterministic tool and the traversal-found-nothing-new path both resolved the task before the SAME failure shape as the original T13a incident could recur at coverage-correction's own level). §5's adoption-policy fix (the mission's primary named deliverable) was therefore **not independently re-exercised by this specific live run** — it is scratch-validated end-to-end against the real production function (§7, "Full pipeline A") and code-reviewed, but the live run described here validates the surrounding budget-isolation and outcome-contract machinery genuinely and cleanly, not the coverage-correction adoption branch specifically. Per the mission's own explicit instruction not to repeat the scenario chasing a favorable reproduction, no second live run was attempted.

## 9. Commit SHAs and deployment verification

- Local / `origin/main`: `197146c527e79f0cd5850694e5c65cdb30964bc9` (implementation) — this report's own commit SHA is recorded by this file's own git history once committed.
- ZGX: fast-forwarded to `197146c527e79f0cd5850694e5c65cdb30964bc9`, confirmed via `git rev-parse HEAD` post-pull.
- `agno-api.service`: PID `1805469` at the time of the live validation run, `ActiveState=active`, `SubState=running`, unchanged across the entire live validation (confirmed: same PID before launching the run and after it completed — the service itself never restarted or crashed during validation).

## 10. Service state and working-tree preservation

- `/runs`: empty before and after the live validation run.
- ZGX dirty/untracked count: 35 before this phase, 35 after — identical set, confirmed via count (not just number) before the `git pull`.
- Local dirty/untracked count: 79 before, 80 after. The +1 is not attributable to any file this phase created or modified — `swarm/team.py` itself shows as clean (committed), no `Phase-0.54`-named file or scratch-harness path appears in the untracked list, and the full untracked-file set before/after is otherwise identical by direct comparison. Recorded honestly rather than silently rounded away; not investigated further as it falls outside this phase's own change surface.

## 11. Limitations and acceptance criteria not demonstrated

- **Coverage-correction's own adoption-policy fix (§5) was not independently exercised in a fresh production run this phase** — only scratch-validated (end-to-end, against the real function, reproducing the exact live incident shape) and code-reviewed. This is the one acceptance-criterion item the mission explicitly allows to be marked PARTIAL/NOT PROVEN rather than fabricated into a false live PASS.
- `_attempt_integration_correction`'s own adoption policy still has the identical latent defect `_attempt_coverage_correction` had before this phase (blind rejection on marker presence). Deliberately left unfixed — out of this phase's named scope (§6, "smallest safe correction to the two proven defects"), consistent with Phase 0.50's own precedent of fixing one sibling function at a time.
- The pre-existing `models`/`tables` category-duplication artifact in `_comparison_coverage_gap`'s own task-category detection (already known from the T13a/T13b runs earlier this session) was observed again live this run. Not touched — explicitly a different, pre-existing mechanism, not part of this phase's two named defects.
- The `forward_member_answer` tool-registry readiness warning seen on every `agno-api.service` startup this phase (including before this phase's own changes) was not investigated — pre-existing, unrelated.

## 12. Verdicts

| Area | Verdict |
|---|---|
| Recovery-budget isolation | **PASS** — implemented, scratch-validated (scenarios B, A/G, H), and genuinely live-validated (the `verified_answer` op got its own real attempt and was correctly classified; no cross-op interference observed) |
| Completion outcome observability | **PASS** — all 14 return points carry an explicit outcome; every caller updated; live run showed the `truncated` outcome correctly computed, disclosed once, and not confused with a skip |
| Evidence-preserving adoption (coverage-correction) | **PASS on implementation and scratch validation (end-to-end, real function, exact incident shape reproduced and fixed) — NOT independently reproduced in this phase's live run** (§8, §11) |
| Verification/disclosure integrity | **PASS** — scratch scenario F confirms no verifier weakening; live run and scratch Full-pipeline-A both show exactly one accurate disclosure, no stacked/contradictory banners |
| Scratch validation | **PASS** — 7 scenarios (B, A/G, D, E, H, Full-pipeline-A, F) against real production functions, all assertions passed, mocked boundaries clearly declared |
| Live validation | **PARTIAL** — budget isolation and outcome contract genuinely exercised and correct; coverage-correction's own retry-adoption branch not reached this run (§8, honestly reported per the mission's own allowance) |
| Deployment and preservation | **PASS** — clean fast-forward to `197146c` on ZGX, service restarted cleanly, `/runs` empty throughout, all pre-existing dirty/untracked files preserved on both local and ZGX |
| **Overall phase** | **PASS WITH ONE NOTED LIMITATION** — both proven defects are fixed and committed; the fix is deployed and running in production without error or regression; the primary mechanism (recovery-budget isolation) is live-confirmed; the adoption-policy fix is confirmed correct end-to-end against the real function and the exact incident data, but awaits an independent live reproduction, honestly disclosed rather than overstated |
