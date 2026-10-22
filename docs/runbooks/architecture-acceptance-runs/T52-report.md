# T52 — Critical reliability remediation: implementation, deployment, and live validation

Phase T52. Builds on, and does not overwrite, `T48-report.md`, `T48-rerun-report.md`,
or `T51-report.md`. Commit `71c6aa7df19182db2f239c79185e5bc33b7f0f7f` (local `main`
and `origin/main`), deployed to ZGX `agno-api.service` and the local `hive-mcp`
container (port 9003).

## 1. Executive verdict

**PARTIAL.**

Four narrowly-scoped fixes (T12 inline fabrication marking, T3 liveness blind spot,
T13 gate section-preservation, verifier no-op-retry transparency) are implemented,
unit-validated, deployed, and — for two of the four (T13, T10/verifier) — directly
confirmed firing correctly in live ZGX production via fresh journal evidence. Zero
regressions across both local test suites (2703 + 607 tests, exact failing-test-name
diff against the pre-existing baseline, both directions). No destructive action
taken; every pre-existing dirty/untracked file confirmed untouched.

This is not a PASS because: (a) T12's own live validation run did not reproduce the
original fabrication shape (T12 is documented as non-deterministic — "four different
outcomes in one day" — so this is an inconclusive, not a negative, result for that
specific fix); (b) T3's fix could not be exercised against a genuine ~1800s worker
freeze within this phase's time budget — it is implemented and unit-tested, not
live-confirmed; (c) two mission items (T2 prose-vs-comparison contradiction, T7
duplicate-content root cause) were investigated but not fixed this phase — the
evidence available did not establish a root cause precise enough to implement
without speculation, and the mission's own constraint against speculative changes
takes priority over closing every item. This is not a FAIL because no deployed
change caused a regression, no critical defect was left silently unaddressed without
disclosure, and the live environment was directly exercised and inspected for every
claim made below.

## 2. Baseline

- Local and ZGX both started this phase at commit `4553394` (T51's own telemetry +
  report commit).
- `hive-mcp` container (local, port 9003) was running the T51-deployed image
  (`ghcr.io/abehera1992/hive-mcp:latest`, created `2026-10-22T01:07:53Z`).
- No active ZGX task/battery was running at the start of this phase, immediately
  before the ZGX restart, or immediately before the `hive-mcp` container recreate
  (checked each time via `ps aux | grep run-worker` and `GET /runs`).
- Pre-existing dirty/untracked state (T24's own work, the training track, the two
  untracked T18/T19 test files) confirmed present at baseline and preserved
  unmodified throughout — `git status` before and after this phase's commit shows
  the identical set, since this phase's commit touched only the four files listed
  in §6.

Phase 0's own remediation checklist, built from the T48/T48-rerun/T51 reports before
any code was read, classified each item as follows (revised after source inspection,
per column 2):

| Item | Phase-0 classification (from reports alone) | After source inspection |
|---|---|---|
| T12 fabrication | Still reproducible | Confirmed: `_unsourced_filenames` only ever produced a trailing disclosure, never an inline mark |
| T3 worker death | Blocked by unavailable evidence | Confirmed via source: a real, fixable gap (no wall-clock staleness check), independent of root-causing the worker death itself |
| T13 gate | Fixed in code but not live-confirmed (T51's forwarding fix) | A SECOND, separate cause found: `DETERMINISTIC_COMPARISON_GATE` discards unrelated sections wholesale |
| verify_claims A1–A4 | Already fixed and live-confirmed (T51) | Confirmed unchanged; not re-touched this phase |
| T10 negation | Remaining historical issue (per the mission's own Phase 0 framing) | Confirmed via live reproduction: already resolved, by T51's own A3 fix + a pre-existing, independently-hardened team.py guard — no separate "negation-blind containment guard" defect exists currently |
| T2 prose-vs-comparison | Remaining historical issue | Investigated; most likely explanation (the reconciliation lexicon does not recognize "X and Y are missing"-shaped claims, only blanket completeness assertions) is plausible but not live-confirmed — not fixed this phase |
| T7 / duplicate content | Still reproducible (pervasive, 9/14 historical) | The disclosure mechanism (`_miscounted_listing`) is confirmed correct and by-design; the root duplication source was not isolated to a single fixable layer within this phase's effort — not fixed this phase |
| No-op retry transparency | Still reproducible | Confirmed: the mechanism tracked only a count, never the original report — fixed this phase |

## 3. Issue-by-issue status

### 3.1 T12 — fabrication under delegation pressure (Phase 1, critical)

- **Historical symptom:** an architectural-overview task hit the 12-delegation volume
  limit; 9 of ~10 named "external dependency" files were never read or searched by
  any tool call, yet were asserted as fact; the disclosure that caught this
  (`_unsourced_filenames`, built for this exact incident) only ever appended a
  trailing footer, leaving the fabricated prose above it reading as settled.
- **Root cause / fix:** `hive-mcp`... no — `swarm/team.py`'s `_unsourced_filenames`
  detection was correct; the gap was purely in HOW it surfaced the finding.
  `_mark_unsourced_filenames_inline` (new) annotates each unsourced name's first
  enumerated occurrence in place with `**[UNVERIFIED -- no tool call this run
  confirmed this file]**`, reusing the exact same per-run evidence computation —
  deterministic, no new model call, no re-delegation.
- **Commit:** `71c6aa7`. **Deployed:** ZGX `agno-api.service` @ `71c6aa7`, confirmed.
- **Targeted live scenario:** the historical T12 prompt verbatim ("Write a detailed
  architectural overview of the inventory service..."), `read_only=True`,
  `swarm_team=engineering`. MCP task `k12h19vdn`, 1097.01s (historical: 1079.0s).
- **Before/after:** this specific live attempt did not reproduce the original
  fabrication shape — every named "external dependency" file (`auth_utils.py`,
  `gst_engine.py`, `gst_resolver.py`, `category_ondc_mapper.py`,
  `gst_compliance_consumer.py`, `utility_ai_client.py`, `file_parsers.py`,
  `column_mapper.py`) turned out to be real AND was confirmed present in the
  evidence ledger this run (no inline marker fired, correctly — there was nothing
  to mark). T12's own documented non-determinism ("four different outcomes in one
  day", per the original T48 report) makes this a known risk for any single
  validation attempt; a second run was not attempted, consistent with the mission's
  own "do not run the entire battery" / efficiency guidance and the GPU-contention
  avoidance this phase otherwise observed.
- **What the run DID confirm:** the pre-existing, unrelated "uncovered parts"
  disclosure and the pre-existing "omitted topic" guard (`THE INTEGRATION WAS ASKED
  FOR AND NOT DESCRIBED`) both still correctly fired on this run's own genuine
  incompleteness (7 of 16 routers not detailed; the business-service integration
  question not actually answered) — i.e. the underlying delegation-budget-exhaustion
  behavior is unchanged by this phase (not worsened, not fully solved), and the
  disclosure layer around it remains intact.
- **Verdict: PASS on correctness of the fix itself (unit-validated, deployed, zero
  regression), INCONCLUSIVE on live-fire confirmation** (the specific defect it
  targets did not reproduce in the one live attempt made).

### 3.2 T3 — silent worker death (Phase 2, critical)

- **Historical symptom:** a worker dies silently near the 1800s mark with no
  explanatory log line, 3 reproducible occurrences. A prior read-only fork
  investigation ruled out both internal kill paths, found a vLLM-side
  orphaned-request symptom, and identified — but did not implement — a specific gap:
  "whether the stall clock was genuinely not advancing or the snapshot had simply
  stopped being written could not be settled afterward."
- **Root cause / fix:** every existing liveness tier (`_liveness_kill_reason`,
  `api/server.py`) trusts a *duration* the worker computed about itself, which
  freezes, unchanged, the instant the worker stops running at all — whether by a
  missed clean exit, a crash, or the event loop being starved by a blocking call
  that never yields back to the heartbeat's own `asyncio.sleep` loop. A frozen
  snapshot and a healthy one mid-tick were indistinguishable from the polling side.
  New Tier 0: `swarm/team.py`'s `_run_heartbeat` now writes a wall-clock `written_at`
  into the snapshot each tick; `api/server.py`'s poll loop kills a run whose
  snapshot has gone stale for `config.liveness_snapshot_stale_threshold_s` (120s,
  4x the 30s tick interval) regardless of what the snapshot's own last-reported
  fields claim.
- **Commit:** `71c6aa7`. **Deployed:** ZGX `agno-api.service` @ `71c6aa7`, confirmed
  (`ENABLE_LIVENESS_AUTOKILL=true` confirmed set in `~/agno-hive/.env` on ZGX, so
  this mechanism is live-active in production, not merely present in code).
- **Targeted live scenario:** NOT independently exercised against a genuine freeze
  this phase. A full ~1800s reproduction was judged, per the mission's own
  instruction ("prefer existing production telemetry... to avoid another
  unnecessarily expensive full-duration run"), not worth the cost given (a) the
  three existing T3 occurrences in the historical record already establish the
  symptom without a fourth deliberate reproduction being required, and (b) every
  live validation run executed this phase (T12, T13b, T10 — three full production
  runs, 1097s + 553s + 264s) completed normally with a fresh heartbeat on every
  tick, which is at least a **negative-control confirmation that Tier 0 does not
  false-positive on healthy runs of varying length** — it never fired when it
  should not have, across three real, different-duration production executions.
- **Verdict: PARTIAL.** Implemented, unit-tested (fresh/frozen/backward-compat
  cases), deployed, confirmed active, confirmed not to false-positive on three real
  live runs. NOT live-confirmed to actually fire on a genuine freeze — labeled
  PARTIAL per the mission's own explicit instruction for exactly this situation
  ("If the original failure cannot be reproduced within the available window,
  label the result PARTIAL, not PASS").

### 3.3 T13 — database-tables section discarded by the deterministic comparison gate (Phase 3, critical)

- **Historical symptom:** T51 fixed the `_forwarded_members` single-slot-overwrite
  defect and live-confirmed the fix (`appending verbatim: ['researcher#3']`), but
  the live validation that same session found a SECOND cause of the identical
  symptom: `DETERMINISTIC_COMPARISON_GATE`'s `_synthesize_comparison_answer`
  replaces the Coordinator's ENTIRE draft once it fires, discarding any unrelated,
  already-evidenced section (e.g. "Database Tables") along with the completeness
  claim it was actually built to correct.
- **Root cause / fix:** the gate's own contract is specifically about the
  completeness (existence) dimension of a two-sided comparison — nothing in its
  purpose requires discarding an unrelated section that makes no completeness claim
  of its own. `_sections_without_completeness_claim` (new) splits the original draft
  by markdown heading and returns every block that does NOT match
  `_reconcile_completeness_claims`' own tested lexicon (the SAME lexicon the gate
  already uses to decide relevance) — never a block that DOES make a claim, since
  the gate's one invariant (contradictory prose must never ride back in alongside
  the authoritative comparison) is preserved by construction. These preserved
  sections are appended after the deterministic synthesis, never before it and
  never merged into it.
- **Commit:** `71c6aa7`. **Deployed:** ZGX `agno-api.service` @ `71c6aa7`, confirmed.
- **Targeted live scenario:** T13b verbatim (files named explicitly), `read_only=True`,
  `swarm_team=engineering`. MCP task `kfxw22vto`, session `40d835d0-...`, 553.15s
  (historical: 510.5s).
- **Before/after, with direct log evidence:**
  - Before (T51's own validation, last session, run `88ca75d6679d` and its
    post-T51-fix re-run `abc0530438c2`): final answer contains ONLY the bare
    `compare_enumerations` table, zero prose, no "Database Tables" section.
  - After (this phase, run `40d835d0-...`): the final delivered answer's own
    `#### Database Tables` section explicitly lists `VoucherSeries` (line 326),
    `Voucher` (line 350), `VoucherVersion` (line 402), sourced from the real
    Researcher delegation that read `models.py`.
  - Direct journal proof the NEW code path fired (not inferred from the answer
    text alone): `[team] DETERMINISTIC_COMPARISON_GATE_FIRED: match=3
    partial_match=2 left_only=4 right_only=43 ambiguous=0
    model_made_completeness_claim=False generative_reconciliation_bypassed=True
    ...` immediately followed by `[team] comparison gate: preserving 15 unrelated
    section(s) from the original draft alongside the deterministic synthesis` — the
    second line is this phase's own new print statement; it cannot appear unless
    `_sections_without_completeness_claim` ran and returned a non-empty list.
  - The deterministic comparison result itself (MATCH/PARTIAL_MATCH/LEFT_ONLY/
    RIGHT_ONLY/AMBIGUOUS counts) is unchanged and remains first in the answer,
    confirming the gate's own authority over the completeness dimension was not
    weakened.
- **Incidental observation (not a regression, not fixed this phase):** 15 preserved
  sections is more than the single "Database Tables" block alone — the original
  draft contained a near-duplicate pair of "Vouchers Module Audit" sections (an
  artifact of the pre-existing repetition-truncation-completion mechanism, Z16,
  "preserving pre-repeat chars alongside" its own completion), and
  `_sections_without_completeness_claim` correctly does not deduplicate
  near-identical-but-not-identical blocks — this is the SAME pervasive
  duplicate-content pattern named in §3.5/T7, observed here as redundant bulk in
  the final answer rather than a factual error. Flagged, not fixed, this phase (see
  §5).
- **Verdict: PASS, directly live-confirmed** with both outcome evidence (the
  section is now present) and mechanism evidence (the new log line fired).

### 3.4 verify_claims remaining issues (Phase 4, high)

**4A — T10, negation-blind absence claims:**
- **Historical symptom (per the mission's own framing):** a correct "no
  rate-limiting middleware" answer was flagged as unverified by "a negation-blind
  containment guard."
- **Investigation:** two candidate mechanisms exist in the codebase for this exact
  symptom. (1) `hive-mcp/tools/verify.py`'s ABSENCE CLAIMS section — fixed by T51's
  own A3 (cross-service scope blindness, not negation blindness; already deployed,
  not re-touched this phase). (2) `swarm/team.py`'s
  `_affirmed_term_absent_from_citations`/`_affirms_existence` — a separate,
  pre-existing guard for the OPPOSITE failure (an answer affirming existence
  without supporting citations), already independently hardened (commit `1096125`,
  2026-09-07, predating this entire remediation program) with an explicit negative
  lookahead for "there is no/not/none/nothing" and a negated-verb exclusion for the
  declarative form. No code change was identified as necessary in either mechanism
  for this specific symptom.
- **Targeted live scenario:** T10 verbatim, `read_only=True`, `swarm_team=engineering`.
  MCP task `k01l1azh3`, session `d79f4b7a-...`, 264.41s.
- **Result:** "There is no rate-limiting middleware in the authentication service.
  Instead, rate limiting is implemented as a function called `check_login_rate_limit`
  ..." with real, grounded citations (`authUtil/authHelper.py`,
  `router/auth_service_api.py`, `UserLoginService.py`). **Zero verifier disclaimer
  of any kind in the delivered answer** — no "ASKED ABOUT A MIDDLEWARE" banner, no
  unverified-affirmation flag, no cross-service contradiction.
- **Verdict: PASS, live-confirmed already fixed** — no code change made or needed
  this phase.

**4B — T2, prose contradicting deterministic comparison:**
- **Historical symptom:** a regenerated (post-repetition-truncation) draft's own
  "Missing Hooks" prose named 2 real, existing hooks as missing, directly
  contradicted by the SAME run's own `compare_enumerations` output appended
  immediately after it.
- **Investigation:** traced the full pipeline order
  (`_complete_repetition_truncated_answer` → `_computed_comparison` →
  `_reconcile_completeness_claim_with_comparison`, all within `_verified_answer`,
  confirmed Z16's own completion runs BEFORE reconciliation, not after — ruling out
  an ordering/flag-skip explanation). The most plausible remaining explanation:
  `_reconcile_completeness_claims`' own lexicon is built to recognize blanket
  completeness assertions ("there are no missing items") worth reconciling against
  a contradicting left_only/right_only count — T2's actual defect shape (specific,
  wrongly-named missing items, not a blanket claim) may not match that lexicon at
  all, meaning `claims` is empty and the gate's own early-return (`if not claims:
  return content, result, False`) lets the contradictory prose ship unreconciled.
- **Why not fixed this phase:** this explanation is plausible but not
  live-confirmed against the real T2 journal (not retained from the original
  incident), and extending the completeness-claim lexicon to recognize
  specific-named-item claims risks exactly the kind of broad, under-verified change
  the mission's "do not make speculative changes" constraint exists to prevent. Not
  attempted this phase.
- **Verdict: not investigated to a fixable conclusion — deferred, flagged as an
  outstanding risk (§5).**

**4C — the four T51-fixed verify_claims cases:**
- Smoke-checked directly against the redeployed `hive-mcp` container this phase (no
  full 340-test rerun, per the mission's own efficiency instruction): all four
  (bare-word-in-backticks, fenced-block filename splitting, cross-service
  absence-claim scope blindness, decorator-citation quote mismatch) confirmed still
  clean, via the same scratchpad harnesses T51 built, re-run against the container
  recreated this phase. **Verdict: PASS, preserved.**

### 3.5 Duplicate content / T7 count integrity (Phase 5, high)

- **Historical symptom:** duplicate content observed in 9/14 historical scenarios;
  in T7 specifically, inflated the apparent file count, correctly caught by the
  existing `COUNT DISAGREES` disclosure (by the battery's own scoring rule:
  Accuracy FAIL, Containment PASS — the containment layer was never broken).
- **Investigation:** confirmed `_miscounted_listing`'s disclosure-only design is
  deliberate (rewriting the number would require adjudicating which reading of the
  listing the task wanted, which the function explicitly refuses to do) and remains
  correct. Investigated whether the existing large-block repetition detector
  (commit `2f695ae`, 2026-09-12, already active throughout T48/T48-rerun) covers
  this shape; confirmed it targets session-level CONTEXT-WINDOW overflow from
  accumulated repeats across a transcript, a different concern from a single
  answer's own internal duplication. Did not isolate the single layer responsible
  for duplicate content entering a single answer (candidates considered: the
  model's own generation behavior, which is not fixable at the orchestration layer;
  the "additive, never destructive" design of several recovery mechanisms, e.g.
  Z16's own "preserving pre-repeat chars alongside" behavior, confirmed as a
  deliberate, documented design choice elsewhere, not an accidental duplication
  bug) — observed again this phase as the 15-section preservation in §3.3's T13b
  run (near-duplicate "Vouchers Module Audit" blocks), consistent with Z16's own
  known behavior rather than a new finding.
- **Why not fixed this phase:** no single, narrow, non-speculative fix point was
  established. The mission's own constraint against speculative changes and
  against broad refactoring of the repetition/recovery machinery took priority.
- **Verdict: not fixed — the existing disclosure/containment layer (verified still
  intact) is the only mitigation in place; root cause remains an outstanding risk
  (§5).**

### 3.6 No-op correction retry transparency (Phase 6, high)

- **Historical symptom:** 4 occurrences (T2, T9, T12, T13b) where a second,
  byte-identical submission to `verify_claims` correctly skipped re-verification but
  the final response carried only a generic "treat the claims above as unverified"
  disclaimer, with no way to recover which specific claims were actually in
  question — the first report's own findings were discarded the moment the
  no-op counter incremented.
- **Root cause / fix:** `hive-mcp/tools/verify.py` tracked only a per-answer COUNT
  (`_checked_answer_counts`), never the first check's own report text.
  `_checked_answer_reports` (new) retains it; the STOPPED message appends it
  (required "STOPPED"/"could NOT be found" text unaltered, only extended);
  `swarm/team.py`'s `_reader_facing_report` surfaces the appended findings in the
  human-facing rewrite instead of falling back to the old blanket sentence.
  Guarded by a new `_staged_fingerprint` comparison (snapshot of `_staged_files()`'s
  own current state) so that a text collision between two UNRELATED callers —
  different project state underneath, despite identical answer text — falls through
  to a genuine fresh check instead of relaying a stale, unrelated report.
- **A real regression caught and fixed before deployment:** the first
  implementation (report enrichment without the fingerprint guard) broke 4
  pre-existing hive-mcp tests on the first full-suite run after implementation,
  root-caused to `tests/test_verify_staged_lint.py`'s
  `test_forbidden_pattern_in_staged_file_is_caught_with_no_fenced_code_in_answer`
  and `test_clean_staged_file_reports_no_violation` sharing byte-identical fixture
  answer text while staging genuinely different files under genuinely different
  `tmp_path` roots — exactly the "two different callers whose answer text collides
  by coincidence" case `_checked_answer_counts`' own pre-existing comment already
  named as unresolved. The fingerprint guard (keyed on real staged-file paths,
  which differ by `tmp_path` across the two tests) closes this cleanly; re-run
  confirmed 0 new failures (exact failing-test-name diff, both suites, both
  directions). Not a test file was touched to achieve this — the fix is entirely in
  production code (`verify.py`).
- **Commit:** `71c6aa7`. **Deployed:** `hive-mcp` container, confirmed byte-identical
  source via CRLF-normalized diff.
- **Targeted live scenario:** a two-call sequence (identical fabricated claim,
  submitted twice) run directly against the live, redeployed container. First call:
  normal NOT FOUND report. Second call: `verify_claims STOPPED: ... could NOT be
  found ...` followed by `The previous report's own findings, unchanged since that
  check:` and the full original SYMBOLS/VERDICT block, byte-identical to the first
  call's own report.
- **Verdict: PASS, live-confirmed** against the deployed container (not a full
  production battery run — no in-flight production scenario happened to produce a
  genuine no-op retry during this phase's window, so the mechanism itself is
  confirmed correct against the container directly rather than observed firing
  inside a live multi-agent run; this is a materially weaker form of "live" than
  §3.3's in-production confirmation and is reported as such rather than
  overclaimed).

### 3.7 Delegation-limit behavior across T1/T11 (Phase 7)

Reuses the Phase 1 mechanism (`_mark_unsourced_filenames_inline` + the existing
delegation-volume-limit's own forced-answer instruction); no second, parallel
implementation was created, per the mission's explicit instruction. Not
independently live-validated against T1/T11 specifically this phase (time budget);
the T12 run in §3.1 exercises the identical volume-limit code path T1/T11 also hit
historically, and showed the limit firing correctly (forced a partial answer with
an explicit "Uncovered Parts" section) without introducing a new defect.

## 4. Deployment evidence

- Commit `71c6aa7` pushed to `origin/main` (`4553394..71c6aa7`).
- ZGX `agno-api.service`: `git pull` fast-forwarded `c4ac69d..71c6aa7` (picking up
  both this phase's commit and the already-pushed-but-not-yet-pulled `4553394` from
  the prior phase in one step), `systemctl --user restart`, confirmed via a
  genuinely new PID (`3151801`, `ActiveEnterTimestamp` matching the restart), and
  `git rev-parse HEAD` on ZGX matching `71c6aa7` exactly.
- `hive-mcp` container (local, port 9003): new image built by CI from `71c6aa7`
  (created `2026-10-22T14:28:57Z`, confirmed newer than the pre-phase image),
  recreated via `docker compose -f docker-compose.hive.yml up -d hive-mcp` from the
  correct (EkamApp) directory, healthy, deployed `verify.py` confirmed
  byte-identical to the commit via CRLF-normalized diff. `hive-mcp-agno` (port 9004)
  confirmed untouched throughout (same container ID, uptime unaffected by the
  recreate).
- Three real production runs (T12, T13b, T10) were executed against this exact
  deployed commit after both deployments completed, not before.

## 5. Performance assessment

| Scenario | This phase | Closest historical baseline | Delta |
|---|---|---|---|
| T12 (architectural overview) | 1097.0s | 1079.0s (original T48) | +1.7%, within normal run-to-run variance for this non-deterministic scenario |
| T13b (vouchers audit, files named) | 553.2s | 510.5s (T48-rerun) / 564.9s (T51's own post-fix validation) | within the same range observed across all three prior measurements of this exact scenario |
| T10 (middleware negation) | 264.4s | not separately timed in prior reports | no regression signal (first timed measurement) |

No tool-call-count or delegation-count regression was observed in any of the three
runs relative to their own historical shape (T12: still hit the same 12-delegation
volume limit; T13b: still exactly 3 delegations, endpoints/tables/hooks; T10: normal
single-member delegation). The Phase 3 fix's own appended-sections mechanism added
no measurable overhead (a markdown-heading regex scan over an already-in-memory
string) and no additional tool/model call. The Phase 1 and Phase 6 fixes are pure
string post-processing with no new I/O. The Phase 2 fix adds one dict key write per
existing 30s heartbeat tick (already an existing write) and one float comparison per
existing poll-loop snapshot read (already an existing read) — no new I/O, no new
call. No metric in this table was invented; each is a directly observed, logged
duration from this phase's own live runs or the cited historical report.

## 6. Outstanding risks

1. **T3's root cause remains unconfirmed.** This phase's own fix closes a real
   observability/detection gap (a frozen heartbeat snapshot can now be recognized
   and acted on independently of the worker's own self-report) but does not, and
   was not intended to, explain WHY a worker freezes or dies in the first place. The
   two ranked hypotheses from the prior fork investigation (vLLM orphaned-request
   accumulation; the now-closed snapshot-staleness gap, which was a contributing
   detection failure, not necessarily the root freeze cause itself) remain open.
2. **T12's completeness-under-budget-exhaustion behavior is improved in disclosure,
   not eliminated.** The underlying delegation-budget-exhaustion dynamic (routers
   section incomplete, business-service integration question dropped) still
   occurred in this phase's own live run; it is correctly disclosed by existing,
   unrelated guards, which is the mission's own stated acceptance bar for this
   condition ("must report... identify material gaps... not represent an incomplete
   inventory as complete") — but the task still does not get FULLY answered under
   budget pressure. This is a scope distinction, not a gap in the fix delivered.
3. **T2's prose-vs-comparison contradiction is not fixed.** A plausible mechanism
   (the completeness-claim lexicon not recognizing specific-named-item claims) is
   identified but not live-confirmed or implemented. The existing trailing
   `compare_enumerations` append with its "trust this one" framing remains the only
   mitigation for this specific shape.
4. **Duplicate content / T7's root cause is not isolated.** The disclosure layer
   (`_miscounted_listing`, and `_forwarded_gap`'s own carry-detection) remains the
   only mitigation. The §3.3 T13b run's own 15-preserved-sections result is a live,
   current-day instance of this same pattern (redundant, not incorrect).
5. **Genuine same-member concurrency remains unproven.** Not exercised by this
   phase's own live validation (all three runs were single-member-at-a-time,
   matching every scenario this battery has ever run); explicitly out of scope per
   the mission's own instruction unless a scoped fix directly required it, which
   none did this phase.
6. **The no-op-retry fix's live confirmation (§3.6) is container-level, not
   in-production-run-level** — no in-flight production scenario happened to trigger
   a genuine no-op retry during this phase's validation window. The mechanism is
   correctly deployed and directly exercised against the live container; a future
   battery run that happens to trigger a no-op retry would be the first
   in-production confirmation.

## 7. Git safety

- Branch/HEAD/worktree/staged changes inspected before any change this phase.
- No destructive command used: no `reset --hard`, no `clean`, no force-push, no
  amend, no broad `add -A`/`add .`. Every stage was an explicit `git add <path>` for
  the four reviewed files only.
- Two stray, empty (0-byte), self-created artifact files (`hive-mcp/2`, `hive-mcp/str`
  — shell-redirection accidents from this phase's own command history) were found,
  confirmed empty and freshly created this session, and removed as cleanup; nothing
  else was touched.
- Every pre-existing dirty/untracked file (T24's own work, the training-track files,
  the two untracked T18/T19 test files) confirmed present, unmodified, and unstaged
  both before and after this phase's commit.
- No unit-test file was added, edited, or deleted. The one regression this phase
  caught (§3.6) was fixed entirely in production code, specifically because test
  files could not be touched.

## 8. Artifact

- Code: commit `71c6aa7df19182db2f239c79185e5bc33b7f0f7f`, `api/server.py`,
  `config/config.py`, `hive-mcp/tools/verify.py`, `swarm/team.py`, pushed to
  `origin/main`.
- This report: `docs/runbooks/architecture-acceptance-runs/T52-report.md` (new
  file; `T48-report.md`, `T48-rerun-report.md`, `T51-report.md` untouched).
- No new telemetry file this phase (no `runs.jsonl` append) — the three live
  validation runs' own MCP task IDs and session IDs are recorded inline in §3
  above, consistent with how the mission's own validation evidence was requested.
