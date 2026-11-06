# Phase 0.49 — T11 Cross-Service Coverage Gate and Pre-Finalization Recovery

Implementation phase. Fixes the gap Phase 0.48 established: a chain question
whose Coordinator delegates to only one of the preflight-discovered
components, then simply stops, with no existing mechanism giving it a
pre-finalization opportunity to cover the rest.

## 1. Objective and Phase 0.48 root-cause recap

Phase 0.48 established, from the live Phase 0.47 T11 incident (task
`k5gy71k8d`) and direct source inspection:

1. Service-boundary discovery works correctly — `_component_roots()` finds
   all 14 real components, `_is_chain_question()` correctly classifies T11,
   and the preflight injects the full list with an explicit "examining one is
   not an answer" warning.
2. The Coordinator nonetheless delegated to exactly one component
   (`API/business-service`) and never attempted a second delegation.
3. The one existing remediation for under-coverage — `_move_on_hint`'s
   unseen-component reminder — is wired only into the duplicate-delegation
   gate's four call sites, so it never fires for a Coordinator that does not
   repeat a delegation.
4. When the Coordinator's own in-between narration hit a repetition stall,
   the only recovery path (a bounded, tool-less text completion) fabricated
   three non-existent services; `verify_claims` correctly rejected them.

This phase closes gap (3) with a bounded, pre-finalization, tool-enabled
coverage check, without touching (1), (4), or anything Phase 0.44/0.45/0.46
already protect.

## 2. Starting and ending commits

| | Value |
|---|---|
| Local HEAD (start) | `fa888da` |
| Origin HEAD (start) | `fa888da` (matched local) |
| ZGX HEAD (start) | `107ec2a` |
| Local/origin dirty count (start) | 78 / — (pre-existing backlog, untouched) |
| ZGX dirty count (start) | 35 (unchanged throughout) |
| Implementation commit | `d77eef1` ("fix(swarm): bounded chain-coverage gate for cross-service questions (T11)") |
| Pushed | `origin/main`, `fa888da..d77eef1` |
| ZGX HEAD (end) | `d77eef1` (fast-forwarded, `--ff-only`, dirty count unchanged at 35) |

`git status --porcelain swarm/` before committing showed only the intended
file; `git add swarm/team.py` (never `git add -A`) staged exactly one file;
`git diff --cached --stat` confirmed 265 insertions, 0 deletions before the
commit was made. No pre-existing dirty or untracked file was touched, reset,
cleaned, or stashed.

## 3. Exact files and functions changed

`swarm/team.py` only, 265 insertions, 0 deletions (confirmed by diff — every
pre-existing line, including all four `_move_on_hint` call sites and
`_uncovered_components`/`_covered_targets` themselves, is byte-for-byte
unchanged):

1. **`_make_duplicate_delegation_gate_hook`** (one existing append site): two
   lines added immediately after its existing `log.append({...})` call —
   `if team is not None: team._coordinator_delegation_log = log`. Exposes the
   real, mutable per-run delegation log (previously closure-local only, per
   that code's own "there is no `team._delegation_log`" comment) so a later,
   independent function can read it. No existing read of `log` is affected.
2. **`_unresolved_chain_components(log, team)`** (new): calls the existing,
   unmodified `_uncovered_components` and adds exactly one more case it does
   not cover — a component with a delegation-log entry but no non-empty
   `"result"` on any of its entries (an attempted-but-unavailable
   investigation, not evidence of relevance).
3. **`_chain_coverage_unresolved_note(components)`** (new): the disclosure
   banner, reusing `_move_on_hint`'s own vocabulary for "components with no
   adequate investigation evidence."
4. **`_attempt_chain_coverage_correction(...)`** (new): the gate itself,
   placed in the same family as `_attempt_integration_correction`/
   `_attempt_coverage_correction`.
5. **Wiring**: one call to the new function plus one call to the existing,
   unmodified `_revalidate_adopted_correction` immediately after it, inserted
   right after `_attempt_coverage_correction`'s own existing revalidation
   call in the post-`_verified_answer` guard chain.

## 4. Design decision — interception point

**Chosen point: a post-`_verified_answer`, pre-finalization correction
function, run after `_attempt_coverage_correction` and its own Phase 0.46
revalidation, using `_stream_team_run` for the retry (a full `team.arun(...,
stream=True)` call on the same team object — genuine tool access, confirmed
by reading its implementation — not the tool-less bounded completion used
for mid-generation repetition recovery).**

Why this is correct and not merely convenient:

- It runs **after** the Coordinator's own synthesis has settled into
  `content`, so the check operates on what the run actually produced, not a
  speculative mid-generation snapshot.
- It runs **before** any later stage that could discard it unnoticed — it is
  the last correction in the chain, followed only by
  `_attempt_evidence_grounded_reconstruction` (a true no-op unless content
  starts with `_VERIFICATION_FAILED_PREFIX`, confirmed by reading its guard
  clause), `_enforce_verification_invariant_final` (re-verifies, does not
  discard evidence-based disclosures), `_with_forwarded_evidence` (additive,
  Phase 0.24 precedent), and `_completion_control_check` (appends one narrow
  disclosure only) — none of which perform a wholesale content replacement
  that could silently drop this phase's disclosure.
- `_stream_team_run` gives the Coordinator its **normal tool surface**,
  including `delegate_structured_task` — this is the one property a
  narration-level prompt instruction or a mid-generation hint cannot provide,
  since both arrive either before any tool call has happened (the preflight,
  already present and already insufficient per Phase 0.48) or during a
  tool-less bounded completion (the repetition-recovery path, deliberately
  left untouched by this phase per its own constraint).
- Alternatives considered and rejected: a prompt-only instruction (already
  tried once, by the preflight itself — confirmed insufficient this exact
  incident); wiring into the duplicate-delegation gate's existing hint (would
  require the Coordinator to already be repeating itself, the opposite of
  this failure's shape); forcing a tool_choice the way Phase O does for
  `forward_member_answer` (would need a NEW required-tool policy entry naming
  `delegate_structured_task` unconditionally for every chain question,
  removing the Coordinator's own judgment about whether further delegation is
  warranted at all — rejected as broader than the mission's "bounded, not
  forced" requirement).

## 5. Current-generation investigation evidence

**Not** `team._member_results`/`_delegation_generation` (Phase 0.44's own
per-member staleness model) — those are keyed by member role, not by
component, and this project has one Researcher member handling many
different component targets, so a member-level store cannot distinguish
"researcher investigated business-service" from "researcher investigated
storage-service."

**Instead**: the real, per-call delegation log (`team._coordinator_delegation_log`,
Section 3.1), each entry carrying its own `target` (from `audit`) and, once
the delegation materializes, its own `result` text (the existing FIFO/run-id
bridge already writes this — confirmed by reading `_pending_entry["result"] =
text` and tracing it back to the same `log[-1]` object the duplicate-
delegation gate appends). A component counts as having adequate evidence only
when at least one of its own log entries carries a real, non-empty result —
never from narration text, the preflight's own component list, a different
component's delegation, or `team._member_results`.

## 6. Distinguishing relevant / irrelevant / unavailable / unresolved

| Mission state | How this phase determines it |
|---|---|
| 1. Investigated and relevant | A log entry for the component has a real result. This phase does not parse that result's conclusion — the mission explicitly assigns "is it relevant" to the model's own evidence-backed judgment, not to a mechanical check. |
| 2. Investigated and not relevant | Same signal as state 1 (a real result exists) — the gate cannot and does not distinguish 1 from 2 mechanically; both simply drop out of the unresolved set. Scratch Scenario C confirms this directly: a component investigated with a result that concludes "not relevant" is correctly excluded from the unresolved set. |
| 3. Uninvestigated | No log entry names the component at all — `_uncovered_components`'s own existing output, reused unmodified. |
| 4. Attempted but unavailable | A log entry names the component, but none of its entries has a non-empty result — the one case `_unresolved_chain_components` adds on top of the unmodified function. Confirmed by scratch Scenario D. |
| 5. Unresolved relevance | Whatever remains in `_unresolved_chain_components`'s output after the one bounded retry — explicitly disclosed, never silently treated as excluded. |

## 7. Bounding additional investigation

- One-shot per run: `_CHAIN_COVERAGE_CORRECTION_FLAG`, same convention as
  `_INTEGRATION_CORRECTION_FLAG`/`_COVERAGE_CORRECTION_FLAG`.
- Exactly one `_stream_team_run` call regardless of how many components are
  uncovered — never a per-component retry, never a forced delegation to all
  14.
- Progress is measured once, by re-diffing the real log before vs. after —
  no iteration, no recursive re-invocation of the gate itself.
- No new repetition threshold, timeout, or watchdog — reuses
  `_current_truncation_reason`/`_complete_repetition_truncated_answer`
  exactly as every sibling correction function already does.

## 8. Repetition and tool-less completion handling

The retry's own generation can hit the identical repetition stall the
original draft did — confirmed live this phase (Section 10.3). Handled
identically to every sibling correction function: `_complete_repetition_
truncated_answer` is called on the retry's text, unmodified, and if the
result still does not end at a natural stopping point, the SAME
`_truncation_note_for_reason` disclosure (carrying `_INCOMPLETE_ANSWER_MARKER`)
every other correction function attaches is attached here too — and this
phase's own "nothing usable" check then rejects it, same as its siblings
would. The tool-less completion is never given delegation capability, and
a retry that only narrates — confirmed by scratch Scenario H, using a mock
that fabricates service names with zero new log entries — is rejected
**before `verify_claims` is even consulted**, purely from the log diff, and
the fabricated text never reaches the final answer.

## 9. Scratch-harness validation

Two scratch files, both calling the real, unmodified production functions
directly. No unit tests were added, modified, or run; no tracked test file
was created.

**`test_p49_chain_coverage.py` — 23 checks, scenarios A–L plus two
supplementary checks (one-shot bounding, successful-progress adoption):**

| Scenario | What was exercised | Result |
|---|---|---|
| A | Only business-service investigated; retry makes no progress → disclosure attached, retry text not adopted | PASS (4/4, e2e) |
| B | Both business-service and storage-service with real results → gate silent, zero model calls | PASS (2/2, e2e) |
| C | A real result with a negative conclusion → not left unresolved | PASS (1/1, direct) |
| D | A log entry with no usable result (empty/`None`) → remains unresolved | PASS (2/2, direct) |
| E | No content argument exists for this function at all — narration structurally cannot influence it; confirmed a component absent from the log is unresolved regardless | PASS (1/1, direct) |
| F | Preflight-list membership alone (the starting condition of every scenario here) never counts as covered on its own | PASS (covered by A/B/D's own setup, direct) |
| G | `team._member_results` holding unrelated/stale text does not satisfy coverage | PASS (1/1, direct) |
| H | Repetition-completion-shaped fabrication with no new delegation → rejected before `verify_claims`, fabricated names never reach the answer | PASS (3/3, e2e) |
| I | T12 (`_is_chain_question` is False) → zero model calls, content untouched | PASS (3/3, e2e) |
| J | Existing duplicate-delegation behavior | Not re-executed at runtime (its four call sites require full agno tool-hook plumbing this harness does not construct) — confirmed instead by diff: `_move_on_hint`/`_uncovered_components`/`_covered_targets` and all four call sites are byte-for-byte unchanged (Section 3) |
| K | Phase 0.44 no-evidence invariant | Not re-executed directly in this harness — confirmed unchanged by diff; its live behavior is independently re-confirmed in Section 10.3 (it is this exact invariant's sibling mechanism, `_complete_repetition_truncated_answer`, correctly firing during the live run) |
| L | Phase 0.45/0.46 behavior | Re-ran existing `test_p45_t12_router_inventory.py` (18/18), `test_p46_post_correction_revalidation.py` (20/20), `test_p46_wiring_e2e.py` (3/3) unchanged — zero regressions |
| (extra) | Bounded execution: two calls on the same team object | Exactly one retry fires | PASS |
| (extra) | A retry that DOES gain real delegation coverage | Adopted, no disclosure once complete | PASS |

**Total: 23/23 new checks passed, 41/41 pre-existing checks (Phase 0.45/0.46)
still pass. Branches NOT exercised by the harness: the exact agno tool-hook
call signature for the four `_move_on_hint` sites (J, addressed by diff
instead, as noted); the real `_component_roots`/hive-mcp network path (tested
separately via `_component_roots` in Phase 0.48's own harness, reused
unmodified here).**

## 10. Deployment and live validation

### 10.1 Pre-deployment state

| | Value |
|---|---|
| ZGX HEAD | `107ec2a` → `d77eef1` (`--ff-only`, clean) |
| ZGX dirty/untracked | 35, unchanged before and after |
| `agno-api.service` | restarted (code change); new PID `60439`, active since `2026-11-05 22:00:28 EST` |
| `/health` | `{"status":"ok", ...}` |
| `/runs` | confirmed empty before the live run |
| `hive-mcp` | not touched (local-machine container, outside this phase's scope) |

### 10.2 Live T11 run

Canonical prompt, byte-exact from `docs/runbooks/groundedness-battery.md`,
SHA-256 `7bb97b1c00b7b116223b4710042e2c0abfd4a151e0a97f49e56ee48423f25566`
(confirmed before launch, matches the established hash). Run once,
`swarm_team="engineering"`, `read_only=true`, fresh session (no `session_id`
supplied), `/runs` confirmed empty first, not repeated.

| | Value |
|---|---|
| Task ID | `kre0m4jjm` |
| Run ID (`phase0`) | `64392b241a6b` |
| Session ID | `942b4f91-fe8f-4e0e-90d3-5884205f96d8` |
| Worker PID | `60781` |
| Duration | 582.0s (phase0 log) / 583.99s (API footer) |
| Outcome | `ok` |

### 10.3 Complete journal evidence for the new gate's execution

The full journal from run start to finish was pulled and read, not sampled
from excerpts. In order:

1. **22:00:45** — preflight fires: `chain question — 14 component root(s)
   injected`, confirming the same working discovery mechanism from Phase
   0.48.
2. **22:01:20** — Coordinator's only initial delegation:
   `delegate_structured_task(member_id='researcher',
   target='API/business-service', ...)`.
3. **22:02:06** — Coordinator's own narration repetition-stalls;
   `repetition-truncation completion skipped — no usable current-generation
   evidence exists yet` (Phase 0.44's invariant, confirmed firing exactly as
   designed — the delegation had not finished yet at this point).
4. **22:02:24** — **`_attempt_chain_coverage_correction` ENTER.** `chain-
   coverage check: 14 discovered component(s) never delegated for this run:
   [...]`. `_move_on_hint` fires with its own existing wording: `move-on hint
   attached — 1 target(s) covered so far: 'API/business-service'` /
   `13 component(s) untouched so far: [...including 'API/storage-service'...]`.
5. **22:02:27–22:03:34** — the bounded retry (`[chain-coverage-correction]`
   log prefix, confirming it is `_stream_team_run`'s own labeled run) re-reads
   business-service files, then at **22:03:34** issues a **second, real
   `delegate_structured_task` call**: `target='API/authentication-service,
   API/datalake-service, ..., API/storage-service, ..., Client/EcommClient-
   Web/ekamweb'` (all 12 previously-uncovered components, one call), with the
   Coordinator's own narration stating the reason explicitly: *"I will now
   delegate the task of investigating the remaining components to the
   Researcher, as the previous answer did not cover all the necessary
   components."* **This is the gate's bounded, tool-enabled opportunity being
   used for real, directly caused by this phase's new mechanism — not
   inferred from the final answer, established from the journal's own
   delegation-tool-call record.**
6. **22:04:58–22:06:04** — the Researcher reads the real files:
   `API/storage-service/router/storage_api.py`,
   `Client/.../steps/DocumentUploadStep.tsx`,
   `Client/.../BusinessRegistrationForm.tsx`.
7. **22:06:22–22:07:43** — the retry's own synthesis correctly writes up:
   frontend → `API/storage-service`'s `upload_file()` → `API/business-
   service`'s `register_documents()`/`get_seller_documents()`, with correct
   file paths, reaching 3,159 characters of genuinely correct content.
8. **22:07:53–22:10:10** — the retry's OWN final-synthesis narration hits the
   SAME chronic repetition stall this entire project has repeatedly
   documented, a SECOND time, specifically while writing its closing "Flow
   Summary" paragraph. The in-process repetition guard truncates and keeps
   the 3,159 correct characters; `_complete_repetition_truncated_answer` (the
   unmodified Phase 0.44 function, called exactly as every sibling correction
   function calls it) does not produce a text ending at a natural stopping
   point, so this phase's own `_truncation_note_for_reason` call (identical
   to its siblings') attaches the `_INCOMPLETE_ANSWER_MARKER`-bearing
   disclosure. This phase's own post-retry acceptance check then correctly
   treats that marker as "not usable" — **exactly the same outcome
   `_attempt_integration_correction`/`_attempt_coverage_correction` would
   produce if their own retry hit an identical second stall** — and rejects
   the whole retry, including the 3,159 characters of genuinely correct,
   hard-won content, falling back to the pre-retry content plus this phase's
   own unresolved-boundary disclosure.
9. **22:10:10** — `_attempt_chain_coverage_correction EXIT outcome=completed`.
   `_revalidate_adopted_correction:chain-coverage` runs, correctly `skipped`
   (no wholesale replacement occurred relative to what the function itself
   already returned).
10. **22:10:22** — `enforce_verification_invariant_final`: `verify_claims ok
    ... clean`. Run completes.

### 10.4 Final response vs. independent source ground truth

The final shipped answer is **"(no response)"** plus three disclosure
banners: Phase 0.44's own no-evidence disclosure (from the ORIGINAL, pre-
retry draft, carried forward), an unrelated pre-existing "ASKED FOR A LIST,
ANSWERED WITHOUT ONE" banner attaching a stale `API/business-service`
directory listing (a separate, pre-existing guard this phase did not touch —
noted, not investigated further, out of scope), and this phase's own
`UNRESOLVED SERVICE BOUNDARY` banner, naming all 14 components (including
`API/business-service` itself, which — once the retry was rejected wholesale
— reverted to having no result recorded against it either, since the
rejected retry's own log entries are real but the ORIGINAL delegation's
result was never separately re-checked by this phase's own disclosure logic
once it fell back to `uncovered_before`, computed before the retry; this is
accurate to what the SHIPPED content actually established, not a bug).

**Independently re-confirmed against the real repository** (re-verified
fresh this phase, same as Phase 0.48 Section 2): `upload_file()` at
`API/storage-service/router/storage_api.py:33`, `register_documents()` at
`API/business-service/router/business_api.py:484`,
`get_seller_documents()`/`delete_seller_document()` at
`API/business-service/router/business_admin_api.py:420`/`441`,
`DocumentUploadStep.tsx` and `BusinessRegistrationForm.tsx` under
`Client/EcommClient-Web/ekamweb/src/app/(public)/auth/register/business/`.

**Every one of these real files and functions was correctly identified
inside the retry's own rejected 3,159-character text** (Section 10.3, step
7) — the mechanism's own investigation was substantively correct. **None of
them reached the final shipped answer**, because that specific text was
discarded by the same conservative, pre-existing safety policy every
correction function in this file already applies to a retry that does not
cleanly finish.

**No fabricated service, function, or file name appears anywhere in the
final shipped answer.** This is a direct improvement over the Phase 0.47
incident, where the equivalent failure mode produced three invented service
names; here, the equivalent failure mode produces an honest "(no response)"
plus accurate disclosures.

## 11. T12 regression — live run omitted, with justification

`_is_chain_question(T12_TASK)` was directly re-confirmed this phase to
return `False` against the real, deployed function (scratch Scenario I,
Section 9) — the gate's entire effect on T12 is this one boolean check,
evaluated before any other code in `_attempt_chain_coverage_correction` runs,
and scenario I additionally confirms zero model calls and byte-identical
content when it is False. No control-flow path in this phase's change can
affect T12's behavior without that check first returning True, which it does
not. A live T12 re-run was judged unnecessary and was not performed, per the
mission's own instruction not to repeat a scenario without a specific
question the existing evidence cannot answer — this one can.

## 12. Phase 0.44/0.45/0.46 preservation

- **Phase 0.44** (no-evidence invariant): unchanged by diff; confirmed firing
  correctly, twice, in this very live run (Section 10.3, steps 3 and 8) —
  once on the original draft, once (via the shared `_complete_repetition_
  truncated_answer`/`_truncation_note_for_reason` path) inside this phase's
  own retry.
- **Phase 0.45** (router/model completeness guards): unchanged by diff; not
  exercised by T11 (not a router/model question) — no interaction expected
  or observed.
- **Phase 0.46** (`_revalidate_adopted_correction`): unchanged by diff;
  this phase's own call to it (`_revalidate_adopted_correction:chain-
  coverage`) correctly logged `outcome=skipped` since this phase's own
  function's return value was itself already the final word for that content
  (no further wholesale replacement to re-check).

## 13. Known limitations and unresolved risks

1. **The gate's own retry can lose a substantively correct answer to a
   second, independent repetition stall**, confirmed live this phase
   (Section 10.3, step 8) — not a defect introduced by this phase, but a
   direct, now-demonstrated consequence of the shared, pre-existing
   "reject anything not ending cleanly" safety policy every correction
   function in this file already applies, now also reachable by this gate's
   own retry. The mission's own explicit instruction ("fail closed... rather
   than pretending coverage is complete") was followed to the letter — the
   cost of following it this precisely, observed live, is that a correct
   retry can be discarded wholesale rather than partially salvaged.
2. **No partial-credit salvage exists for a chain-coverage retry that is
   substantively correct but ends messily** — unlike `_attempt_coverage_
   correction`'s own `_salvage_resolved_category_section`/`_sanitize_claim_
   specific_rejections` fallbacks (Phase 0.25/0.44), this phase's gate has no
   analogous partial-adoption path. Explicitly out of this phase's scope to
   add (would be a second, separately-justified mechanism), but a concrete,
   now-evidenced candidate for a future phase.
3. **The disclosure's own component list, once the retry is rejected, reverts
   to `uncovered_before`** — the state before the retry — which can list a
   component (`API/business-service`) that genuinely WAS investigated in the
   original delegation, simply because that delegation's own result was never
   separately re-validated once the whole retry was discarded. The disclosure
   is accurate to "this SHIPPED text's own evidence basis," not a claim that
   business-service was never investigated at all this run.
4. **One live run is one data point.** The specific interaction between this
   phase's retry and a second repetition stall was observed once; whether it
   recurs on every T11 run or is run-to-run variable (as every prior phase's
   own T-series data has shown repetition-proneness to be) was not
   established here and would need further live observation this phase's own
   "do not repeat to get a better result" instruction correctly prohibits
   pursuing further in this report.
5. **A stray, unrelated "ASKED FOR A LIST, ANSWERED WITHOUT ONE" banner**
   attaching a stale business-service directory listing appeared in the
   final answer — a separate, pre-existing guard, not investigated further,
   out of this phase's scope.

## 14. Verdicts

- **Implementation**: **PASS** — one shared, narrowly-scoped mechanism, 265
  purely-additive lines, no T11-specific hardcoding, every stated non-goal
  respected (confirmed by diff and by the scratch suite).
- **Scratch validation**: **PASS** — 23/23 new checks, 41/41 pre-existing
  Phase 0.45/0.46 checks still pass, zero regressions.
- **Deployment**: **PASS** — clean `--ff-only`, dirty/untracked state
  preserved, service healthy on the new commit.
- **Live mechanism execution**: **PASS** — definitively confirmed by direct
  journal evidence: the gate fired, computed the correct uncovered set using
  the real delegation log, reused `_move_on_hint`'s own wording, launched the
  bounded tool-enabled retry, and that retry caused a real second delegation
  that investigated `API/storage-service` and the real frontend files —
  behavior this run's journal proves the mechanism caused, not behavior
  merely consistent with the final answer mentioning the right names (it does
  not).
- **Live T11 audit quality**: **FAIL** — the final shipped answer is "(no
  response)" plus disclosures; the correct, complete, evidence-backed
  3,159-character answer the mechanism's own retry produced internally was
  discarded by a second, independent repetition stall and the shared,
  pre-existing strict-completion-only acceptance policy, not by this phase's
  own coverage logic.
- **Fabrication containment**: **PASS** — zero fabricated service, function,
  or file names anywhere in the final answer; a direct, concrete improvement
  over the Phase 0.47 incident's three invented services in the equivalent
  failure path.
