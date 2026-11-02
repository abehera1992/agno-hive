# Phase 0.37 — Evidence-Scoped Route Verification and Targeted Source Discovery

Two independently-reviewable workstreams against the live ZGX-hosted `agno-hive` swarm: P0 fixes T13a's "final-answer route verification loses trusted router-file context across correction passes." P1 fixes T3's "broad, unfocused source discovery fails to identify and trace the requested seller-verification implementation." Mission constraints: no unit tests, no broad refactoring, preserve Phase 0.35/0.36/the `51e6398` hotfix/T13b's `compare_enumerations` behavior exactly, live validation required before any PASS claim, separate verdicts, do not round up.

## Section A — Starting / Ending State

| | Start of phase | End of phase |
|---|---|---|
| `agno-hive` local + origin + ZGX | `51e6398` (prior mid-battery hotfix) | `75d8d7a` (P1, on top of P0's `06d4c53`) |
| `agno-api.service` (ZGX) | PID `838884` | PID `1557931` — restarted once, for P1 (`swarm/team.py` changed); health 200, no errors in post-restart journal |
| `hive-mcp` image (local Docker) | `603e45f7e3dd` | `237a88da3701d...` — rebuilt/redeployed once, for P0 (`hive-mcp/tools/verify.py` changed); clean startup log, healthy |

**Files changed:**
- P0: `swarm/team.py` (`_verify_claims` gained a `team` param; ~15 call sites updated to pass `team=team`, sourcing `context_files` from `team._evidence_ledger`'s `file_read` records) + `hive-mcp/tools/verify.py` (`_prose_route_verb_mismatches` gained a `context_files` fallback; `verify_claims` signature/docstring updated; two incidental bugs fixed, see below).
- P1: `swarm/team.py` only — the `_COORDINATOR_INSTRUCTIONS` "For 'how does X work' / feature questions" recipe (originally lines ~537–543) rewritten.

One call site was deliberately **not** updated: `repair_unguarded_draft(...)` has no `team` parameter at all and no caller found within `team.py`; carried forward as a known, documented gap (see Section F).

## Section B — P0: Evidence-Scoped Route Verification

### Reconstructed failure
T13a's route-verb check (`_prose_route_verb_mismatches`) only fires when **exactly one** router-like filename is named literally in the answer's own text — a deliberately conservative design to avoid guessing which router a claim belongs to. The actual T13a incident ran a correction chain (`evidence-integrity` → `coverage-correction`) whose **final** draft opened "I'll reconcile the previous answer with the new evidence from the `inventoryApi.ts` file" and never re-named `vouchers_api.py` anywhere in its own generated text — even though the Researcher genuinely read that file earlier in the same run. The check never ran, and a fabricated `DELETE /vouchers/{id}` plus a generic, wrong `PUT /vouchers/{id}` shipped as fact.

### Root cause
The route check's single source of "which file is this claim about" was the **answer's own prose**, discarded with every correction pass. The run's genuine read history — already recorded in `team._evidence_ledger` as `file_read` `EvidenceRecord`s every time `get_file_content` is called — was never consulted.

### Design
`_verify_claims` (swarm/team.py) now accepts an optional `team` parameter. When present, it collects every `source_path` of `evidence_type == "file_read"` from `team._evidence_ledger.list()` (de-duplicated, in encounter order) and passes them as `context_files` to the `verify_claims` MCP tool. `_prose_route_verb_mismatches` (hive-mcp/tools/verify.py), when in-text naming is absent or ambiguous, filters `context_files` to router-shaped `.py` paths (`router` in the path, or ending `_api.py`) that exist on disk; **only if exactly one survives** does it become the resolution target — multiple router files, zero router files, or only non-router files all fall through to the pre-existing "unresolved" behavior. In-text naming, when present, still wins outright. ~15 of the 16 call sites of `_verify_claims` across `team.py` were updated to pass `team=team`.

`context_files` is documented in `verify_claims`'s own docstring as "Orchestrator-supplied only — leave unset when calling this yourself" — a convention, not a hard technical barrier, consistent with the existing pattern for `evidence_id` in this codebase (the MCP tool surface gives the orchestrator's trusted calls and a model's own direct calls to the same tool no technically-separate channel).

### Two incidental bugs found and fixed during validation
1. **Unanchored wildcard regex.** The per-segment matching loop built `_PARAM_RE.sub(r"[^\"'`]+", re.escape(cand)) + ...` with no `$` anchor and no exclusion of `/` from the wildcard class — a short, generic claim like `PUT /vouchers/{id}` matched as a bare prefix of a longer, more specific real route (`/vouchers/{voucher_id}/post`), incorrectly returning `PLAUSIBLE` for a claim the mission explicitly required to be rejected ("Generic `PUT /vouchers/{id}` is rejected when the actual implementation exposes specific `/post` and `/cancel` actions instead"). Fixed by excluding `/` from the wildcard character class and anchoring with `$`, matching the sibling ROUTES section's own stricter convention.
2. **Early-exit omission.** `verify_claims`'s "no checkable claims found" short-circuit condition never included `prose_route_results` in its OR-clause, even though that value is computed earlier in the same function body. An answer whose *only* checkable claims were bare route claims (no other symbol/citation) returned "no checkable claims found" instead of ever printing the ROUTE VERBS section — a latent bug present since Phase 0.34 (when `prose_route_results` was first introduced), exposed by this phase's own minimal end-to-end test case. Fixed by adding `or prose_route_results` to the condition.

### Controlled validation (scratch harness, against real repo data)
- Without `context_files`: the exact T13a incident text (names only `inventoryApi.ts`, fabricated DELETE + generic PUT) stays **unresolved** — unchanged from pre-fix behavior, confirming no regression to the conservative default.
- With `context_files=[vouchers_api.py]`: the fabricated DELETE is caught, the generic PUT is caught, and the real GET/PUT-`/post` claims resolve `PLAUSIBLE`.
- With two router files in `context_files` (`vouchers_api.py` + `parties_api.py`): stays unresolved — no arbitrary cross-file attribution when ownership is ambiguous.
- With a non-router file only (`models.py`) in `context_files`: stays unresolved.
- Real claims only, via `context_files`: no `NOT FOUND`.
- In-text naming, no `context_files` needed: unchanged (regression check, byte-identical to pre-fix).
- Full end-to-end `verify_claims()` report on the exact T13a text with `context_files` supplied: ROUTE VERBS section now prints and correctly flags both fabrications (this is what exposed bug #2 above).

### Live result — T13a (files NOT named)
**Accuracy: PASS.** 9/9 real backend routes (correct paths/methods/handler names via `compare_enumerations`), 5/5 real frontend hooks, the exact 4-item gap (`create_grn_from_po`/`create_credit_note`/`create_stock_adjustment`/`create_stock_transfer`), and the real PUT-vs-POST method difference on `/post` and `/cancel` correctly reported as `PARTIAL_MATCH` rather than fabricated as a vague "update" claim. No `DELETE` fabrication shipped.

**Honest caveat — this run did not exercise the new mechanism.** Journal evidence (PID `1558326`): a `repetition-completion` pass at 16:34–16:37 *did* fabricate a draft with wrong handler names (`get_voucher`/`create_voucher`/`update_voucher`/`delete_voucher`/`list_vouchers`, including a fake `delete_voucher`/`useDeleteVoucher` pair and a false "no gaps" claim). This was caught and rejected by **Phase 0.35's pre-existing completion-adoption gate** ("rejecting the completion's new content... keeping the pre-repeat draft"), not by anything built this phase. The run then recovered via the Researcher's own `compare_enumerations` call, producing a correct draft that `_enforce_verification_invariant` / `_enforce_verification_invariant_final` both found clean (`outcome=skipped`) — meaning no route-verb mismatch ever needed checking, so the new `context_files` fallback was never invoked. **T13a's accuracy is a clean PASS; it is not a direct live confirmation of the `context_files` mechanism specifically.**

## Section C — P1: Coordinator Discovery-Strategy Fix

### Reconstructed failure (exact journal evidence)
T3 ("How does seller verification work in this codebase? Trace it from the API route to the database model.") was delegated by the Coordinator as:
> `{'target': 'the whole codebase', 'objective': 'search_files for "seller" and return every matching file:line — find every file that references seller.'}`

`lightrag_query` was never invoked anywhere in that run. The resulting `search_files('seller', '**/*')` returned 80 hits (60 retained) scattered across README.md, DOCS.md, migration files, email templates, frontend SCSS, and dozens of unrelated services — never converging on `verify_seller` or any specific model.

### Root cause
The Researcher's own `teams/engineering.yaml` instructions (Step 3a) already required `search_files(<domain term>) and/or lightrag_query(<key term>)` for "how does X work" questions — a reasonable rule. It never got the chance to apply judgment: the Coordinator's own `_COORDINATOR_INSTRUCTIONS` recipe for "For 'how does X work' / feature questions" (swarm/team.py, ~lines 537–543) *explicitly templated* the delegation text as `search_files for "X"`, told the Coordinator to substitute `X` with the bare subject noun from the question, and never mentioned `lightrag_query` at all. The Coordinator followed that recipe exactly; the delegation text it produced is a near-verbatim match to the recipe's own wording. This is a template defect, not a Researcher tool-selection failure or a model instruction-following gap.

### Fix
Rewrote the recipe (swarm/team.py) to: (1) call `lightrag_query` with the **full topic phrase** first — explicitly instructing against a single generic noun, citing the live T3 incident as evidence; (2) `search_files` for that same full phrase plus whatever specific symbol/term `lightrag_query` surfaces; (3) read the **specific** route/handler file the search points to, not simply "the 2–3 most relevant by hit count"; (4) follow that file's own imports/calls toward the model or persistence layer it actually uses, rather than delegating a second, equally broad "read file X, return every declaration" task.

### Live result — T3 (files NOT named): CONFIRMED PASS
Journal evidence (PID `1594069`, 16:55:25): the Coordinator's delegation now reads
> `'call lightrag_query with the full topic phrase "seller verification" first, then search_files for that same full phrase and any specific symbol/term the lightrag_query result surfaces, and return every matching file:line.'`

— a near-verbatim instantiation of the rewritten recipe. `lightrag_query({'query': 'seller verification', 'project_id': 'ekam', 'mode': 'hybrid'})` was genuinely invoked (16:56:00) and returned correctly-targeted results naming `business_admin_api.py`, `verify_seller`, `BusinessProfile`, `AdminVerifyRequest`, `BusinessStatus`, `PortalShell.tsx`.

The final answer correctly named `verify_seller` (`API/business-service/router/business_admin_api.py`) and traced it to `BusinessProfile`, `BusinessStatus`, `AdminVerifyAction`, and `BusinessAuditEvent`.

**Ground-truth correction.** This session's earlier-established ground truth for T3 (from the ad-hoc retest two phases ago) asserted the relevant model was `VerificationCheck` at `models.py:266`. A direct read of `verify_seller`'s actual function body this phase (`business_admin_api.py:84–160`) shows this was wrong: the function does `select(BusinessProfile).where(BusinessProfile.business_id == business_id)`, sets `seller.status = BusinessStatus.ACTIVE` / `BusinessStatus.REJECTED`, and writes `BusinessAuditEvent` rows — `VerificationCheck` is not touched by this route at all. The live run's citation of `BusinessProfile` is correct; my own prior ground truth was stale. This is recorded here rather than silently corrected, per the mission's own emphasis on verifying actual state rather than trusting reported values — including previously-reported values from earlier in this same engagement.

This is a dramatic, confirmed improvement over the original failure, which named nothing specific at all.

### Live result — independent generalization control: GST rate-change notification trace
Prompt: "How does GST rate-change notification work for sellers? Trace it from the API route to where it stores or updates data." Ground truth (verified by direct grep/read): `GET /gst/rate-changes` → `list_gst_rate_changes` (`business_api.py:527`), `POST /gst/rate-changes/{notification_id}/acknowledge` → `acknowledge_gst_rate_change` (`business_api.py:568`), both querying `business.gst_rate_change_notification` via raw SQL `text()` — deliberately chosen because **no ORM model class exists** for this table, a structurally different shape from the seller-verification trace, to test generalization rather than a one-off fit to T3's exact wording.

Journal (PID `1603275`): the Coordinator's delegation again used the full phrase — `'call lightrag_query with "GST rate-change notification" and search_files for that same phrase...'` — not a bare noun like "GST" or "notification". `lightrag_query({'query': 'GST rate-change notification', ...})` was genuinely attempted and **timed out at 180s** (an infrastructure fluke — `McpError: Timed out while waiting for response`, no indication this is related to the Phase 0.37 change). The Researcher correctly fell back to `search_files` on the **same full phrase**, which returned exactly 2 real, relevant files (`business_api.py:524`, `useSystemNotifications.ts:42`) — no keyword-dump noise. The final answer correctly named `list_gst_rate_changes`, `acknowledge_gst_rate_change`, and `business.gst_rate_change_notification`, and did **not** fabricate an ORM model for the table (correctly described it as a raw SQL target, "This table likely contains fields such as..." — a reasonable inference about columns, not an invented class). A separate evidence-ledger coverage flag ("required relationship not demonstrated for: the api route, where it stores or updates data") appropriately noted the trace's completeness was not fully nailed down to one single observation — a pre-existing, unrelated thoroughness guard being appropriately cautious, not a fabrication.

**This control confirms the fix generalizes**: the core mechanism — full topic phrase instead of a single generic noun — is what narrows discovery, and it held even when `lightrag_query` itself failed outright.

## Section D — Regression Assessment

- **Phase 0.35** (completion-adoption gate, full-draft revalidation): confirmed **still firing correctly** — the T13a journal shows the gate rejecting a fabricated repetition-completion draft exactly as designed, unrelated to any change this phase.
- **Phase 0.36** (citation/quote-pairing, entity-attribution, bold-symbol extraction) and the **`51e6398` hotfix** (flat-sibling-list false positive): not directly exercised by this phase's specific prompts, but no code touched this phase affects any of those regexes/functions, and nothing in any journal this phase suggests recurrence.
- **T13b / `compare_enumerations`**: backend-enumeration accuracy is unaffected — 9/9 endpoints correct with verified line numbers, real tables including `Voucher`/`VoucherSeries`/`VoucherVersion`. However, the Coordinator's **final synthesis** failed to surface the Researcher's own correctly-found 5/5 hooks (relegated to a "FORWARDED FROM THE MEMBERS" section instead of the main answer) and wrongly claimed "no specific gaps... were identified" despite the known 4-item gap — `compare_enumerations` was not invoked this run. This is a **pre-existing Coordinator aggregation-fidelity issue** (documented in `teams/engineering.yaml`'s own history: "the aggregation step was not faithful to what came back"), explicitly unrelated to P0 or P1 — neither workstream touches the Coordinator's synthesis step or this task's "audit a named service" recipe.

## Section E — Verdicts

- **P0: PARTIAL.** Controlled validation is thorough and precise — two real latent bugs (unanchored wildcard, missing `prose_route_results` in the early-exit condition) were found and fixed as a direct byproduct of that rigor, and both are independently confirmed correct against real repo data. The live run (T13a) shows a correct, fabrication-free outcome and no regression — but it did **not** exercise the new `context_files` code path; a different, pre-existing mechanism (Phase 0.35's gate + `compare_enumerations`) produced the clean result. There is no direct live confirmation of the new mechanism's own operation, only of the system's continued correctness around it.
- **P1: PASS.** Direct, unambiguous live journal proof that the Coordinator's delegation behavior changed exactly as designed on two independent prompts (T3 and the GST-rate-change control), `lightrag_query` genuinely invoked both times (succeeding once, timing out once with a correct fallback), and a materially more accurate, specific trace produced in both cases where the original T3 prompt previously failed completely.
- **Overall: PARTIAL.** Applying the mission's own "do not round up" rule: P1 has confirmed, repeated live behavior change with clean outcomes; P0 has thorough controlled validation and a clean but mechanism-ambiguous live result. This is one confirmed pass and one partially-confirmed pass, not two confirmed passes.

## Section F — Remaining Limitations

- **Confirmed defects:** none outstanding from this phase.
- **Controlled-only findings:** P0's `context_files` fallback mechanism — correctness demonstrated only in the scratch harness (6 cases covering absence, presence, multi-router ambiguity, non-router-only, real-claims-clean, and in-text-naming-wins), not yet directly observed live.
- **Live-unobserved behavior:** a final draft that both omits the router filename *and* asserts an unresolved/wrong route claim — the exact trigger for `context_files` — did not occur naturally in any of this phase's four live runs. Still needed for full live confirmation of P0.
- **Model-level variance:** T13b's aggregation-fidelity hiccup (Coordinator sometimes fails to surface a member's own correct findings into the final synthesized answer) — pre-existing, outside this phase's scope, flagged for possible future investigation, not fixed here. The GST control's `lightrag_query` timeout (180s) is noted as an infrastructure variance, not a logic defect — the fallback path handled it correctly.
- **`repair_unguarded_draft`'s known-uncovered call site:** has no `team` parameter and no caller found within `team.py` itself; not threaded with `context_files` this phase. Documented gap, carried forward.
- **Speculative concerns:** none added without evidence this phase.
