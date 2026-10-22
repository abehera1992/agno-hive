# T63 — Whitespace Collapse + Coordinator Task-Relevance Control

## 1. Initial state (verified independently, not assumed)

- Local HEAD at phase start: `c9e0247d722a3f83b8bce360ce03d511d3dbc034` (T62's own commit).
- `origin/main` HEAD: identical — `c9e0247` is `origin/main`'s own tip, confirmed via `git fetch` + `git log --oneline -1 origin/main` (not merely an ancestor; the exact same commit).
- Working tree: clean except the long-standing, pre-existing modified/untracked set from prior phases (`docs/runbooks/groundedness-battery.md`, `tests/test_evidence_fidelity.py`, `training/train.py`, `zgx-ai-setup/litellm-config.yaml`, several untracked `training/*`/`tests/*` files). None of these were touched this phase.
- ZGX deployed HEAD at phase start: `783ff0907fe041fefd0025dc77e9b983e1aebb92` (T60's commit) — **`c9e0247` was committed and pushed in T62 but had never been deployed**, correctly blocked pending the active T3 run's own termination.
- `/runs` on ZGX: confirmed empty (`{"runs":[]}`) at phase start — the T3 retry was stopped (by explicit user instruction) immediately before this phase began. No orphaned worker processes found.

## 2. Workstream 1 — Whitespace-only generation collapse

### 2.1 Verification of `c9e0247`

Inspected the full diff (`git show c9e0247 -- swarm/team.py`): 21 insertions, 1 deletion, entirely inside `_looks_like_repetition_loop`'s own docstring + one new guard clause:

```python
if len(new_segment) >= _REPETITION_MIN_SEGMENT_LEN and not new_segment.strip():
    return True
```

Checked against the **raw** `new_segment` argument, placed before the existing `" ".join(new_segment.split())` normalization line that (pre-fix) discarded whitespace-only content entirely.

Reviewed in context against this phase's own checklist (Part B1):
- **Threshold/rationale:** reuses `_REPETITION_MIN_SEGMENT_LEN` (60 chars) — the same constant every other tier in this function already uses, deliberately not a new threshold, so "how long is long enough to judge" stays answered in one place.
- **Raw text availability:** `new_segment` is the function's own first parameter, unmodified at the point this check runs — confirmed by reading the full function body.
- **Short/empty segments stay harmless:** the `len(new_segment) >= 60` guard was re-verified against a 20-char tab run and an empty string — both correctly return `False`.
- **Legitimate content unaffected:** any real prose or code contains non-whitespace characters, so `.strip()` on it is never empty — reverified against ordinary prose and indented multi-line code.
- **Both execution paths reached:** `grep` confirms exactly two call sites for `_looks_like_repetition_loop` (`_stream_team_run`, `run_task_async`'s own copy), both calling the identical shared function — the fix covers both simultaneously.
- **Interaction with `_looks_like_repetition_decay`:** both call sites evaluate `loop_detected = _looks_like_repetition_loop(...)` first, then `decay_detected = not loop_detected and _looks_like_repetition_decay(...)`. Since the new check makes `_looks_like_repetition_loop` return `True` for this exact shape, `_looks_like_repetition_decay` is short-circuited and never reached for a whitespace collapse — its own, independent blind spot (identical `str.split()` pattern) remains unfixed but unreachable for this specific failure shape.

**Conclusion: `c9e0247` is correct, sufficient, and well-scoped for the observed failure. No remaining gap requiring a B2 correction.**

### 2.2 Expanded focused validation (scratch harness, not repository tests)

Ran `t63_whitespace_expanded_test.py` against the real, imported `swarm.team` module (not a reimplementation):

| # | Scenario | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Captured live tab-collapse segment | flagged | flagged | PASS |
| 2 | Spaces-only (80 chars) | flagged | flagged | PASS |
| 3 | Mixed tabs/spaces/newlines (90 chars) | flagged | flagged | PASS |
| 4 | Empty segment | not flagged | not flagged | PASS |
| 5 | Short whitespace (30 chars) | not flagged | not flagged | PASS |
| 6 | Ordinary prose | not flagged | not flagged | PASS |
| 7 | Legitimate multiline code (indentation, blank lines) | not flagged | not flagged | PASS |
| 8 | Repeated opening WITH new identifier (genuine progress) | not flagged | not flagged | PASS |
| 9 | Repeated opening WITHOUT new identifier (no progress) | flagged (by pre-existing tier, not this one) | flagged | PASS |

All 9 scenarios passed. Scenario 9 confirms the new whitespace tier does not interfere with or duplicate the pre-existing repetition-without-progress detection.

Downstream recovery/adoption behavior was **not** re-proven from scratch this phase: the new check sets the same `loop_detected` flag every other repetition tier sets, feeding the identical, already-validated (T57–T59) truncation → recovery → completion-decision pipeline. Since that pipeline's correctness does not depend on which tier triggered the flag, re-validating it here would duplicate prior phases' own work rather than test anything new.

### 2.3 Deployment

- `git pull` on ZGX: fast-forward `783ff09 → c9e0247` (3 files: `swarm/team.py` + two pre-existing report docs from T60/T61 that had not yet synced). No other files touched; all of ZGX's own pre-existing untracked/modified files (`.env.bak-*`, `training/*`, `zgx-ai-setup/*.bak-*`, etc.) preserved untouched.
- `systemctl --user restart agno-api.service` → active, PID 1351630, confirmed via `systemctl status`.
- `curl localhost:9001/health` → `{"status":"ok",...}`.
- Post-deployment validation: re-ran the whitespace-detection check directly against the **deployed** module on ZGX (via the service's own interpreter, `~/miniforge3/envs/zgx/bin/python3`) — tab-collapse segment → `True`; short whitespace → `False`; ordinary prose → `False`. Matches local results exactly.

### Verdict: Workstream 1 — **PASS**

Confirmed defect, confirmed-sufficient fix, validated against the real function both pre- and post-deployment, deployed, and independently verified live.

## 3. Workstream 2 — Coordinator task-relevance / progress

### 3.1 Reconstructed sequence (from direct trace evidence gathered during the T3 battery, not assumption)

**Attempt 1** (ended via MCP client's own ~1800s httpx timeout, an environmental/documented limitation, not a behavioral defect):
1. Delegated reads of `business_api.py` and `models.py` — directly relevant, correctly grounds the investigation.
2. Delegated a verbatim read of a seller-email migration (`e5f6a7b8c9d2_add_seller_email_and_audit_events.py`) — marginally relevant (adds a `seller_profiles.email` column).
3. Delegated a verbatim read of a rename migration (`d0e5f6a7b8c9_rename_seller_tables_to_business.py`) — marginally relevant (explains current table naming).
4. Drifted into reading `modules_api.py`-style tenant/subscription-module routes — **no evidenced dependency on the seller-verification question**.
5. Client-side timeout terminated the run.

**Attempt 2** (retry):
1. Coordinator's own generation entered a genuine, correctly-detected T16 repetition loop (describing a migration-filename typo), recovered correctly by T57.
2. The recovery pass's own follow-up delegation asked for an exhaustive enumeration of **all** tables/fields in `models.py`, most unrelated to seller verification (`PricingPlan`, `TenantModule`, `BundleDefinition`, etc.).
3. Generation collapsed into the whitespace runaway (Workstream 1) mid-enumeration.
4. The run was killed by explicit user instruction. The final recovered partial answer (captured via the task-notification after the kill) shows the Researcher's own underlying reads **did** eventually surface the directly relevant facts (`VerificationCheck` table, `/business/admin/verification-queue`, `/business/admin/verify/{business_id}` endpoints) — buried amid substantial irrelevant schema dump.

### 3.2 Root-cause analysis against the six candidate causes (C1)

Initially hypothesized that "verbatim reproduction" delegation phrasing was the defect. **This hypothesis was checked against the Coordinator's own instructions and rejected**: `_COORDINATOR_INSTRUCTIONS` extensively and deliberately recommends verbatim relaying (`"read X and return its raw output verbatim"`) as a grounding strategy to prevent hallucinated content — confirmed via direct grep (10+ occurrences). This is an intentional, beneficial, pre-existing design choice, not a bug.

Revised finding, confirmed against the actual trace:
- **Cause 1 (delegation objective quality):** not a phrasing defect — the issue is *target selection* (which files to delegate to), not how the delegation is worded.
- **Cause 3 (continued exploring after sufficient evidence):** supported — by the time `business_api.py` + `models.py` + the first migration were read, the Researcher already held the core facts needed to answer; exploration continued regardless.
- **Cause 4 (runtime lacks a way to recognize low-value exploration):** supported, and precisely located (see 3.3).
- **Causes 2, 5, 6:** not supported as primary — member reports did contain the relevant facts (cause 2 partially present as a synthesis/selection issue downstream of 3/4, not a retrieval failure); the trace is sufficiently detailed to locate causes 3+4 as earliest, so cause 6 (insufficient evidence) does not apply.

**Earliest proven divergence:** the point at which the investigation began accumulating file reads whose relevance to the seller-verification question was never checked against the task's own outstanding question before the next delegation was issued — concretely, the migration-history reads in Attempt 1, and the full-schema enumeration in Attempt 2's own recovery pass.

### 3.3 Existing-control inspection (C3) — the precise architectural boundary

Located the one existing mechanism closest to this problem: `_record_member_action` / `MemberControlState.no_progress_streak`, consumed by `_make_coordinator_progress_guard_hook` (added T50, 2026-10-21). Its own docstring states the design intent explicitly: *"a call with different arguments (or a different tool) than the member's own immediately prior action is automatically progress — a genuinely different action, never blocked merely for being a repeat"* — a deliberate trade-off to never falsely block legitimate deep multi-file investigation.

**Controlled validation (scratch harness, `t63_coordinator_progress_scope_test.py`), run directly against the real, imported `_record_member_action`:** five synthetic delegate calls reproducing the T3 incident's own shape (route file → model file → two migrations → unrelated tenant/subscription file) were fed through the actual function. Result: **every single call reset `no_progress_streak` to 0** — including the two marginal migration reads and the clearly-unrelated drift into tenant/subscription code — solely because each targeted a different file path. Output:

```
RELEVANT: the route file              -> no_progress_streak=0
RELEVANT: the model file              -> no_progress_streak=0
MARGINAL: seller-adjacent migration   -> no_progress_streak=0
MARGINAL: second migration            -> no_progress_streak=0
DRIFT: unrelated tenant/subscription  -> no_progress_streak=0
```

This directly confirms, against the real implementation rather than by inference: the existing guard can only detect a **literal repeat of the identical call** (same tool, same arguments). It has no concept of "relevant to the task" at all, by design. It therefore could not have interrupted the T3 drift, and no other existing mechanism (T16/T58/T59, the evidence ledger, read cache) carries a task-relevance signal either — all operate on textual/referential identity, not on the outstanding question's own scope.

### 3.4 Decision: no code change implemented this phase

Per this phase's explicit constraints (do not hardcode T3 file names or ban migration reads; do not add an LLM-based relevance classifier without demonstrating a simpler deterministic control cannot work; do not patch behavior based on speculation; make no speculative change if no defensible fix can be derived from the evidence):

No deterministic, generalizable signal was identified that could distinguish "task-relevant deep exploration" from "novel-but-irrelevant drift" without either (a) risking false positives against legitimate multi-hop investigation the existing guard was specifically designed to protect, or (b) relying on a narrow proxy (e.g., counting "verbatim"-phrased objectives, or file-path keyword matching) that would not generalize beyond this one incident's specific shape — exactly the kind of speculative, narrowly-targeted patch this phase's constraints rule out.

Extending the existing progress guard to also weigh task relevance would require some proxy for "relevance to the current objective" that does not yet exist in a deterministic, reusable form anywhere in this architecture. Building one responsibly is a separate, larger design question than this phase's scope permits, and the phase's own acceptance criteria explicitly allow concluding this.

### Verdict: Workstream 2 — **PARTIAL** (root cause and architectural boundary conclusively identified and evidenced against the real implementation; no safe implementation-level fix found within this phase's constraints; unresolved)

This does **not** mean the whitespace fix (Workstream 1) is held back — per this phase's own instruction, a confirmed, safe fix is deployed independently of an unresolved second workstream.

## 4. Combined integration/safety review (Part D)

- Whitespace detection confirmed to not misfire on legitimate formatted code (scenario 7 above) — no interaction risk with Workstream 2 since no Workstream 2 code change was made.
- Repetition/semantic-stall recovery retry budgets: unchanged this phase.
- Incomplete recovery output adoption: unchanged this phase; T57–T60's existing disclosure/completion-decision mechanisms are untouched and still govern this.
- No client-side (MCP) process was modified.
- No unrelated files were overwritten — confirmed via `git status --short` before and after the ZGX pull.

## 5. Deployment summary

| | Before | After |
|---|---|---|
| ZGX HEAD | `783ff09` | `c9e0247` |
| Service state | active | active (restarted, PID 1351630) |
| `/health` | n/a (pre-existing) | `{"status":"ok"}` |
| Whitespace guard (deployed) | absent | present, live-validated |

## 6. Remaining risks and smallest next step

- **Workstream 2 remains open.** The smallest controlled next step, if pursued in a future phase: instrument the existing evidence ledger's own `relevance_status` field (seen in `_evaluate_requirement_coverage`) to be logged per-delegation across several real battery runs, producing real precision/recall data on whether a relevance-aware extension of the progress guard is viable — before committing to any specific mechanism. This phase deliberately did not build that mechanism itself, since no such data yet exists and building first would reintroduce exactly the speculative-patch risk this phase's constraints are designed to prevent.
- `_looks_like_repetition_decay`'s own independent whitespace blind spot remains technically present but unreachable for this failure shape (see 2.1) — noted for completeness, not treated as an open defect requiring action, since the shared call-order already prevents it from ever being exercised by a pure-whitespace segment.

## 7. Honesty notes

- The original T3 run (both attempts) did **not** reach a reliable, reviewed completion — both were terminated (client timeout, then explicit user stop) before a final answer could be adjudicated. This report does not claim T3 "passed" or "failed" as a battery task; it uses the trace solely as root-cause evidence for the two workstreams above.
- No repository unit/regression tests were added, modified, or run. All validation was scratch-harness-based against the real, imported implementation, or live post-deployment checks against the actual deployed module.
- `hive-mcp` and `hive-mcp-agno` were not touched.
