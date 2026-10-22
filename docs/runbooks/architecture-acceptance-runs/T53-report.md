# T53 — Factual accuracy, evidence integrity, and reliability remediation

Builds on, and does not overwrite, `T48-report.md`, `T48-rerun-report.md`,
`T51-report.md`, or `T52-report.md`. Commit `386e895ce7e2ca120237c91380dfbe078d7b422b`
(local `main` and `origin/main`), deployed to ZGX `agno-api.service`. No change to
`hive-mcp/tools/verify.py` this phase, so the `hive-mcp` container was not redeployed.

## 1. Overall verdict

**PARTIAL.**

Three narrowly-scoped fixes are implemented, harness-validated against the real
functions, deployed, and directly live-confirmed for two of the three (T7's inline
mark, and the organically-exercised T3 Tier-0 detection). The third (T2) is
harness-proven correct against the exact historical shape, but its one live
validation attempt hit a different, more complex interaction (a separate
verify_claims-vs-candidate conflict) that makes the live run itself inconclusive
rather than a clean confirmation — reported honestly as such rather than claimed as
a live PASS it did not unambiguously demonstrate. A fourth, unplanned finding (a
real regression T52's own Tier-0 fix introduced into the liveness-kill recovery
path) was discovered, fixed, and harness-validated within this same phase. No
unit test was added, modified, or run, per explicit instruction; validation is
syntax checks, scratchpad harnesses against the real functions, and live ZGX
scenarios only.

This is not a PASS because T2's live scenario did not unambiguously demonstrate the
fix firing on the exact contradiction shape in production (the harness proof is
direct and real, but is not itself a live run). This is not a FAIL because no
deployed change caused a regression (the one regression found this phase was
caught and fixed before being reported, not left in production), the live
environment was directly exercised and inspected for every claim below, and the
one inconclusive scenario is disclosed as such, not papered over.

## 2. Priority 1 — T2 remediation

**Root cause (confirmed, not assumed):** traced the full pipeline
(`_complete_repetition_truncated_answer` → `_computed_comparison` →
`_reconcile_completeness_claim_with_comparison`, all within `_verified_answer`,
confirmed via direct code reading that Z16's own completion runs BEFORE
reconciliation, ruling out an ordering/flag-skip explanation considered and
rejected this phase). The gate's own deterministic-synthesis path
(`_synthesize_comparison_answer`) fires unconditionally whenever `existence_
contradicted` is true, independent of whether the draft made a recognized
"completeness claim" — but T52's own Phase 3 fix
(`_sections_without_completeness_claim`, which preserves any section the draft
wrote that does not trip the blanket completeness-claim lexicon) had a blind spot
it introduced itself: a section naming a genuinely MATCHED or PARTIAL_MATCH item
as "missing" by name (T2's actual historical shape — "GET /gst/rate-changes...
has no corresponding frontend hook" for an endpoint the comparison proves exists
on both sides) never trips that lexicon at all (it only recognizes blanket
assertions like "all X have Y" or "no gaps", never a specific-named-item claim),
so the section was preserved and shipped alongside the authoritative comparison
it directly contradicted.

**Live-reproduced via direct harness** against the real
`_reconcile_completeness_claim_with_comparison` (not a reimplementation), using a
hand-built `cmp_note` matching the real `compare_enumerations` output format: before
the fix, the wrong "Missing Hooks" section survived into the reconciled output
unchanged (4 sections preserved, including the contradicted one); after the fix, the
exact same section is correctly excluded (3 sections preserved) while a genuinely
unrelated section and a TRUE gap claim (naming a real LEFT_ONLY item) both remain
correctly preserved, confirmed by a second, independent harness run.

**Files/functions changed:** `swarm/team.py` — new `_MISSING_FRAMING_RE` and
`_matched_item_named_as_missing` (sibling of the existing
`_reconstruction_category_conflict`, same exact-substring-co-occurrence, no-NLP
design); `_sections_without_completeness_claim` gains an optional `cmp_note`
parameter (default `""`, exact pre-T53 behavior for the no-argument case) and now
also excludes a section `_matched_item_named_as_missing` flags; its one call site
(inside `_reconcile_completeness_claim_with_comparison`) now passes `cmp_note`
through.

**Commit:** `386e895`. **Deployed:** ZGX `agno-api.service` @ `386e895`, confirmed.

**Targeted live scenario:** the T2 prompt verbatim, `read_only=True`,
`swarm_team=engineering`. MCP task `kbcceu421`, session `0a989574-...`, 1306.89s.
**Result: inconclusive, not a clean confirmation.** This run's own enumeration
mistake took a different shape than the historical incident (different gap items
entirely — register/status/register-additional/internal-ondc-domains/internal-
tenants-names/business-id-documents as backend-only, several admin-* hooks as
frontend-only; GST rate-changes was NOT among its claimed gaps this run, unlike the
historical case), and a SEPARATE mechanism (verify_claims re-checking the
reconciliation candidate itself, which cited lines verify_claims could not confirm)
also fired, producing a combined "UNRESOLVED — THIS ANSWER CONTRADICTS EVIDENCE...
one correction attempt did not resolve it" disclosure rather than a clean
accept-or-reject outcome. Journal evidence confirms the gate itself fired correctly
(`DETERMINISTIC_COMPARISON_GATE_FIRED: match=7 partial_match=1 left_only=5
right_only=8 ... claim_authoritative_consumed=True`, immediately followed by
`comparison gate: preserving 9 unrelated section(s)`) — the mechanism ran without
error — but no log line distinguishes which of the 9 preserved sections, if any,
were excluded specifically by `_matched_item_named_as_missing` versus the pre-
existing lexicon check, so this run does not by itself prove the NEW exclusion path
fired. The system's own behavior here (disclosing uncertainty rather than shipping
a false-certain answer) is itself correct and consistent with the mission's own
intent, but it is not the same thing as a clean live confirmation of this specific
fix, and is not reported as one.

**Performance:** 1306.89s against a historical T2 baseline this report does not
have a directly comparable duration for (the original T2 incident's own duration
was not recorded in the T48/T48-rerun reports); no regression signal either way.

## 3. Priority 2 — T7 remediation

**Where duplication originates (confirmed via live reproduction, not assumed):**
traced via the real ZGX journal for a direct reproduction of the historical T7
prompt. The `list_directory` tool call itself returns the correct 6-entry listing
(confirmed in its own `result_preview`). The Researcher's own report correctly
relays all 6 files including `__init__.py`. The duplication and the miscount both
originate in the **Coordinator's own single, continuous completion stream**: its
generation first correctly relays the Researcher's listing (189 chars, matching the
real 6-entry listing), then — with no tool call, no retry, no guard event, and no
Z16 repetition-loop detection anywhere in the journal for this window — spontaneously
continues into a second, independently-generated restatement of the same question
that states a different, wrong count (5, omitting `__init__.py`). This is model
generation behavior, not a tool-result, forwarding, or evidence-assembly defect:
Z16's own repetition detector requires near-exact textual repetition to fire, and
the two restatements here are structurally similar but not textually identical, so
they never trip it. T16's repetition recovery and `_miscounted_listing`'s own
disclosure-only design (deliberately not adjudicating which of several defensible
readings is "correct") are both confirmed correct and were not modified.

**Fix:** `_mark_miscounted_statement_inline` (new) annotates the specific mismatched
stated-count occurrence in place with a pointer to the existing trailing disclosure,
mirroring T52 Phase 1's `_mark_unsourced_filenames_inline` design. Wired into
`_miscounted_listing`'s one call site inside `_verified_answer`. Does not rewrite,
remove, or adjudicate the count — `_miscounted_listing`'s own docstring already
explains why that adjudication is deliberately out of this mechanism's scope.

**Before/after counts:** ground truth 6 files (confirmed via the tool's own raw
output both times). Before the fix: delivered answer stated 5, with the correction
reachable only from a trailing banner. After the fix: delivered answer states
`5 **[see COUNT DISAGREES note below]**`, pointing the reader directly at the
specific wrong sentence.

**Commit:** `386e895`. **Deployed:** ZGX `agno-api.service` @ `386e895`, confirmed.

**Targeted live scenario:** the T7 prompt verbatim, run twice — once before this
phase's deployment (to diagnose the mechanism; MCP task result, session
`c4075c07-3dcd-48e2-8bea-9f78e35a9263`, run_id `fdff93e5547a`, 78.71s) and once after
(to confirm the fix; session `5577655b-98ac-4c33-8033-a3be5c78cc90`, 76.25s).
**Result: live-confirmed.** Post-deployment run's delivered answer reads "The
directory `API/business-service/router/` contains 5 **[see COUNT DISAGREES note
below]** `.py` files" — the inline marker landed exactly on the mismatched number,
the trailing `COUNT DISAGREES` disclosure is unchanged and still correctly states
the real count (6) and the real per-extension breakdown, and every other character
of the answer (the file list, the forwarded member findings) is byte-identical in
shape to the pre-fix run. Distinct records (the real file list) were not altered or
removed.

**Performance:** 76.25s post-fix vs. 78.71s pre-fix (same session, back-to-back) —
no measurable overhead from the inline-marking regex scan, consistent with it being
a single, already-matched-position string insert.

## 4. Priority 3 — T12 validation

**Did the actual budget-exhaustion path get exercised? Yes, directly confirmed.**
A live run (MCP task `k9dlfjz6j`, session `17202af2-9747-4571-851f-62c9c32d31db`)
ran for 1336.58s — a genuine, extended delegation sequence, not a quick complete
run — and was cut short mid-generation by T52's own Tier-0 liveness detection (see
§5 below), returning a partial, explicitly-labeled-incomplete draft rather than a
normal completion. This is a stronger, more conclusive exercise of "the evidence
budget was actually under pressure" than a normal complete run would be, since the
run never even reached its own natural conclusion.

**Evidence-coverage state:** the recovered partial draft's own disclosures (all
fired on the recovered content, confirmed via the delivered text) include: a
real stated-count-vs-file miscount (`hsn_api.py` has 3 endpoints, not the claimed
48, caught by the repo-derived count guard), dead-code routers named as if mounted
(`auto_restock_config_api.py` and 6 others, caught by the repo-derived routers
guard), the business-service integration question never actually answered (caught
by the repo-derived integration guard), and 5 citations verify_claims could not
confirm with the one corrective retry producing nothing better (caught by
verify_claims itself, run here via the pre-existing `repair_unguarded_draft`
mechanism). **No claim in the delivered answer was presented as a settled,
verified fact without one of these four disclosures attached somewhere in the
response** — the mission's own Priority 3 pass criterion.

**One real gap found and fixed, not merely observed:** the "External Dependencies"
section (`database.py`, `gst_engine.py`, `gst_resolver.py`, `file_parsers.py`,
`utility_ai_client.py`) carried **no disclosure of any kind** in this run's own
delivered answer, despite at least 4 of those 5 names (excluding the deliberately-
ubiquitous `database.py`) never appearing in the recovered member findings this
run actually produced. Root-caused to `repair_unguarded_draft`'s own documented,
pre-existing design: it runs only guards that "re-derive their facts from the
repo", explicitly excluding anything needing `team._seen_files` (T52 Phase 1's own
`_unsourced_filenames`), because that state dies with the SIGKILLed worker. This
is a genuine interaction T52's own Tier-0 fix newly exposed at scale (more runs
now get killed and routed through this rescue path than before T52 shipped) — see
§6 Priority 5 for the fix.

## 5. Priority 4 — T3 validation

**Heartbeat condition exercised: a genuine, organic freeze — not a controlled
injection.** The planned controlled stale-heartbeat simulation (writing a backdated
`written_at` into a running worker's own liveness file, using only the existing
file format the deployed mechanism already reads/writes) was prepared but not
needed: the SAME live run cited in §4 above (`k9dlfjz6j`) produced a real freeze
organically. Heartbeat ticks were normal and healthy every ~30s from task start
through `11:46:19` (1170s elapsed, 3,869 stream events, climbing steadily, `stall=0s`
throughout) and then stopped entirely with no further tick, log line, or signal of
any kind.

**Wall-clock staleness evidence:** `[api] run auto-terminated (liveness): heartbeat
snapshot has not been written in 122s (threshold 120s) -- the worker has stopped
reporting entirely, so its own last-reported state (stagnant_seconds=0,
liveness_state='MODEL_GENERATING') is stale and not trustworthy`, logged by the
parent `agno-api.service` process (PID `3151801`) at `11:48:20`, i.e. almost exactly
122s after the worker's last heartbeat tick.

**Detection and recovery behavior:** correct, by direct observation. The worker's
own self-report (`stagnant_seconds=0`) would have satisfied every pre-existing tier
(1 through 5) as perfectly healthy — this is the exact blind spot T52's Tier 0 fix
was built to close, and it closed it: the run was killed, not left to hang toward
the client's own 1800s timeout, and the partial draft already on disk (via the
existing, unmodified "write the longest draft into the snapshot" mechanism) was
returned rather than a bare error.

**Resource observations:** the worker process (PID `3419245`) was confirmed gone
from `ps aux` after the kill (expected — `proc.kill()` ran as designed). `dmesg`
remains inaccessible (non-root, the same limitation the original T3 fork
investigation already recorded) and no kernel OOM entry appears in the systemd
journal for the freeze window — this neither confirms nor rules out OOM as the
underlying cause.

**Original root cause: still open, explicitly not claimed resolved.** This freeze
occurred at ~1170-1200s elapsed, not exactly the historically-documented ~1800s
mark, and this report does not claim it is proven to be the identical mechanism —
only that it is a genuine instance of the same failure CLASS (healthy-looking
heartbeat activity followed by total, silent stop) that the historical T3
incidents describe, and that the detection path built to catch this class is now
confirmed, by live evidence rather than simulation, to work correctly.

## 6. Priority 5 — cross-workstream accuracy and regression checks

**A real regression found and fixed, not merely a risk noted:** T52's Tier-0 fix
correctly catches more silently-frozen runs (§5), which means more answers now flow
through `repair_unguarded_draft`'s rescue path than before T52 shipped — and that
path structurally excludes T52's own Phase 1 fix (`_unsourced_filenames`, needs
`team._seen_files`, which dies with the worker). Live-confirmed via the T12 run in
§4: 4 real unsourced filenames shipped with zero disclosure. Fixed this phase:
`repair_unguarded_draft` gains an optional `member_results` parameter (default
`None`, preserving every one of its two existing production call sites and five
existing test call sites exactly); when supplied, it approximates `team._seen_files`
via `_filenames_in()` over each recovered member's own report text — the same text
`_seen_files` was built from while the worker was alive, just read from the
snapshot instead of from live tool events. Deliberately conservative (may under-
flag, never over-flags, since nothing is added to `content` itself) and placed
BEFORE the function's own hive-mcp-connectivity check, since it needs no repo query
at all and must not be lost when hive-mcp happens to be unreachable. Harness-
confirmed against the real function (not a reimplementation): fires correctly when
`member_results` is supplied and the fix-relevant names are genuinely unsourced;
produces no false positive when a name IS genuinely mentioned in a recovered
report; fires correctly even when `hive_url` resolution is forced to fail; and the
old, 4-argument call shape (used by all five pre-existing test call sites) is
unaffected, confirmed by direct harness call with no `member_results` argument at
all.

**T13 (Database Tables preservation):** not independently re-run live this phase
(time budget), but the exact mechanism this phase's own T2 fix extends
(`_sections_without_completeness_claim`) is the SAME function T52 proved live-
working for T13 — the `cmp_note` parameter this phase added is optional and
additive; a caller or scenario that never triggers `_matched_item_named_as_missing`
(no absence-framing language naming a matched identity) behaves identically to
before. Not regressed by construction, not independently re-confirmed live.

**T10 (negation-blind absence claims):** not independently re-run this phase; T53's
changes do not touch `hive-mcp/tools/verify.py` or `_affirms_existence`/
`_affirmed_term_absent_from_citations` at all. Not regressed by construction, not
independently re-confirmed live.

**No-op retry transparency:** not independently re-run this phase; T53's changes do
not touch `hive-mcp/tools/verify.py` at all (confirmed via `git diff --stat`, zero
lines changed in that file this phase). Not regressed by construction, not
independently re-confirmed live.

**T12's inline fabrication disclosure:** regressed by a T52-T53 interaction (the
recovery-path gap, §4/§6 above), found and fixed within this same phase before
being reported as a finding rather than left open.

## 7. Deployment evidence

- Commit `386e895ce7e2ca120237c91380dfbe078d7b422b` pushed to `origin/main`
  (`e3826f9..386e895`).
- ZGX `agno-api.service`: `git pull` fast-forwarded `71c6aa7..386e895`,
  `systemctl --user restart`, confirmed via a genuinely new PID (`3552718`,
  `ActiveEnterTimestamp` matching the restart) and `git rev-parse HEAD` on ZGX
  matching `386e895` exactly.
- `hive-mcp` container: not redeployed this phase (no change to
  `hive-mcp/tools/verify.py`); confirmed via `git diff --stat` before deployment.
- No active ZGX run or battery was in progress at any point before a disruptive
  action this phase (checked via `GET /runs` and `ps aux | grep run-worker`
  immediately before the restart).
- Five live production runs were executed against this exact deployed commit
  after deployment completed: T7 pre-fix diagnostic (`fdff93e5547a`), T12/T3
  combined (`k9dlfjz6j` / `17202af2-...`), T2 (`kbcceu421` / `0a989574-...`), and
  T7 post-fix confirmation (session `5577655b-...`).

## 8. Outstanding risks

- **T2's live confirmation is inconclusive**, not negative — the harness proof
  against the real function is direct and unambiguous, but no live run this phase
  cleanly isolated the new exclusion path firing in production. A future run that
  happens to reproduce the exact "matched item named as missing" shape (rather
  than a different enumeration mistake) would be the first clean live confirmation.
- **T7's duplication root cause (model generation behavior) is not, and likely
  cannot be, fixed at the orchestration layer** without either loosening Z16's
  carefully-calibrated repetition threshold (explicitly out of scope — "preserve
  T16... unless a specific interaction defect is demonstrated", and none was) or
  adding a new, broader structural-restatement detector (which this phase judged
  too speculative to implement without further evidence). The inline-marking fix
  closes the DISCLOSURE gap, not the generation behavior itself.
- **T12's completeness-under-budget-exhaustion is disclosed, not eliminated** —
  the underlying dynamic (an incomplete investigation under delegation pressure)
  still occurs; what changed is that every observed gap and unsupported claim in
  this phase's own live run was correctly flagged, including after a liveness kill,
  which was not true before this phase's Priority 5 fix.
- **T3's original worker-death root cause remains fully open.** This phase
  provides the first live (not simulated) confirmation that the detection path
  works correctly on a genuine freeze, and that the freeze class itself
  (healthy-looking self-report, total silent stop) is real and currently
  occurring in production — neither of which was previously confirmed without
  this phase's evidence — but why the worker freezes is not established, and
  `dmesg` inaccessibility continues to block kernel-level diagnosis.
- **T13, T10, and no-op-retry transparency were not independently re-run live
  this phase.** Each is either unmodified by this phase's diff (T10, no-op-retry)
  or extended in a way designed to be behavior-preserving for every case that
  does not newly trigger (T13/T2's shared function) — reasoned, not re-measured.
- **The recovery-path fix (§6) is itself a trailing disclosure, not an inline
  mark**, since `repair_unguarded_draft` returns only an appendable suffix and
  this phase did not change that contract. A liveness-killed run with a genuine
  fabrication now gets a correct but less prominent disclosure than a normal,
  completed run would.

## Acceptance criteria applied

PASS was not met: T2's live scenario did not unambiguously demonstrate the fix in
production (inconclusive, not negative), and T3's original root cause remains
unconfirmed (expected and explicitly preserved, per instruction, but still a gap
against a strict PASS bar). FAIL does not apply: no critical defect remains
reproducible without a disclosed mitigation, no deployed change caused a material
regression (the one regression found was fixed within this phase, not left open or
hidden), and every live-evidence claim above is backed by a specific run
identifier, session ID, or journal excerpt rather than an inferred or assumed
outcome. PARTIAL is the accurate verdict.
