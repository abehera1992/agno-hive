# Phase 0.50 — Repetition-Stalled Correction Salvage and Final Coverage Reconciliation

Implementation phase. Fixes the two problems the Phase 0.49 live T11 run
established: a chain-coverage retry that does real, substantive work can
still be discarded WHOLESALE over a second, independent repetition stall in
its own closing paragraph, and the unresolved-component disclosure reused a
pre-retry coverage snapshot that could misreport a component the retry
actually investigated.

## 1. Phase 0.49 failure recap

Phase 0.49's live T11 run (task `kre0m4jjm`, run `64392b241a6b`) proved its
new chain-coverage gate worked exactly as designed through step 7: it
detected 13 uncovered components, launched a bounded tool-enabled retry via
`_stream_team_run`, that retry made a real second `delegate_structured_task`
call, the Researcher read `API/storage-service/router/storage_api.py` and the
real frontend upload files, and the retry's own synthesis correctly wrote up
3,159 characters describing the real upload flow.

Then, specifically while writing its closing "Flow Summary" paragraph, the
retry's own generation hit the SAME chronic repetition stall this project has
repeatedly documented — a second time, independent of the original draft's
own earlier stall. `_complete_repetition_truncated_answer` (Phase 0.44,
unmodified) could not produce a version of this text that ends at a natural
stopping point, so an `_INCOMPLETE_ANSWER_MARKER`-bearing disclosure was
attached to it. The pre-existing rejection check in
`_attempt_chain_coverage_correction` —
`if ... or _INCOMPLETE_ANSWER_MARKER in retried: return content + <disclosure>`
— is binary: it discarded the ENTIRE retry, all 3,159 correct characters
included, and fell back to the pre-retry draft plus a disclosure computed
from `uncovered_before`, a snapshot taken BEFORE the retry ran. The final
shipped answer was `(no response)` plus disclosure banners.

## 2. Exact source-level root cause of wholesale retry rejection

`swarm/team.py`, `_attempt_chain_coverage_correction`, the branch (pre-Phase
0.50):

```python
if not retried or not _strip_leaked_tool_tags(retried).strip() \
        or retried.strip() == _BUDGET_EXHAUSTED_ANSWER \
        or _INCOMPLETE_ANSWER_MARKER in retried:
    print("[team] chain-coverage-correction retry produced nothing usable", ...)
    return content + _chain_coverage_unresolved_note(uncovered_before)
```

Four genuinely different "nothing usable" conditions (empty, leaked tool
tags, budget-exhausted stub, incomplete-answer marker) were folded into one
all-or-nothing OR, matching every sibling correction function's own
all-or-nothing policy — which is correct for THEM (they already have their
own narrow exceptions: `_salvage_resolved_category_section`,
`_sanitize_claim_specific_rejections`, both on `_attempt_coverage_correction`)
but this gate had no equivalent narrow exception for the fourth condition,
and every disclosure return in the function used the stale `uncovered_before`
snapshot instead of the live delegation log's current state.

The safety guarantee that check protects — never adopt a narrative
conclusion the model never actually finished reaching, never let a
repetition-decayed tail ship as if it were a real answer — is sound and is
preserved untouched by this phase. What was missing was a narrower exception
for the case where the retry's OWN earlier text, before the stall began, is
independently complete and verifiable on its own terms.

## 3. Starting and ending commits

| | Value |
|---|---|
| Local HEAD (start) | `e85372b` |
| Origin HEAD (start) | `e85372b` (matched local) |
| ZGX HEAD (start) | `e85372b` (matched local/origin) |
| Local/origin dirty+untracked count (start) | 78 (pre-existing backlog, confirmed unchanged by this phase — see §4) |
| ZGX dirty+untracked count (start) | 35 |
| Implementation commit | `6b55ebd` ("fix(swarm): salvage repetition-stalled chain-coverage retries + reconcile final coverage from live log (T11)") |
| Pushed | `origin/main`, `e85372b..6b55ebd` |
| ZGX HEAD (end) | `6b55ebd` (fast-forwarded, `--ff-only`) |
| Local/origin dirty+untracked count (end) | 79 (78 pre-existing + this report, before its own commit) |
| ZGX dirty+untracked count (end) | 35, unchanged |

`git status --short swarm/ tests/` before committing showed only
`swarm/team.py` modified (plus pre-existing, untouched dirty entries in
`tests/test_evidence_fidelity.py` and two untracked `tests/test_t18_*`/
`test_t19_*` files that were already present before this phase and were
never staged, edited, or run). `git add swarm/team.py` (never `git add -A`)
staged exactly one file; `git diff --cached --stat` confirmed 162 insertions,
14 deletions before the commit was made. No pre-existing dirty or untracked
file was touched, reset, cleaned, or stashed, on local or on ZGX.

## 4. Dirty/untracked preservation evidence

Local, before this phase's own edit: 78 entries (31 deleted `T*-report.md`
files, 4 modified files, 43 untracked files/dirs) — the exact same backlog
Phase 0.49's own report recorded as "78 / pre-existing backlog, untouched."
After `git add swarm/team.py && git commit`: `git status --short` showed the
identical 78 entries, now joined by this report file itself (untracked, 79
total) until its own commit below. ZGX: 35 before `git fetch && git merge
--ff-only`, 35 after, confirmed by direct count both times — the merge
touched only `swarm/team.py` (the fast-forward's own diff, 162
insertions/14 deletions, nothing else).

## 5. Exact files and functions changed

`swarm/team.py` only, 162 insertions, 14 deletions (confirmed by diff —
every pre-existing line outside `_attempt_chain_coverage_correction` itself,
including `_attempt_integration_correction`, `_attempt_coverage_correction`,
`_component_roots`, `_is_chain_question`, and all four `_move_on_hint` call
sites, is byte-for-byte unchanged):

1. **`_strip_incomplete_answer_note(text)`** (new): removes a trailing
   `_TRUNCATED_ANSWER_NOTE`/`_SEMANTIC_STALL_TRUNCATED_NOTE`/
   `_INACTIVITY_TIMEOUT_TRUNCATED_NOTE` suffix, if present — an exact
   substring match, since every site that attaches one of these three notes
   does so via plain string concatenation, never reworded.
2. **`_salvage_truncated_prefix(text)`** (new): the longest leading,
   blank-line-paragraph-delimited prefix of `text` (note stripped first)
   that genuinely `_ends_at_a_natural_stopping_point` (reused, unmodified)
   and has no dangling open code fence, subject to a 30%-of-original-length
   retention floor. Returns `None` — never completes, guesses, or rewrites
   anything — when nothing qualifies.
3. **`_attempt_chain_coverage_correction`** (modified, in place): the
   rejection branch now computes `uncovered_now` once (from the live
   delegation log) immediately after the retry/completion sequence, tries
   `_salvage_truncated_prefix` before giving up when the marker is present,
   and reuses the existing, unmodified `_sanitize_claim_specific_rejections`
   for claim-level isolation on a salvaged candidate exactly as
   `_attempt_coverage_correction` already does for an identical shape. Every
   disclosure return in the function now uses `uncovered_now` instead of the
   stale `uncovered_before`; `uncovered_before` is kept only for the
   progress-delta comparison, unchanged.

No change to `_attempt_integration_correction`, `_attempt_coverage_correction`,
`_component_roots`, `_is_chain_question`, `_uncovered_components`,
`_covered_targets`, `_move_on_hint` or any of its four call sites,
`_complete_repetition_truncated_answer`, `_current_truncation_reason`,
`_truncation_note_for_reason`, `_has_usable_current_generation_evidence`,
`_revalidate_adopted_correction`, or `_verify_claims`.

## 6. Salvage design and claim-provenance model

`_salvage_truncated_prefix` operates at blank-line-paragraph-block
granularity — the same unit `_section_for_category`/
`_without_forwarded_members_section` already treat as "one piece of the
answer." It tries the smallest drop first (the trailing block alone),
widening one block at a time, and accepts the first candidate that both ends
naturally AND has a balanced count of ```` ``` ```` fences. Provenance is
structural, not inferred: the salvaged text is a byte-for-byte prefix of
what the model actually generated — nothing is added, reworded, or
completed. The caller (`_attempt_chain_coverage_correction`) then routes the
salvaged candidate through the EXACT SAME coverage-progress gate and
`verify_claims` recheck a clean (non-stalled) retry already goes through,
so claim verification is never duplicated or special-cased for a salvage —
it is the same check, on a (possibly shorter) candidate.

## 7. Handling of incomplete sentences, fragments, conclusions, unsupported claims

- **Incomplete sentence** (mission Scenario C): the last paragraph block
  containing it is dropped entirely; `_salvage_truncated_prefix` never
  splits mid-sentence.
- **Unfinished code block/route declaration** (Scenario D): a candidate
  with an odd `` ``` `` count is skipped even if its tail otherwise reads as
  complete.
- **Unfinished final conclusion** (Scenario E): since the function only ever
  removes trailing material, a stalled conclusion is dropped along with its
  containing block; nothing is ever written in its place.
- **Unsupported/fabricated claims** (Scenario B): a salvaged candidate still
  goes through `_verify_claims`; a newly-introduced flagged claim is passed
  to the real, unmodified `_sanitize_claim_specific_rejections`, which
  replaces only the literal flagged phrase with a neutral
  "(path unresolved...)" marker, re-verified before adoption. If that
  isolation cannot clear the recheck, the whole salvage is rejected
  (Scenario F behavior) rather than adopting an unresolved claim.
- **No safely separable claims at all** (Scenario F): `_salvage_truncated_
  prefix` returns `None` (no paragraph break, or no candidate ever clears
  the natural-stopping-point bar, or the only candidate that does falls
  below the 30% retention floor); the caller falls back to the pre-existing
  full-rejection path.

## 8. Reconciliation of original-draft and retry content

The function never concatenates `content` and `retried`. Every return is one
or the other, never both — so there is no risk of duplicating a claim
between the two or mixing evidence from unrelated generations. When the
salvage succeeds and passes the coverage-progress gate and `verify_claims`,
the salvaged `retried` (plus the incomplete-answer disclosure and the
coverage note) is adopted in full, replacing `content`. When the salvage
fails, or the retry (salvaged or not) made no new delegation progress, or
introduces an unsanitizable new `verify_claims` problem, `content` — the
original, pre-retry draft — is returned unchanged, exactly as before this
phase.

## 9. Current-generation evidence retained independently of prose adoption

`uncovered_now = _unresolved_chain_components(log, team)` is computed once,
immediately after the retry/completion sequence, directly from
`team._coordinator_delegation_log` — the same live list object every
delegation this run (original AND retry) appends to. This computation does
not depend on whether `retried`'s prose is ultimately adopted: it reads the
log's own `target`/`result` fields, never the candidate text. A component
investigated during the retry with a real result is excluded from
`uncovered_now` whether or not the retry's own prose survives the rest of
this function — directly satisfying mission Scenarios J/K/N.

## 10. Final component-coverage reconciliation design

Every disclosure-producing return in `_attempt_chain_coverage_correction`
now uses `uncovered_now` instead of the Phase 0.49 `uncovered_before`
snapshot. `uncovered_before` survives only as the baseline for the
progress-delta comparison (`len(uncovered_now) >= len(uncovered_before)`)
and in the "N previously-uncovered component(s)" log message — a
genuinely different question ("did this retry improve on where coverage
started") that recomputing the disclosure value does not change.

## 11. Scratch-harness validation

One new scratch file (`test_p50_salvage.py`), calling the real, unmodified
production functions directly, plus the real `_attempt_chain_coverage_
correction` with only `_stream_team_run`/`_complete_repetition_truncated_
answer`/`_verify_claims` monkeypatched (same convention as Phase 0.49's own
harness). No unit tests were added, modified, or run; no tracked test file
was created or touched.

| Scenario | What was exercised | Result |
|---|---|---|
| A | Direct: a complete paragraph + stalled closing tail → salvage keeps the complete part, drops the tail and the note. E2e: real second delegation + salvaged adoption, disclosure preserved, coverage reconciled | PASS (direct 4/4, e2e 4/4) |
| B | E2e: salvaged candidate with one valid and one fabricated claim → fabricated claim isolated via the real `_sanitize_claim_specific_rejections`, valid claim retained | PASS (3/3) |
| C | Direct: no paragraph break at all → `None` (fail closed) | PASS (1/1) |
| D | Direct: cut inside an unfinished code fence → never adopted as a finished fragment | PASS (1/1) |
| E | Direct: salvaged text is always a literal substring of the input — nothing invented | PASS (1/1) |
| F | Direct: single undifferentiated block → `None`. E2e: nothing-salvageable retry → original content preserved, not duplicated | PASS (direct 1/1, e2e 3/3) |
| G | E2e: original has valid content, retry has no usable output → original is not lost | PASS (covered by F's own e2e, same scenario) |
| H | Not directly re-exercised this phase — unchanged by this phase's diff; Phase 0.49's own Scenario H (fabrication with no new delegation, rejected before `verify_claims`) re-passes unchanged in the re-run below | PASS (regression, 23/23) |
| I | Direct: retention floor rejects a trivial preamble once most content is the stalled tail | PASS (1/1) |
| J/K/N | E2e: retry's prose rejected wholesale (unsalvageable), but its real delegation (storage-service) and the original delegation (business-service) both remain correctly accounted for in `uncovered_now`, never reported as still-unresolved | PASS (2/2) |
| L | E2e: a component delegated for but with no usable result stays unresolved (attempted/unavailable), unaffected by a no-progress retry | PASS (1/1) |
| M | E2e: a component never delegated for at all stays unresolved | PASS (1/1) |
| O | E2e: a clean `verify_claims` result on the salvaged candidate still carries the incomplete-answer disclosure — never implies the audit is complete | PASS (1/1) |
| P | Phase 0.44 no-evidence invariant | Unchanged by diff; not re-exercised directly (this phase's harness mocks `_complete_repetition_truncated_answer` itself for determinism, same as Phase 0.49's harness did for its own analogous calls) |
| Q | Phase 0.45 router/model completeness | Unchanged by diff; not T11/T12-specific, not exercised by this function |
| R | Phase 0.46 `_revalidate_adopted_correction` | Unchanged by diff; its own call site inside this function is untouched (still fires immediately after, confirmed by diff) |
| S | E2e: the gate still fires exactly once per run and is still tool-enabled (one `_stream_team_run` call across two invocations on the same team) | PASS (1/1) |
| T | E2e: T12 (`_is_chain_question` False) → zero model calls, content returned completely unchanged | PASS (1/1) |

**Total: 33/33 new checks passed.** Re-ran the directly relevant existing
suites unchanged: `test_p49_chain_coverage.py` (23/23), `test_p45_t12_router_
inventory.py` (18/18), `test_p46_post_correction_revalidation.py` (20/20),
`test_p46_wiring_e2e.py` (3/3) — zero regressions. Per this phase's own
instruction, no broad historical suite beyond these four directly-relevant
ones was re-run.

## 12. Branches not exercised and limitations

- Scenario H (fabrication-without-delegation rejection) and the Phase 0.44/
  0.45/0.46 preservation scenarios (P/Q/R) were confirmed by diff (zero
  lines changed in the functions they protect) and by the unchanged
  pre-existing suites, not re-executed fresh against new mocks this phase —
  there was no code change in those paths to justify new direct tests.
- The exact agno tool-hook call signature for the four `_move_on_hint` sites
  (duplicate-delegation gate) was not exercised by this harness, same
  limitation Phase 0.49's own harness recorded — unaffected by this phase's
  diff regardless.
- **Live validation exercised the fail-closed salvage branch, not the
  successful-salvage branch** — see §16/§21. The live T11 run's own
  retry stalled early, during exploratory narration, before producing any
  complete section worth keeping, so `_salvage_truncated_prefix` correctly
  returned `None`. The scenario Phase 0.49's own incident actually showed
  (a genuinely complete, substantive section followed by a stalled
  conclusion) was reproduced and validated in the scratch harness (§11,
  Scenario A) but was not reproduced live this phase. Per this phase's own
  explicit instruction, the live run was not repeated, and no stall was
  deliberately induced, to try to force that shape.

## 13. No unit tests added, modified, or run

Confirmed by `git status --short tests/` before and after this phase's own
work: the only entries under `tests/` are pre-existing (one already-modified
file, two already-untracked files), present before this phase started and
never staged, edited, or executed by it. The one new scratch file lives
only in the session scratchpad directory, outside the repository.

## 14. Commit and push SHAs

| | Value |
|---|---|
| Implementation commit | `6b55ebd` |
| Pushed | `origin/main`, `e85372b..6b55ebd` |
| Report commit | (recorded after this file is committed — see closing message) |

## 15. ZGX deployment and service health

| | Value |
|---|---|
| ZGX HEAD | `e85372b` → `6b55ebd` (`--ff-only`, clean) |
| ZGX dirty/untracked | 35, unchanged before and after |
| `agno-api.service` | restarted for the code change; new PID `1351925`, active since `2026-11-06 07:04:13 EST` |
| `/health` | `{"status":"ok", ...}` |
| `/runs` | confirmed empty before the live run |

## 16. Canonical T11 prompt hash and live run identifiers

Canonical prompt, byte-exact from `docs/runbooks/groundedness-battery.md`:
> When a seller uploads a document, which services are involved end to end,
> and which function in each one handles it? Name every file in the chain.

Independently recomputed SHA-256 before launch: `7bb97b1c00b7b116223b4710042e2c0abfd4a151e0a97f49e56ee48423f25566` —
matches the mission's stated hash exactly.

| | Value |
|---|---|
| Task ID (MCP) | `kirx21980` |
| Run ID (`phase0`) | `1266cff61a5a` |
| Session ID | `6c09421a-18c2-48ae-a1d4-f68005aaa2cc` |
| Duration | 508.0s (phase0 log) / 510.2s (API footer) |
| `swarm_team` | `engineering` |
| `read_only` | `true` |
| Session | fresh, no `session_id` supplied |
| `/runs` | confirmed empty immediately before launch |
| Run count | run once, not repeated |
| Outcome (process-level) | `ok` (completed without crashing) |
| Outcome (content-level) | `VERIFICATION FAILED` / final answer `(no response)` plus disclosure banners |

## 17. Complete journal evidence for salvage and coverage reconciliation

Pulled and read start-to-finish via `journalctl --user -u agno-api.service`,
not sampled from excerpts:

1. **07:05:53** — `_attempt_chain_coverage_correction ENTER`. `chain-coverage
   check: 14 discovered component(s) never delegated for this run` — same
   working discovery mechanism as Phase 0.48/0.49. `move-on hint attached —
   1 target(s) covered so far: 'API/business-service'` (the OLDER,
   unmodified `_covered_targets`-based hint, which only checks "was this
   target named in the log," not whether it has a real result) / `13
   component(s) untouched so far`.
2. **07:06:49** — the retry's real second `delegate_structured_task` call:
   `target='API/authentication-service'`.
3. **07:07:09** — the Researcher reads `API/storage-service/router/
   storage_api.py` directly (a file read during broader exploration, not
   itself a delegation audit entry naming `API/storage-service` as a
   target — so it does not, correctly, clear storage-service from the
   unresolved set on its own).
4. **07:07:51–07:08:22** — the retry's own narration enters a repetition
   loop while repeatedly re-searching `API/authentication-service` for
   "document upload" terms (`repetition loop detected` ×4), then: `07:08:22
   — repetition threshold reached (4 >= 4) -- stopping this generation
   in-process and keeping the 494 chars generated before the repeating
   segment began`.
5. **07:08:22** — **`[team] chain-coverage-correction retry produced
   nothing usable (incomplete, nothing safely salvageable)`** — this
   phase's own new salvage path firing for real: `_salvage_truncated_
   prefix` was called on the 494-char retry (pure narration/planning text,
   no paragraph break and no complete synthesized section), correctly
   returned `None`, and the function fell through to the full-rejection
   path, now using `uncovered_now` (recomputed fresh) rather than the
   stale pre-retry snapshot.
6. **07:08:22** — `_attempt_chain_coverage_correction EXIT outcome=completed
   out_chars=1,818`. `_revalidate_adopted_correction:chain-coverage`
   correctly `skipped` (no wholesale replacement occurred).
7. **07:08:31 onward** — a SEPARATE, pre-existing mechanism
   (`_enforce_verification_invariant_final`'s own `repair (verify_claims)`
   path, unmodified by this phase) flags `NOT FOUND _is_chain_question` —
   the disclosure text this phase's unmodified `_chain_coverage_unresolved_
   note` writes names the internal function `` `_is_chain_question` `` in
   backticks, which `verify_claims`'s symbol-existence check then greps for
   in the EkamApp repository (where it does not exist, being an agno-hive
   internal name, not an EkamApp one) and flags as a fabricated symbol.
   This repair path launches its own full re-delegation ("the whole
   codebase"), itself stalls on a 180s `lightrag_query` MCP timeout, and
   ultimately (07:13:01) gives up: `recheck passed but the correction does
   not verifiably preserve every unrelated paragraph of the existing answer
   ... keeping the original, disclosed as unverified`.
8. **07:13:01** — run completes, `outcome: "ok"` at the process level; the
   shipped content is `VERIFICATION FAILED ... (no response)` plus the
   `_is_chain_question`-naming `UNRESOLVED SERVICE BOUNDARY` disclosure,
   now correctly listing all 14 components (since none of this run's
   delegations — including the retry's own authentication-service one —
   ever produced a real, materialized result before being cut off).

## 18. Independent comparison against source ground truth

The final shipped answer contains `(no response)` — no file, function, or
service claim was adopted this run, so there is nothing to check against
the real repository for THIS live run. The known Phase 0.49 reference
points (`upload_file()` at `API/storage-service/router/storage_api.py:33`,
`register_documents()` at `API/business-service/router/business_api.py:484`,
the `DocumentUploadStep.tsx`/`BusinessRegistrationForm.tsx` frontend files)
were not independently re-verified this phase since no claim referencing
them reached the final answer to check.

## 19. Phase 0.44/0.45/0.46/0.49 preservation evidence

- **Phase 0.44** (no-evidence invariant, `_has_usable_current_generation_
  evidence`): unchanged by diff; not directly re-exercised against a fresh
  live incident this phase (the live run's own repetition stalls were
  mid-generation, not the "delegation never returned at all" shape that
  invariant specifically guards — Phase 0.49's live run already confirmed
  it firing twice).
- **Phase 0.45** (router/model completeness guards): unchanged by diff; not
  exercised by T11 (not a router/model question).
- **Phase 0.46** (`_revalidate_adopted_correction`): unchanged by diff; its
  own call immediately after this phase's function correctly logged
  `outcome=skipped` in the live run (step 6 above), confirming the call
  site survived this phase's edits intact.
- **Phase 0.49** (the chain-coverage gate itself): its discovery/delegation
  design, one-shot bound, chain-question gating, and tool-enabled
  `_stream_team_run` retry are all unchanged by diff and all confirmed
  firing correctly in the live run (steps 1–4 above) — only the
  post-retry rejection/disclosure logic (steps 5–6) changed.

## 20. Known limitations and unresolved risks

1. **The live run did not reproduce the exact Phase 0.49 incident shape**
   (a complete, substantive synthesis followed by a late closing-paragraph
   stall) — it hit an earlier, narration-only stall with nothing safely
   separable, exercising this phase's fail-closed path rather than its
   adoption path. The adoption path is scratch-confirmed (§11, Scenario A)
   but not live-confirmed this phase. Per this phase's own explicit
   instruction, the run was not repeated and no stall was deliberately
   induced to try to force the other shape.
2. **A separate, pre-existing defect was newly surfaced, not fixed**: the
   unmodified `_chain_coverage_unresolved_note` text names the internal
   function `` `_is_chain_question` `` in backticks, which `verify_claims`'s
   symbol-existence check mistakes for a claimed EkamApp symbol and flags
   as `NOT FOUND`, triggering a separate, pre-existing repair cascade that
   this specific live run never recovered from. This is explicitly out of
   this phase's scope (Section 6 of the mission: preserve
   `_attempt_chain_coverage_correction`'s "existing disclosure behavior,
   except for the specific final-coverage reconciliation improvement
   required here") and was not touched. A concrete candidate for a future
   phase: reword the disclosure to avoid naming an internal agno-hive
   symbol, or extend `verify_claims`'s own `_MCP_TOOL_NAMES`-style exclusion
   list to cover this file's own internal function names the same way it
   already excludes hive-mcp's own tool names.
3. **One live run is one data point**, same caveat Phase 0.49's own report
   carried forward — whether the salvage-adoption path fires on a typical
   T11 run, or the fail-closed path like this one did, was not established
   as a stable pattern here and this phase's own "do not repeat to get a
   better result" instruction correctly prohibits pursuing that further in
   this report.
4. **The 30% retention floor and paragraph-block granularity are fixed,
   hand-chosen constants** (`_CHAIN_COVERAGE_SALVAGE_MIN_RETENTION = 0.3`),
   not derived from any measurement of real stall shapes beyond the single
   Phase 0.49 incident and this phase's own scratch scenarios. A future
   phase with more live incidents to compare against could tune this with
   real data.

## 21. Verdicts

- **Implementation**: **PASS** — two small, purely-additive helper
  functions plus an in-place rewrite of exactly one existing function's
  rejection/disclosure branches, 162 insertions/14 deletions, no change to
  any other correction function, no change to the discovery/delegation
  design, every stated non-goal respected (confirmed by diff and by the
  scratch suite).
- **Scratch validation**: **PASS** — 33/33 new checks across both
  Requirement A (salvage) and Requirement B (coverage reconciliation)
  scenarios, 23/23 + 18/18 + 20/20 + 3/3 pre-existing Phase 0.45/0.46/0.49
  checks unchanged — zero regressions. No unit tests added, modified, or
  run.
- **Deployment**: **PASS** — clean `--ff-only`, dirty/untracked state
  preserved on both local and ZGX, service healthy and restarted on the new
  commit.
- **Live salvage execution**: **PARTIAL** — the new code path executed for
  real (confirmed by its own distinctive log line) and made the CORRECT
  decision for what it was actually given (nothing safely salvageable in a
  494-character narration-only stall) — this is the fail-closed branch
  working exactly as specified, not a malfunction. It is PARTIAL rather than
  PASS because the successful-adoption branch (the one Phase 0.49's own
  incident actually needed) was not exercised by this specific live run —
  only by the scratch harness.
- **Final coverage accuracy**: **PASS** — `uncovered_now` was correctly
  recomputed from the live delegation log at finalization and correctly
  reflected that NO component's investigation actually produced a
  materialized result this run (including the retry's own
  authentication-service delegation, cut off before completion) — an
  honest, accurate report of a run where nothing was actually resolved,
  not a stale or misleading one.
- **Live T11 audit quality**: **FAIL** — the final shipped answer is
  `(no response)` plus disclosure banners, same shape as the Phase 0.49
  incident this phase set out to improve on, though for a different proximate
  reason this time (an early, pre-synthesis repetition stall combined with
  a separate, pre-existing `verify_claims`/disclosure-wording interaction,
  not the retention defect this phase fixed).
- **Fabrication containment**: **PASS** — zero fabricated service,
  function, or file names anywhere in the final answer; the only
  `verify_claims` flag was a false positive against this file's own
  internal function name, not against anything claimed to exist in
  EkamApp.
