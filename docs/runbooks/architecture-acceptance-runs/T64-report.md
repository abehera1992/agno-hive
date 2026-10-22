# T64 — Evidence-Relevance Signal Audit and Runtime Observability

## 1. Repository, remote, and deployed state

- Local/origin HEAD at phase start: `fe22b29` (T63's report commit). Working tree clean except the long-standing, pre-existing modified/untracked set carried from prior phases (unchanged, untouched this phase).
- ZGX deployed HEAD at phase start: `c9e0247` (T62/T63's whitespace fix — one commit behind `origin/main`, but the gap was docs-only, nothing to deploy).
- `/runs` on ZGX: confirmed empty throughout.

## 2. Source-level data flow for `relevance_status`

Traced directly from `swarm/team.py`:

- `EvidenceRecord.relevance_status` (`team.py:5231`) is a field on **one evidence record** — one fact extracted from one tool-call observation. It is not a field on a delegation (`MemberControlState`), a requirement (`EvidenceObligation`), or a claim.
- Assigned exactly once, at record-creation time, by `_classify_relevance(evidence_type, retrieval_query)` (`team.py:5681-5705`), called from `_record_evidence_from_observation` (`team.py:5754`).
- Rule, read directly from the function:
  - `file_read` → **always `"retained"`, unconditionally.** No content, path, or query check of any kind.
  - `lexical_search` → `"retained"` only if `retrieval_query` is >4 characters and not in `_GENERIC_SEARCH_TERMS`; else `"needs_review"`.
  - `semantic_search` → always `"needs_review"`, unconditionally.
- Once written, `_EvidenceLedger.record()` never allows mutation (recording the same `evidence_id` twice raises, per its own docstring) — there is no mechanism to revise a record's `relevance_status` later, even if subsequent evidence shows the read was irrelevant.
- The only consumer that reads `relevance_status` is `_evaluate_requirement_coverage` (`team.py:6082-6116`), which filters the ledger to `relevance_status == "retained"` records before running its own, separate substring/exact-declaration match against task-derived obligation targets.

## 3. Answers to Part A's nine questions

1. **What does `relevance_status` mean?** `"retained"` or `"needs_review"` — a distinction between evidence TYPES (a deliberate file read vs. an unbidden/weak search hit), not a judgment about task relevance.
2. **Deterministic or model-generated?** Fully deterministic — `_classify_relevance` takes only `evidence_type` and a query string; no model call.
3. **Scope?** Evidence record only. Never conflated with delegation or requirement scope in the source.
4. **Before or after the next delegation?** Computed at observation time (synchronously inside the tool-call hook), strictly before the Coordinator's next delegation decision — but nothing reads it back into that decision. Confirmed: `_record_member_action` / `_make_coordinator_progress_guard_hook` (T63's subject) never touch the evidence ledger or `relevance_status` at all.
5. **Direct evidence of requirement advancement, or textual/identity similarity?** Neither, for `relevance_status` itself — it carries zero information for `file_read` (the dominant type). The actual textual/path-identity matching against task obligations happens entirely in the separate `_evaluate_requirement_coverage` function, which only consumes the (trivially-always-true, for file reads) `relevance_status` filter as a pass-through.
6. **Many-to-many?** Yes to both — confirmed by `_evaluate_requirement_coverage`'s structure: all retained records are pooled into one combined `haystacks` list, and each obligation is checked independently against the whole pool (first substring match wins; no per-record attribution).
7. **Missing/ambiguous/stale/contradicted handling?** None exists. The field is a fixed two-value, write-once field with no "unknown" state and no revision path.
8. **Any active consumer influencing completion/execution control?** One: `_evaluate_requirement_coverage` → `_completion_decision`'s coverage check, which can block completion (`INSUFFICIENT_EVIDENCE`). This is a **post-hoc, end-of-run** gate only — never consulted during in-flight exploration.
9. **"Progress" semantics, distinct from "a new action happened"?** Two entirely disconnected notions exist, confirmed never cross-referenced in the source: (a) T63's argument-novelty check (drives real-time stop/continue), and (b) this phase's literal-text coverage check (drives only final-answer acceptance, post-hoc). Neither measures a given delegation's actual contribution to the task in real time.

## 4. Action novelty vs. task progress — and a confirmed, separate defect

Beyond confirming T63's finding (argument novelty ≠ task relevance), this phase found and empirically confirmed a second, independent problem in the coverage-evaluation path itself.

**Finding:** the real, established T3 battery task (`docs/runbooks/groundedness-battery.md`) reads: *"How does seller verification work in this codebase? Trace it from the API route to the database model."* This matches `_NAMED_TRACE_RE` (`r"\bfrom\s+(.+?)\s+to\s+(?:the\s+)?(.+?)[.?!]"`, `team.py:5997`), which extracts the literal prose spans **"the API route"** and **"database model"** as obligation targets (normalized to lowercase). `_evaluate_requirement_coverage` then checks these as **literal substrings** against evidence paths/excerpts.

**Empirically verified** (scratch harness, against the real functions): even feeding in perfectly on-topic, directly relevant evidence (the actual seller route handlers and model classes a correct investigation would find — `register_seller`, `verify_seller`, `VerificationCheck`, `BusinessProfile`), `_completion_decision` returns `INSUFFICIENT_EVIDENCE` every time, because no real file path or code identifier will ever contain the literal phrase "the api route" or "database model". This is a **false negative independent of investigation quality** — the coverage check for this exact, real, repeatedly-used battery task cannot pass regardless of how well the Coordinator actually performs.

Also checked whether the control-box-derived obligation path (tried first, per `_task_evidence_obligations`'s own preference order) would avoid this: it requires ≥2 delegations whose own `objective`/`evidence_required` text matches the narrower `_EVIDENCE_ENTITY_RE` (`"the <Capitalized> model/class/table/schema"` shape). With realistic, mixed delegation phrasing (only some delegations happen to use that exact shape), the control-box path returns `None` and the run falls through to the unreliable task-text fallback — confirmed via harness.

**This defect is reported, not fixed, this phase.** It is separate from the relevance-aware-control question T64 was built to investigate, and a safe correction requires first characterizing the full set of "from X to Y"-phrased task shapes this extractor needs to keep working for (to avoid narrowing it into exactly the kind of under-generalizing proxy T63/T64 both warn against). This is recorded as the clearest candidate for a future, independently-scoped, narrow phase.

## 5. Observability design and implementation (Outcome 1 + Outcome 3, combined)

**Outcome 3 (primary): `relevance_status`/coverage are not reliable enough for runtime control**, for three independent, now-confirmed reasons: (a) `relevance_status` itself is unconditional for the dominant evidence type and carries no relevance information; (b) the one mechanism that does attempt relevance judgment (`_evaluate_requirement_coverage`) is a post-hoc, literal-substring match that can produce guaranteed false negatives on realistic, real task phrasing (section 4); (c) nothing in this signal is available or consulted during in-flight exploration — it cannot gate a delegation decision that has already happened by the time it is computed.

**Outcome 1 (secondary, implemented): a genuine, bounded observability gap exists and was closed.** The coverage verdict was computed every run but never logged anywhere — confirmed by grep: no `print`/journal line referencing the `coverage` dict or `obligations` existed before this phase. A `SATISFIED` run left zero trace of what was checked or why it passed; an `UNKNOWN`-shaped task (obligations not extractable — the common case for free-form tasks) was indistinguishable in the journal from "checked and passed", since both produced silence. Separately, a delegation's own stated `evidence_required`/`completion_criteria` were already validated and held in `MemberControlState` at `_start_member_objective`'s own point of execution, but only `objective` was logged.

**Implemented** (`swarm/team.py`, two additive changes, 34 lines, no control-flow change):

1. In `_completion_decision`, when `obligations` exist: log `source`, `targets`, and the full `coverage` dict (`{target: "satisfied"|"missing"}`) — values already computed, now also journaled. When `obligations` is `None` (UNKNOWN): log that explicitly, instead of silence.
2. In `_start_member_objective`: extend the existing `CONTROL_BOX_OBJECTIVE_STARTED` line to also log `evidence_required` and `completion_criteria` — both already-validated arguments at that exact point, previously dropped.

Both changes: reuse existing computed values (no new computation), change no return value, log only short, already-model-or-task-derived strings (no raw evidence payloads, no secrets, no unbounded content), and touch no stop/continue decision anywhere.

## 6. Controlled scenarios (Part C) — expected vs. actual, against the real implementation

All 7 run via `t64_part_c_scenarios.py`, a scratch harness directly exercising the real, imported `_completion_decision`, `_evaluate_requirement_coverage`, `_start_member_objective`, and `_record_member_action` (not reimplementations). This is **field-assignment and requirement-coverage-semantics testing**, not an end-to-end Coordinator run — noted explicitly, per this phase's own instruction not to conflate the two.

| # | Scenario | Expected | Actual | New journal line |
|---|---|---|---|---|
| 1 | Relevant multi-hop, named-entity task, both targets found | COMPLETE | COMPLETE | `coverage={'pricingplan': 'satisfied', 'businessprofile': 'satisfied'}` |
| 2 | Justified migration lookup | n/a (demonstration) | `relevance_status='retained'` — same value an unjustified read gets | n/a — demonstrates the field cannot distinguish justified from unjustified |
| 3 | Unproductive distinct reads, neither target found | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | `coverage={...: 'missing', ...: 'missing'}` |
| 4 | Unrelated-module drift | `no_progress_streak` stays 0 (T63 finding reconfirmed) | confirmed `no_progress_streak=0` | (progress-guard log line; coverage log would show `missing` but cannot attribute which delegation caused it — coverage is global, not per-delegation) |
| 5 | Relevant file read, specific claim not established | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | `coverage={...: 'missing', ...: 'missing'}` |
| 6 | Genuine unresolved gap — the real T3 task text, zero evidence | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | `coverage={'the api route': 'missing', 'database model': 'missing'}` |
| 7 | UNKNOWN task shape (no obligations extractable) | COMPLETE, with explicit UNKNOWN disclosure | COMPLETE | `REQUIREMENT_COVERAGE: UNKNOWN -- task shape not mechanically decomposable` |

All 7 passed against expectations. Scenario 1 (entity-shaped task, correct positive) directly contrasts with scenario 6 (the real T3 task, same underlying mechanism, guaranteed-false-negative shape) — the same code path, two different outcomes purely from task phrasing, which is the core of section 4's finding.

**Limitation, stated plainly:** these are implementation-level and field/requirement-coverage-semantics harnesses. None of them exercise an actual Coordinator decision loop; they confirm what the deterministic functions do with controlled inputs, not what a live run's Coordinator would choose to do next. No live Coordinator run was exercised this phase (per this phase's own constraint against launching a new T3 battery). If a future phase wants to measure this signal's real precision/false-positive rate, the smallest bounded real-trace sample is: re-run a small number (3-5) of already-established battery tasks (T3, T11, and 1-2 others with differing phrasing shapes) with this phase's new logging live, and read the `REQUIREMENT_COVERAGE` lines back from the journal — no new battery infrastructure needed, since the logging is already deployed.

## 7. Code change, file scope, commits

- `swarm/team.py` only — 34 insertions, 1 modified call site (the `print` in `_start_member_objective`), no deletions of logic, no control-flow change.
- Commit: `32f061b6384944509acce12f1c94d464e60e2055` (`feat(T64): log requirement-coverage verdicts and delegation evidence requirements`), pushed to `origin/main`.
- No repository unit/regression tests added, modified, or run. No change to the T62 whitespace fix, `hive-mcp`, or `hive-mcp-agno`.

## 8. Deployment evidence

- ZGX: `git pull` fast-forwarded `c9e0247 → 32f061b` (2 files: `swarm/team.py` + T63's report doc, which had not yet synced). All of ZGX's own pre-existing untracked/modified files preserved untouched (confirmed via `git status --short` before the pull).
- `systemctl --user restart agno-api.service` → active, PID 1404253.
- `curl localhost:9001/health` → `{"status":"ok",...}`.
- Post-deployment validation: ran the real T3 task text through the deployed `_completion_decision` directly on ZGX (via the service's own interpreter) — correctly logged `REQUIREMENT_COVERAGE: source='task_text' targets=['the api route', 'database model'] coverage={'the api route': 'missing', 'database model': 'missing'}` and returned `INSUFFICIENT_EVIDENCE`, matching local results exactly.

## 9. Recommendation on trustworthiness for future runtime control

**Not yet trustworthy enough to drive any stop/continue or completion decision beyond what it already (weakly) does.** Three independent, now-evidenced reasons: `relevance_status` itself carries no relevance information for the dominant evidence type (unconditional for `file_read`); the one mechanism that does attempt relevance judgment is a literal-substring match proven to produce false negatives on real, already-established battery task phrasing; and the signal is computed post-hoc only, never available to gate an in-flight delegation decision. Using it as the basis for a relevance-aware stopping control today would risk exactly the failure mode this phase's constraints were written to prevent — a speculative mechanism built on an unvalidated, and in one concrete case demonstrably wrong, signal.

## 10. Smallest next step

Two independent, small, evidence-grounded next steps, neither of which requires a new mechanism yet:

1. **Fix the confirmed `_NAMED_TRACE_RE` false-negative defect (section 4)** in its own narrowly-scoped phase — first by cataloguing every existing/intended "from X to Y" task shape this extractor must keep supporting (not just T3), then deciding whether to require entity-shaped targets (as `_EVIDENCE_ENTITY_RE` already does) or to drop literal-substring matching for this extraction source entirely in favor of marking such obligations `UNKNOWN` rather than guessing wrong.
2. **Collect real precision/false-positive data** using this phase's new logging across a small number of already-established battery tasks (section 6's limitation note) before designing any relevance-aware runtime control — the data needed to evaluate whether such a control is even viable does not yet exist, and building the control before the data would repeat the speculative-patch risk this phase and T63 were both built to avoid.

## Verdict: **PARTIAL**

Observability gap confirmed and closed (Outcome 1, deployed, validated). Relevance semantics confirmed unreliable for runtime control, with a precise, evidenced limitation documented (Outcome 3). A separate, concrete correctness defect was found and evidenced but deliberately not fixed this phase, pending proper scoping (Outcome 4, deferred). No claim of task-relevance control being solved — none was attempted, consistent with this phase's own mandate.
