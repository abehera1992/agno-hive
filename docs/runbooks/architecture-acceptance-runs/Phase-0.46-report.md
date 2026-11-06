# Phase 0.46 — Post-Correction Repository Guard Revalidation

Implementation phase (not investigation-only). Closes the finalization gap Phase
0.45 identified: `_scoped_coverage_gap` and `_declared_models_not_reported` run
inside `_verified_answer`, but `_attempt_integration_correction` and
`_attempt_coverage_correction` run later, at their own choke point, and a
successful wholesale replacement on either path previously bypassed both guards
entirely. No canonical prompts, model configuration, routing, sampling, or
infrastructure was changed. No unit tests were added — validation is a scratch
harness against the real production functions, never committed.

## 0. Starting state

| | Value |
|---|---|
| Local HEAD | `f482419` ("docs(phase-0.45): T12 authoritative router inventory report") |
| Origin HEAD | `f482419` (matched local) |
| ZGX HEAD | `0921ae2` (one commit behind — missing only the Phase 0.45 report, a docs-only commit; the code it needed, `0921ae2`, was already deployed) |
| Local dirty/untracked count | 76 lines of `git status --porcelain` (pre-existing phase-report backlog and stray artifacts, none touched) |
| ZGX dirty/untracked count | 35 (pre-existing, unchanged throughout) |

## 1. Investigation — the complete answer lifecycle, traced

Read in full before any change: `_verified_answer`, `_scoped_coverage_gap`,
`_declared_models_not_reported`, `_attempt_integration_correction`,
`_attempt_coverage_correction`, `_coverage_verdict`, `_retry_lost_enumeration`,
the `_unmounted_note` logic inside `_scoped_coverage_gap`, `_with_forwarded_
evidence`, `enforce_verification_invariant_final`'s call site, and
`_run_repo_derived_guards` (the shared guard set `_verified_answer`'s own
internal retry-recheck already trusts).

**Lifecycle, as the code actually runs it** (`swarm/team.py`'s post-member-answer
assembly, inside the main run path):

1. `_verified_answer` computes the repo-derived guard notes directly
   (`_scope_note`, `_models_note`, `_integ_note`, …) against the draft and
   appends them via its own `_tail()` closure. It also has its OWN internal,
   separately-bounded retry for `verify_claims` failures — and when THAT retry
   succeeds, it already calls the shared `_run_repo_derived_guards(..., "recheck
   after retry", ...)` against the retry's output. This part was already
   correct and is unchanged.
2. Control returns to the caller with `content` = `_verified_answer`'s full
   output, banners included.
3. `_hoist_denied_premise` → `_enforce_verification_invariant` →
   `_evidence_integrity_check` run — each can rewrite `content`, each is
   outside `_verified_answer` entirely.
4. `_attempt_integration_correction` runs. If `_integration_mechanism_missing`
   finds a gap, it spends ONE retry: on success it returns `retried` directly
   (**no guard re-run**); on failure it returns `content + finding` (an
   **append**, not a replace).
5. `_attempt_coverage_correction` runs immediately after, same shape: success
   returns `retried`/`_salvaged`/`_sanitized` + a disclosure (**no guard
   re-run**); every failure path appends to the existing `content`.
6. `_attempt_evidence_grounded_reconstruction`, `_enforce_verification_
   invariant_final`, `_with_forwarded_evidence`, `_completion_control_check`
   run last.

**Established, not assumed:** reading both correction functions' full bodies
confirmed every one of their "no change" / "append a disclosure" return paths
is a literal `content + <already-computed note>` — a true string prefix of
their input — while every "adopt" path (`return retried`, `return retried +
_flagged_draft_note(...)`, `return _salvaged + disclosure`, `return _sanitized
+ disclosure`) builds its result from `retried` (or a derivative of it), never
from `content`, and is therefore NOT a prefix of the input. This distinction —
checkable from outside either function without touching their internals — is
what Section 2's fix is built on.

**Confirmed, not assumed, that a guard call at one site does not guarantee
survival to final assembly**: Phase 0.45's own live validation run (task
`kqluvmwn7`, session `d15048e4`) is direct proof. `_scoped_coverage_gap`
correctly fired inside `_verified_answer` and attached the complete 23-router
list to a 0-named draft — and `_attempt_integration_correction` then discarded
that entire draft, banner included, replacing it with a fresh regeneration
nothing downstream ever rechecked. That run's regeneration happened to be
complete on its own merits; nothing in the code guaranteed it would be.

Also confirmed: `_attempt_evidence_grounded_reconstruction` is a true no-op
unless `content` starts with `_VERIFICATION_FAILED_PREFIX` (read directly,
`swarm/team.py:13684`), and `_with_forwarded_evidence` is additive/idempotent
by its own Phase 0.24 precedent — neither can discard evidence this phase's
fix appends, so placing the new check immediately after each correction call
(before either of those) is safe.

## 2. The fix

**File changed:** `swarm/team.py` only. Commit `107ec2a`, 89 insertions, 0
deletions — purely additive, no existing line touched.

**New function, `_revalidate_adopted_correction`** (placed immediately after
`_run_repo_derived_guards`, which it reuses unchanged):

```python
async def _revalidate_adopted_correction(
        before: str, after: str, task: str, hive_mcp_url, hive_mcp_tools,
        label: str) -> str:
    if not after or after == before or after.startswith(before):
        return after
    notes = await _run_repo_derived_guards(task, after, hive_mcp_url, hive_mcp_tools, label)
    if notes:
        print(f"[team] {label}: content was replaced wholesale -- re-checked "
              f"against the repository, {len(notes)} repair/disclosure note(s) "
              f"reattached", flush=True)
    return after + "".join(notes)
```

- **Shared, not router-specific**: it is parameterized over `task`/`before`/
  `after` and invokes the SAME `_run_repo_derived_guards` set
  `_verified_answer`'s own retry path already trusts — `_scoped_coverage_gap`,
  `_declared_models_not_reported`, and every other repo-derived guard in that
  set, none of them modified. No router-only special case was added.
- **Prefix-identity gate, not a semantic diff**: a correction function's
  append-only returns are a literal prefix of their input (Section 1) and are
  left untouched — the guards already ran against that exact text upstream, so
  re-deriving the same facts again would only risk a duplicate banner, never
  find something new. Only a genuine wholesale replacement fails the check.
- **No model call, ever** — only re-reads the repository and re-derives facts
  already derivable from it; cannot trigger a further retry or recursive
  correction cycle by construction.
- **Pure addition on firing**: `after + "".join(notes)`, never a rewrite of
  `after` itself — whatever the correction function adopted survives verbatim,
  with repair notes appended on top, exactly as `_verified_answer`'s own
  retry-recheck already does.
- **No new gating logic**: each constituent guard keeps its own existing
  task-shape gate (`_scoped_coverage_gap`'s literal-"router" word check,
  `_declared_models_not_reported`'s literal-"models" word check, …) — an
  unrelated task costs nothing beyond those guards' own cheap early returns.

**Wired at both call sites**, each with its own `before`/`after` snapshot and
its own `_timed_guard_call`-wrapped telemetry name
(`_revalidate_adopted_correction:integration` /
`_revalidate_adopted_correction:coverage`), run immediately after the
correction call it revalidates and before the next correction stage sees the
result — so a SECOND wholesale replacement (by coverage-correction, on top of
an already-revalidated integration-correction output) gets its own fresh
recheck rather than inheriting a stale one.

## 3. Scratch-harness validation

Two files, run directly against the real, imported `swarm.team` module — no
tracked test files added.

### `test_p46_post_correction_revalidation.py` — 20 checks, scenarios A–H plus a
supplementary gate check, against the real `_revalidate_adopted_correction`
with monkeypatched repo I/O (`_repo_find_files`/`_repo_file_text`) so the
function under test is the real production code:

| Scenario | What was verified | Result |
|---|---|---|
| A — Incomplete integration retry | A wholesale-replaced, incomplete candidate is detected and repaired | PASS (2/2) |
| B — T12 router inventory | 17-of-23 (the highest count at-or-below the *preserved* 0.75 bar) repaired to all 23 with correct mount status¹ | PASS (2/2) |
| C — Model inventory | A model-only task's incomplete retry gets the full 31-model repair | PASS (1/1) |
| D — Correct integration retry | An already-complete retry (23 routers + 31 models) draws no router/model repair banner; content isn't needlessly mutated | PASS (3/3) |
| E — Verification failure | A pre-existing fabrication disclosure survives untouched; completeness repair runs alongside it, never suppressing or legitimizing the fabrication | PASS (3/3) |
| F — Final assembly | A repair from the integration stage survives an immediately-following no-op coverage-correction stage, with no duplicate banner | PASS (2/2) |
| G — Bounded execution | Multiple firing guards (routers + models) land in one pass; zero model calls made (verified by making `_stream_team_run` raise if called) | PASS (2/2) |
| H — Unrelated task | Zero repository lookups triggered for a task with no router/model wording | PASS (2/2) |
| Gate (supplementary) | The exact `content + finding` / `content + disclosure` append shapes both correction functions actually use never trigger a lookup at all | PASS (3/3) |

¹ The mission's own Scenario B example ("18 of 23") in fact clears the
*preserved* 0.75 ratio (18 > 17.25) and is correctly silent by Phase 0.45's own
design — not a gap. 17-of-23 is the highest count that still falls at-or-below
that bar, used here so the scenario exercises the repair path without touching
the threshold Phase 0.46 is explicitly forbidden from changing without new
evidence. Section 4 confirms the threshold itself was not touched.

**Result: 20/20 passed.**

### `test_p46_wiring_e2e.py` — 3 checks, reproducing the exact Phase 0.45 live
incident shape using the REAL, unmodified `_attempt_integration_correction`
(not reimplemented or mocked) chained into the REAL
`_revalidate_adopted_correction`. Only `_integration_mechanism_missing` (the
deterministic gap finder) and `_stream_team_run` (the one model call) are
faked — made to behave exactly like the live incident: the fake retry "fixes"
the integration gap it was asked about but, like the real Phase 0.45 run, only
gets 5 of 23 routers right.

**Result: 3/3 passed** — `_attempt_integration_correction` adopted the
wholesale replacement as designed; `_revalidate_adopted_correction` correctly
detected it was not a prefix of the input, re-ran the repo-derived guards, and
the final content contains all 23 routers (the integration-correction's own
adopted text preserved verbatim, repair notes appended on top).

### Regression check

`test_p45_t12_router_inventory.py` (Phase 0.45's own 18-scenario suite, against
the unmodified `_scoped_coverage_gap`) was re-run unchanged: **18/18 still
pass** — no regression from this phase's additions.

**Grand total this phase: 23/23 new checks passed, 0 failed; 0 regressions in
Phase 0.45's own suite.**

## 4. Phase 0.45 preservation

Confirmed by diff (`git diff f482419..107ec2a -- swarm/team.py`): the commit is
89 insertions, 0 deletions. `_scoped_coverage_gap`, `_mounted_router_files`,
the 23-file directory-inventory logic, the 16-file mounted-router discovery,
the 7-file unmounted classification, the `0.75` router completeness ratio, the
literal-"router" task gate, and `_declared_models_not_reported`'s existing
direct-attachment mechanism are byte-for-byte unchanged. Nothing in this phase
touches canonical prompts, model configuration, routing, sampling, repetition
thresholds, token budgets, or infrastructure.

## 5. Deployment

| | Value |
|---|---|
| Commit | `107ec2a` ("fix(swarm): revalidate repo-derived guards after correction-stage adoption") |
| Pushed | `origin/main`, `f482419..107ec2a` |
| ZGX | fast-forwarded `0921ae2..107ec2a` (picked up both the Phase 0.45 report and this phase's code in one `--ff-only` merge); dirty/untracked count unchanged (35) before and after |
| `agno-api.service` restarted | required (code change) — new PID `3920322`, active since `2026-11-05 19:31:47 EST`, health `{"status":"ok", ...}` |
| `hive-mcp` | not touched — local-machine container, outside this phase's scope |
| `/runs` | confirmed empty before the validation run |

## 6. Live validation

Canonical T12 prompt, extracted verbatim from `docs/runbooks/groundedness-battery.md`
(lines 115–120), SHA-256 `f2249c203d6c295042b16840ec50883b667df9ce132844f7a7211221fb7f32a0`
— identical to Phase 0.42's and Phase 0.45's hash. Run once,
`swarm_team="engineering"`, `read_only=true`, fresh session, `/runs` confirmed
empty first, not repeated.

| | Value |
|---|---|
| Task ID | `k5d90vpsg` |
| Session ID | `143c4da1-5758-4266-810a-4eb877d8fa0a` |
| Run ID (`phase0`) | `cc6fdab6709b` |
| Worker PID | `3920500` (per-run subprocess; the long-lived service PID is `3920322`) |
| Duration | 330.7s (phase0 log) / 332.33s (API footer) |
| Outcome | `ok` |

### 6.1 What the journal shows, in order

1. **19:32:05–19:34:07** — the Coordinator's own narration repeats four times;
   the repetition-threshold guard fires, truncating in-process and keeping the
   1,141 characters generated before the repeat — the same upstream trigger
   every prior T12 run on record hits.
2. **19:34:07** — the Phase 0.44 T11 no-evidence invariant engages (generic
   trigger, not T11-specific): `repetition-truncation completion skipped -- no
   usable current-generation evidence exists yet`. Correct — no synthesis
   attempted without real evidence.
3. **19:34:18–19:34:19** — inside `_verified_answer`'s own direct guard calls
   (unchanged from Phase 0.45): `mount check` flags one unmounted file
   (`inventory_api.py`) presented as real; **`scope check` fires — `23 router
   files (16 mounted), answer names 1 — attaching the 22 it omitted`**;
   `integration check` flags the missing `BUSINESS_SERVICE_URL` mechanism;
   `models check` fires — `31 models, answer names 0 — attaching the 31 it
   omitted`. All four are Phase 0.45/pre-existing mechanisms, unmodified,
   confirmed firing correctly on real ground truth.
4. **19:34:19–19:34:38** — `_hoist_denied_premise`, `_enforce_verification_
   invariant` (18.6s), `_evidence_integrity_check` all run and report
   `outcome=skipped` (no change needed).
5. **19:34:38** — `_attempt_integration_correction` **ENTER**. At 19:34:39 its
   own internal `_integration_mechanism_missing` call re-confirms the gap
   (same three citations, re-ordered — `search_files`' own result ordering is
   not guaranteed stable across calls, a pre-existing, cosmetic characteristic
   unrelated to this phase) and it spends its one retry.
6. **19:34:49–19:37:08** — the retry's OWN generation hits a repetition loop
   (`"I'll provide a detailed architectural overview..."` repeating) and is
   truncated after 149.6s; the truncated result is judged **not usable**
   (`integration-correction retry produced nothing usable`). `_attempt_
   integration_correction` **EXIT**s having returned `content + finding` — an
   **append**, not a wholesale replace, because the retry itself never
   produced a usable candidate to adopt.
7. **19:37:08** — **`_revalidate_adopted_correction:integration` ENTER → EXIT
   after 0.0s, `outcome=skipped`.** This phase's new code correctly identified
   `content + finding` as a literal prefix match (the exact "pure append, no
   wholesale replace" case Scenario D/the supplementary gate validated) and
   made zero repository calls — proof the gate works correctly on a real
   production append, not just in the scratch harness.
8. **19:37:08** — `_attempt_coverage_correction` **ENTER → EXIT, skipped**
   (no comparison gap applicable to this task). **`_revalidate_adopted_
   correction:coverage` ENTER → EXIT, skipped** — same correct no-op, for the
   same reason.
9. **19:37:08–19:37:26** — `_attempt_evidence_grounded_reconstruction`
   (skipped — content never carried the `VERIFICATION_FAILED` prefix),
   `_enforce_verification_invariant_final` (17.7s, skipped — nothing left to
   correct).
10. Final `answer_chars=5,735` vs. `out_chars=5,293` entering the last
    invariant check — the ~442-character difference is the evidence-ledger
    citation disclosure appended afterward (an unrelated, pre-existing
    mechanism, same as Phase 0.45's run).

### 6.2 Final answer vs. ground truth

Checked line-by-line against the same independently-verified ground truth as
Phase 0.45 (Section 3 of that report, re-confirmed unchanged this phase): the
shipped final answer's attached repair content states all **23** router
files with correct mount status — all 16 mounted files correctly labeled
`— mounted in main.py`, all 7 unmounted files correctly labeled `— present as
a file but NOT mounted in main.py` (6 in the completeness repair list, plus
`inventory_api.py` separately flagged by the pre-existing mount-check banner
for being named without disclosure that it's unmounted) — all **31** model
classes by exact name, and all **3** correct integration citations
(`API/inventory-service/config.py:41`,
`API/inventory-service/router/categories_api.py:186`,
`API/inventory-service/router/admin_gst_api.py:684`). **Zero omissions, zero
fabrications.** Final verification (`enforce_verification_invariant_final`)
passed cleanly.

One cosmetic, pre-existing characteristic observed and worth naming
precisely rather than glossing over: the integration-citation disclosure
appears **twice** in the final text, with citations in a different order each
time. This is `_attempt_integration_correction`'s own "retry failed → return
content + finding" path unconditionally re-appending its freshly-recomputed
`finding` string on top of `content`, which already carried an equivalent
disclosure from `_verified_answer`'s own direct guard call — a pre-existing
behavior of that function, confirmed via the journal (two separate
`_integration_mechanism_missing` invocations, 19:34:19 and 19:34:39, each
logging its own "integration check" line) and **not caused by, or within the
scope of, this phase's fix** — `_revalidate_adopted_correction` never ran a
repo-derived guard at all this run (Section 6.1, steps 7–8), so it cannot be
the source of this duplication. Not fixed here: deduplicating `_attempt_
integration_correction`'s own append behavior is a different, narrowly-scoped
change this mission does not authorize ("do not expand this into a general
recovery or verification refactor").

### 6.3 Honest assessment — which mechanism produced this run's completeness,
and what was and was not exercised live

**This run's completeness is fully attributable to Phase 0.45's own,
unmodified mechanism** — the direct `_scoped_coverage_gap`/`_declared_models_
not_reported` calls inside `_verified_answer` (Section 6.1, step 3) — not to
Phase 0.46's new code. `_attempt_integration_correction`'s retry failed
outright this run (its own generation repeated and was judged unusable), so it
never reached a successful wholesale-replacement state, and `_revalidate_
adopted_correction` correctly had nothing to repair: both of its call sites
logged `outcome=skipped` with zero repository lookups (Section 6.1, steps 7–8).

**What this live run DOES prove about Phase 0.46's fix:**
- The new guard-trace instrumentation executes at both intended call sites in
  a real production run.
- The prefix-identity gate correctly classifies a REAL `content + finding`
  append from the real `_attempt_integration_correction` as "nothing to
  revalidate" and skips — avoiding a wasted repository round-trip and a
  further duplicate banner on top of the one already described in Section 6.2.
- Nothing about wiring the new checks into this choke point destabilized,
  slowed materially, or altered the existing, correct completeness outcome.

**What this live run does NOT prove, and is not claimed here:** that the
*repair* branch of `_revalidate_adopted_correction` — re-running the guards
against a genuine wholesale replacement and reattaching missing inventory —
executes correctly in a full, real production pipeline run. That branch is
proven correct by Section 3's scratch validation, including the
`test_p46_wiring_e2e.py` reproduction using the real, unmodified
`_attempt_integration_correction` function chained into the real
`_revalidate_adopted_correction` — but a live run exercising that exact branch
end-to-end, with a real (not faked) model producing a successful-but-incomplete
regeneration, was not observed this phase. Per the mission's own "do not
repeat the scenario merely to obtain a favorable outcome" instruction, no
further live run was attempted to try to force that branch to fire.

## 7. Remaining limitations

1. **The repair branch's live firing remains unobserved** (Section 6.3) —
   proven by scratch validation against the real production functions, not
   yet observed in a full live pipeline run where it was actually needed.
2. **The pre-existing duplicate-citation-disclosure cosmetic issue** (Section
   6.2) was observed and traced to its exact cause but is out of this phase's
   authorized scope to fix.
3. One run is one data point. As with every prior phase, this is not a claim
   that `_attempt_integration_correction`'s retry will always fail, or always
   succeed, on a future T12 run — only that whichever outcome occurs, the
   finalization invariant this phase adds is now present to catch an
   incomplete *successful* replacement, which Phase 0.45's own live run proved
   can happen.
4. `_attempt_coverage_correction` was not separately exercised live this run
   (its own gap-detector found nothing applicable to T12's shape, consistent
   with every prior T12 run on record) — its own revalidation wiring is
   validated only via Scenario F's scratch check (chained no-op after a prior
   repair) and the isolated unit-style scenarios, not a live firing.

## 8. Verdicts

- **Implementation**: **COMPLETE** — one shared, narrowly-scoped function,
  wired at both correction call sites, 89 purely-additive lines, Phase 0.45
  untouched (confirmed by diff).
- **Scratch validation**: **PASS** — 23/23 new checks (20 scenario-style + 3
  real-function wiring), 0 failures; 18/18 Phase 0.45 regression check, 0
  failures.
- **Live execution**: **EXECUTED, SKIP-PATH CONFIRMED** — both new call sites
  ran in a real production request (journal-confirmed), correctly determined
  no wholesale replacement had occurred, and made zero unnecessary repository
  calls. The repair path was not triggered because the precondition for it
  (`_attempt_integration_correction` successfully adopting a replacement) did
  not occur this run — honestly reported as not exercised, not claimed as
  proven live.
- **End-to-end acceptance**: **MET for this run, by a different mechanism than
  the one this phase added.** The final answer names all 23 router files with
  correct mount status, all 31 models, and all 3 correct integration
  citations, and passes final verification — but via Phase 0.45's own,
  unmodified `_verified_answer`-level guards, not via this phase's new
  post-correction revalidation. Per the mission's own explicit instruction,
  this is reported as fact rather than credited to the new invariant.
