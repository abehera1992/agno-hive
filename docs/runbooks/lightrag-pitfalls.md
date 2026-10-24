# LightRAG Integration Pitfalls

**Research date:** 2026-10-23
**Deployed LightRAG version:** 1.5.4 (confirmed via `import lightrag; lightrag.__version__` in the ZGX `zgx` conda env, `~/miniforge3/envs/zgx/lib/python3.12/site-packages/lightrag/__init__.py`)
**Integration layer:** `lightrag_mcp/server.py` (this repo's own MCP wrapper), consumed by `swarm/team.py`'s `_repo_lightrag_query`/`_lightrag_prefetch_for_delegation`/`_lightrag_result_is_usable`
**Environment scope:** ZGX (`zgx-32df`), `lightrag.service` (port 9002) + `vllm-embed` (Qwen3-Embedding-0.6B, port 8002) + `vllm-extract`/`vllm-coord` (synthesis LLMs) + PostgreSQL/AGE (`agno-postgres-age` container, `agno_graph` database, `lightrag_doc_status` table)

This document is the canonical reference for LightRAG-specific operational and implementation pitfalls in AgnoHive. Update it in place as findings change status — do not create a second overlapping document.

## How to read this document

Each entry is classified as:
- **Confirmed locally** — reproduced or directly established against this deployment.
- **Confirmed implementation behavior** — verified directly in source, not necessarily reproduced as a live failure.
- **Upstream-reported and applicable** — not independently investigated against upstream LightRAG 1.5.4 release notes/issues in this phase; none of the findings below required that source, since all were established directly against the deployed code and logs. If a future finding needs it, add a row here with the upstream reference.
- **Suspected** — plausible, not yet sufficiently verified.
- **Not applicable** — investigated and ruled out for this deployment.

---

## 1. Namespace exceptions conflated with genuine empty results

- **Component:** `lightrag_mcp/server.py`, `lightrag_query` (the MCP tool entrypoint)
- **Classification:** Confirmed locally
- **Evidence:** Direct source read, `lightrag_mcp/server.py:221-225` (pre-fix): `asyncio.gather(project_rag.aquery(...), global_rag.aquery(...), return_exceptions=True)` turns a raised exception into an object; the subsequent `isinstance(result, str)` checks are false for that object, so both branches silently skip it, falling through to the literal `"[no results]"` sentinel — the same string returned for a genuinely empty index. Reproduced live on 2026-10-23: with `vllm-embed` stopped for 4 days (pre-existing, unrelated outage), every `lightrag_query` call returned `"[no results]"` despite the "ekam" workspace holding 2,662 indexed docs (confirmed via `SELECT workspace, count(*) FROM lightrag_doc_status`); `lightrag.service`'s own journal showed `ERROR: Embedding func: ... Connection error` / `ERROR: Query failed: Connection error` at the exact same timestamps the MCP tool was returning `"[no results]"`.
- **Symptoms:** `agno-api.service`'s own `[team] LIGHTRAG_PREFETCH: ... outcome='empty'` log line fires for what is actually a backend outage, with no way to tell the two apart from that line alone.
- **Impact on AgnoHive:** a caller cannot distinguish "nothing indexed for this" from "the retrieval backend is down" — silently degrades every delegation to pure fallback search with no operator-visible signal that LightRAG itself is unhealthy.
- **Mitigation:** **Implemented**, commit `0213266`. `lightrag_query` now checks `isinstance(result, Exception)` per namespace; if every namespace lacking real evidence genuinely failed, it returns a `"Query failed: ..."`-prefixed string (bounded: exception type name + message truncated to 200 chars, never a raw credential); if some namespaces succeed, their evidence ships with a bounded failure note appended. The two common cases (clean success, clean empty) are byte-identical to the pre-fix contract. `swarm/team.py`'s `_repo_lightrag_query` now checks this prefix before `_lightrag_result_is_usable`, so its own `outcome` field reports `"error"` instead of `"empty"` for this case — the gating decision (not usable either way) is unchanged.
- **Status:** Implemented and deployed (ZGX `0213266`, both `agno-api.service` and `lightrag.service` restarted, live-verified both the clean-success path — identical 3123-char output for `register_documents` — and the connection-error path).
- **Detection/regression check:** `validate_lightrag_error_semantics.py` extracts the real post-gather block from `lightrag_mcp/server.py` and runs 6 synthetic cases (both-empty, both-evidence, partial-failure, total-failure, one-fail-one-empty, one-empty-one-evidence) — all passing as of this commit. Re-run this after any future edit to that block.

## 2. Hybrid/global-mode latency, and the still-imprecise bottleneck attribution

- **Component:** `lightrag_mcp/server.py`'s `mode` dispatch (`local`/`global`/`hybrid`/`naive`/`mix`) → LightRAG's own `aquery()`
- **Classification:** Confirmed locally (the latency itself); **suspected** (the precise internal stage breakdown)
- **Evidence:** Three bounded, live measurements against the real deployed server, same "ekam" workspace:

  | Mode | Query | Outcome | Duration |
  |---|---|---|---:|
  | `local` | "register_documents" | usable | 1.0 s |
  | `local` | "Voucher model fields" | usable | 1.0 s |
  | `hybrid` | T10-shaped repository question | usable | 173.7 s |
  | `global` | "Voucher model relationships to stock ledger" | timeout | 180.0 s (hit `_MCP_TIMEOUT`) |

  `lightrag.service`'s own journal for the 173.7s hybrid call shows entity/relation retrieval (`Query nodes:`/`Query edges:` lines) completing within ~2 seconds of the request starting, but the `LLM cache == saving: hybrid:query:...` line (final result persistence) not appearing until 164 seconds later.
- **What is confirmed:** retrieval (vector/graph lookup) is fast; local mode, which stops after that step, completes in ~1s on comparable queries. The gap between retrieval completing and the hybrid/global result persisting is real and large.
- **What is NOT confirmed:** the 164-second gap was attributed to "LLM synthesis" by inference from timestamps, not by isolating that stage with its own instrumentation. Concurrent load on the shared synthesis LLM backend (other delegations' own model calls running at the same time) was not ruled out as a contributing factor — multiple unrelated queries ("seller", "document upload") were visibly interleaved in the same log window.
- **Impact on AgnoHive:** the mandatory prefetch (`_lightrag_prefetch_for_delegation`) defaults to `mode="hybrid"`, meaning every delegation's prefetch can cost up to 180 real seconds, and can time out entirely under global-mode-like conditions.
- **Mitigation:** **Not changed in this phase.** Per this project's own explicit decision policy, `mode="hybrid"` was *not* switched to `"local"` merely because local was faster on two queries — that would be a speed-only justification without a corresponding quality assessment. Section 3 below records the comparison that was done.
- **Status:** Monitored, deferred. A future phase should instrument LightRAG's own query pipeline stages directly (or check for existing per-stage timing hooks in the 1.5.4 library) before concluding synthesis load is the sole cause, and should assess `mode="local"`'s evidence quality across a larger, representative query set before any default change.
- **Detection/operational check:** watch `[team] LIGHTRAG_PREFETCH: ... duration_s=...` in `agno-api.service`'s journal; a `duration_s` near 180.0 with `outcome='timeout'` indicates this risk firing in production.

## 3. Local-mode adequacy for the prefetch role (evaluated, not adopted)

- **Component:** `_lightrag_prefetch_for_delegation`'s default `mode="hybrid"` (via `_repo_lightrag_query`)
- **Classification:** Confirmed locally (comparison), decision deferred
- **Evidence:** Two representative comparisons, same real server:
  - `mode="local"`, "register_documents" → usable, 3123 chars, accurate: *"The `register_documents` function is designed to register document references from storage-service uploads. It operates within the `API/business-service/router/business_api.py` file..."* — matches the real code exactly (confirmed independently via direct `get_file_content` reads earlier in this engagement).
  - `mode="local"`, "Voucher model fields" → usable, 1662 chars, on-topic.
  - Both completed in ~1 second, versus hybrid's 173.7s for a comparably-scoped question.
- **What local mode may omit (inferred, not directly measured against an omission):** `global` mode's own docstring describes it as "relationship-centric: finds cross-module themes and patterns" — a cross-service or cross-module relationship question (e.g., "how does inventory-service integrate with business-service") plausibly benefits from global/hybrid's relationship traversal in a way local's entity-centric lookup would not. This was not directly tested with a known relationship-shaped ground truth in this phase.
- **Confirmed safety net regardless of mode choice:** the existing fallback is unconditional — `_build_canonical_researcher_task`'s own disclosure block tells the member the LightRAG content "may only describe a file's role without quoting its literal content... do not skip your own investigation merely because this block is nonempty," and the live end-to-end validation (both the prior phase's T10 run and this phase's smoke test) confirms members do still call `get_file_content`/`search_files` independently.
- **Decision:** **Default left unchanged (`hybrid`)**, per the explicit instruction not to switch the production mode merely because local was faster on a small sample. The evidence is suggestive but not sufficient to justify a default change — a relationship-shaped comparison case is still needed.
- **Status:** Deferred. If a future phase wants to pursue this, the smallest safe change would be a per-call `mode` parameter threaded from `_lightrag_prefetch_for_delegation` (configurable, reversible, defaulting to the current `"hybrid"`), validated against at least one relationship-shaped ground-truth query before any default flip.

## 4. Broadcast delegations bypassing mandatory prefetch

- **Component:** `swarm/team.py`'s `delegate_structured_task` (broadcast branch, `is_broadcast`)
- **Classification:** Confirmed locally (the gap); confirmed implementation behavior (the fix)
- **Evidence:** `teams/parallel-review.yaml` (the only `mode: broadcast` team) instructs its Researcher/SecurityReviewer/PerformanceReviewer roles to use `lightrag_query`, with detailed KNOWN-PATH and FALLBACK rules — direct proof that broadcast work performs repository retrieval. The original 2026-10-23 implementation unconditionally skipped the mandatory prefetch whenever `is_broadcast` was true, since there was no single `member_id` to gate capability eligibility on.
- **Impact on AgnoHive:** `parallel-review` ran with zero LightRAG-first enforcement despite being a real repository-retrieval team — the same "prompt instruction without deterministic enforcement" failure mode this whole integration exists to close, left open for exactly one team.
- **Mitigation:** **Implemented**, commit `0213266`. `_lightrag_prefetch_for_delegation(team, member_id=None, ...)` now signals a broadcast call; eligibility is computed by `_any_member_eligible_for_lightrag(team)` (true if *any* resolved `team.members[i]` has a repo-retrieval tool, never a hardcoded team/role name), and exactly one shared query runs regardless of how many eligible recipients exist — the result becomes part of the one `canonical_task` every broadcast recipient already receives.
- **Status:** Implemented and deployed. Focused, controlled tests (`validate_lightrag_hardening_full.py`) cover: zero-eligible broadcast → no call; 1-of-3-eligible broadcast → exactly one shared call; 3-of-3-eligible broadcast → still exactly one call (not three); cache isolation across distinct team objects with identical (target, objective, evidence_required). All passing. Not yet live-verified against a real `parallel-review` run in this phase (would require a dedicated broadcast-shaped live task, deferred to avoid expanding this phase's live-validation scope beyond the one bounded smoke test already run).
- **Detection/regression check:** the same test script; re-run after any change to `delegate_structured_task`'s broadcast branch or to `_LIGHTRAG_REPO_RETRIEVAL_TOOLS`.

## 5. Preliminary context mistaken for verified literal source evidence

- **Component:** `_build_canonical_researcher_task`'s `lightrag_context` block
- **Classification:** Confirmed implementation behavior (the safeguard); confirmed locally (the underlying risk it guards against)
- **Evidence:** `teams/parallel-review.yaml:34-35` documents a live 2026-08-15 incident: `lightrag_query` "kept 'succeeding' with real, correct-sounding but purely DESCRIPTIVE content about config/config.py's role — never its literal source" — a real, previously-observed failure mode where LightRAG's synthesized prose was accurate in substance but not a quotable citation.
- **Impact on AgnoHive:** if a member treated LightRAG's own prose as a citable fact, verify_claims-style grounding checks would fail or, worse, the answer would carry an uncited but confidently-stated claim.
- **Mitigation:** already implemented (prior phase, commit `6d2f53c`), unchanged this phase. The injected block explicitly states: "NOT independently confirmed, and NOT a substitute for reading the actual file... may only describe a file's role without quoting its literal content... do not cite it in place of a real file you have read."
- **Status:** Implemented, live-verified twice now — the prior phase's T10-shaped run and this phase's smoke test both show the member independently calling `get_file_content`/`search_files` to confirm literal content after receiving the LightRAG lead, and the final answer's citations trace to the real file read, not to LightRAG's own prose.
- **Detection/operational check:** `_build_canonical_researcher_task`'s controlled tests (prior phase) confirm the disclosure text renders whenever `lightrag_context` is present; live verification requires inspecting a real run's final answer citations against its own `get_file_content`/`search_files` log lines, as done for both runs above.

## 6. Partial document ingestion failures (embedding/indexing)

- **Component:** LightRAG's own indexing pipeline, `lightrag_doc_status` (PostgreSQL/AGE, `agno_graph` database)
- **Classification:** Confirmed locally
- **Evidence:** Direct query against the live deployment, 2026-10-23: `SELECT workspace, status, count(*) FROM lightrag_doc_status WHERE workspace='ekam' GROUP BY status` → `failed: 17`, `processed: 2645` (17 of 2,662 docs, ~0.6%, permanently in a failed state).
- **Impact on AgnoHive:** a small, non-zero fraction of the indexed corpus is silently absent from retrieval results for both `local` and `hybrid`/`global` modes — any query whose only relevant evidence lives in one of those 17 failed documents returns incomplete or empty results with no signal to the caller.
- **Mitigation:** not addressed in this phase (no reproduction of *which* 17 documents, nor why they failed, was performed — out of scope for the two confirmed correctness fixes this phase targets).
- **Status:** Monitored, deferred. A prior engagement phase (referenced in this project's own operational history) already established that `doc_status='failed'` rows are safe to delete and re-queue for reprocessing; that remediation was not re-applied here since it wasn't demonstrated to be causing a currently-observed failure.
- **Detection/operational check:** periodic `SELECT workspace, status, count(*) FROM lightrag_doc_status GROUP BY workspace, status` — a nonzero and growing `failed` count for a workspace actively being queried is the signal to act on.

## 7. Rerank configured-but-inert

- **Component:** LightRAG 1.5.4's own query pipeline (not this repo's wrapper code — no `enable_rerank`/`rerank_model` setting exists anywhere in `lightrag_mcp/`)
- **Classification:** Confirmed locally
- **Evidence:** `lightrag.service`'s own journal, observed twice during this phase's live testing: `WARNING: Rerank is enabled but no rerank model is configured. Please set up a rerank model or set enable_rerank=False in query parameters.`
- **Impact on AgnoHive:** reranking is silently skipped on every query (the library falls back gracefully, per the warning's own wording), meaning retrieval ranking quality is whatever the base vector/graph similarity produces, with no reranking boost — purely a quality ceiling, not a correctness bug, and not contributing additional latency (rerank is skipped, not attempted-and-failed).
- **Mitigation:** not implemented — this is a configuration/tuning decision (whether to deploy a rerank model) outside this phase's two confirmed-defect fixes.
- **Status:** Deliberately deferred. Worth a future phase if retrieval relevance (not latency or error semantics) becomes the limiting factor.
- **Detection/operational check:** grep `lightrag.service`'s journal for `"Rerank is enabled but no rerank model"` — if it stops appearing, a rerank model was configured; if it keeps appearing, the ceiling remains.

## 8. Tool exposure versus enforced invocation (the original motivating finding)

- **Component:** `api/server.py`'s `_resolve_mcp_urls` + `teams/engineering.yaml`/`teams/parallel-review.yaml`'s prompt instructions
- **Classification:** Confirmed locally
- **Evidence:** `_resolve_mcp_urls` (`api/server.py:43-47`) always appends the LightRAG MCP URL regardless of caller opt-in; `teams/engineering.yaml` already instructed every role to call `lightrag_query` first, in "NO EXCEPTIONS" language. The full 2026-10-23 T7-T13b battery (6 live runs) showed zero actual invocations across the entire window — confirmed via exhaustive journal correlation, not inferred from one missing log string.
- **Impact on AgnoHive:** this was the root cause the entire mandatory-prefetch feature (commit `6d2f53c`) was built to close.
- **Mitigation:** Implemented in the prior phase (`6d2f53c`) and hardened this phase (`0213266` — broadcast coverage). Not re-tested this phase beyond the targeted broadcast/error-semantics cases, consistent with "do not rerun the full battery."
- **Status:** Implemented for non-broadcast and broadcast delegations. Live-verified for non-broadcast (prior phase + this phase's smoke test); unit/controlled-tested but not live-verified for broadcast (see finding 4 above).

---

## Summary table

| # | Finding | Classification | Status |
|---|---|---|---|
| 1 | Empty/error conflation | Confirmed locally | **Implemented**, deployed, live-verified |
| 2 | Hybrid/global latency, imprecise bottleneck | Confirmed locally (latency); suspected (exact cause) | Monitored, deferred |
| 3 | Local-mode adequacy for prefetch | Confirmed locally (comparison) | Evaluated, default unchanged |
| 4 | Broadcast coverage gap | Confirmed locally | **Implemented**, deployed, controlled-tested; not yet live-verified |
| 5 | Preliminary context vs. literal evidence | Confirmed implementation behavior | Implemented (prior phase), live-verified twice |
| 6 | Partial ingestion failures (17 failed docs) | Confirmed locally | Monitored, deferred |
| 7 | Rerank configured-but-inert | Confirmed locally | Deliberately deferred |
| 8 | Tool exposure vs. enforced invocation | Confirmed locally | Implemented (prior + this phase) |
