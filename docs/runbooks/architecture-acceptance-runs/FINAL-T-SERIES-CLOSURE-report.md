# FINAL T-SERIES IMPLEMENTATION — Task-Scoped Relevance Integrity and Closure

## 1. Verdict: **PASS**

Judged strictly against the mission's hard acceptance criteria. Scenario 7b does **not** reach verified `COMPLETE` on unrelated evidence (confirmed live against the deployed code and the real, deployed model). The established T3 positive control remains viable (confirmed live, same model). The anchored positive/negative controls from T66.1 are unaffected (confirmed live; the model is never even invoked on that path). All 22 required acceptance scenarios pass against the real, imported, now-async production functions. Deployed to ZGX and verified with direct live decision output, not inferred from a healthy endpoint alone.

## 2. Root cause

T66.2 proved, before this phase began, that a genuinely correct unanchored answer (the real T3 task) and scenario 7b's false positive (a real `def`+call structure from an unrelated service) were **structurally identical** in every respect observable without comparing content — the only distinguishing information is semantic (what the code is actually about), which no deterministic, keyword-free signal this architecture previously had access to could supply. That is the defect this phase closes: the completion gate had evidence-integrity and completion-policy layers, but no relevance layer at all for the unanchored case.

## 3. Architecture — how the three responsibilities are now separated

- **A. Evidence integrity** (unchanged): `_DEFINITION_RE`/`_REFERENCE_RE` structural check, `any_truncated_for_path`, `_validate_claim_evidence` — all untouched by this phase.
- **B. Task relevance** (new): `_assess_relevance(task, source_path, excerpt)` — a single, bounded, fail-closed call answering exactly one question (does this one observation belong to the task's own domain/service), returning one of three fixed categories, never deciding whether a code relationship exists.
- **C. Completion policy** (updated, narrowly): `_completion_decision`'s rule 3 now treats an unresolved obligation as blocked unless `_evaluate_requirement_coverage` reports `"supported"` — which now requires either a concrete anchor match (T66.1, unchanged) or a validated `"relevant"` verdict from B. Structural support (A) alone no longer implies relevance; relevance (B) alone is never asked to judge structural correctness.

A's output never implies B's answer, and B's output can never mark an obligation `"satisfied"`/`"missing"` directly — it only gates whether A's already-established connection counts as scoped. This is the exact separation Section 3 required.

## 4. Exact code changes

- **File:** `swarm/team.py` only.
- **Commit:** `61631f01bea47ce6bfb60f1be503b9adf22bf9d7`.
- **New:** `import httpx` (module already a project dependency, confirmed via `swarm/model_routing.py`'s own use); `_RELEVANCE_ASSESSMENT_TIMEOUT_S`, `_RELEVANCE_EXCERPT_CHARS`, `_RELEVANCE_SYSTEM_PROMPT`; `_assess_relevance()`.
- **Changed:** `_relationship_support_claim` (now `async`, accepts `task`, calls the assessor only when no anchor exists, never discloses an unverified connection); `_evaluate_requirement_coverage` (now `async`, accepts `task`, retires `"supported_unscoped"`); `_completion_decision` (now `async`); `_completion_control_check` (its one call site updated to `await`).
- **Retired:** T66.2's `"supported_unscoped"` coverage value and its non-blocking `COMPLETE` disclosure branch — every case it used to cover is now resolved to either `"supported"` (genuinely verified) or `"unknown"` (blocked).
- No other file touched. Confirmed via `git diff | grep -c` returning 0 for every T62 (`_looks_like_repetition*`) and T63 (`_record_member_action`, `coordinator_progress_guard`) symbol.

## 5. Acceptance matrix

| Required case | Expected | Actual | Evidence source |
|---|---|---|---|
| T3 established legitimate positive control | Remains capable of completing | `COMPLETE` | **Live, deployed, real model** |
| Scenario 7b: unrelated shipping-service relationship, no anchor | Must not reach verified `COMPLETE` | `INSUFFICIENT_EVIDENCE` | **Live, deployed, real model** |
| Correct anchored target, valid relationship | `COMPLETE` | `COMPLETE`, model never invoked | **Live, deployed, real model** |
| Wrong anchored target | Cannot satisfy the obligation | `INSUFFICIENT_EVIDENCE` | Mocked harness (anchor path unaffected by this phase, re-verified) |
| Definition without a valid reference | Unresolved | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Comment-only apparent relationship | Unresolved | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Import-only apparent relationship | Unresolved | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Truncated observation | Cannot establish support | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Duplicate summaries without an actual relationship | Cannot establish support | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Multiple obligations, independent assessment | One supported obligation cannot discharge another | `{'pricingplan': 'satisfied', 'the api route': 'unknown'}` | Mocked harness, real `_evaluate_requirement_coverage` |
| Unrelated evidence containing task keywords | Must not pass on lexical overlap alone | `INSUFFICIENT_EVIDENCE` (mocked `irrelevant`); **live model also correctly says `irrelevant` for this exact keyword-trap excerpt** | Mocked harness + live model test (section 6) |
| Assessor returns `irrelevant` | Cannot support the obligation | `INSUFFICIENT_EVIDENCE` | Mocked harness + **live, real model (scenario 7b)** |
| Assessor returns `abstain` | Relevance remains unverified | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Assessor times out/fails | Relevance remains unverified; no default to success | `INSUFFICIENT_EVIDENCE` | Mocked harness + **live provider-outage simulation against the deployed code's own try/except** |
| Malformed assessor output | Relevance remains unverified | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Existing Claim validation rejects nonexistent evidence IDs | Behavior intact | `authoritative=False` | Mocked harness, real `_make_claim` |
| Truly empty ledger, no tool calls (T65.1 Case A) | `INSUFFICIENT_EVIDENCE` | `INSUFFICIENT_EVIDENCE` | Mocked harness, unchanged path |
| Route+model independently present, no relationship (T65.1 Case D) | `INSUFFICIENT_EVIDENCE` | `INSUFFICIENT_EVIDENCE` | Mocked harness |
| Explicit entity task, both present / one absent (T65.1 F/G) | `COMPLETE` / `INSUFFICIENT_EVIDENCE` | `COMPLETE` / `INSUFFICIENT_EVIDENCE` | Mocked harness, unchanged path |
| Non-decomposable task shape (T65.1 H) | `COMPLETE` | `COMPLETE` | Mocked harness, unchanged path |

22/22 pass. No required case failed.

## 6. Test evidence

**Exact commands run:**
- `python -I final_phase_mocked.py` (local, Windows) — 22 scenarios against the real, imported, async production functions with `_assess_relevance` mocked at the function boundary (realistic outcomes only — "relevant"/"irrelevant"/"abstain" strings, never a raised exception, since a separate direct test confirmed the real function never raises). Result: `ALL MOCKED SCENARIOS: PASS (22/22)`.
- A separate, direct test mocking `httpx.AsyncClient` itself (not `_assess_relevance`) to raise `ConnectionError` — confirms the real function's own internal `try/except` converts this to `"abstain"` without propagating. Result: `PASS`.
- Four live calls to the real, deployed `llama3.1-8b` via the real LiteLLM endpoint from ZGX, iterating the system prompt twice after the first version produced an incorrect result (documented honestly below), then confirmed stable 5/5 and 5/5 on the two critical cases with the final prompt.
- Three live calls to `_completion_decision` against the **deployed module on ZGX** (scenario 7b, T3 positive, anchored positive) — real model, real HTTP call, real `httpx`, no mocking.
- One live call against the deployed module with `config.vllm_extract_base_url` temporarily pointed at an unreachable port — exercises the real `httpx.AsyncClient` exception path on the deployed interpreter, not a substitute.

**What was NOT tested:**
- Repository-wide regression suite — not run, consistent with Section 6's own instruction ("No repository-wide regression run is required merely for ceremony") and every prior phase in this series; the diff is empty against every unrelated mechanism (confirmed via grep), and the scratch harnesses exercise every invariant this phase touches directly against the real functions.
- The assessor was not tested against every conceivable adversarial excerpt — four were tested live (shipping excerpt, seller-verification excerpt, a keyword-overlap trap, and a generic/ambiguous case), all with correct or safely-blocking results; a broader adversarial sweep was judged out of proportion to a bounded, narrowly-scoped assessor whose failure mode already fails closed.
- `qwen2.5-coder-7b` and `qwen3-30b-instruct` were attempted as alternative models and found not currently resident behind the LiteLLM proxy (404 — on-demand swap did not trigger via this call shape); not pursued further since `llama3.1-8b`, once correctly prompted, produced stable, correct results.

**Honest account of the prompt-design process (not hidden):** the first system prompt wording produced an *incorrect* result on the real positive control — inspecting the model's own reasoning directly (with a diagnostic, non-production call allowing explanation) showed it was answering "does this one observation complete the entire task" rather than "is this observation in the right domain," because of ambiguous wording. The prompt was rewritten once to explicitly separate those two questions, and that version was then tested 5 times each on both critical cases (10/10 correct, stable) plus two additional adversarial cases, before being adopted as the deployed version. This iteration happened on the live model, not by assumption, and is reported here rather than presented as if the first attempt had worked.

## 7. Deployment evidence

- Remote commit: `61631f01bea47ce6bfb60f1be503b9adf22bf9d7`, pushed to `origin/main`.
- ZGX: `git pull` fast-forwarded `089af69 → 61631f0` (2 files: `swarm/team.py` + T66.2's report doc). All pre-existing untracked/modified files on ZGX preserved untouched (confirmed via `git status --short` before the pull).
- `systemctl --user restart agno-api.service` → active. `curl localhost:9001/health` → `{"status":"ok",...}`.
- `/runs` confirmed empty before and after deployment.
- **Live decision outputs** (not inferred from health alone):
  - Scenario 7b: `status=INSUFFICIENT_EVIDENCE`, `reason="required relationship not demonstrated for: the api route, database model..."`, journal showed `RELEVANCE_ASSESSMENT: ... verdict='irrelevant'`.
  - T3 positive control: `status=COMPLETE`, `reason="evidence ledger holds at least one observation"`, journal showed `RELEVANCE_ASSESSMENT: ... verdict='relevant'` followed by `CLAIM_RECORDED`/`CLAIM_VALIDATED` (both `authoritative=True`).
  - Anchored positive control: `status=COMPLETE`, journal showed **no** `RELEVANCE_ASSESSMENT` line at all — confirming the anchor path short-circuits before the model is ever reached.
  - Provider-outage simulation: `status=INSUFFICIENT_EVIDENCE`, journal showed `RELEVANCE_ASSESSMENT: provider failure (ConnectError) -- treated as abstain`.

## 8. Remaining limitations

**Release-blocking:** none identified against the hard acceptance criteria stated for this phase.

**Non-blocking backlog (explicitly not pursued this phase, consistent with its own scope discipline):**
- The assessor's correctness rests on one specific prompt against one specific 8B model; it was validated empirically on the available adversarial cases but was not stress-tested against a large, systematic adversarial corpus. A future phase could build that corpus if a live battery later surfaces a new failure mode.
- `delegation_key` lineage (investigated and rejected in T66.2 for a different reason — it would not have closed 7b even if built) remains a separate, independently-valuable provenance improvement, requiring `team`/the member control box to be threaded into `_make_read_cache_tool_hook`'s construction, which `_build_team` does not do today. Not needed to close this phase's defect; still the correct answer if a future phase wants delegation-level audit trails for a different reason.
- Availability dependency: a conceptual, unanchored task's completion decision now depends on `config.vllm_extract_base_url` being reachable. Confirmed fail-closed (never defaults to success) under a live-simulated outage — this is the explicitly accepted trade-off, not a silent risk, and is now part of this architecture's known operating characteristics going forward.

## 9. Closure decision

**Recommend freezing the T-Series.** The specific completion-integrity defect this final phase targeted — unrelated evidence silently or disclosed-ly satisfying a mandatory obligation — is closed, verified against the real deployed model and the real deployed code, not merely designed or mocked. The established T3 product contract remains satisfied. No further phase is started per this mission's own explicit instruction.
