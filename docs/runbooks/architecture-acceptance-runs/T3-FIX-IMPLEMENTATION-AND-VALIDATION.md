# AgnoHive — T3 Retry False-Positive + Unsafe Retry Adoption: Fix, Deployment, Validation

## 1. Implementation Summary

Two changes to `swarm/team.py`, both scoped exactly to the T3 root cause documented in `T3-FINAL-SYNTHESIS-EVIDENCE-LOSS-BOUNDARY.md`:

- **Fix A** (`_has_bare_absence_claim`, now preceded by new helpers `_code_span_ranges`/`_in_code_span`): a `_NOT_FOUND_RE` match is now judged individually by position. A match falling inside a fenced code block or inline code span (a quoted source-code excerpt) no longer counts toward the guard; only a match in plain prose does. A genuine plain-prose bare-absence claim elsewhere in the same answer, alongside unrelated code, is still caught exactly as before — each match is evaluated on its own position, not on whether the text contains code anywhere at all.
- **Fix B** (the search-claim retry adoption call site inside `_verified_answer`, ~swarm/team.py:9591–9623 after the patch): before `_adopt_retry("search-claim", ...)` is called, the retry's own text must now pass `_answer_supported_by_evidence(retried, team)` — the existing, deterministic, no-LLM-call mechanism that checks whether every salient token in a candidate is one a tool actually returned this run. If it fails (or is unavailable/inconclusive — conservative by construction, per that function's own docstring), the retry is rejected outright and the original draft is retained; `_adopt_retry`'s existing structural checks never even run in that case.

## 2. Root-Cause Coverage

- Fix A prevents the exact trigger that destroyed the correct T3 draft: the model faithfully quoting `HTTPException(status_code=404, detail="Seller not found")` from real source it had actually read. That quote no longer satisfies the bare-absence guard, so the correct draft is no longer discarded and no forced retry is dispatched on this basis.
- Fix B closes the second half of the chain even if a search-claim retry is dispatched for some other, legitimate reason: a retry that introduces brand-new, uncited symbols (as the fabricated T3 retry did — `seller_verification_api.py`, `SellerVerificationService`, `/seller-verification`, a `Seller` model) now fails the evidence-support gate and cannot replace a draft the way it did in the incident.

## 3. Diff and Commit

- **Changed file:** `swarm/team.py` only — 84 insertions, 5 deletions. No other file touched.
- **Commit:** `009044e08161d782830cd76d7699fb102b34b2a2` on `main`, pushed to `origin/main` (fast-forward from `d80f559`).
- **Confirmed no unrelated files included:** `git diff --cached --stat` before commit showed exactly one file; `git status` before and after confirmed the five pre-existing, unrelated working-tree modifications (`docs/runbooks/groundedness-battery.md`, `tests/test_evidence_fidelity.py`, `training/train.py`, `zgx-ai-setup/litellm-config.yaml`, plus several untracked files) were left untouched and unstaged throughout.
- One stray, pre-existing, 0-byte artifact file (`{v5}`) left over from an earlier shell-escaping mishap in this same session was deleted as part of my own scratch cleanup — it was never tracked, never staged, and is unrelated to this patch.

## 4. Validation A Results — Absence-Guard Behavior (targeted checks, not unit tests)

Run directly against the real, deployed `_has_bare_absence_claim` via a standalone script importing `swarm.team`, no test files created:

| Case | Input shape | Result | Expected | Verdict |
|---|---|---|---|---|
| 1 | The exact incident shape: `"Seller not found"` inside a fenced code block, no genuine prose absence claim | `False` | `False` | **PASS** |
| 2 | Genuine plain-prose bare absence claim ("...Not found.") with no code anywhere | `True` | `True` | **PASS** |
| 3 | Unrelated code elsewhere + a genuine prose claimed-search term (`` `expiry_date` ``) in the guard's own existing after-the-phrase grammar | `False` (correctly not a *bare* absence — it names a term) | `False` | **PASS** |
| 3b | Unrelated code elsewhere + a genuine BARE prose absence claim with no named term | `True` (not masked by the unrelated code) | `True` | **PASS** |

All four cases pass. (Case 3's original test wording used "the X field was not found" — before-the-phrase grammar the pre-existing `_claimed_search_terms` windowing has never supported, confirmed by testing the unmodified function directly; corrected to the guard's own established after-the-phrase grammar, which is pre-existing, unchanged behavior, not something this patch altered.)

## 5. Validation B Results — Retry-Adoption Behavior

Run directly against the real `_answer_supported_by_evidence` and `_adopt_retry`, with a minimal `FakeTeam` carrying `_evidence_tokens` built the same way production code builds it (`_salient_tokens` over realistic tool-result text), no test files created:

| Case | Scenario | Result | Verdict |
|---|---|---|---|
| 1 | Verified original + fabricated retry (the actual T3 shape: invented `/seller-verification`, `seller_verification_api.py`, `SellerVerificationService`) | Rejected; original retained | **PASS** |
| 2 | Verified original + acceptable retry (restates only real, previously-gathered evidence) | Adopted | **PASS** |
| 3 | Verification unavailable (`team` has no `_evidence_tokens` captured at all) | Rejected; original retained (conservative by construction) | **PASS** |
| 4 | Original not accepted / abstention path | **NOT INDEPENDENTLY VALIDATED** — this fix only gates replacement of an already-produced draft at one call site; it does not touch, and this harness does not exercise, the separate upstream mechanisms that decide whether the original draft itself is disclosed as incomplete/unverified. No code in those upstream paths was changed. |
| 5 | Structural rejection (empty/whitespace-only retry) still works | Rejected; original retained | **PASS** |

## 6. Deployment

- **Branch:** `main`. **Local baseline before this work:** `d80f559` (== `origin/main` at the time, confirmed clean, no divergence).
- **Pushed commit:** `009044e`, fast-forward, no force, no overwrite of any remote work.
- **ZGX:** fetched `origin/main`; confirmed `61631f0` (ZGX's pre-patch HEAD) is an ancestor of `009044e` and that the incoming diff (two pre-existing report `.md` files + `swarm/team.py`) does not overlap with ZGX's own pre-existing local modifications (`training/train.py`, `zgx-ai-setup/docker-compose.yml`, `zgx-ai-setup/litellm-config.yaml`) — confirmed via `git diff --name-only HEAD` before and after. `git merge --ff-only origin/main` succeeded cleanly; those three unrelated local files remain modified exactly as before.
- **Service restart:** `agno-api.service` restarted. `curl localhost:9001/health` → `{"status":"ok",...}`. `/runs` empty before the validation run.
- **Deployed SHA verified:** `git rev-parse HEAD` on ZGX → `009044e08161d782830cd76d7699fb102b34b2a2` — exactly the reviewed, pushed commit.
- No deployment issues encountered.

## 7. Live T3 Result (Validation C)

- **Session:** `bf59a1ff-cc7b-42e8-a2d9-570da05832ae`. **Duration:** 1789.32s. **Team:** `engineering`, `read_only=True`, fresh session, the exact persisted prompt from `docs/runbooks/groundedness-battery.md`'s T3 entry: *"How does seller verification work in this codebase? Trace it from the API route to the database model."*
- **Final answer claims — all independently checkable against the real codebase, and all real:** API routes in `business_api.py` (`/register`, `/status/{user_id}`, `/register-additional`, `/status`), database models `BusinessProfile` and `VerificationCheck` in `models.py`, and a set of real migration files (e.g. `d0e1f2a3b4c5_rename_seller_tables_to_business.py`, `e5f6a7b8c9d2_add_seller_email_and_audit_events.py`) — every one of these is a real, confirmed file/symbol from the original investigation's own exhaustive ground-truth verification.
- **Zero occurrence**, anywhere in the published answer or its appended member sections, of `/seller-verification`, `seller_verification_api.py`, `SellerVerificationService`, `seller_verification_service.py`, or an invented `Seller` model. This is the single most important result of this validation: the exact fabrication from the original incident did not recur.
- **This run took a materially different exploration path** than the original incident (heavier migration-file exploration, no dedicated delegation to `/verify/{business_id}`'s own implementation) — expected and unsurprising given this battery's own long-documented run-to-run variance; not itself evidence of anything, positive or negative, about the fix.
- **One disclosure banner is present** ("NOT AUTHORIZED AS COMPLETE BY THIS RUN'S OWN EVIDENCE LEDGER..."), listing eight citations as unverifiable because the underlying read was truncated before retention — all eight are **real files** (`stock_api.py`, `tenant_service_api.py`, several real migration files, etc.), not fabricated ones. This is a legitimate, separate completeness disclosure, not evidence of any new fabrication.
- **No new tracebacks or service errors observed** in the portion of the journal reviewed.
- **Whether the bare-absence guard fired at all during this specific run, and whether a search-claim retry was dispatched, could not be confirmed from the journal before this report was finalized** — the ZGX command channel became persistently backlogged (even a trivial `echo` queued for minutes) partway through this investigation, and the targeted `grep` of this run's own journal segment (`/tmp/t3_validation_c.log`, saved during this session) did not return in time. This is reported honestly as an open item rather than inferred. **The decisive, directly-observed fact — that the published final answer contains none of the four fabricated entities and cites only real evidence — stands independently of this open item** and is sufficient to answer the validation's central question (did the fabrication recur) on its own.

**Honest classification of Validation C: PASS on the decisive question (no fabrication occurred, all final claims are real), PARTIAL on full mechanism confirmation (guard-firing/retry-path specifics not confirmed before this report, due to tooling backlog, not due to any negative finding).**

## 8. Remaining Risks

- This is a single live run. Per this battery's own repeated, documented caution, one run does not establish a rate — only that the specific fabrication observed in the original incident did not recur this time, under materially similar but not identical conditions (different exploration path taken by the model).
- The open item in Section 7 (unconfirmed guard-firing mechanics for this specific run) means this report cannot yet certify that Fix A's trigger condition was exercised at all in this run — it is possible the model simply never produced a code-quoted "not found" this time, in which case this run exercises Fix B's path (if any retry occurred) more than Fix A's. The Validation A/B targeted checks (Section 4–5) remain the authoritative, direct evidence that both fixes behave correctly in isolation, independent of this run.
- Fix B is scoped to exactly one call site (the search-claim retry adoption) and deliberately does not touch `_adopt_retry`'s ~11 other call sites, per the mission's explicit scope limit. Those other retry paths (write-claim, no-evidence, etc.) remain exactly as they were and were not re-examined here.

## 9. Explicit Scope Confirmation

- **No unit tests were written or run.** All validation (Sections 4–5) used standalone scripts in the session scratchpad directory, exercising the real, deployed functions directly — never pytest, never a committed test file.
- **No full T1–T13 battery was run.** Exactly one live scenario (T3) was executed, per the mission's explicit authorization.
- **No T14 or follow-on phase was started.**
- **No unrelated changes were made.** Only `swarm/team.py` was modified and committed; all pre-existing unrelated working-tree modifications on both the local machine and ZGX were confirmed present and untouched before and after every step.
- **No destructive action was taken** at any point — no reset, no force-push, no overwrite of any unrelated work, on either the local machine or ZGX.
