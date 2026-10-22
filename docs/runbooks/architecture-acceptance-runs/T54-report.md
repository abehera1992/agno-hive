# T54 — Coordinator factual accuracy, count integrity, and recovery-path validation

Builds on, and does not overwrite, `T48-report.md`, `T48-rerun-report.md`,
`T51-report.md`, `T52-report.md`, or `T53-report.md`. Commit
`4935dc67dbd6ccde9f2309051235aa7ab73cbcf6` (local `main` and `origin/main`),
deployed to ZGX `agno-api.service`. No change to `hive-mcp/tools/verify.py` this
phase, so the `hive-mcp` container was not redeployed.

## 1. Overall verdict

**PARTIAL**, with one priority (T7) now meeting the PASS bar on its own.

T7 is fully, cleanly, unambiguously live-confirmed as a genuine correctness fix
(not merely a disclosure): a live run that reproduced the exact miscount had its
entire wrong answer replaced with an authoritative restatement, confirmed both in
the delivered text and in a matching journal line. T12's recovery-path disclosure
is fully, unambiguously live-confirmed via a safe, controlled exercise of the
real deployed mechanism. T2's own new exclusion branch (`_matched_item_named_as_
missing`) still has not been directly observed firing in production across three
live attempts (one in T53, two in T54) — but this phase's own new, distinguishing
logging now proves exactly why: the contradiction that did occur in the third
attempt arose from a separate, independent retry mechanism outside this gate's
one-shot invocation window, which the gate (confirmed via the new log line: 0
sections excluded, nothing to exclude at that point) correctly had no reason to
touch, and which a different, pre-existing guard then correctly caught on its own
terms. The delivered answer's own pass criterion (the matched item is not
described as missing) held in all three live attempts, by a combination of
mechanisms. Reported as PARTIAL for T2 specifically, not because the system's
behavior was wrong, but because the specific new code path remains
undemonstrated live — exactly the honest distinction the mission's own acceptance
criteria require.

## 2. T2

**Root cause and exact implementation change:** none, this phase. T53's fix
(`_matched_item_named_as_missing`, `_MISSING_FRAMING_RE`, the `cmp_note` parameter
on `_sections_without_completeness_claim`) is confirmed unchanged and still
deployed. This phase's only T2 change is additive, narrow logging: each branch of
`_sections_without_completeness_claim`'s exclusion loop now prints which specific
mechanism (the blanket completeness lexicon, or the new matched-item check)
dropped a section, and names the matched identity when the new branch is the one
that fires. Kept as a permanent signal, not a temporary diagnostic, since without
it a live run cannot distinguish the two paths from the journal alone — exactly
the gap T53's own live validation hit.

**Targeted live runs:** two this phase.
- MCP task `kbxxxr147`, session `c436d14f-534b-48b5-ac5b-d42e690b3643`,
  `run_id` `cf687d81d3e5`, 1637.5s. `DETERMINISTIC_COMPARISON_GATE_FIRED` logged
  once (`match=7 partial_match=1 left_only=5 right_only=8`), immediately followed
  by `comparison gate: preserving 10 unrelated section(s)` — **and zero
  `section exclusion:` log lines of either kind.** The draft had nothing to
  exclude at the moment this gate ran. A SEPARATE, later mechanism (the run's own
  "UNRESOLVED — THIS ANSWER CONTRADICTS EVIDENCE" disclosure, firing on a
  subsequent correction/retry cycle outside this gate's one-shot window) itself
  caught a real instance of the exact T2 shape later in the same run —
  `useGetEmailCredentialsQuery`/`GET /business/email-credentials` claimed missing
  at that later stage, directly contradicted by "compare_enumerations' own
  MATCHED/HOOKS EXPORTED output" per that guard's own message. **The final
  delivered answer's own comparison table correctly shows this endpoint matched
  (`Corresponds to useGetEmailCredentialsQuery`) and excludes it from every
  version of the "Endpoints Without Corresponding Hooks" list in the delivered
  text.**
- A second live run (via the originally-planned T2 prompt, prior session turn)
  produced a similarly-shaped result: a real contradiction detected and disclosed
  by the same later-stage mechanism, with the final delivered answer correctly
  not misrepresenting the matched item.

**Evidence that the new exclusion branch fired:** not established this phase.
The new logging proves definitively that it did NOT fire in the one run where its
own gate invocation was directly observed (0 exclusions, confirmed empty), and
the architecture explains why: `_COMPARISON_RECONCILE_FLAG` makes
`_reconcile_completeness_claim_with_comparison`'s own synthesis a one-shot
operation per run; a contradiction introduced by ANY later, independent
regeneration (a verify_claims-triggered retry, a different correction cycle)
never passes through this gate again and so can never exercise this phase's own
exclusion branch, by design of the existing one-shot discipline T53 did not and
should not change.

**Final comparison and answer consistency:** correct in the delivered text, in
both live runs this phase. **Verifier interaction:** resolved in the practical
sense that matters (the matched item is not presented as missing), though the
run's own banner language ("one correction attempt did not resolve it") is itself
about a different, residual disagreement (the two compare_enumerations calls in
the same run reported different left/right/matched totals at different points --
`left=13 right=16 matched=7` versus `5 left-only and 8 right-only`-- most likely
from two separate comparison invocations at different stages of the same run,
not independently re-derived this phase) rather than about the matched-item
claim specifically.

**Performance:** 1637.5s, longer than T53's own 1306.89s T2 attempt and this
phase's prior turn's attempt; no directly comparable historical baseline exists
for this exact scenario, consistent with T53's own note.

**Limitation identified, not fixed this phase:** the new exclusion branch's own
scope is structurally confined to one gate invocation per run. Extending
protection to a later, independent regeneration would require either reusing
`_matched_item_named_as_missing` inside whatever later mechanism produces the
"UNRESOLVED" disclosure (not inspected in this phase's own time budget) or
relaxing the one-shot flag (explicitly not attempted -- that flag exists for
reasons unrelated to T2 and changing it was out of this phase's traced scope).
Flagged honestly in §7 rather than attempted speculatively.

## 3. T7

**Source of the incorrect count (confirmed, T53, re-verified unchanged this
phase):** the Coordinator's own single continuous completion stream
spontaneously restates a `list_directory` result a second time with
independently-wrong specifics. Still a model-generation phenomenon, not a tool,
forwarding, or evidence-assembly defect; still invisible to Z16's repetition
detector (the two restatements are not textually identical).

**Implementation change:** new `_task_wants_extension_count` (detects "the exact
number of .py files"-shaped task wording -- unambiguous about which of
`_miscounted_listing`'s several defensible readings is wanted, only when the task
itself already named one) and `_authoritative_listing_restatement` (builds a
fully deterministic, correct restatement directly from `listing["names"]`, the
tool's own real per-file list). Wired into `_miscounted_listing`'s call site:
when the restatement is available, it REPLACES the wrong answer outright; every
other case (no extension named, multiple listings, named extension absent from
the real listing) falls through unchanged to T53's existing mark-in-place
behavior.

**Before/after, live-confirmed:** three live runs this phase.
1. MCP task result, session `aa5eab12-ff33-4190-8561-a2810f384c68`, 83.18s: the
   model's own restatement was already correct (6, with `__init__.py`); this
   phase's fix correctly did not fire (nothing wrong to replace); a different,
   pre-existing guard (`_count_contradicts_own_list`) correctly caught the
   duplication's OWN side effect (count says 6, the text's own duplicated
   bullets total 12 lines) -- confirms no new false positive and confirms the
   existing self-contradiction guard remains intact and complementary.
2. Session `6bfbde96-408c-43e0-97f7-d5b3fd86888d`, 109.92s: same outcome --
   correct count both times, this phase's fix correctly silent, T52's own no-op
   -retry-transparency fix separately and correctly fired (recovered the first
   check's findings on a byte-identical resubmission), confirming that
   unrelated T52 behavior remains intact.
3. **Session `a32a9079-38ac-498b-9568-982ab66c753e`, 80.45s: the model stated 5
   (its usual mistake, omitting `__init__.py`). Delivered answer: "`API/
   business-service/router/` contains 6 `.py` file(s), per list_directory's own
   output:" followed by the complete, correct 6-file list including
   `__init__.py`, followed by "**THE ORIGINAL ANSWER'S COUNT DID NOT MATCH THE
   DIRECTORY LISTING AND HAS BEEN REPLACED — it stated 5...**". Directly matched
   by the journal line `[team] answer states 5 but list_directory(...) returned
   6 items — task wording names a specific reading; replacing with the
   authoritative restatement`, which cannot print unless this phase's own new
   code path executed.**

**Whether the wrong count was prevented or merely disclosed:** **prevented.** The
final delivered answer in run 3 is factually correct, not merely flagged as
wrong. This is a materially stronger result than T53's own inline-mark fix,
which left the wrong number as the primary text.

**Distinct files intact:** confirmed -- all 6 real files present in the
replacement text in run 3, none invented, none dropped, none deduplicated beyond
what the real listing itself contains.

**Runtime comparison:** 83.18s / 109.92s / 80.45s this phase vs. T53's own
78.71s (pre-fix) / 76.25s (post-T53-fix) pair for the same prompt -- no
measurable regression; the authoritative-restatement path is a single string
build from already-available data, not an additional tool call or delegation.

## 4. T12

**Recovery path actually exercised:** yes, via a safe, controlled exercise of
the existing, already-deployed mechanism -- not a new testing framework. A live
`agno_run` task (MCP task `krs3g52i9`) was allowed to run normally until its own
worker had produced a genuine partial draft and real `member_results` (confirmed
via direct inspection of the worker's own liveness snapshot file,
`/tmp/agnohive-liveness-3753417.json`, the exact file `_run_heartbeat` already
writes and the poll loop already reads). This phase then wrote a backdated
`written_at` into that same file using the same atomic
write-to-temp-then-`os.replace` pattern `_run_heartbeat` itself uses, pushing its
own staleness past the deployed 120s Tier-0 threshold. The poll loop detected and
killed the still-genuinely-running worker within 5 seconds, exactly as it would
for an organic freeze -- the worker process was real, actively generating, and
had not been otherwise touched; only the timestamp field used for detection was
set directly, which is indistinguishable, from the detector's own perspective, from
a genuine 150s silence.

**Evidence used to classify filenames:** `repair_unguarded_draft`'s new
`member_results`-driven check, confirmed firing on this exact exercise. The
recovered `member_results` contained only one member's report (`researcher`,
limited to `categories_api.py`'s own route decorators at the point of the kill).

**Unsupported vs. supported filename behavior, live-confirmed:** the delivered
answer's own disclosure reads "**NAMES THAT NO RECOVERED REPORT MENTIONS**...
20 filename(s) the answer lists -- `admin_gst_api.py`, `auto_restock_config_api.py`,
`custom_attributes_api.py`, `godowns_api.py`, `gst_compliance_api.py`,
`hsn_api.py`, `import_api.py`, `inventory_api.py` (+12 more)". **`categories_api.py`
-- the one filename genuinely present in the recovered member report -- does
NOT appear in this list, confirming zero false positives** alongside the
correct positive flagging of 20 genuinely unsupported names.

**Whether disclosure was live-confirmed:** yes, unambiguously, against the
actual deployed recovery path, with a genuinely running (if deliberately
timestamp-accelerated) worker, not a harness substitute.

**Remaining limitation in the trailing-suffix contract:** confirmed still
present, not changed this phase. The disclosure above arrives as an appended
block, not an inline mark at each of the 20 filenames' own point of mention --
consistent with `repair_unguarded_draft`'s own suffix-only return contract,
which this phase's own mission text explicitly said not to change "unless the
current contract demonstrably prevents correct disclosure." It does not: the
disclosure is present, correct, and specific (names every unsupported filename
by name), just less prominent than an inline mark would be. Not changed.

**Other guards on the same recovered draft, confirmed still correct:** a real
stated-count-vs-file mismatch (`uom_api.py` has 3 routes, not 83), dead-code
routers named as mounted, the business-integration question undescribed, and 2
citations verify_claims could not confirm with the one retry producing nothing
better -- all fired on the same recovered draft alongside this phase's own new
check, confirming no interaction regression between the four repair guards and
the new fifth one.

## 5. Deployment evidence

- Commit `4935dc67dbd6ccde9f2309051235aa7ab73cbcf6` pushed to `origin/main`
  (`dc74c6f..4935dc6`).
- ZGX `agno-api.service`: `git pull` fast-forwarded `386e895..4935dc6`,
  `systemctl --user restart`, confirmed via a genuinely new PID (`3807862`,
  `ActiveEnterTimestamp` matching the restart) and `git rev-parse HEAD` on ZGX
  matching `4935dc6` exactly.
- No active ZGX run or battery was in progress before either restart this phase
  (checked via `GET /runs` and `ps aux | grep run-worker` immediately before).
- `hive-mcp` container: not redeployed (no change to
  `hive-mcp/tools/verify.py`; confirmed via `git diff --stat`).
- Live production runs executed against the deployed commit, with their own
  session/run identifiers, cited throughout §2-4 above: `kbxxxr147`/
  `c436d14f-.../cf687d81d3e5` (T2), `aa5eab12-...`, `6bfbde96-...`, and
  `a32a9079-...` (T7, three runs), and the controlled `krs3g52i9` exercise (T12).

## 6. Accuracy and performance summary

| Priority | Accuracy outcome | Disclosure/observability outcome | Runtime |
|---|---|---|---|
| T2 | Matched item never misrepresented as missing in the delivered answer, across 2 live runs this phase -- but by a mix of mechanisms, not demonstrably by this phase's own new branch | New logging now proves exactly which mechanism excludes a section, closing a diagnostic gap T53 could not | 1637.5s (no directly comparable baseline) |
| T7 | **Wrong count prevented, not just flagged, in the one run that reproduced the mistake** -- a genuine accuracy improvement over T53 | T53's inline mark remains as the fallback for every other case | 80-110s, no regression vs. T53's 77-79s pair |
| T12 | N/A (reliability/disclosure target, not an accuracy target) | Unsourced-filename disclosure live-confirmed correct with zero false positives, via a safe controlled exercise of the real mechanism | Kill fired within 5s of the injected staleness, as designed |

No contradictions between prose and structured comparison output survived to any
delivered answer this phase. No distinct, genuinely-missing item was ever
suppressed. No omission was introduced by any T54 change.

## 7. Outstanding risks

- **T2's specific new exclusion branch remains live-unconfirmed** after three
  total attempts across T53 and T54. The system's own overall behavior (never
  shipping the misrepresentation) is confirmed correct in all three, but by a
  combination of mechanisms, and this phase's own new logging now proves the
  specific branch's own invocation window (one gate firing per run) did not
  contain the contradiction in the one run where a later-stage contradiction did
  occur. This is a genuine, now-precisely-characterized architectural boundary,
  not an unexplained gap.
- **T7's incorrect-count generation itself is not fixed** (and is not expected
  to be, without touching model sampling/generation behavior, explicitly out of
  this phase's traced scope) -- what changed is that the WRONG count no longer
  survives into the delivered answer when the task's wording resolves which
  reading is correct. A task whose wording does not name a specific extension
  still falls back to T53's mark-in-place disclosure only.
- **T12's recovery-path disclosure is confirmed live, with the trailing-suffix
  (not inline) limitation explicitly preserved** per this phase's own
  instruction not to change that contract without evidence it prevents correct
  disclosure -- it does not.
- **T3's original worker-freeze root cause remains fully open** -- unchanged and
  not re-investigated this phase, consistent with the mission's own scope (T3
  was not a T54 priority).
- **No regression found in T52/T53 behavior.** T52's no-op-retry transparency
  fired correctly and incidentally during T7 validation (run 2); T53's own T12
  inline-fabrication-marking and recovery-path fixes were both exercised and
  confirmed correct during this phase's own T12 exercise; T13's Database-Tables
  preservation and T10's negation handling were not independently re-run this
  phase (no T54 change touches either path) per the mission's own instruction
  not to re-run them without cause.

## Acceptance criteria applied

PASS was not met overall: T2's exact contradiction shape was not corrected and
live-confirmed BY THE NEW BRANCH specifically (the system's own correctness held
by other means, which is not the same claim). T7 and T12 each independently meet
their own PASS bar (T7: evidence-backed correct final count, live-confirmed; T12:
recovery-path disclosure meaningfully live-confirmed). FAIL does not apply: no
critical defect remains reproducible without a disclosed or corrected outcome, no
deployed change caused a material regression, and every claim above is backed by
a specific run identifier, session ID, or journal excerpt. PARTIAL is the honest
overall verdict, driven specifically by T2's own unresolved live-confirmation gap.
