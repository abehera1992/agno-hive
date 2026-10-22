# T66 — Provenance-Backed Evidence Sufficiency and Completion Integrity

## 1. Starting and ending commits

- Local HEAD at start: `ade8edc` (T65.1's report commit). ZGX deployed at start: `a33bb3c` (T65's code commit).
- Local/origin HEAD at end: `a1b0819`. ZGX deployed HEAD at end: `a1b08196252520de5ccf29f6de87d5951703d4e1`.
- Working tree: preserved throughout — the same long-standing, pre-existing modified/untracked set carried from prior phases, untouched. `/runs` on ZGX confirmed empty before and after deployment.

## 2. Files changed and purpose

`swarm/team.py` only (131 insertions, 23 deletions):
- `_CODE_IDENTIFIER_RE` — new regex, structural identifier-shape detector (PascalCase with an internal case transition, or snake_case with an underscore).
- `_relationship_support_claim(team)` — new function: the one deterministic signal this phase adds.
- `_evaluate_requirement_coverage` — extended to call the above for unresolved obligations, introducing a new coverage value `"supported"` alongside the existing `"satisfied"/"missing"/"unknown"`.
- `_completion_decision` — new rule 3 (additive): fails closed on an unresolved obligation still reported `"unknown"`, with a distinct reason string from `"missing"`.

No other file was changed. No test file was added, modified, or run.

## 3. Completion path before and after

**Before (T65.1's confirmed defect):** once a conceptual-trace task's evidence ledger was non-empty, `_completion_decision` always reached `COMPLETE` via its fallthrough line, regardless of whether the evidence was unrelated, partial, or merely co-incidental — because `_evaluate_requirement_coverage` reported every unresolved obligation `"unknown"` unconditionally, and `"unknown"` never blocked.

**After:** `_evaluate_requirement_coverage` now consults `_relationship_support_claim` once per call for any unresolved obligation. If a validated Claim exists (a single, untruncated, retained observation's own excerpt contains 2+ distinct code-identifier-shaped tokens), every unresolved target in that obligation set is reported `"supported"` — non-blocking, genuinely verified. Otherwise every unresolved target stays `"unknown"`, now blocking via the new rule 3. Resolved obligations (explicit entities, file paths, control-box-derived) take the exact same `"satisfied"/"missing"` path as before T65/T66 — entirely unaffected.

## 4. Implemented provenance and completion invariants

**Provenance invariant:** a relationship is never inferred from evidence assembled across separate observations. The ONLY accepted proof is that one real, untruncated tool observation's own content independently names two or more distinct identifiers together. This is built on the pre-existing, unmodified `Claim`/`_ClaimStore`/`_validate_claim_evidence` pipeline — `_relationship_support_claim` constructs a real `Claim` via the existing `_create_factual_claim`→`_make_claim` path, whose `authoritative` flag is computed, exactly as for every other claim type in this codebase, by checking that every cited `evidence_id` currently resolves in the ledger. No new, parallel source of truth was created.

**Completion invariant:** `_completion_decision`'s existing two blocking checks (ledger-empty+no-tool-called; confirmed-missing resolved obligations) are joined by a third: an unresolved obligation without a validated relationship claim blocks completion, with a reason string that explicitly distinguishes "relationship not demonstrated" from "confirmed missing" (per T65.1's own instruction: missing evidence is never proof of nonexistence). `_task_evidence_obligations` returning `None` (a genuinely non-decomposable task) is unaffected — that path never reaches this rule at all.

## 5. B–G results

All 8 run via the exact T65.1 scratch harness (`t65_1_scenarios.py`), against the real, imported `_task_evidence_obligations`/`_evaluate_requirement_coverage`/`_completion_decision`/`_relationship_support_claim` (not reimplementations):

| Case | Setup | Before T66 | After T66 | Required outcome | Result |
|---|---|---|---|---|---|
| A | Empty ledger, no tool calls | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | unchanged | **unchanged, correct** |
| B | Non-empty, unrelated evidence | COMPLETE (defect) | **INSUFFICIENT_EVIDENCE** | must no longer be COMPLETE | **FIXED** |
| C | Relevant route evidence only | COMPLETE (defect) | **INSUFFICIENT_EVIDENCE** | must no longer be COMPLETE | **FIXED** |
| D | Route + model independently, no relationship | COMPLETE (defect) | **INSUFFICIENT_EVIDENCE** | must no longer be COMPLETE | **FIXED** |
| E | Single excerpt explicitly shows the relationship | COMPLETE (same path as D — indistinguishable) | **COMPLETE** (via a validated `"supported"` claim — now a genuinely distinct, verified path) | a genuinely satisfied task must still complete | **PASS, now for the correct reason** |
| F | Explicit entities, both present | COMPLETE | COMPLETE | unaffected | **unchanged, correct** |
| G | Explicit entities, one absent | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | unaffected | **unchanged, correct** |
| H | Non-decomposable task shape | COMPLETE | COMPLETE | unaffected | **unchanged, correct** |

Zero failures against the required outcomes.

## 6. Focused boundary tests (newly discovered conditions)

Run via `t66_boundary_tests.py`, 5 scenarios, all against the real implementation:

| # | Test | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Truncated single observation, 2+ identifiers in excerpt | no claim (`None`) | `None` | PASS — truncation correctly blocks the signal, reusing `any_truncated_for_path` (GAP A) |
| 2 | Same excerpt, not truncated | an authoritative `Claim` | `Claim(..., authoritative=True)` | PASS |
| 3 | Two separate observations, each naming the same single identifier | no claim | `None` | PASS — repetition across observations is never corroboration |
| 4 | Plain English prose, no code-identifier-shaped tokens | no claim | `None` | PASS — confirms the signal is structural, not a word-count/keyword heuristic |
| 5 | A claim citing a nonexistent `evidence_id` | `authoritative=False` | `False` | PASS — the reused, unmodified `_validate_claim_evidence` discipline still governs this claim type |

## 7. Why no repository tests were run

Per this phase's own explicit instruction ("Avoid running the entire regression suite for a narrowly scoped fix unless a concrete risk requires it"), and consistent with every prior phase this session (T62–T65.1): no repository test file was added, modified, or run. All validation (13 scenario/boundary checks total) is scratch-harness execution against the real, imported production functions — `python -I t65_1_scenarios.py` and `python -I t66_boundary_tests.py`, both re-run after the fix and reported verbatim above. A full regression run was not judged necessary: the change is additive to one function pair (`_evaluate_requirement_coverage`/`_completion_decision`) with a confirmed-empty diff against every other mechanism (T62/T63 — see section 2), and every pre-existing call shape for those two functions (F/G/H, and the control-box path, scenarios 8/9 from the prior T65 harness) was re-verified unaffected.

## 8. Deployment and health evidence

- `git pull` on ZGX: fast-forward `a33bb3c → a1b0819` (3 files: `swarm/team.py` + T65/T65.1's report docs, not yet synced). All pre-existing untracked/modified files on ZGX preserved untouched.
- `systemctl --user restart agno-api.service` → active. `curl localhost:9001/health` → `{"status":"ok",...}`.
- Live post-deployment check, run directly against the deployed module on ZGX (service's own interpreter): Case B (unrelated evidence) → `INSUFFICIENT_EVIDENCE -- required relationship not demonstrated for: the api route, database model (...)`; Case E (single excerpt demonstrating the relationship) → `COMPLETE -- evidence ledger holds at least one observation`, with `CLAIM_RECORDED`/`CLAIM_VALIDATED` lines confirming the real Claim pipeline fired. Both match local results exactly.

## 9. Remaining limitations — semantic assessment not independently verifiable

- **The relationship-support signal is deliberately narrow and purely structural.** It proves "one real observation named two or more distinct identifiers together," never "those two identifiers are the specific route and model this task asked about," and never "the call relationship is semantically correct." A pathological excerpt naming two unrelated-but-identifier-shaped tokens together (e.g., two sibling classes mentioned in the same docstring, with no actual call relationship) would also pass. This is a known, accepted imprecision — the signal is intentionally conservative in the *other* direction (fails closed far more often than the old, unconditional "unknown never blocks" behavior), and no claim is made that it verifies call-graph correctness.
- **No new general relationship-lineage mechanism was built.** This phase deliberately avoided associating a specific delegation's own stated `evidence_required` with the evidence it produced (the broader extension named in T65's and T65.1's own reports) — the single-observation co-occurrence signal is a narrower, more conservative substitute that happens to satisfy the B–G acceptance cases without that larger undertaking.
- **The T65-era `_resolve_trace_target` tier-2 embedded-entity extraction has a latent, newly discovered, out-of-scope defect:** `_NAMED_TRACE_RE`'s own non-capturing group consumes a leading "the " before its second capture group begins, so a span like "to the Party model" is captured as "Party model" (no leading "the"), which `_EVIDENCE_ENTITY_RE` cannot match (it requires a literal "the " immediately before the capitalized entity). This means tier 2 of `_resolve_trace_target` currently never fires for the second endpoint of a `_NAMED_TRACE_RE` match in practice. No B–G acceptance case is affected (none involve an embedded entity in either span), and this was not fixed here, consistent with this phase's own scope boundaries — flagged for a future, independently-scoped phase.
- **No model-based classification or new model call was introduced**, consistent with the mission's explicit prohibition. The "model may propose an assessment" option in Section 2 was considered and deliberately not exercised — the deterministic, structural signal alone was sufficient to satisfy every required B–G outcome without it.

## 10. Verdict

**PASS.** All nine acceptance criteria are met with direct evidence:
1. Original task scope preserved — obligation targets are never altered, only their coverage classification.
2. Provenance is auditable — every relationship claim is a real, `CLAIM_RECORDED`/`CLAIM_VALIDATED`-logged `Claim` with inspectable `evidence_ids`/`authoritative`/`provenance`.
3. Unrelated evidence cannot satisfy an obligation — Case B, confirmed.
4. Required relationships cannot be inferred from mere co-occurrence across separate observations — Case D, confirmed, and boundary test 3.
5. Missing/ambiguous/incomplete evidence cannot cause false COMPLETE — Cases B/C/D and boundary test 1 (truncation).
6. B–G pass with the exact required outcomes (section 5).
7. Existing valid completion behavior (F/G/H, control-box path) is intact.
8. No delegation-stopping policy was introduced or touched — confirmed via diff (zero matches for T62/T63's own mechanisms).
9. Deployment and live validation are reported with direct evidence (section 8), not asserted.
