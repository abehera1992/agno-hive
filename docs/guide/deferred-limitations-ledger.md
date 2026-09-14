# Deferred limitations ledger — Phases A through L

A durable backlog, not a to-do list to clear. Every row is a limitation that
was found, deliberately NOT fixed, and documented so it is never silently
rediscovered. **Do not solve an item merely because it is listed here** —
each carries its own trigger condition; act only when that trigger fires.

**Only mark an item Resolved when a later phase actually implements and
tests the fix** — never by editing this row's own text.

Columns: Phase · Limitation · Evidence/reason · Why deferred · Trigger for
implementation · Dependency · Priority.

## Carried forward from before Phase A (pre-existing groundedness work)

These predate the durable execution/evidence/memory architecture (Phases
A–L) entirely — they concern the swarm's own answer-generation quality, not
persistence — but the Phase L prompt explicitly asks that they be carried
forward here rather than silently dropped.

| # | Limitation | Evidence/reason | Why deferred | Trigger | Dependency | Priority | Status |
|---|---|---|---|---|---|---|---|
| 1 | Stochastic LLM evidence compression / delegation loss (a member reading 548k chars can write back as little as 17k, ~30.6:1) | Measured directly, pre-Phase-A | The literature treats this compression as inherent to delegation; the working mitigation is post-hoc re-grounding (the existing guard suite), already shipped outside this effort | A measured regression in guard coverage | none | Low (already mitigated) | Open |
| 2 | Model-level synthesis/arithmetic inaccuracies survive even when the underlying Evidence is correct | `verify_claims`/`compare_enumerations` are grep-based fabrication checks — they catch invented symbols/paths, not "looks plausible but arithmetically wrong" | Would need semantic/numeric verification, a materially different validation architecture from the deterministic string checks this whole effort builds on | A measured incident of a wrong-but-plausible synthesis slipping through every existing guard | none identified | Medium | Open |
| 3 | `team._tool_evidence`'s 200-char-preview LLM-context cache is lossy by design | `swarm/team.py`'s own docstrings; explicitly NOT touched by Phase D (its own non-goal: "replacing `_tool_evidence`") | Intentional: bounds prompt size for the model. Phase D's durable `evidence` table is the actual provenance fix; the lossy cache remains only for prompt construction | none — permanent, accepted behavior | n/a | Informational (not a defect) | By design |
| 4 | Remaining relay/evidence-cap limitations (T12-style runaway repetition/context overflow) | "T12's runaway generation loop is the top open failure" (pre-Phase-A finding); the liveness heartbeat logs the signal but does not act on this specific failure mode | Reliably distinguishing "repeating itself" from "genuinely repetitive but correct work" is unsolved | A repeat incident with a proposed, measured detection heuristic | none | Medium-high | Open |
| 5 | No raw model request/completion observability (only derived signals: stream event counts, tool logs, Phase0 telemetry) | The whole Phase0Run design is derived/summarized, never raw-capture | Raw capture at scale raises storage/PII/cost questions not yet justified by a concrete debugging need | A debugging need the derived signals cannot resolve | Item 16 below (a retention policy would need to exist first) | Low | Open |

## Phase A–E

| # | Limitation | Evidence/reason | Why deferred | Trigger | Dependency | Priority | Status |
|---|---|---|---|---|---|---|---|
| 6 | No automatic ToolCall replay for ambiguous external side effects | Phase D/H/K/L: a non-terminal ToolCall makes the whole run unconditionally non-resumable, by explicit design, re-confirmed every subsequent phase's own regression tests | **Not a gap — a permanent safety boundary.** Listed here per this phase's own instruction to carry it forward, not because it awaits a fix | none — will not implement | n/a | n/a | By design, will not resolve |
| 7 | Non-text/non-serializable tool results (e.g. an async generator from `delegate_task_to_member`) are stored as a deterministic type-marker, never their real content | `execution_store._canonicalize_result`'s own docstring (Phase D) | No general-purpose way to faithfully serialize an arbitrary Python object; the marker preserves hash determinism without fabricating content | A concrete tool whose non-text result genuinely needs exact preservation | A tool-specific serialization contract, not yet designed | Low | Open |
| 8 | Deterministic reconciliation (Phase E) is wired to exactly ONE boundary (`_reconcile_completeness_claim_with_comparison`), not to `_verify_claims`'s broader multi-finding fabrication reports | Phase E's own final report: traced `_verify_claims`, explicitly declined to instrument it | Its freeform, multi-finding report shape doesn't map onto the Claim schema's single statement/status without inventing a new ontology | A demonstrated need for durable fabrication-check provenance | Possibly a Claim schema extension | Medium | Open |

## Phase F–G

| # | Limitation | Evidence/reason | Why deferred | Trigger | Dependency | Priority | Status |
|---|---|---|---|---|---|---|---|
| 9 | Project memory is a flat, append-only ledger with no reconciliation between contradicting promoted Claims (e.g. "5 endpoints" and "6 endpoints" both promoted, as two separate rows) | Phase F/G's own explicit design and tests: "both survive as separate rows... reconciling between them is explicitly left unsolved" | Contradiction resolution is a retrieval/ranking concern, explicitly out of scope for both phases | Measured agent confusion from reading two contradicting promoted memories | none designed | Medium | Open |
| 10 | Project-memory retrieval relevance filtering reuses `_significant_tokens` (a token-overlap heuristic), not embeddings/semantic search | Phase G's own design choice, matching `load_failure_context`'s pre-existing convention | Consistency with the established pattern; embedding-based retrieval would mean new infrastructure (LightRAG/Qdrant), explicitly out of scope to redesign | A measured relevance-quality gap | none | Low | Open |

## Phase H–K

| # | Limitation | Evidence/reason | Why deferred | Trigger | Dependency | Priority | Status |
|---|---|---|---|---|---|---|---|
| 11 | No authentication system anywhere — Phase J's "authorization" is ownership-verification only, never identity verification | Phase J's own forensics: every `api/server.py` endpoint is unauthenticated, by documented design, over a private Tailscale network | Building real auth is "unrelated enterprise infrastructure" for a currently single-operator deployment | A genuine multi-operator/multi-tenant deployment requirement | none designed (would need session/API-key issuance at minimum) | Low today; **high** if ever exposed beyond a private network | Open |
| 12 | `projects.tenant_id` exists but is never populated or enforced by any caller or endpoint | Phase J's own implementation: the column anchors the "project → tenant" chain, but only project-level isolation is actually tested/enforced | No current caller or use case asserts a tenant identity | A real multi-tenant deployment need | Item 11 (auth) | Low | Open |
| 13 | `run_id`'s 48-bit scheme (`uuid4().hex[:12]`) has non-negligible birthday-bound collision probability above roughly 10⁶–10⁷ total runs | Computed during Phase K's own forensics | Changing Phase A's run_id scheme would ripple through every phase's identity contract (A–K); realistic usage volume for this tool is far below the risk threshold | A measured or projected run volume approaching that scale | A coordinated identity-scheme migration across Phases A–K | Very low | Open |
| 14 | PostgreSQL live-concurrency (and, more broadly, live PostgreSQL of any kind) was never validated in any phase A–L — every PostgreSQL claim across this entire effort is DDL-compile-only | Stated explicitly in every phase's own final report, including this one | No live PostgreSQL instance was available in this development environment at any point | Access to a live PostgreSQL instance | none | Medium — should be validated before a real production Postgres deployment | Open |
| 15 | The run-ownership primitive (`acquire_run_ownership`/`release_run_ownership`, Phase K) is built and tested but wired into no live code path | Phase K's own explicit scope boundary: nothing races on the same `run_id` today, so nothing calls it | Wiring it in prematurely would add locking overhead/complexity without a real need | A future phase that lets an operator/system launch a continuation from a `rehydrate_run` verdict | Such a continuation-launcher does not yet exist and would need to be designed first | Medium — the most likely next real need in this lineage | Open |
| 19 | Session-level concurrent-message-append races: two overlapping `/run` calls sharing the SAME `session_id` (always DIFFERENT `run_id`s) could both append to the same `current_leaf_id`, diverging the message tree | Observed during Phase K's own forensics, explicitly judged out of scope for the run-ownership invariant (which is about one `run_id`, never about a shared session) | Not a "Run ownership" problem per se — the tree/branch mechanism (Phase 5/6) already supports divergent branches structurally; unmeasured whether this is actually harmful in practice | An observed/reported case of unwanted session-message-tree divergence | none | Low | Open |

## Phase L

| # | Limitation | Evidence/reason | Why deferred | Trigger | Dependency | Priority | Status |
|---|---|---|---|---|---|---|---|
| 16 | `failure_log` has no retention/cleanup at all and grows strictly unboundedly (every `/feedback` bad rating adds a row forever, no dedupe); `task_outcome_queue` is naturally bounded by its own `(project_id, task_hash)` UniqueConstraint but likewise has no time-based retention | Phase L's own forensics (this session): confirmed no DELETE/cleanup code path exists for either table anywhere in the codebase | "Do not invent retention defaults without forensic justification" — no evidence of actual operational harm (disk usage, query slowdown) was found or is currently measurable | Observed/measured unbounded growth becoming an operational concern | none | Medium — the clearest, most concrete "do this next if it becomes a problem" item this phase surfaced | Open |
| 17 | `check_storage_integrity()`'s Phase L additions (impossible lifecycle states, invalid promotion rows, duplicate checkpoint sequences) are detection-only; no automatic repair exists | Phase L's own explicit design: "detection is preferable to silent repair unless repair is demonstrably safe" — no repair here has been proven safe | Repairing e.g. an "impossible lifecycle state" row requires knowing WHICH field was supposed to be correct — genuinely ambiguous without more context | A specific, well-understood corruption pattern recurring with a PROVEN-safe fix | none | Low (rare, tamper/bug-only condition) | Open |
| 18 | Hive performs no backup/restore operations itself (no `pg_dump` wrapper, no scheduled snapshot, no cloud storage integration) | Explicit non-goal this phase: "do not build cloud-specific backup infrastructure" | Correctly scoped to the operator/PostgreSQL layer, not Hive's application layer | none — a permanent boundary decision | n/a | n/a | By design, will not resolve |
