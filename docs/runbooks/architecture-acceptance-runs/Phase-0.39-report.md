# Phase 0.39 — Repetition-Truncated Completion Adoption and Code-Block Fabrication Guard

Investigation into the November 5, 2026 T13b failure in which a repetition-truncated completion pass fabricated two API operations (`PUT`/`DELETE` on `/vouchers/{voucher_id}`) inside a raw Python code block, and those operations reached the final answer unflagged. A separate investigation from Phase 0.38 — no part of Phase 0.38's reconciliation-disclosure work is assumed to address this defect, and none of it was touched.

## 1. Starting and Ending State

| | Start of phase | End of phase |
|---|---|---|
| `agno-hive` local/origin | `4c6b900` | `653df2e` |
| `agno-hive` ZGX | `4c6b900` | `653df2e` (pulled, fast-forward) |
| ZGX dirty/untracked count | 35 | 35 — unchanged, confirmed by exact count match |
| `hive-mcp` image | `237a88da3701d...` | `7b203660ab65...`, healthy, clean startup log |
| `agno-api.service` | PID `2812255` | **unchanged** — `swarm/team.py` was not touched this phase, so no restart was performed |

**Files changed:** `hive-mcp/tools/verify.py` only — 23 insertions, 0 deletions, commit `653df2e`.

**Exact incident:** run ID `c5ca3afd9b63`, journal PID `2933510` (the T13b rerun executed earlier the same session, at the user's explicit request to re-run T13a/T13b).

## 2. Chronological Reconstruction

Reconstructed directly from the journal (PID `2933510`) and cross-referenced against source:

1. **Researcher delegation (genuine, grounded):** reads `API/inventory-service/router/vouchers_api.py` for real — 45,338 chars, 30 declarations indexed. The real file's own token set (captured verbatim in the tool-result evidence) contains `list_vouchers`, `get_voucher`, `create_voucher`, `post_voucher`, `cancel_voucher`, `create_grn_from_po`, `create_credit_note`, `create_stock_adjustment`, `create_stock_transfer` — **and confirmed zero trace of `update_voucher` or `delete_voucher` anywhere in this real evidence.**
2. **First repetition stall:** the model's own generation hits the repetition threshold at 1,032 chars, cut off mid "`async def get_v`".
3. **Repetition-completion pass (bounded, no new tool calls):** re-types `list_vouchers`/`get_voucher`/`create_voucher` correctly and verbatim through 2,072 chars — matching the real file exactly.
4. **At 2,072 chars, the first fabrication:** `@router.put("/vouchers/{voucher_id}")` / `async def update_voucher` — a genuinely new continuation with no basis in anything read this run.
5. **Internal re-stall:** two further "repetition loop detected" events (re-emitting the `update_voucher` body) before the generation proceeds past them.
6. **At 2,581–2,745 chars, the second fabrication:** `@router.delete("/vouchers/{voucher_id}")` / `async def delete_voucher`.
7. **Second internal repetition threshold:** the completion's own generation is cut a second time at 2,745 chars, mid-word in `delete_voucher`'s signature.
8. **`new-claim-check` runs** (`content_chars=2,713`) — on the full continuation, including both fabrications — and genuinely reports "ok" (not bad). **This was confirmed, by direct source read of `swarm/team.py` (the `_complete_repetition_truncated_answer` function, lines ~8900–8945), to be a real verdict, not a bypass:** the `nested_truncation` adoption branch (line 8940, "adopting it but disclosing it as still incomplete") is reached only by falling through the `if _new_claim_bad and not _new_claim_unavailable:` block above it (line 8929) — there is no `elif`/`else` connecting them. It is only reached when that check already did **not** return early, i.e. only when `_new_claim_bad` was already `False`.
9. **The content ships** with `update_voucher`/`delete_voucher` intact, flagged only as structurally "incomplete" (the generic second-truncation disclosure) — never as containing an unverified or fabricated claim.

## 3. Root Cause, Separated from Contributing Factors

**Root cause (the extraction gap — confirmed by direct source read of `hive-mcp/tools/verify.py`):**

- `_code_idents`/`_dotted_idents`, the mechanism that pulls checkable claims out of fenced code blocks, **only extracts dotted attribute-access tokens** (`Voucher.tenant_id`, `db.execute`, `q.where`). Confirmed directly: the incident's own `verify_claims` SYMBOLS output checked exactly 12 symbols, all dotted attribute-access patterns from the real, correctly-reproduced `list_vouchers` body — **not one of them was a bare function name**, not even the real `list_vouchers`/`get_voucher`/`create_voucher`, let alone the fabricated `update_voucher`/`delete_voucher`. Bare `async def NAME(` declarations are never extracted as claims by this mechanism, real or fake.
- Separately, `_DECORATOR_ROUTE_RE` (feeding the plain, path-existence-only `ROUTES` section) **does** recognize `@router.verb("path")` decorator syntax — but its regex uses a non-capturing group for the verb, discarding it entirely and feeding only the bare path into a check that only asks "does this path exist anywhere," never "under this verb." Since `/vouchers/{voucher_id}` is a real path (under `GET`), this verb-blind existence check passed both fabrications clean.
- The **only** verb-aware check in the entire file, `_prose_route_verb_mismatches`, had patterns for exactly two claim shapes before this fix — bare backtick prose (`` `PUT /path` ``) and the separate labeled `**Method**`/`**Path**` form — and **no pattern at all** recognizing decorator syntax, so it never even saw either fabrication as a candidate claim.

**Contributing factor (not the root cause, and not what this phase fixed):** the repetition-completion pass's own behavioral tendency, observed consistently across all six T13a/T13b runs executed this session, to correctly re-type real content for a while and then drift into genuinely novel, unsupported continuation once it exhausts what it actually read. This is model behavior, not a code defect, and is explicitly out of scope — no changes to repetition thresholds, retry logic, prompting, or any watchdog/timeout mechanism were made.

## 4. Exact Code Change and Rationale

One new 23-line block inside `_prose_route_verb_mismatches` (`hive-mcp/tools/verify.py`), adding `_DECORATOR_VERB_PATH_RE.findall(answer)` as a **third claim-extraction source**, alongside the pre-existing bare-prose and labeled-form sources — feeding the exact same aggregated `claims` list and the exact same downstream comparison/verdict logic already in the function. No new regex (`_DECORATOR_VERB_PATH_RE` already existed, already used identically against the real source file two lines below, for the same verb+path shape), no new verdict shape, no new disclosure text.

**Deliberately did not** extend `_code_idents`/`_dotted_idents` to extract bare function-declaration names in general. That would be a much broader change — affecting every fenced code block in every answer, not just FastAPI-style router files — with genuine risk of flagging legitimate illustrative or example code that happens to define a plausibly-named function. It is not justified by this specific incident, which is fully explained and fully caught by the narrower route-verb fix alone: no fabricated handler inside a router file exists without an accompanying fabricated route decorator — the decorator is itself the signal.

## 5. Controlled Validation (scratch harness, no unit tests)

All against the real, unmodified-elsewhere production `_prose_route_verb_mismatches`/`verify_claims` (`test_p39_decorator_route_claims.py`):

| Case | Result |
|---|---|
| Exact T13b incident shape (real GET + fabricated PUT + fabricated DELETE) | 1 `PLAUSIBLE`, PUT and DELETE both `NOT FOUND`, correctly naming the real verbs (`GET, POST`) — **PASS** |
| Standalone fabricated PUT | Caught — **PASS** |
| Standalone fabricated DELETE | Caught — **PASS** |
| Real decorator-form route (`PUT /vouchers/{voucher_id}/post`) | `PLAUSIBLE` — **PASS** (regression) |
| Multiple real methods on related paths (`/post`, `/cancel`) | Both `PLAUSIBLE` — **PASS** (regression) |
| Pre-existing bare-prose form | Unchanged behavior — **PASS** (regression) |
| No router file named in the answer | Stays unresolved (empty list) — **PASS** (regression, single-candidate conservatism preserved) |
| Full end-to-end `verify_claims()` report on the incident text | Correct `ROUTE VERBS (3 checked)` section, PUT/DELETE both `NOT FOUND`, `VERDICT` correctly flags 7 unconfirmed claims — **PASS** |

Additionally re-ran Phase 0.37's own `context_files` regression suite (`test_p0_evidence_scoped_routes.py`) and Phase 0.36/the `51e6398` hotfix's own regression suite (`test_t3_regression_fix.py`, `test_p0_quote_pairing.py`) — all produced **identical** output to their original runs. No unit tests were added, modified, or run anywhere this phase; the unit-test suite was not run.

## 6. Live Validation

Targeted live run, exact T13b prompt, against the redeployed `hive-mcp` image (`7b203660ab65...`): PID `2972528`, run `18615aa8e769`.

**Result: accurate and safe, but did not naturally exercise the fix's new code path.** The model correctly reproduced 7 real endpoints (`list_vouchers`/`get_voucher`/`create_voucher`/`post_voucher`/`cancel_voucher`/`create_grn_from_po`, cut off mid-decorator on `create_credit_note`) — all genuine, no fabrication of any kind this time — before the repetition guard stopped the generation and correctly disclosed it as incomplete. Confirmed via journal: no `new-claim-check` fired, no `ROUTE VERBS` section appears anywhere, `_attempt_coverage_correction` entered and exited in 0.0s with `outcome=skipped` — there was nothing to reconcile, because nothing was fabricated.

Per the mission's own explicit Section 8 instruction, this run was **not** repeated in hope of a different result. The specific repetition-stall-into-fabrication pattern recurred in one of the two T13b attempts observed this session (the original incident), and did not recur in this one — consistent with genuine run-to-run model variance, not a sign the fix has a gap (the controlled validation directly exercises and confirms the fix's own logic against the real production function, independent of whether any single live run happens to reproduce the triggering fabrication).

**This is honestly reported as NOT OBSERVED for direct live confirmation of the fix firing** — the deployment is healthy and correct, and the controlled validation stands on its own for the mechanism itself.

## 7. T3/T4 Regression (scope-based reasoning, not live re-runs)

The diff touches exactly one function, `_prose_route_verb_mismatches`, adding one claim-extraction source. T3 (conceptual-trace discovery via `lightrag_query`, governed by `swarm/team.py`'s Coordinator instructions) and T4 (`SYMBOLS`/`CITATIONS` extraction for `Party`/`PartyRegistration` model fields, a separate code path with no route or decorator involvement at all) are structurally disjoint from this change — confirmed by the diff itself, not by re-running either test live. A full T3/T4/T13a/T13b re-battery was judged unnecessary given this narrow, structurally-isolated scope, per the mission's own guidance.

## 8. Remaining Limitations

- **Bare function-declaration extraction** (non-route fabrications — e.g., a fabricated helper function with no accompanying route decorator) remains an unaddressed gap. Deliberately out of scope, not a regression introduced by this phase.
- **Illustrative/proposed-pseudocode code blocks are not specially exempted** by the new decorator-claim check, the same as the two pre-existing claim shapes it was added alongside. Confirmed via direct code read: no call to `_is_proposed_new_claim`/`_PROPOSED_NEW_CUE_RE` exists anywhere in `_prose_route_verb_mismatches`, for any of its three claim shapes. This is a pre-existing characteristic of the function as a whole, not something this fix newly introduces or worsens.
- **The retry-exception branch in `_attempt_coverage_correction`** (Phase 0.38's own documented, separate limitation) remains unaddressed — different file, different function, out of this phase's scope entirely.
- **One live confirmation (not obtained this session) would be one instance, not a deterministic guarantee** against every possible future repetition-completion fabrication shape. The repetition-completion pass's own behavioral tendency to drift into fabrication after exhausting real content remains present and unaddressed as a root behavior — only this one specific fabrication shape (decorator-syntax route claims) now has a detection path where it previously had none.

## 9. Verdicts

- **Investigation: PASS.** Full chronological reconstruction achieved directly from journal and source evidence, with zero remaining hypothesis — the `nested_truncation` branch's fallthrough-only reachability was confirmed by direct code read, not inferred.
- **Implementation: PASS.** Narrow, reuses an existing regex and existing comparison logic, zero regressions across two prior phases' own test suites plus eight new scenarios.
- **Controlled validation: PASS.** All scenarios pass against the real, unmodified-elsewhere production function.
- **Live validation: NOT OBSERVED.** The specific fabrication shape the fix targets did not recur naturally in the one live run executed post-deployment; per the mission's own explicit rule, a correct answer alone — when the target condition was never exercised — is not claimed as live confirmation.
- **Overall Phase 0.39: PARTIAL.** Applying "do not round up": the investigation, implementation, and controlled validation are all sound and complete, but direct live confirmation of the fix firing was not achieved this session.

## 10. Addendum (2026-11-05, second live attempt, at the user's explicit request to retry)

A second live attempt against the same deployed fix (`hive-mcp` image `7b203660ab65...`, unchanged), same T13b prompt, PID `3090173`, run `bd2fa891fbb7`.

**Result: a fabrication recurred and was safely caught — but not the target fabrication shape, and not by this phase's new mechanism.** The repetition-completion pass correctly re-typed `list_vouchers`/`get_voucher`/`create_voucher`'s real signatures, then — while continuing `create_voucher`'s body — hallucinated an extensive, unrelated fabrication: a long chain of fake field assignments on the real local variable `voucher_create` (`voucher_create.voucher_code = generate_voucher_code()`, `.expiry_date`, `.voucher_redemption_limit_per_customer_per_week`, and over a dozen similar invented fields), consistent with a semantic drift toward "voucher" meaning a discount/coupon code rather than this codebase's real meaning (an accounting document) — followed by a fabricated `class Voucher(Base): __tablename__ = "vouchers"` (cut off), a fabricated frontend hook module (`useVouchersApiServiceHooks: IVouchersApiServiceHooks`, not matching the real `useGet...Query`/`use...Mutation` naming convention at all), and an incorrect gap conclusion ("`POST /vouchers` does not have a corresponding frontend hook" — wrong; `useCreateVoucherMutation` is real).

`verify_claims`'s `new-claim-check` correctly flagged 6 claims as unconfirmed and rejected the entire continuation, falling back to the pre-repeat 1,034-char draft (just the real, correct `list_vouchers` definition, cut off mid `get_voucher`), disclosed as incomplete — **a safe outcome, no fabrication shipped.** But this rejection was driven by the **pre-existing** dotted-attribute-access check (`_dotted_idents`, which already existed before this phase and is designed exactly for fake `owner.attribute`-shaped claims like `voucher_create.voucher_code`) — confirmed via journal: no `ROUTE VERBS` section appears anywhere in this run's output, and `_attempt_coverage_correction` again entered/exited in `0.0s`, `outcome=skipped`, because the rejected content never reached a new PUT/DELETE route decorator at all.

**This is a third consecutive live attempt (of three run today against the deployed fix) that does not exercise Phase 0.39's specific new code path.** Two attempts produced no fabrication of any kind; this one produced a fabrication of a different, broader shape than the fix targets, caught by different, pre-existing machinery. This is reported as an honest additional data point, not a reason to change the verdicts above: **Live Validation remains NOT OBSERVED, Overall remains PARTIAL.** It is also a reassuring secondary finding — the swarm's existing fabrication defenses (Phase 0.35's gate plus the pre-existing dotted-attribute check) continue to catch real, novel fabrication shapes they were not specifically built for — but it is not evidence for or against this phase's own decorator-route-verb fix specifically, which still awaits a live run where the model happens to fabricate a route decorator claim rather than a dotted-field claim.
