# T59 — Architectural Execution-Control Review and Invariant Enforcement

Builds on, and does not overwrite, prior reports through `T58-report.md`.
Commit `d808c173b62755baa05bb4a6af3ac7b7ff836929` (local `main` and
`origin/main`), deployed to ZGX `agno-api.service`. No change to
`hive-mcp/tools/verify.py`, `hive-mcp`, or `hive-mcp-agno`.

## A. Verdict

**PASS WITH QUALIFICATION.** A real, confirmed cross-path control gap was
found and corrected: truncation-awareness (T57/T58's own flags) was
checked at exactly 1 of `_adopt_retry`'s 12 call sites before this phase,
leaving the other 11 free to adopt a repetition- or semantic-stall
-truncated retry as a normal, complete one. The correction is implemented,
harness-verified against the real function (locally and against the
deployed artifact), and deployed. The qualification: no naturally
-occurring live run exercised the specific gap this phase closes (none of
the 11 previously-unprotected guards happened to hit a truncated retry
during this session's own validation window), so live confirmation rests
on the controlled-path exercise, not an organic incident.

## B. Architectural finding

**Confirmed, from direct source trace, not assumed from a path difference
alone.** `_adopt_retry` (`swarm/team.py:4436`) is the single, universal
adoption gate every retry-based guard in this file funnels through —
verified by grepping for the historical "unconditional adopt" pattern
(`content, result = retried, retry`) that `_more_grounded`'s own docstring
describes having fixed everywhere; zero such bypasses remain in the
current source. T57 added a truncation check, and T58 extended it, but
only inside `_complete_repetition_truncated_answer`'s own call site —
which computes the check itself, separately, rather than through this
shared helper. The other 11 call sites (enumerated in §C) called
`_adopt_retry` with no truncation awareness at all: `_more_grounded`, the
function `_adopt_retry` uses to decide adoption, compares only read
counts and has no way to know a retry was cut short mid-generation.

**Risk vs. confirmed defect**: this was a genuine defect, not merely a
theoretical risk — any of the 11 guards could, in principle, have its own
`_stream_team_run` retry hit Z16's repetition guard or T58's semantic
-stall guard (both of which fire on ANY `_stream_team_run` call, not only
the two call sites T57/T58 happened to touch), and the resulting truncated
text would have been scored purely on read count and potentially adopted
with zero disclosure that it was incomplete.

## C. Execution-path map

- **`_stream_team_run`** — the one generation-streaming primitive used by
  every retry in the file (13 call sites found via grep: the 12 listed
  below plus `_complete_repetition_truncated_answer`'s own completion
  pass). Runs Z16 textual-repetition detection and T58 semantic-stall
  detection; on either, truncates to `last_good_len` and sets a flag
  (`_repetition_truncated` or `_semantic_stall_truncated`) and returns.
- **`run_task_async`** — the top-level entry point for the INITIAL
  generation (not a retry). Has its OWN separate, duplicated copy of Z16's
  textual-repetition logic (confirmed in T57's own trace; not
  re-inspected line-by-line this phase since no new evidence pointed at
  it). Does NOT have T58's semantic-stall logic — a disclosed, deliberate
  T58 limitation, re-affirmed in §D/§H below rather than fixed
  speculatively, since no incident evidence exists for that specific
  path.
- **`_adopt_retry`** (this phase's fix point) — the universal
  retry-adoption gate. 12 callers, all inside functions that take `team`
  as a parameter: `_reconcile_completeness_claim_with_comparison`,
  `_reask_after_syntax_loss`, `_complete_repetition_truncated_answer`,
  eight separate guards inside `_verified_answer` (unfinished-intent,
  write-claim, search-claim, no-evidence, db-evidence, narrated-tool,
  enumeration, citation-correction), and `_grounded_retry_from_evidence`
  (itself shared by four further call sites —
  `_reconcile_thin_answer_with_tool_evidence` and three others at lines
  9808/23046/23128 — so fixing it once covers all four transitively).
- **`_adopt_authoritative_correction`** — a SEPARATE adoption gate, traced
  and ruled out: exactly one real caller, always `consumed_llm_call=False`
  (candidate built purely from `team._member_results`, never a
  `team.arun()` call), so it cannot be truncated by either guard. Verified
  not applicable, not left unchecked by oversight.
- **`_evidence_integrity_check`** — does NOT call `_adopt_retry` at all;
  uses its own strip/`verify_claims`/`_force_uncertainty_answer`
  disposition (T55/T57/T58's own work). Already correctly checks both
  truncation flags, confirmed in T57/T58's own reports and re-verified
  present and unchanged this phase (not re-modified).
- **`_checkpoint_block`** — supplies the real, already-gathered evidence
  `_complete_repetition_truncated_answer`'s completion prompt uses.
  Unchanged this phase; not implicated in the confirmed gap (the gap was
  about adoption judgment, not evidence quality).

## D. Control-invariant matrix

| Invariant | Status | Evidence / gap |
|---|---|---|
| Execution lifecycle (completion vs. truncation vs. disconnect distinguishable) | **Verified** | `_repetition_truncated`/`_semantic_stall_truncated` flags, `[api] client disconnected` log line (confirmed live in T58's own natural run), `_TRUNCATED_ANSWER_NOTE` sentinel all distinct and non-overlapping, confirmed by source read. |
| Text repetition (T16) | **Verified** | Unchanged this phase; zero lines touched in `_looks_like_repetition_loop`/`_looks_like_repetition_decay` or the periodic-check branch, confirmed via diff inspection. |
| Semantic non-progress (T58) | **Verified** on `_stream_team_run`'s own callers; **Not applicable (no evidence)** on `run_task_async`'s separate copy | T58's detector fires for every `_stream_team_run` call (13 call sites, confirmed). `run_task_async`'s own initial-generation path lacks it by design, with no incident evidence pointing at that path specifically — extending it there would be symmetry-for-its-own-sake, which the mission explicitly says not to do without evidence. |
| Recovery boundedness | **Verified** | `_REPETITION_COMPLETION_FLAG` is the single, shared one-shot budget for the ONE completion mechanism (`_complete_repetition_truncated_answer`); confirmed only one caller pattern writes it, and T58 already confirmed it is shared (not duplicated) across both trigger reasons. |
| Nested recovery | **Verified** | `_complete_repetition_truncated_answer`'s own `nested_truncation` check (T57, re-verified unchanged) catches its own completion pass tripping either guard a second time; this phase's new `_adopt_retry` check is a no-op at that one call site specifically because the flags are already cleared there by the time `_adopt_retry` runs — confirmed by direct trace, not merely assumed, and does not conflict with or duplicate the existing handling. |
| Evidence preservation | **Verified** | `_checkpoint_block(team)` (real member findings) is what every completion/reconciliation prompt is built from, never the raw repetitive transcript; unchanged this phase. |
| Completion integrity | **Gap confirmed, now fixed** | This phase's own finding: 11 of 12 `_adopt_retry` call sites could adopt a truncated retry with no disclosure before this fix. Closed by moving the check into the shared helper. |
| Unresolved requirements stay visible | **Verified** | Rejecting a truncated retry via `_adopt_retry` returns `(content, result)` unchanged — the SAME disposition as the pre-existing "less grounded" rejection, so whatever disclosure/fallback the calling guard already had for "retry didn't help" (unchanged by this phase) still runs; nothing is newly hidden. |
| State lifecycle (flags don't leak) | **Verified** | Both flags are per-`team`-instance attributes (`setattr`/`getattr` on the run's own `team` object, never module- or process-global); confirmed by reading every set/clear site. `_adopt_retry`'s new check clears both flags itself on rejection, closing one more place they could otherwise have lingered uncleared into a LATER guard in the same call. |
| Diagnostics | **Verified** | New rejection path prints the guard's own `label` plus an explicit "cut short by the repetition/semantic-stall guard" reason, distinguishable from the pre-existing "nothing survives stripping" / "LESS evidence" rejection messages at the same call sites. |

## E. Implementation

**File changed**: `swarm/team.py` only (44 insertions, 13 deletions).

**Behavioral change**: `_adopt_retry` gained a `team=None` parameter and,
when `team` is supplied and either truncation flag is set, rejects the
retry outright (returns `(content, result)` unchanged, clears both
flags) before any of its existing checks run. All 12 call sites updated
to pass `team=team` (every one already had `team` in scope, confirmed
individually). No other function's body, return contract, or public
signature changed.

**Why this is the smallest sound correction**: the alternative — adding
the same three-line check at each of the 11 unprotected call sites
individually — would have reproduced the exact duplication pattern that
caused the gap (T57 and T58 each touched one call site and the other 11
were simply never revisited). Consolidating into the one shared helper
guarantees every current AND future caller gets the protection by
construction, with no per-call-site discipline required. A full shared
execution-control object/class was considered and rejected: the gap was
narrow (one missing check in one shared function, not a structural
absence of a lifecycle contract), `_stream_team_run` and
`_complete_repetition_truncated_answer` already correctly isolate their
state per-`team`-instance, and introducing a new controller object would
have meant touching every one of the 12 call sites' surrounding code
anyway for no additional guarantee beyond what the smaller fix provides.

## F. Validation

**Local harness** (`t59_adopt_retry_test.py`, this session's scratchpad,
not committed): run against the real, imported `_adopt_retry` (not a
reimplementation). Five scenarios: repetition-truncated retry rejected and
flag cleared; semantic-stall-truncated retry rejected and flag cleared; a
normal, genuinely-more-grounded retry still adopted (regression); `team=
None` preserves prior behavior exactly (backward compatibility); the
pre-existing empty-retry rejection unaffected. All five passed.

**Regression re-runs**: T57's own `t57_repetition_completion_test.py` and
T58's own `t58_completion_provenance_test.py` and
`t58_semantic_stall_test.py` (all pre-existing, unmodified scratch
harnesses from those phases) were re-run unchanged against the post-T59
code. All scenarios in all three passed identically to their own original
phase's results — confirms `_complete_repetition_truncated_answer` and
`_stream_team_run`'s own mechanics are unaffected by this phase's change.

**Deployed-path exercise**, clearly separated from the local harness: on
ZGX, using the exact interpreter `agno-api.service` runs under, with
`swarm.team` imported from the confirmed deployed commit, the three core
`_adopt_retry` scenarios (truncated-by-repetition rejected,
truncated-by-stall rejected, normal retry still adopted) were re-run
directly against the deployed module and passed identically.

**No natural live run was launched for this phase.** Per the mission's
own instruction to prefer a controlled exercise over a long natural run
when it validates the fix more efficiently, and because none of the 11
previously-unprotected guards' own specific trigger conditions (claimed
-write without a tool call, search-claim without a search, thin/
no-evidence answers, etc.) were independently reproduced this phase, no
natural run was attempted to chase this specific gap.

## G. Deployment

- Commit `d808c173b62755baa05bb4a6af3ac7b7ff836929` pushed to
  `origin/main` (`700a210..d808c17`).
- ZGX: `git pull` fast-forwarded `7b9cf74..d808c17`, confirmed via `git
  rev-parse HEAD` matching exactly, both at deployment and during the
  §F deployed-path exercise.
- `systemctl --user restart agno-api.service`: new PID `860415`,
  `/health` returning `{"status":"ok", ...}`.
- No active run before the restart (`GET /runs` empty, no `run-worker`
  process).
- `hive-mcp`/`hive-mcp-agno`: not touched — no source change, not
  restarted.
- `git status --short` before and after this phase's work shows only
  `swarm/team.py` newly dirty; the same pre-existing dirty/untracked files
  recorded in every prior report remain exactly as found. No stray
  artifact files were created this phase.

## H. Remaining risks

- **`run_task_async`'s own separate Z16 copy still lacks T58's
  semantic-stall signal** — disclosed in T58's own report, re-confirmed
  as still true this phase, not newly discovered. Evidence-backed
  decision to leave it: the one recorded semantic-stall incident
  occurred inside a `_stream_team_run`-mediated retry, not the top-level
  generation; porting the signal there without a matching incident would
  be speculative, which the mission explicitly discourages.
- **The 11 newly-protected call sites were validated via the shared
  helper's own unit-level behavior, not by independently reproducing each
  guard's own specific trigger condition truncated mid-retry.** This is a
  reasonable inference (the shared helper's logic is caller-agnostic by
  construction) but is explicitly labeled as inference, not as 11
  separately-confirmed live reproductions.
- **`_adopt_authoritative_correction`'s `consumed_llm_call=True` code path
  is currently dead** (no caller ever passes it) — not a risk today, but
  if a future caller is added with `consumed_llm_call=True`, it would
  create a model-calling candidate-adoption path with no truncation
  check, reproducing the same class of gap this phase just closed
  elsewhere. Flagged as a latent risk for whoever adds that caller, not a
  present defect.

## I. Architectural next step

Execution-control guarantees for the retry-adoption lifecycle are now
consistently enforced across every active path in the file — confirmed
by a complete enumeration of `_adopt_retry`'s callers (12, all now
protected) and a ruled-out second gate (`_adopt_authoritative_correction`,
immune by construction given its one real caller). No further detector or
consolidation phase is proposed from this review alone. The one concrete,
evidence-backed next step — porting T58's semantic-stall signal to
`run_task_async`'s own copy — should wait for a recorded incident on that
specific path, per this phase's own finding in §H, not be scheduled
speculatively.
