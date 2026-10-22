# T61 — Completion Contract and Consumer Boundary Review

Builds on, and does not overwrite, prior reports through `T60-report.md`.
No code change this phase. Local HEAD and `origin/main` remain at
`266edd37e7906f13a839f7901253b78ef8297d7b` (T60's final report commit).
No deployment was performed — none was warranted (§G).

## A. Verdict

**PASS.** The actual consumer map is traced end to end, from the user's
request through `_completion_decision`'s own already-computed,
already-active status, through every real caller of `run_task_async`, to
the two real secondary consumers (`run_chunked`, `/feedback`) that read a
run's final text. No current consumer is confirmed to make an incorrect
decision because structured completion status is unavailable — two
theoretically-grounded, source-confirmed gaps were found in those
secondary consumers, but neither has demonstrated production harm, and
the mission's own Outcome A criteria are squarely met.

## B. Architectural decision

**No current real consumer needs a structured completion status.**
AgnoHive already has a deterministic, actively-running completion
decision (`_completion_decision`, INV-11, status `"COMPLETE"` or
`"INSUFFICIENT_EVIDENCE"` with a reason) — this is NOT the same finding
as T60's `_coverage_verdict` (which was genuinely dead code). This
mechanism runs on every request, correctly refuses to guess
`INSUFFICIENT_EVIDENCE` as complete, and — when it or any of three
sibling signals (`_unresolved_ledger_citations`, `_fabricated_execution_
claims`, `_claimed_action_without_lineage`) fires — appends a specific,
loud, well-worded disclosure banner to the final text. Every traced,
real, PRIMARY consumer (an LLM or a human reading `RunResponse.result`/
`PlanResponse.plan`) receives this disclosure and, confirmed empirically
throughout this session's own T51–T60 work, correctly acts on it every
time it has appeared. The decision is NOT exposed as a separate
structured field anywhere in the return path — but no traced consumer
currently needs it to be.

Two SECONDARY, automated (non-LLM) consumers — `run_chunked`'s own
chaining loop and `/feedback`'s "good" rating path — consume a run's
final text without inspecting it for this disclosure, and therefore
cannot distinguish "this chunk/session's evidence was flagged
insufficient" from "this chunk/session was clean." Both are real,
source-confirmed observations. Neither has a demonstrated incident,
log evidence, or reported symptom behind it this phase — they are
risks identified by tracing the architecture, not defects discovered by
tracing a failure. Per the mission's own explicit instruction not to
treat a theoretically cleaner contract as proof of a production defect,
and given the significant scope a fix would require (crossing the
worker-subprocess process boundary that currently carries only
`{content, tokens, clarification}`), this phase makes no code change and
documents the precise trigger that would justify revisiting each.

## C. Actual consumer map

Traced via direct source read, not inferred:

1. **The 3 real callers of `run_task_async`** (confirmed by grep — no
   others exist):
   - `main.py`'s `_run_worker()` (the `/run` endpoint's worker-subprocess
     entry point) — unpacks `(content, tokens, clarification)`, returns
     `{"content": content, "tokens": tokens, "clarification":
     clarification}` as the ONE JSON line the parent process reads. No
     completion-status field crosses this boundary.
   - `main.py`'s CLI one-shot path (`__main__`) — same unpacking, prints
     `result` directly to a human's terminal.
   - `api/server.py`'s `/plan` endpoint — unpacks `(plan_text, _,
     clarification)`, returns `PlanResponse(plan=plan_text, ...)`. Also
     human-in-the-loop by design (`hive CLI --review`) — a human reads
     the plan text before approving execution.
2. **`api/server.py`'s `/run` endpoint** — calls `_run_worker_subprocess`
   (which spawns the above worker and parses its one JSON line),
   receives `(result, tokens, clarification)`, returns `RunResponse`.
   `RunResponse`'s actual fields (read directly from its construction at
   the call site): `result`, `team`, `agents_used`, `models_pulled`,
   `duration_seconds`, `session`, `input_tokens`, `output_tokens`,
   `total_tokens`, `needs_clarification`. **No completion-status field.**
   `needs_clarification` is a genuinely separate mechanism (the model
   asking a clarifying question), not an evidence-sufficiency signal.
3. **`run_chunked`** — calls `/run` once per chunk via the SAME in-process
   `run()` function, builds `ChunkResult(index, task, result=resp.result,
   status="ok"|"failed", ...)`. Confirmed by direct read: `status` is set
   from EXCEPTION/HTTP-level outcome ONLY — the loop never inspects
   `resp.result`'s own text for a disclosure banner before chaining the
   NEXT chunk's session onto it, or before reporting `status="ok"`.
4. **`/feedback`** — a human-submitted rating (`good`/`bad`), NOT an
   automated decision. On `rating="good"`, queues `_queue_outcome` and
   (if `session_id` given) `execution_store.promote_session_claims` —
   both feed FUTURE runs' own exemplar/memory context. Confirmed by
   direct read: never consults `_completion_decision`'s own status for
   that session; relies entirely on the human's own judgment, which may
   or may not have accounted for a disclosure banner the human read.
5. **The actual primary consumer, confirmed by this session's own
   history, not inferred**: an LLM (Claude Code, via
   `mcp__ekamapp__agno_run`) or a human (hive CLI), reading
   `RunResponse.result`/`ChunkResult.result`/`PlanResponse.plan` as free
   text. This session's own T52–T60 work repeatedly encountered exactly
   these banners live (e.g., T54/T56's `"**NOT AUTHORIZED AS COMPLETE..."`,
   T53's `"**UNRESOLVED — THIS ANSWER CONTRADICTS..."`, T57's
   `"**INCOMPLETE ANSWER..."`) and correctly treated them as signals that
   the answer needed scrutiny, every time — direct, repeated, empirical
   confirmation that the free-text contract serves this consumer
   correctly today.

## D. `_coverage_verdict` assessment

Re-confirmed, not re-litigated: still zero callers (unchanged since T60).
Separately, and more importantly for this phase, `_completion_decision`
(a DIFFERENT, ACTIVELY-WIRED function, not examined in T60's own report)
already provides the authoritative completion signal `_coverage_verdict`
was designed to normalize INTO — `_completion_decision`'s own "GAP B"
layer explicitly folds requirement-coverage (`_task_evidence_obligations`
/ `_evaluate_requirement_coverage`) into its `COMPLETE`/
`INSUFFICIENT_EVIDENCE` decision, using a narrower, more conservative
semantics than `_coverage_verdict`'s own three-way `complete`/
`incomplete`/`unknown` (confirmed by direct read of `_completion_decision`'s
own docstring: "Sufficiency stays UNKNOWN... folded into COMPLETE, not
asserted as a separate blocking state... only a CONFIRMED-MISSING
obligation blocks completion"). Reusing `_coverage_verdict` for a
real consumer would require reconciling TWO differently-opinionated
normalizers over overlapping signals — not a clean activation. This
phase's finding on `_coverage_verdict` is unchanged from T60: still
correct, still unused, still not causing loss (its underlying signals
remain independently disclosed), and now additionally understood to be
redundant with `_completion_decision`'s own, already-active coverage
layer rather than filling a genuine void.

## E. Consumer-contract matrix

| Invariant | Status | Evidence |
|---|---|---|
| A consumer cannot mistake an explicit incomplete/interrupted result for verified-complete | **Verified for the primary consumer; Gap confirmed (unproven harm) for the two secondary ones** | The primary consumer (LLM/human reading free text) has correctly distinguished these every time this session. `run_chunked`'s `status="ok"` and `/feedback`'s human-rating path do not cross-check `_completion_decision`'s own signal — confirmed by source, not yet confirmed to have caused a wrong decision. |
| Unknown verification result stays distinct from clean | **Verified** | `_verify_claims`' own `unavailable` vs. `bad=False` distinction (T60's own fix target), `_completion_decision`'s own UNKNOWN-folds-into-COMPLETE-but-never-the-reverse rule, both confirmed by direct read this phase and T60's. |
| A structured status, if present, agrees with actual evidence/verification disposition | **Not applicable** | No structured status exists in the traced return path; nothing to check for disagreement. |
| Existing useful partial answers and disclosures are preserved | **Verified** | `_tail()`'s own composition (T60's own fix), `_completion_control_check`'s own additive-only banner (never replaces `content`, only appends) — both confirmed unchanged this phase. |
| Completion status is scoped to the correct run, no cross-run leak | **Verified** | `_completion_decision` reads only `team`-instance attributes (`_member_result_chars`, `_evidence_ledger`, `_tool_outcomes`) — confirmed per-run, not module/process-global, by direct read. |
| No consumer is forced to parse prose when it has a demonstrated need for machine-readable status | **Not applicable — no consumer with a DEMONSTRATED need was found** | `run_chunked`/`/feedback` have a PLAUSIBLE need, not a demonstrated one (§B). The primary consumer parses prose successfully by design and by this session's own repeated, confirmed experience. |
| Adding structured status would not falsely imply deterministic semantic completeness | **Not applicable this phase** | No structured status is being added. |

## F. Implementation

**No code change.** The investigation did not establish a demonstrated
defect meeting the bar for Outcome B or C — both candidate gaps
(`run_chunked`, `/feedback`) are real, source-confirmed inconsistencies
in principle, but neither has an observed incident, log evidence, or
reported symptom behind it, and the mission's own explicit instruction
is not to treat a theoretically cleaner contract as proof of a
production defect. Manufacturing a multi-file, process-boundary-crossing
change (threading a new field through `run_task_async`'s return tuple,
the worker-subprocess JSON contract, and `RunResponse`/`ChunkResult`) to
close a risk with no demonstrated harm would itself violate this phase's
own "smallest justified correction" and "do not manufacture work"
constraints.

## G. Validation and deployment

**Source tracing only** — the applicable evidence tier for this phase,
per the mission's own framing ("if no concrete gap is established,
finish T61 with a source-backed architectural decision and no code
change"). Every claim in §C was confirmed by direct read of the current
source (`swarm/team.py`'s `_completion_decision`/`_completion_control_check`/
`run_task_async`; `main.py`'s `_run_worker`; `api/server.py`'s `/run`,
`/plan`, `run_chunked`, `/feedback`), not inferred from prior reports or
comments. No local controlled harness, deployed-path validation, or
natural run was performed or needed, since no code change was made.

**No deployment was performed.** `git status --short` confirms the
working tree is unchanged by this phase beyond the usual pre-existing
dirty/untracked files recorded in every prior report. ZGX's own deployed
HEAD remains whatever T60 last deployed (`783ff0907fe041fefd0025dc77e9b983e1aebb92`);
this phase did not check or restart `agno-api.service`, since restarting
a service to "complete" a phase that made no code change would itself
violate this phase's own explicit instruction.

## H. Remaining risks

- **`run_chunked`'s chaining loop cannot distinguish a chunk whose own
  evidence was flagged `INSUFFICIENT_EVIDENCE` (or carries a
  `_completion_control_check`/Phase H/Phase R disclosure banner) from a
  clean one** — confirmed by source, not by an observed incident. The
  concrete future requirement that would justify revisiting this: a
  reported or logged case where a chained run's own degraded evidence
  propagated into a later chunk's context without the caller noticing,
  or `run_chunked`'s own usage growing enough that this risk's expected
  frequency rises materially above today's.
- **`/feedback`'s "good" rating path does not cross-check
  `_completion_decision`'s own status for that session** before
  promoting its content into exemplar memory / `project_memory_promotions`
  — confirmed by source. The concrete future requirement that would
  justify revisiting: evidence that a human-rated-good run whose own
  evidence ledger had flagged `INSUFFICIENT_EVIDENCE` was later found to
  have poisoned a retrieved exemplar (the SAME class of incident
  `project_lightrag_groundedness_poisoning` already documented for a
  DIFFERENT auto-recording mechanism, retired for exactly this reason —
  worth checking against if `/feedback`'s own "good" ratings are ever
  audited).
- **This phase's own finding rests on a single session's worth of direct
  experience as "confirmation" that the primary consumer correctly reads
  free-text disclosures.** This is real, repeated, empirical evidence
  (not a guess), but it is one observer's experience across one
  extended session, not a large-sample audit.

## I. Next step

No material gap was confirmed that justifies a code change this phase.
The highest-value action, should either risk in §H ever materialize into
a demonstrated incident, is the SAME narrow fix in both cases: have the
specific secondary consumer (`run_chunked` or `/feedback`) check the
relevant session/chunk's own content for `_completion_decision`'s
disclosure — reusing the already-computed, already-correct signal, not
adding a new judge or a new return-path field. Absent that triggering
evidence, no T62-style follow-up is proposed from this review alone.
