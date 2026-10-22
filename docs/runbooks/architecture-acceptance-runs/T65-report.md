# T65 — Correct Requirement-Target Extraction and Evidence-Coverage Semantics

## 1. Initial state

- Local/origin HEAD at phase start: `7362eaa` (T64's report commit).
- ZGX deployed HEAD at phase start: `32f061b` (T64's code commit — one commit behind origin, gap was docs-only).
- Working tree clean except the long-standing, pre-existing modified/untracked set from prior phases (unchanged, untouched this phase).
- `/runs` on ZGX: confirmed empty throughout.

## 2. Old extraction and coverage behavior

`_NAMED_TRACE_RE` (`r"\bfrom\s+(.+?)\s+to\s+(?:the\s+)?(.+?)[.?!]"`) has no shape constraint on its two captured groups — it extracts whatever prose sits between "from"/"to" and the sentence end. Applied to the established T3 task ("Trace it from the API route to the database model."), this produced `EvidenceObligation` targets `"the api route"` and `"database model"` (normalized lowercase). `_evaluate_requirement_coverage` then checked these as **literal substrings** against evidence paths/excerpts. Since no real file path or code declaration will ever contain those exact prose phrases, the check reported `INSUFFICIENT_EVIDENCE` regardless of investigation quality — confirmed empirically in T64 with perfectly on-topic, directly relevant evidence.

## 3. Task-shape catalogue (Part A)

Catalogued directly from the source and `docs/runbooks/groundedness-battery.md` (the only existing "from X to Y"-shaped battery task is T3 itself — no second real example exists to calibrate against, so the fix is designed from the mechanism's own stated categories, not overfit to T3's exact wording):

| # | Shape | Example | Handled before this phase | Handled after |
|---|---|---|---|---|
| 1 | Explicit entity/path targets | "from parties_api.py to the Party model" | Correctly (literal match works) | Unchanged |
| 2 | Conceptual targets | "from the API route to the database model" (real T3) | **False negative** (literal match on prose) | Fixed — unresolved, reported `"unknown"` |
| 3 | Multi-hop relationship (route→service→model→schema) | — | Not distinguished from simple 2-endpoint check; no path/relationship evidence exists | Unchanged — documented limitation (section 7) |
| 4 | Multiple concrete required entities | "find the PricingPlan model and BusinessProfile model" | Correct (entity-list branch, `_EVIDENCE_ENTITY_RE`) | Unchanged |
| 5 | Non-decomposable/ambiguous | "take a look at the storage service" | Correct (`None`/UNKNOWN) | Unchanged |
| 6 | Control-box-derived, complete | 2+ delegations with entity-shaped `evidence_required` | Correct | Unchanged (`resolved=True` throughout) |
| 7 | Control-box incomplete → task-text fallback | 1 delegation, or non-entity-shaped text | Correct fallback, but fallback itself was unsafe for shape 2 | Fallback now safe for shape 2 |

## 4. The obligation contract (Part B)

`EvidenceObligation` gained one additive field: `resolved: bool = True`. The default preserves every pre-existing call site's behavior unchanged (control-box path, entity-list path). `resolved=False` is produced only by the new `_resolve_trace_target` classifier, applied to each `_NAMED_TRACE_RE`-captured span, in three deterministic tiers:

1. **Path-shaped** (contains `/`, `\`, or ends in a short dotted extension) → literal target, `resolved=True`.
2. **Contains an embedded `_EVIDENCE_ENTITY_RE` entity** (e.g. "the Party model" → "Party") → the embedded entity becomes the target, `resolved=True` — reusing the same extraction the entity-list branch and control-box path already trust, applied to a sub-span.
3. **Neither** (a generic architectural/conceptual phrase, e.g. "the API route") → `resolved=False`.

No model call, no new keyword list, no T3-specific identifier anywhere in the classifier.

`_evaluate_requirement_coverage`'s return type gained a third value: `"unknown"`, reported unconditionally for any `resolved=False` obligation, which is **never checked against the ledger at all**. `_completion_decision` required **no code change**: its blocking check (`missing = [t for t, s in coverage.items() if s == "missing"]`) already only reacts to the literal string `"missing"`, so `"unknown"` is automatically non-blocking.

## 5. Code change, files, commit

- `swarm/team.py` only — 95 insertions, 27 deletions (mostly docstring expansion). New function `_resolve_trace_target`; `EvidenceObligation` gains `resolved: bool = True`; `_task_evidence_obligations`'s `_NAMED_TRACE_RE` branch now classifies each span; `_evaluate_requirement_coverage` short-circuits unresolved obligations to `"unknown"` before any ledger check.
- Commit: `a33bb3c533b0cb8fd65d0c4ea5f5b53195c7f8c0` (`fix(T65): stop treating conceptual trace phrases as literal evidence targets`), pushed to `origin/main`.
- No change to `_looks_like_repetition_loop`/`_looks_like_repetition_decay` (T62), `_record_member_action`/the coordinator progress guard (T63), or T64's logging statements themselves (confirmed via diff: zero matches for either in the diff) — T64's existing `REQUIREMENT_COVERAGE` print line is unchanged code, it simply now also surfaces `"unknown"` values as part of the same, already-deployed logging.
- No repository unit/regression tests added, modified, or run. No change to `hive-mcp` or `hive-mcp-agno`.

## 6. Scratch-harness scenarios and outcomes (Part D)

All 10 run via `t65_part_d_scenarios.py` against the real, imported `_task_evidence_obligations`, `_evaluate_requirement_coverage`, `_completion_decision`, and `_resolve_trace_target` (not reimplementations).

| # | Scenario | Expected | Actual | Coverage/obligation detail |
|---|---|---|---|---|
| 1 | Real T3 task + realistic relevant evidence | COMPLETE | **COMPLETE** | `{'the api route': 'unknown', 'database model': 'unknown'}` |
| 2 | Real T3 task, zero evidence | INSUFFICIENT_EVIDENCE | **INSUFFICIENT_EVIDENCE** | via the pre-existing, untouched "ledger empty + no tool called" check — not via coverage |
| 3a | Explicit concrete targets, both present | COMPLETE | **COMPLETE** | `{'pricingplan': 'satisfied', 'businessprofile': 'satisfied'}` |
| 3b | Explicit concrete targets, both absent | INSUFFICIENT_EVIDENCE | **INSUFFICIENT_EVIDENCE** | `{'pricingplan': 'missing', 'businessprofile': 'missing'}` |
| 4 | Mixed conceptual/entity endpoints — resolution check | endpoint1 unresolved, endpoint2 resolved to "party" | **confirmed exactly** | `_resolve_trace_target("the API route") → ('the api route', False)`; `_resolve_trace_target("the Party model") → ('party', True)` |
| 4b | Same mixed task, evidence supports the resolved endpoint | COMPLETE | **COMPLETE** | both reported `'unknown'` (current implementation does not yet selectively verify the resolved sub-target independently — see limitation, section 7) |
| 5/6 | Both endpoints resolve to real entities; relationship not verifiable | n/a — documents a limitation, not a pass/fail check | — | see section 7 |
| 7 | Multiple explicit targets, one missing | INSUFFICIENT_EVIDENCE | **INSUFFICIENT_EVIDENCE** | `{'pricingplan': 'satisfied', 'businessprofile': 'missing'}` |
| 8 | Control-box obligations, complete/valid | all `resolved=True`, unchanged behavior | **confirmed** | `[EvidenceObligation(..., resolved=True), EvidenceObligation(..., resolved=True)]` |
| 9 | Control-box incomplete → task-text fallback | falls back, both targets now unresolved (not falsely literal) | **confirmed** | `[EvidenceObligation(target='the api route', resolved=False), EvidenceObligation(target='database model', resolved=False)]` |
| 10 | Non-decomposable task shape | `None` (UNKNOWN) | **`None`** | unchanged |

All 10/10 passed. (One transient failure during the first run of scenario 2 was traced to a bug in the scratch harness itself — `tool_outcomes or {...}` silently treating an intentionally-empty `{}` as falsy and substituting the default dict — not a defect in the real implementation; fixed in the harness and the scenario re-run and confirmed passing.)

## 7. What the fix proves, and what remains unknown

**Proven:** the specific, confirmed false negative (T64) is fixed — the real T3 task, with relevant evidence, now correctly reaches `COMPLETE` instead of a guaranteed `INSUFFICIENT_EVIDENCE`. Explicit, genuinely concrete requirements (named entities, file paths, multi-entity tasks) are checked exactly as before, including correctly reporting `missing` when genuinely absent — the fix does not weaken real evidence requirements. A task with zero evidence at all still correctly reports `INSUFFICIENT_EVIDENCE`, via a wholly separate, pre-existing, untouched check.

**Explicitly not proven, and not claimed:**
- **No relationship/path verification exists.** Even when both endpoints of a "from X to Y" task resolve to real, checkable entities and both are independently found in evidence, the implementation cannot distinguish "both endpoints happen to be present somewhere in the evidence" from "a route was actually traced to that specific model." `EvidenceRecord` carries no call-graph/reference-edge information between records, and this phase did not add any — per the mission's own instruction to report what remains unsupported rather than invent a relevance mechanism.
- **Endpoint existence is not proof of a complete trace.** This is the same limitation stated differently: the fix makes the *target-extraction* step safe, not the *connection-verification* step, which does not exist in this codebase at any evidence granularity.
- **No live Coordinator run was exercised.** All validation is implementation-level (direct calls to the real functions with controlled inputs), consistent with the mission's explicit prohibition on launching a full T3 battery for this phase. Whether a live Coordinator run's real evidence shape matches the synthetic scenarios here has not been demonstrated end-to-end.

## 8. Deployment and post-deployment evidence

- `git pull` on ZGX: fast-forward `32f061b → a33bb3c` (2 files: `swarm/team.py` + T64's report doc, not yet synced). All pre-existing untracked/modified files on ZGX preserved untouched.
- `systemctl --user restart agno-api.service` → active. `curl localhost:9001/health` → `{"status":"ok",...}`.
- Post-deployment check, run directly against the deployed module on ZGX (service's own interpreter): the exact T3 task text with relevant evidence → `COMPLETE` (positive control); the same task with zero evidence → `INSUFFICIENT_EVIDENCE` (negative control). Both match local results exactly.

## 9. Separate verdicts

- **Extraction correctness: PASS.** `_resolve_trace_target`'s three-tier classification is deterministic, bounded, reuses the existing, already-trusted `_EVIDENCE_ENTITY_RE` for the embedded-entity case, and was validated against both the real T3 phrasing and independently-constructed task shapes (not just T3).
- **Evidence sufficiency: PARTIAL.** Genuine evidence requirements are preserved and correctly enforced for every resolvable target shape. The multi-hop relationship/connection-verification gap (section 7) remains fully open and is explicitly not claimed as solved.
- **Deployment: PASS.** Clean fast-forward, service healthy, change confirmed live against the deployed module with both a positive and negative control.

## 10. Next smallest step

If a future phase wants to close the relationship-verification gap (section 7, the only structurally open piece of this mechanism), the smallest evidence-grounded step is: determine whether `EvidenceRecord`'s existing `tool_call_id` lineage (shared by all records from one observation) can be extended, additively, to also record a caller-stated "this evidence was read in service of resolving endpoint X's connection to endpoint Y" association at delegation time — reusing the Control Box's own `evidence_required` text rather than inferring it — before considering any heuristic or model-based relationship check. This is deliberately not attempted here, consistent with this phase's own scope (requirement-target extraction and coverage semantics only).
