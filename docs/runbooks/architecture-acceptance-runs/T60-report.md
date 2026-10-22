# T60 — Runtime-Enforced Task Completion and Evidence Integrity

Builds on, and does not overwrite, prior reports through `T59-report.md`.
Commit `783ff0907fe041fefd0025dc77e9b983e1aebb92` (local `main` and
`origin/main`), deployed to ZGX `agno-api.service`. No change to
`hive-mcp/tools/verify.py`, `hive-mcp`, or `hive-mcp-agno`.

## A. Verdict

**PASS WITH QUALIFICATION.** A real, source-confirmed gap was found and
corrected: one of `_verified_answer`'s return paths — the one taken when
`verify_claims` is attempted but fails — discarded every other guard's
own, independently-computed coverage/completeness disclosure, even though
none of those other checks depend on `verify_claims`' own availability at
all. The fix is implemented, harness-verified against the real function
(both locally and against the deployed artifact), and deployed. The
qualification: this was confirmed and validated via a controlled harness
that deterministically forces the triggering condition, not via a
naturally-occurring hive-mcp outage during this session's own work.

## B. Architectural finding

**The runtime does enforce several real completion/evidence invariants
correctly** (detailed in §D), but one confirmed inconsistency existed:
a single return path's choice of which disclosures to include depended
on which GUARD fired, not on what evidence had actually been gathered —
exactly the class of defect this mission's own framing warns about
("a fluent final answer" vs. "an answer that satisfies the user's actual
requirements").

**A second, related observation, not treated as a defect requiring a
code change this phase**: `_coverage_verdict` (Phase I-A, 2026-09-23) is
a carefully-designed, deterministic `complete`/`incomplete`/`unknown`
normalizer over existing coverage signals (`_scoped_coverage_gap`,
`_declared_models_not_reported`, `_integration_mechanism_missing`), with
an explicit, correct contract ("undetermined... NEVER treated as
complete"). **It has zero callers anywhere in the file** — confirmed by
grep; its only other reference is a comment describing what it would let
a future caller do. The underlying signals it was built to unify ARE each
independently consulted (at two separate call sites, `_verified_answer`'s
own guards and a second site at line ~12776), and each one's own note
correctly reaches the final answer via `_tail()` — so no evidence is
currently lost by this specific function's disuse. What is missing is a
SINGLE, structured, machine-consumable "is this task complete" signal;
today a caller can only infer it by parsing free text for known note
strings. This is flagged in §H as an unresolved observation, not
implemented this phase: building the integration would mean threading a
new return value through `_verified_answer`'s 20+ return sites, which is
a materially larger change than this phase's own "smallest sound
correction" mandate supports without a demonstrated consumer that needs
the structured signal.

## C. Actual completion flow

1. **`run_task_async`** streams the initial generation via its own Z16
   copy, producing `content`.
2. **`content = await _verified_answer(...)`** — the main guard chain
   (unfinished-intent, write-claim, search-claim, no-evidence,
   db-evidence, comparison-reconciliation, syntax-loss-reask,
   narrated-tool, enumeration, repetition-completion, citation-correction
   — 12 `_adopt_retry`-mediated guards, all protected against truncated
   -retry adoption as of T59), each bounded by the shared
   `len(all_results) <= 1` one-retry-per-call budget. Up front and after
   any adoption, `verify_claims` is checked; the up-front coverage/
   completeness/comparison notes (`_scope_note`, `_integ_note`,
   `_models_note`, etc.) are computed once, unconditionally, and ride
   along via the shared `_tail()` closure on every return site that calls
   it. **This phase's fix**: one return site — the post-adoption
   `verify_claims`-unavailable branch — was NOT calling `_tail()`,
   confirmed and corrected (§E).
3. **`content = await _hoist_denied_premise(...)`**, then
   **`_enforce_verification_invariant(...)`** (Phase H) — a SEPARATE
   choke point, re-verifying the FINAL text regardless of which of
   `_verified_answer`'s 20+ internal return sites produced it, repairing
   or failing explicitly (`_repair_verification_failure` /
   `_verification_failed_answer`) rather than letting a still-invalid
   answer through silently.
4. **`content = await _evidence_integrity_check(...)`** (Phase R) — the
   final choke point for the 5 structured-evidence-contradiction
   categories (file/path existence, enumeration/count, comparison
   completeness/gap [T53/T54/T56-extended], DB row counts, environment/
   branch facts), independently bounded and, as of T57/T58/T59, aware of
   repetition/semantic-stall truncation and protected by the shared
   `_adopt_retry` gate for its own internal retry's adoption decision
   (though it does not itself call `_adopt_retry` — its own disposition
   uses `_force_uncertainty_answer` directly, confirmed unchanged and
   correct this phase).
5. **`content = await _attempt_evidence_grounded_reconstruction(...)`**
   (Phase I-C) — runs ONLY when Phase H's gate concluded
   `VERIFICATION FAILED`; attempts one bounded, deterministic
   reconstruction from `compare_enumerations`' own literal output, never
   in place of or before Phase H, and never ships unless ITS OWN
   verification passes — otherwise the original `VERIFICATION FAILED`
   result stands unmodified.

**No bypass found** that lets a later stage overwrite an earlier stage's
honest "unresolved"/"failed" disposition with something that merely
looks complete: Phase H's repair either clears the SAME check or
explicitly fails; Phase I-C either reconstructs from real evidence and
passes its own re-verification, or leaves Phase H's result untouched.

## D. Completion matrix

| Invariant | Status | Evidence |
|---|---|---|
| Execution completion (normal vs. truncated/interrupted) | **Verified** | T57/T58/T59's own flags (`_repetition_truncated`, `_semantic_stall_truncated`), now protected at all 12 `_adopt_retry` call sites; `_TRUNCATED_ANSWER_NOTE` sentinel distinguishes disclosed-incomplete from adopted-complete. Unchanged, re-confirmed this phase by source read, not re-tested. |
| Required coverage (enforceable representation of user requirements) | **Gap confirmed (disuse, not loss)** | `_coverage_verdict` exists, is correctly designed, and is never called (§B). The underlying signals ARE individually surfaced via `_tail()`'s own notes, so no evidence is lost today — but there is no single, structured completion verdict a caller could consult without parsing free text. Not fixed this phase; flagged in §H. |
| Evidence support (claims checked against gathered evidence, uncertainty preserved) | **Verified** | `verify_claims`' own DECLARED/NOT FOUND/BAD/MISMATCH/PLAUSIBLE categories (hive-mcp), Phase H's repair-or-fail gate, Phase R's 5-category structured check — all confirmed via source trace to preserve "could not verify" as a distinct state from "verified clean," never silently converted. |
| Negative claims (absence grounded in correctly-scoped search) | **Verified, pre-existing** | `_contradicted_by_failed_lookup`'s own scoping, `verify_claims`' ABSENCE CLAIMS category (`CONTRADICTED`/plain-not-found distinction, visible in this phase's own harness output). Not independently re-tested this phase; no new evidence prompted re-verification. |
| Partial results (findings preserved, gaps named) | **Gap confirmed and fixed this phase** | The exact defect in §E: `_summarize_actual_writes` alone (confirmed empty in this phase's own harness) carried none of the independently-gathered coverage findings on the `verify_claims`-unavailable path; `_tail()` now does. |
| Final acceptance (every path passes through the necessary controls) | **Verified, with the one exception now closed** | Traced every return site reachable from `run_task_async`'s single call chain (§C); the one exception found is this phase's own fix. |
| Recovery interaction (retries/synthesis cannot overwrite a known-incomplete disposition) | **Verified** | T59's `_adopt_retry` fix (all 12 sites) plus Phase H/Phase I-C's own explicit "only release on the reconstruction's OWN passing re-verification" contracts, confirmed by source read this phase, unchanged. |
| Verifier failure (unavailable/ambiguous stays unknown, never becomes success) | **Gap confirmed and fixed this phase** | This is the exact invariant the fixed defect violated: an unavailable verify_claims call was correctly kept from being treated as "clean," but its own NARROW disclosure silently discarded 16 OTHER, unrelated findings in the same return. |
| State isolation (task state, evidence, completion status scoped to the correct run) | **Verified** | All relevant flags (`_repetition_truncated`, `_semantic_stall_truncated`, `_REPETITION_COMPLETION_FLAG`, `_REPAIR_ATTEMPTED_FLAG`, `_RECONSTRUCTION_ATTEMPTED_FLAG`, `_EVIDENCE_RECONCILE_FLAG`, etc.) are per-`team`-instance attributes, confirmed by direct read of every set/clear site encountered this phase and in T59's own prior trace. |
| Diagnostics (operators can distinguish complete from fluent-but-incomplete) | **Verified, improved this phase** | Every guard already prints its own label and finding; this phase's fix means the `verify_claims`-unavailable path's own print line (`[team] verify (post-adoption)...`) is now followed by the SAME rich disclosure every other path already produces, rather than a bare write summary. |

## E. Implementation

**File changed**: `swarm/team.py` only (22 insertions, 1 deletion).

**Behavioral change**: in `_verified_answer`'s post-adoption
`verify_claims` re-check, the `if unavailable:` branch now returns
`content + _UNVERIFIED_DISCLAIMER + _tail()` instead of
`content + _UNVERIFIED_DISCLAIMER + _summarize_actual_writes(*all_results)`.
`_tail()` already includes `_summarize_actual_writes(*all_results)` as
its own final component, so nothing that branch previously returned is
lost — it is a strict superset.

**Why this is the smallest sound correction**: `_tail()` is the EXISTING
shared closure every other return path in the function already calls;
this fix makes the one exception consistent with the other ~19 return
sites, rather than inventing a new disclosure mechanism or duplicating
`_tail()`'s own 17-component composition at this one site. Confirmed
safe by direct read: `_fab_note` (the one `_tail()` component that
relates to `verify_claims` itself) is only ever non-empty when `_fab_bad`
is True, which cannot co-occur with `unavailable=True` in the same call
— so widening to `_tail()` introduces no stale or misleading fabrication
note, only the independent disclosures that were being dropped.

## F. Validation

**Source-level trace**: the exact single return site was located by
systematically checking every `return` statement in `_verified_answer`
(8665–10609) for the presence of `_tail()` on the same or a following
line; all but one multi-line `return (...)` statement already included
it. Confirmed `_fab_note`'s own assignment (`_fab_note = ""` unless
`_fab_bad`) makes the fix safe without needing to execute anything.

**Local controlled harness** (`t60_tail_dropping_test.py`,
`t60_regression_clean_path.py`, this session's scratchpad, not
committed), run against the real, imported `_verified_answer` (not a
reimplementation), with `_verify_claims` monkey-patched to
deterministically force `unavailable=True` (simulating an attempted-
but-failed hive-mcp call — distinct from the "never configured" case,
confirmed by reading `_verify_claims`' own early-return condition) and a
fake `team._read_state` engineered to trip `_swept_directory_not_reported`
(pure Python logic over the read log; no MCP call needed, so its note is
genuinely independent of the forced `verify_claims` failure):
- With the fix: the final answer contains `"A DIRECTORY WAS READ THROUGH
  AND ALMOST NONE OF IT REACHED THE ANSWER"` — the previously-dropped
  note — confirmed present.
- Direct confirmation that `_summarize_actual_writes(FakeResult())` alone
  (the prior return value, minus the disclaimer) is `""` — proving the
  note genuinely would not have survived before this fix.
- A second run with `_verify_claims` left unpatched (the real "not
  configured" early-return, `unavailable=False`, already using `_tail()`
  before this phase) produces the same note and no regression — the
  unaffected branch is confirmed unaffected.

**Deployed-path exercise**, clearly separated from the local harness: on
ZGX, using the exact interpreter `agno-api.service` runs under, with
`swarm.team` imported from the confirmed deployed commit, the same
monkey-patched scenario was re-run directly against the deployed module
and produced the identical result (`"DIRECTORY WAS READ" in final` ==
`True`).

**No natural live run was launched for this phase.** Forcing a genuine
hive-mcp timeout/failure organically, on demand, is not something this
session can safely or efficiently arrange, and the mission's own guidance
prefers a controlled exercise when it validates the fix more efficiently
than waiting for an intermittent condition. This is the correct use of
that allowance: the defect is about a CODE PATH's behavior when a
specific, well-defined precondition holds, not about model behavior that
varies run to run.

## G. Deployment

- Commit `783ff0907fe041fefd0025dc77e9b983e1aebb92` pushed to
  `origin/main` (`9fc72ee..783ff09`).
- ZGX: `git pull` fast-forwarded `d808c17..783ff09`, confirmed via `git
  rev-parse HEAD` matching exactly, both at deployment and during the
  §F deployed-path exercise.
- `systemctl --user restart agno-api.service`: new PID `955077`,
  `/health` returning `{"status":"ok", ...}`.
- No active run before the restart (`GET /runs` empty, no `run-worker`
  process).
- `hive-mcp`/`hive-mcp-agno`: not touched — no source change, not
  restarted.
- `git status --short` before and after this phase's work shows only
  `swarm/team.py` newly dirty; the same pre-existing dirty/untracked
  files recorded in every prior report remain exactly as found. One
  stray, self-created, empty (0-byte) artifact file (`absolute`)
  appeared from a shell-parsing side effect of a command run this
  session; it was inspected and removed before staging, never staged or
  committed.

## H. Remaining risks

- **`_coverage_verdict` remains dead code** (§B). Confirmed not to cause
  evidence loss today (its underlying signals are each independently
  disclosed), but it means no caller can ask "is this task complete" as
  a single structured question — only by parsing free text. Not
  implemented this phase: wiring it in would require threading a new
  return value through `_verified_answer`'s 20+ return sites, a
  materially larger change than this phase's "smallest sound
  correction" mandate justifies without a demonstrated consumer. If a
  future caller (e.g., `api/server.py` surfacing a structured
  `completion_status` field to callers) needs this, that is the
  concrete trigger to revisit it — not speculative.
- **This phase's fix was validated via a controlled, monkey-patched
  harness, not a naturally-occurring hive-mcp outage.** The underlying
  mechanics (`_fab_note`'s own assignment, `_tail()`'s composition) were
  also independently confirmed by direct source read, which is why this
  is reported as a confirmed fix rather than a speculative one — but a
  natural reproduction was not attempted or available this phase.
- **Other `_verified_answer` return sites were not individually
  re-audited beyond the systematic `_tail()`-presence check** described
  in §F. That check is mechanical and complete for this ONE question
  (does this return call `_tail()`), but does not itself prove every
  OTHER completion property for every site — those properties were
  assessed at the matrix level (§D) using the mission's own invariant
  list, not re-derived per return site.

## I. Next architectural decision

The completion/evidence-integrity architecture is now consistent for the
specific invariant this phase targeted (every `_verified_answer` return
site includes the same independent disclosures, regardless of which
guard produced it) and remains consistent for the invariants T57–T59
already closed. The single highest-value confirmed-but-unaddressed item
is `_coverage_verdict`'s disuse (§B/§H) — but per this phase's own
finding, it is not currently causing evidence loss, only the absence of
a structured signal nothing yet consumes. The next phase should not
treat this as a default next step; it should wait for a concrete
consumer or a demonstrated incident where free-text-only disclosure
(as opposed to a structured verdict) caused a real downstream problem.
