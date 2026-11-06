# Phase 0.45 — T12 Authoritative Router Inventory and Enumeration Completeness

Implementation phase (not investigation-only). Fixes the T12 architectural-audit
failure where a live battery run named only 12 of 23 inventory-service router files.
No canonical prompts, model configuration, routing, sampling, or infrastructure was
changed. No unit tests were added — validation is a scratch harness against the real
production functions, run directly and against the live local hive-mcp, never
committed.

## 1. Investigation

### 1.1 What the prior reports already established

- **Phase 0.42** (battery execution) measured the live shortfall directly: T12's
  shipped answer named 12 of 23 real router `.py` files (~52%), with zero fabricated
  router names — an omission, not a hallucination. Models (31/31) and both integration
  citations were fully correct in that same run.
- **Phase 0.43** (root-cause investigation) traced the mechanism: the Coordinator's own
  narration repetition-stalled early (as in every T12 run), the forced completion
  fabricated fake models (`InventoryItem`, `Category`, `Supplier`, `StockMovement` —
  none real), `verify_claims` caught and rejected that, and the **pre-existing** guard
  layer then substantially repaired the thin surviving draft: `models check` directly
  attached the real 31-model list (not by asking the model to reproduce it — by
  inserting it from `models.py`), and `_attempt_integration_correction` regenerated the
  whole answer from scratch, which passed cleanly. But in that clean regenerated
  answer's own free-text router list, enumeration broke down mid-list with **no
  repetition-loop event anywhere in that stretch of the log** — a genuine model
  thoroughness limit on a long, flat, 23-item list, not an infrastructure defect.
  Phase 0.43 explicitly recommended (but did not implement, being investigation-only)
  "whether a structural aid (e.g., a direct-attachment mechanism for router lists,
  mirroring `models check`'s approach for models) is worth adding."

### 1.2 What this phase additionally established, reading the current code

Read in full before any change: `_scoped_coverage_gap` (the existing router "scope
check"), `_mounted_router_files` (the main.py-mount reader), `_declared_models_not_reported`
(the existing "models check" — the direct-attachment mechanism Phase 0.43
recommended mirroring), `_attempt_integration_correction` and `_attempt_coverage_correction`
(the two existing correction-retry mechanisms), their shared call site in
`_verified_answer` (where all repo-derived guards run and their notes are concatenated
into the final text via one `_tail()` closure — not a first-match-wins chain; every
guard's note that fires is appended), and `_coverage_verdict`/`_retry_lost_enumeration`
(the generic `(found, expected)` readers of `record["routers"]`/`record["routers_total"]`).

**Root cause, precisely:** `_scoped_coverage_gap` measured coverage against **whichever
of two different sets happened to be in hand**, not against the complete, authoritative
inventory:

- When `main.py` was readable (the normal case), it used the **16 mounted routers** as
  both the denominator and the set compared against the answer.
- Only when `main.py` was unreadable did it fall back to the **23-file directory
  listing**.

So a 12-of-16 (75%) reading against the mounted denominator and a 12-of-23 (52%)
reading against the true directory denominator are the same literal event, and the
mission's own instruction not to conflate the two is exactly the bug: the guard was
treating "16 mounted" as if it were "the service's routers" whenever that read
succeeded, which is the common case. Compounding this, the guard's own firing threshold
(`_SCOPED_COVERAGE_RATIO = 0.25`) was tuned as a **pure disclosure** guard (per its own
long-standing comment, distinguishing it from `models check`'s 0.5 "repair" threshold)
— so even a correct 12-of-16 reading cleared the bar and stayed silent, and even when
it did fire, the banner disclosed at most 12 missing names in prose rather than
attaching the complete list the way `models check` already does for models.

Net effect: a model-generated answer naming 12 of 23 real router files could reach the
final answer with **no banner at all** — exactly what Phase 0.42's battery run shipped.

## 2. The fix

**File changed:** `swarm/team.py` only (one function, `_scoped_coverage_gap`, plus its
two tuning constants; no other function touched). Commit `0921ae2`, 90 insertions / 43
deletions, net +47 lines.

`_scoped_coverage_gap` now:

1. **Always reads both facts structurally from the repo**, independently — the
   complete router-directory listing (`_repo_find_files`, unchanged call) and the
   mounted subset (`_mounted_router_files`, unchanged call) — never one standing in for
   the other, and never hardcoded to the current 23/16 filenames.
2. **Measures completeness against the complete directory inventory** (the actual "its
   routers" the question asked about), with the mounted subset carried as a *separate*,
   per-file annotation rather than a competing denominator.
3. **Gates on the literal word "routers"/"router"** in the task text — `_SCOPED_ASK_RE`
   alone matches a models-only or endpoints-only question sharing the same sentence
   shape (`routers?|models?|endpoints?` is an alternation, not a conjunction), so this
   guard now additionally requires the specific word it exists to check, mirroring
   `_declared_models_not_reported`'s own explicit `\bmodels?\b` gate. This was a latent
   false-fire surface, not something the live battery run hit, but is now closed per
   the mission's own validation requirement #7.
4. **REPAIRS instead of only disclosing, mirroring `models check`'s existing
   direct-attachment approach.** When named coverage of the complete directory falls to
   or below 0.75 (deliberately set above `models check`'s own 0.5 — the live shortfall
   this phase fixes was 52% complete, which a 0.5 bar would still have let through
   silently, the same way the old 0.25-against-the-wrong-denominator bar did), the
   guard attaches the **full** missing list — not a 12-item prose preview — with each
   entry labeled `— mounted in main.py` or `— present as a file but NOT mounted in
   main.py (not part of this service's API surface)`, so the mounted/unmounted
   distinction the mission requires is preserved file-by-file in the final answer, not
   collapsed into one side or the other.
5. **Discloses ambiguity instead of claiming completeness** when `main.py` cannot be
   read: the attached list is still the complete directory inventory, but each entry is
   left unannotated and the banner states explicitly that "mount status could not be
   independently verified for this service and is not claimed here" — never silently
   presented as if every file's mount status were known.
6. **Never claims a router is mounted merely because its file exists** — the existing
   "answer presents an unmounted router as if real" check (`_unmounted_note`) is
   unchanged in behavior, now reusing the single directory-listing read this function
   already does instead of a second, redundant fetch.
7. **Preserves `record["routers"]`/`record["routers_total"]`'s existing generic
   contract.** `_coverage_verdict` and `_retry_lost_enumeration` both read these as a
   caller-agnostic `(found, expected)` pair and compare raw counts (not the specific
   denominator's identity), so changing the denominator to always be the complete
   directory total is a pure improvement in accuracy for both existing readers, not a
   breaking change — confirmed by reading both call sites before changing the
   denominator.

No broad refactor: the notation-matching logic (filename or `.router` mounted-module
form), the duplicate-collapsing logic, and the `_unmounted_note` detection are all
unchanged from the pre-existing, carefully-tuned implementation — only the denominator,
the gating, the threshold, and the shape of the attached evidence changed.

## 3. Ground truth, independently verified against the live repository

Via direct `docker exec` into the local hive-mcp container (never trusted from any
agent's own claims):

- **23** real router `.py` files in `API/inventory-service/router/` (24 directory
  entries minus `__init__.py`): `admin_gst_api.py`, `auto_restock_config_api.py`,
  `categories_api.py`, `custom_attributes_api.py`, `godowns_api.py`,
  `gst_compliance_api.py`, `hsn_api.py`, `import_api.py`, `inventory_api.py`,
  `inventory_suppliers_api.py`, `item_variants_api.py`, `items_api.py`,
  `parties_api.py`, `payments_api.py`, `purchase_order_api.py`,
  `purchase_order_items_api.py`, `smart_alerts_api.py`, `stock_api.py`,
  `stock_transactions_api.py`, `supplier_items_api.py`, `tally_import_api.py`,
  `uom_api.py`, `vouchers_api.py`.
- **16** of those are `include_router`'d in `main.py` (grep-confirmed):
  `admin_gst_api.py`, `categories_api.py`, `custom_attributes_api.py`,
  `godowns_api.py`, `gst_compliance_api.py`, `hsn_api.py`, `import_api.py`,
  `item_variants_api.py`, `items_api.py`, `parties_api.py`, `payments_api.py`,
  `smart_alerts_api.py`, `stock_api.py`, `tally_import_api.py`, `uom_api.py`,
  `vouchers_api.py`.
- **7** exist as files but are never mounted: `auto_restock_config_api.py`,
  `inventory_api.py`, `inventory_suppliers_api.py`, `purchase_order_api.py`,
  `purchase_order_items_api.py`, `stock_transactions_api.py`, `supplier_items_api.py`.
- **31** real, non-`Base` model classes in `models.py` (32 `class` lines total,
  including `Base`) — matches Phase 0.42's own ground truth exactly, including
  `GSTR1ExportLog` (`models.py:602`), which one earlier, imperfect grep in this phase's
  own working notes initially missed and a second, corrected grep confirmed real.

## 4. Scratch-harness validation

Run directly against the real, imported `swarm.team` module
(`sys.path.insert(0, agno-hive)`, `PROJECT_ROOT=EkamApp`) — no files added to the
tracked repository, no test runner invoked.

**`test_p45_t12_router_inventory.py`** — 18 checks, the 7 required scenarios, against
`_scoped_coverage_gap`/`_mounted_router_files` with a monkeypatched `_repo_find_files`/
`_repo_file_text` (so the function under test is the real production code; only the
repo I/O is faked):

| # | Scenario | Checks | Result |
|---|---|---|---|
| 1 | All 23 router files discovered | 2 | PASS |
| 2 | Mounted subset (16) independently identified from `main.py` | 1 | PASS |
| 3 | An unmounted file is never described as mounted, even when the answer presents it as real | 2 | PASS |
| 4 | A 12-of-23 (52%) answer — the exact live shortfall shape — is supplemented from authoritative evidence, not silently passed | 2 | PASS |
| 5 | Duplicate directory entries collapse; an unreadable `main.py` discloses mount status as unverified rather than claiming it | 3 | PASS |
| 6 | Final attached banner retains the complete inventory, the mounted-subset distinction, and explicit provenance | 4 | PASS |
| 7 | A models-only task (no "routers" word) triggers zero router-directory lookups; an unrelated single-fact question does not fire | 3 | PASS |

**TOTAL: 18/18 passed.**

**`test_p45_t12_live_realcheck.py`** — a second, separate validation with **no
monkeypatching at all**: calls the real `_mounted_router_files`/`_scoped_coverage_gap`
against the real local hive-mcp (`http://localhost:9003/mcp`) and the real EkamApp
repository. Confirmed the real mounted set is exactly the 16 files listed in Section 3,
and that feeding the real guard the exact live-shortfall shape (a 12-router answer)
produces a banner stating "23 router file(s)... of which 16 are mounted," attaching all
11 missing files with correct per-file mount annotation — the complete real inventory,
not a fixture.

## 5. Deployment

| | Value |
|---|---|
| Commit | `0921ae2` ("fix(swarm): authoritative router inventory + complete-inventory repair for T12") |
| Pushed | `origin/main`, `7fbfde8..0921ae2` |
| ZGX | fast-forwarded `7fbfde8..0921ae2`; dirty/untracked file count unchanged (35) before and after |
| `agno-api.service` | restarted; PID `3827476`; active since `2026-11-05 19:03:12 EST`; health `{"status":"ok", ...}` |
| `hive-mcp` | not touched — it is a local-machine container, not a ZGX service, and this phase changed nothing under `hive-mcp/` |
| `/runs` | confirmed empty immediately before the validation run |

## 6. Live validation

Canonical T12 prompt, extracted verbatim from `docs/runbooks/groundedness-battery.md`
(lines 115–120), SHA-256 `f2249c203d6c295042b16840ec50883b667df9ce132844f7a7211221fb7f32a0`
— identical to the hash recorded in Phase 0.42, confirming byte-exact reuse. Run once,
`swarm_team="engineering"`, `read_only=true`, fresh session, `/runs` confirmed empty
first, not repeated.

| | Value |
|---|---|
| Task ID | `kqluvmwn7` |
| Session ID | `d15048e4-c292-4379-a56c-9063986fd71e` |
| Run ID (`phase0`) | `bce8620e06d6` |
| Duration | 283.8s (phase0 log) / 285.73s (API footer) |
| Outcome | `ok` |

### 6.1 What the journal shows, in order

1. **19:03:38–19:04:42** — the Coordinator's own narration repeats three times and the
   standard repetition-threshold guard fires at the 4th, truncating in-process and
   keeping the 218 characters generated before the repeat — the same upstream trigger
   every prior T12 run hit (Phase 0.43, Finding 0).
2. **19:04:42** — the Phase 0.44 T11 no-evidence invariant also engages here (its
   trigger condition is generic, not T11-specific): `repetition-truncation completion
   skipped -- no usable current-generation evidence exists yet for this task`. Correct
   behavior — no synthesis is attempted without real evidence; the near-empty draft is
   kept rather than risking fabrication.
3. **19:04:49** — **this phase's new scope check fires, live, exactly as designed:**
   ```
   [team] scope check: inventory-service has 23 router files (16 mounted), answer names 0 — attaching the 23 it omitted
   ```
   confirming the fix executed against the real draft with the correct 23/16 ground
   truth and attached the complete missing list.
4. **19:04:50** — `models check` (0/31) and `integration check` (0/3 citations) also
   fire on the same thin draft — pre-existing mechanisms, unmodified.
5. **19:05:09–19:08:10** — `_attempt_integration_correction` (pre-existing, Phase
   T12/2026-10-23, not touched by this phase) spends its one retry budget: a full
   wholesale regeneration, streamed over ~181s, that this run happened to produce
   independently complete and correct — all 23 routers named with the mount
   distinction, all 31 models, and all 3 integration citations. `verify_claims` passed
   it cleanly (`retry-recheck`), and it was adopted: `integration-correction: retry now
   names the real integration and passes verify_claims -- adopting`.
6. **19:08:10** — `_attempt_coverage_correction` (Phase 0.44's T13a mechanism, not
   touched by this phase) enters and immediately exits `outcome=skipped` — no
   comparison-style gap remained to correct.
7. Final `enforce_verification_invariant_final` pass: clean.

### 6.2 Final answer vs. ground truth

The shipped final answer (reproduced from the task result, 3,135 characters including
one appended evidence-ledger disclosure) states:

> "The inventory service has 23 router files in its router directory, of which 16 are
> mounted in main.py" — followed by the exact 16 mounted filenames, then "the following
> router files are present but not mounted in main.py" — followed by the exact 7
> unmounted filenames; then "31 model classes," listing all 31 by name; then the same 3
> integration citations verified in Phase 0.42.

Checked line-by-line against Section 3's independently-verified ground truth: **every
router filename, every mount/unmount classification, all 31 model names, and all 3
integration citations are exactly correct. Zero omissions, zero fabrications.**

One pre-existing, unrelated guard fired on the final text: an evidence-ledger citation
check flagged the 3 integration citations as "not confirmed by the ledger... reported
as unconfirmed, not as a proven error" — a separate mechanism (checking whether a
citation was captured by this specific run's own tool-call ledger, not whether it is
factually correct) that this phase did not touch and that does not affect the
router/model completeness this phase was scoped to fix.

### 6.3 Honest attribution — the repair mechanism fired, but a different, pre-existing
mechanism produced the shipped text

This phase's new repair banner is confirmed executing correctly in production (Section
6.1, step 3): given the real 0-of-23 draft, it correctly read the real 23/16 ground
truth and attached the complete missing list with correct mount annotations — the exact
behavior validated in Section 4. **It did not, however, end up as part of the final
shipped text.** `_attempt_integration_correction` — a separate, pre-existing mechanism
this phase did not modify — discarded the entire draft (including this guard's
attached banner) and replaced it wholesale with a freshly regenerated answer. That
regeneration happened, on this run, to independently enumerate all 23 routers and all
31 models correctly on its own, with no need for repair.

**This is a real, honestly-reported residual limitation, not a success to round up:**
nothing in the current pipeline re-invokes `_scoped_coverage_gap` (or `models check`)
against `_attempt_integration_correction`'s adopted output. Its call site returns
`retried` directly on success (`swarm/team.py:16958`), with no equivalent to
`_attempt_coverage_correction`'s own "recheck after retry" step. Had this run's
regeneration instead produced a partial router list — say 18 of 23, a plausible
shortfall on a long enumeration per Phase 0.43's own finding — this phase's fix would
not have gotten a second chance to catch and repair it, because the only place it runs
(`_verified_answer`, upstream of the integration-correction call) had already been
superseded. Closing that gap would mean adding a recheck step to
`_attempt_integration_correction`'s success path — a second function touched, which
this phase's "narrow fix" and "no broad refactor" constraints place out of scope here.
Flagged for a future phase, not implemented.

## 7. Remaining limitations

1. **Section 6.3's gap**: `_attempt_integration_correction`'s successful-retry path does
   not re-run the repo-derived router/model guards against its own output. This
   phase's fix is confirmed correct and firing on the draft stage it actually runs at;
   it cannot yet protect a regenerated answer that comes out of that *later* stage
   still incomplete.
2. This phase, like every prior T12 run on record, still hit the Coordinator-narration
   repetition stall at the very start of generation (Section 6.1, step 1) — unchanged,
   out of scope, already covered by the existing repetition/no-evidence machinery.
3. The evidence-ledger citation guard's "unconfirmed, not a proven error" disclosure on
   this run's 3 correct integration citations (Section 6.2) is a pre-existing,
   unrelated mechanism and was not investigated further — it does not affect this
   phase's acceptance criterion.
4. One run is one data point. Per every prior phase's own stated caution, this is not a
   claim that T12 is now permanently fixed against every possible shortfall shape —
   only that the specific, confirmed-live 12-of-23 omission shape is now caught and
   repaired at the stage this guard runs, and that the guard itself is proven correct
   against real ground truth via two independent scratch-validation methods plus one
   live production firing.

## 8. Verdict

- **Root cause**: **CONFIRMED** — a denominator conflation (16 mounted vs. 23
  directory) combined with a disclosure-tuned threshold too lenient for either reading.
- **Fix**: implemented, scoped to one function in `swarm/team.py`, no canonical
  prompts/model config/routing/infra touched.
- **Scratch validation**: **18/18 PASS**, covering all 7 required scenarios, plus a
  second, unpatched live-repo check.
- **Live validation**: the new guard **CONFIRMED FIRING** in production with correct
  ground truth (Section 6.1 step 3); the **final shipped answer is completely correct**
  against ground truth (Section 6.2) — but via a different, pre-existing mechanism's
  independent success, not via this guard's own output surviving to the final text
  (Section 6.3).
- **Overall**: **PARTIAL** — the implementation is correct, independently verified, and
  proven firing live exactly as designed; the specific live run's shipped answer is
  fully complete and correct; but this phase cannot claim the fix is what made *this
  run's* final text complete, and documents a real, specific gap (Section 6.3/7.1)
  where a future incomplete regeneration could still slip through uncaught. Not a FAIL
  — the acceptance criterion's literal requirement (the final answer enumerates the
  complete authoritative inventory and preserves the mount distinction) is met for this
  run — but not a clean PASS either, since the mechanism built to guarantee that outcome
  did not end up being the one that delivered it.
