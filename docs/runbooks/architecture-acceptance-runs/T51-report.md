# T51 — verify_claims precision fixes (A1–A4) + T13 forward-answer loss

Phase T51. Does not overwrite or modify `T48-report.md` or `T48-rerun-report.md`.
Commit `c4ac69d4acc3d3aa765118548aa329ad4832dfdd` (local `main` and `origin/main`),
deployed to ZGX `agno-api.service` and the local `hive-mcp` container (port 9003).

## 1. Executive verdict

| Workstream | Verdict | Basis |
|---|---|---|
| A — verify_claims A1–A4 | **PASS** | All four false-positive modes independently reproduced before any fix, each fix validated against its own original failure shape after, live-exercised against the deployed `hive-mcp` container (not just a health check), zero regression across the full 340-test hive-mcp suite (exact failing-test-name diff, not a count), and genuine-error preservation directly confirmed (fabricated path, fabricated dotted symbol, unscoped absence claim all still correctly flagged). |
| B — T13 forward-answer loss | **PARTIAL** | The specific, proven root cause (`team._forwarded_members`' single-slot-per-member overwrite) is fixed and confirmed live in production (direct log evidence the mechanism now behaves differently and correctly). The full user-visible T13a/T13b symptom (a "Database Tables" section present in the final delivered answer) is **not yet achieved for this specific task shape**, because a separate, pre-existing mechanism (`DETERMINISTIC_COMPARISON_GATE`) unconditionally discards the Coordinator's entire prose answer — including this fix's correctly-forwarded content — in favor of a bare `compare_enumerations` table, for any two-sided comparison task. This gate fired identically in both the original defect-reproducing run and today's post-fix validation run; it is not introduced by this phase and has not been traced with the same reproduce-before-fix rigor, so it is reported as a remaining risk, not fixed under this mission's narrow, evidence-gated scope (user's explicit instruction, this phase: report PARTIAL and stop rather than extend scope to trace the gate). |

Neither verdict is based on "code compiles" or "service is healthy" alone — both are
based on direct behavioral evidence: live reproduction against the real deployed
function (A), and direct ZGX production journal evidence of each described failure
and post-fix behavior change (B).

## 2. Baseline

- Local and ZGX both started this phase at commit `a5b63eb` (`fix(execution_context):
  accept the agent_name parameter T50's deployed caller requires`), the tip of an
  existing, independently-committed T42–T50 track not covered by this phase.
- Branch: `main`, both locally and on ZGX, tracking `origin/main` cleanly.
- `hive-mcp` container (local, port 9003) was running image digest
  `sha256:4d24c9261ad24d54505a5906cda99b90d28d243feb6ea4b9c3975fe44c5f84ac`
  (built `2026-10-20T12:16:19Z`), confirmed byte-identical (CRLF-normalized diff) to
  the then-current `hive-mcp/tools/verify.py` on disk before any edit in this phase.
- `hive-mcp-agno` (port 9004, a separate project's container) confirmed untouched
  throughout, both before and after this phase's `hive-mcp` recreate
  (`8cde4d3e8848`, "Up 3 days (healthy)" both before and after).
- Pre-existing dirty/untracked state confirmed present at baseline and preserved
  unmodified throughout: `tests/test_evidence_fidelity.py` and the (already-committed
  by T50) `swarm/execution_context.py` T24 work; `training/train.py`,
  `zgx-ai-setup/litellm-config.yaml`, and the untracked `training/*.py`,
  `tokenizer.json`, `tests/test_t18_evidence_synthesis_boundary.py`,
  `tests/test_t19_source_context_disclosure.py` files (confirmed via `git diff
  --stat` before and after this phase's own commit: identical set, this phase's
  commit touched only `hive-mcp/tools/verify.py` and `swarm/team.py`).
- No active ZGX task/battery was running at any point this phase was about to take a
  disruptive action (checked via `ps aux | grep run-worker` immediately before the
  ZGX service restart and immediately before the `hive-mcp` container recreate).

## 3. A1–A4 findings

All four were independently reproduced against the real, deployed `hive-mcp`
`verify.py` before any code change, using isolated scratchpad harnesses (never a
tracked test file) that import and call the real module functions directly.

**A1 — bare backtick-wrapped common word misread as a symbol claim.** A model's
inline aside such as "...is declared on line 129. `` `The` `` exact line is: ..." has
`` `The` `` match `_IDENT_RE` (3 letters, no dot) and get checked as a symbol claim,
reported `NOT FOUND`, alongside an otherwise fully correct citation. Reproduced live
exactly this shape. Root cause: `_NOISE`, the existing deliberately-narrow set of
common-English words excluded from identifier checking (`get`/`post`/`data`/`value`,
etc.), had no articles/demonstratives/pronouns. Fix: extended `_NOISE` with `the,
this, that, these, those, it, its`.

**A2 — fenced-code-block filename split into a false dotted-symbol claim.** The
existing T30 fix (`_looks_like_bare_filename`, 2026-10-19) excludes a bare filename
(`items_api.py`) from being read as an `owner.attribute` symbol claim, but was wired
only into the inline-backtick extraction loop, never into `_dotted_idents` (the
fenced-code-block/staged-file extraction path). Reproduced live: a raw
`list_directory` dump inside a fenced block (24 real filenames, not individually
backtick-wrapped) had every filename split into `owner="items_api"`,
`attr="py"` and grepped as a dotted claim — `py` trivially exists elsewhere in the
repo, so every filename in a 100%-accurate listing reported `SPLIT-FOUND`. Fix:
applied the same `_looks_like_bare_filename` exclusion inside `_dotted_idents`.

**A3 — cross-service absence-claim false contradiction.** A correctly-identified
negated claim ("There is no rate-limiting `` `middleware` `` anywhere in the
authentication service") was reported `CONTRADICTED` by an unrelated match in a
*different* service's own unrelated middleware (`CORSMiddleware` imports,
`uvicorn.middleware.proxy_headers`), because the existing `ABSENCE CLAIMS` check
(added 2026-08-23) greps the bare term against the *whole* repository regardless of
what the claim's own text names as its scope. The mission's own brief described this
as "negation-blind keyword checking" — live reproduction disproved that framing
directly: negation *was* correctly detected (the claim correctly reaches the
`ABSENCE CLAIMS` section, not the positive-claim path); the actual bug is scope-
blindness in the contradiction search, not negation handling. Fix: `_service_scope_glob`
detects a real, filesystem-verified `API/<name>-service/` directory named in the
claim's own surrounding text (±150 chars) and narrows the `ripgrep` search to it;
a claim naming no service, or naming one that does not exist, is still searched
unscoped exactly as before, so a genuinely scopeless fabrication is caught exactly
as it always was. (One self-correction during this phase: the first version of the
scope-name regex was too greedy and captured leading filler words — "in the
authentication" instead of "authentication" — which silently built a non-existent
directory name and never fired; caught by live-testing the fix itself before
declaring it complete, and corrected by anchoring to exactly one preceding word,
matching how every one of EkamApp's own service directories is actually named.)

**A4 — decorator-citation quote mismatch on a faithful partial restatement.** Six
citations in a single answer, each independently confirmed byte-exact against the
real source by direct file read, were all reported `MISMATCH`, because the model's
inline restatement (`` `@router.post("/upload")` ``) faithfully omitted trailing
kwargs the real declared line carries (`, response_model=..., status_code=201`), so
the quote is a correct, syntactically-tidy restatement but not a literal substring
of the real line, and carries no `...` to trigger the existing ellipsis-matching
path. The mission's brief framed this as a citation-*pairing* problem (decorator in
backticks plus a separate trailing file:line reference); live internal-function
testing disproved that framing directly — `_find_nearby_quote`/`_find_preceding_quote`/
`_citation_bounds` all paired every citation with the correct quote correctly; the
actual bug is one level deeper, in `_quote_matches`' own string comparison. Fix:
added a verb+path comparison fallback (`_DECORATOR_VERB_PATH_RE`) that recognizes two
decorator calls as equivalent when their HTTP verb and quoted path literal match,
independent of trailing kwargs — the same two fields `_DECORATOR_ROUTE_RE` already
extracts and verifies elsewhere for the `ROUTES` section, so a genuinely wrong verb
or invented path still falls through to a real `MISMATCH` unchanged.

## 4. T13a/T13b forensic timeline

Reconstructed from the real ZGX production journal (`journalctl --user -u
agno-api.service`), not from assumption or the battery report's own prose alone.

**Original defect-reproducing run** (`run_id` `88ca75d6679d`, 2026-10-21
13:58:32–14:06:36 EDT, 509.1s, the T13b "files named" shape, superseding the earlier
T48/T48-rerun observations of the identical defect):

| Time (EDT) | Event |
|---|---|
| 13:58:32 | `delegate_structured_task` seq 1 → researcher, target `vouchers_api.py` (endpoints) |
| 13:59:56 | member result captured: 2,457 chars |
| 14:00:00 | `forward_member_answer('researcher')` → **forwarding 941 chars of 2,457 raw** |
| 14:00:19 | `delegate_structured_task` seq 2 → researcher, target `models.py` (**tables**) |
| 14:00:56 | member result captured: 1,584 chars |
| 14:00:59 | `forward_member_answer('researcher')` → **forwarding 287 chars of 1,584 raw** — the tables delegation's own result, correctly retrieved |
| 14:01:26 | `delegate_structured_task` seq 3 → researcher, target `inventoryApi.ts` (hooks) |
| 14:04:16 | member result captured: 3,224 chars |
| 14:04:20 | `forward_member_answer('researcher')` → forwarding 2,500 chars of 3,224 raw |
| 14:06:35 | run ends; final delivered answer has no "Database Tables" section |

The Coordinator called `forward_member_answer` once per delegation, each time
*before* dispatching the next one to the same member — the retrieval step itself
worked correctly every time, confirmed by the journal's own "forwarding ... of 1,584
raw" line matching the tables delegation's own `member_output_chars` exactly.

**Post-fix validation run** (`run_id` `abc0530438c2`, session
`ecd153b1-8d3b-4a4c-9a3b-8b26d0909dee`, 2026-10-21 21:11–21:20 EDT, 564.9s, the same
T13b prompt, run against the deployed fix): identical three-delegation shape
(endpoints seq 1, tables seq 2, hooks seq 3). The repetition-completion mechanism's
own progressively-captured draft (21:17:05–21:18:56) shows the Coordinator's own
prose *did* recover a "### Database Tables" section this run, quoting the real
`VoucherSeries`/`Voucher`/`VoucherVersion` classes from `models.py` by name and line
number. At 21:19:02, the log reads:

```
[team] forwarded answer(s) not carried by the final answer -- appending verbatim: ['researcher#3']
```

The `#3` suffix is direct proof the fix's accumulation mechanism fired: only the
*third* (hooks) forward needed appending, because the first two (endpoints, tables)
were already carried in the Coordinator's own recovered prose — a key shape
(`researcher#3`) that cannot exist in the pre-fix code at all. The *original* run's
equivalent log line read `appending verbatim: ['researcher']` (unsuffixed, single
slot) — a clean, direct before/after comparison of the exact mechanism changed.

Immediately after (21:19:02), `DETERMINISTIC_COMPARISON_GATE_FIRED` logged
`generative_reconciliation_bypassed=True`, and the final answer actually returned by
this run's `agno_run` call contains *only* the bare `compare_enumerations` table —
no prose, no "Database Tables" section, no forwarded hooks block. Checking the
*original* run's own journal for the same marker: `DETERMINISTIC_COMPARISON_GATE_FIRED`
also fired there (`model_made_completeness_claim=True`), confirming this gate is a
pre-existing mechanism, active in both the original defect and the post-fix
validation run alike, independent of this phase's own change.

## 5. T13 root cause

`team._forwarded_members` (bound by reference to the `forwarded_members` dict built
in `_build_team`, read by `_with_forwarded_evidence` at final-answer-assembly time)
was written at `forward_member_answer`'s own line `forwarded[key] = result.answer` —
a plain, unconditional overwrite of a single slot keyed *only* by member id, with no
per-delegation or per-target disambiguation. `_finalise_member_chunks`'s own
docstring states this single-latest-value behavior is correct and intentional *for
the separate `team._member_results` dict it fills* ("a member delegated to twice
must not have its second answer concatenated onto its first... the result is the
latest complete answer that member gave") — and T40/T41's own fixes to *that*
dict's dispatch-to-finalize bridge are correct and unaffected by anything in this
phase. The bug is specifically in the *downstream* `_forwarded_members`/
`_with_forwarded_evidence` pair: a dict meant to let final-answer assembly "catch"
whatever the Coordinator's own prose didn't carry, which could only ever remember
the *newest* of several same-member forwards, discarding every earlier one — not
because it was never captured (`forward_member_answer` retrieved and forwarded each
one correctly, confirmed by the raw-char-count match in the journal), but because
this specific map had nowhere to keep more than one.

The mission brief's own stated hypothesis ("`forward_member_answer` results aren't
accumulated across multiple forwarding events") is confirmed correct in substance,
though the precise mechanism is one level more specific: the loss is not inside
`forward_member_answer`'s retrieval logic, and not inside `team._member_results`
(T40/T41's own territory), but specifically in `team._forwarded_members`'s
single-slot write.

## 6. Implementation details

**`hive-mcp/tools/verify.py`** (A1–A4, hive-mcp container + ZGX, same file):
- `_NOISE` extended with `the, this, that, these, those, it, its` (A1).
- `_dotted_idents` gains a `_looks_like_bare_filename(tok)` guard, mirroring the
  existing inline-backtick-loop guard (A2).
- New `_SERVICE_SCOPE_WINDOW` constant, `_SERVICE_SCOPE_RE`, and `_service_scope_glob`
  helper; the `ABSENCE CLAIMS` loop's `_rg` call now prefers a caller-supplied
  `glob_filter` (unchanged precedence), then a detected service scope, then falls
  back to the original unscoped search (A3).
- New `_DECORATOR_VERB_PATH_RE`; `_quote_matches` gains a verb+path equivalence
  fallback before the existing ellipsis-split path (A4).

**`swarm/team.py`** (T13, ZGX only — not hive-mcp-relevant):
- `forward_member_answer`'s write site: the first forward for a member keeps the
  exact pre-fix shape (`forwarded[key] = result.answer`, a bare string) so every
  existing single-forward caller and test asserting that exact dict shape (e.g.
  `t._forwarded_members == {"researcher": LIST_ANSWER}`, `tests/
  test_forward_member_answer_handoff.py`) is byte-for-byte unaffected. A
  *subsequent*, genuinely *different* forward for the same member gets an additional
  `"{member}#2"`/`"#3"`/... key instead of overwriting. An identical re-forward of
  unchanged content (no new delegation in between) is deliberately not given a new
  slot.
- `_with_forwarded_evidence`: iterates `forwarded.items()` exactly as before (no
  structural change needed — it already handles an arbitrary number of distinct
  dict keys); the only change is a cosmetic suffix-strip (`k.split("#", 1)[0]`) so
  the "### From {member}" attribution label reads as the real member name, not a
  literal "researcher#2".

Neither change touches `_member_results` (T40/T41's own territory), the
`_pending_log_entry_by_member`/`_pending_entry_by_expected_run_id` bridges, any
delegation-budget, repetition-handling, or worker-lifecycle code, or
`DETERMINISTIC_COMPARISON_GATE`.

## 7. Validation matrix

| Case | Method | Result |
|---|---|---|
| A1 original failure shape | Scratchpad harness, direct call vs. deployed `verify.py` | Fixed: clean verdict, `The` no longer appears as a checked symbol |
| A2 original failure shape | Scratchpad harness | Fixed: clean verdict, no `SPLIT-FOUND` noise on a correct listing |
| A3 original failure shape (qualified + declarative) | Scratchpad harness, 3 iterations (first attempt caught the greedy-regex self-bug before declaring done) | Fixed: `ABSENT OK`, scoped search confirmed via debug script showing `API/authentication-service/**` |
| A4 original failure shape (all 6 citations) | Scratchpad harness | Fixed: all 6 report "content verified within 5 lines" |
| Preservation: fabricated multi-segment path | Scratchpad harness | Still correctly `NOT FOUND` |
| Preservation: genuine fabricated dotted symbol | Scratchpad harness | Still correctly `NOT FOUND` |
| Preservation: unscoped absence claim (no service named) | Scratchpad harness | Still correctly `ABSENT OK`, unscoped |
| hive-mcp full test suite (340 tests) | `pytest`, exact failing-test-name diff, fix present vs. stashed out | **Identical** 10 pre-existing failures both ways |
| T13-shaped 3-delegation sequence | Scratchpad harness calling the real `_make_forward_member_answer`/`_with_forwarded_evidence` | All 3 distinct forwards survive; tables content reaches the appended text |
| T13 single-forward preservation | Scratchpad harness | `_forwarded_members` byte-identical pre-fix shape (`{"researcher": str}`) |
| T13 duplicate-forward (no inflation) | Scratchpad harness | Re-forwarding identical content creates no second slot |
| T13 multi-member (no collision) | Scratchpad harness | Two different members each keep their own unsuffixed key |
| agno-hive root test suite (2,703 tests) | `pytest`, exact failing-test-name diff, fix present vs. stashed out | **Identical** 55 pre-existing failures both ways |
| A1–A4 live against deployed `hive-mcp` container | Re-run of every harness above against the recreated container (post-deploy) | All pass identically to the pre-deploy scratchpad runs |
| T13 live against deployed `agno-api.service` | One narrowly-scoped live T13b run (not a battery rerun) | Fix's own mechanism confirmed firing correctly (`appending verbatim: ['researcher#3']`); full user-visible symptom not resolved for this task shape — see §1/§11 |

## 8. Genuine-error preservation

No T7 (count-mismatch) or T12 (fabricated-dependency) mechanism lives in
`hive-mcp/tools/verify.py` or in the `forward_member_answer`/`_with_forwarded_evidence`
pair touched this phase — `COUNT DISAGREES`-style count checking and the
evidence-ledger-based fabrication tracking the original T12 finding depends on are
both implemented elsewhere in `swarm/team.py`, untouched by this phase's diff. This
phase instead directly re-verified the two specific detectors nearest to A1–A4's own
changes: a path that genuinely does not exist anywhere (`preserve_fabricated_path_in_fence`)
still correctly reports `NOT FOUND`, and a genuinely fabricated dotted attribute
(`Voucher.nonexistent_field_xyz`) still correctly reports `NOT FOUND` — both after
all four fixes, confirming none of the four narrowed exclusions became a blanket
exemption.

## 9. Deployment evidence

- Commit `c4ac69d` pushed to `origin/main` (`a5b63eb..c4ac69d`).
- `hive-mcp` container (local, port 9003): new image built by CI from `c4ac69d`
  (`ghcr.io/abehera1992/hive-mcp:latest`, created `2026-10-22T01:07:53Z`, confirmed
  newer than the pre-phase image via `docker inspect`), pulled and recreated via
  `docker compose -f docker-compose.hive.yml up -d hive-mcp` (run from the EkamApp
  directory, where this compose file actually lives — an initial attempt from the
  agno-hive directory failed to find the file and is noted in §10), container
  healthy, deployed source confirmed byte-identical to the commit via CRLF-normalized
  diff against the live container's own `/app/tools/verify.py`.
  `hive-mcp-agno` (port 9004) confirmed untouched (`8cde4d3e8848`, unchanged status).
- ZGX `agno-api.service`: `git pull` to `c4ac69d` (fast-forward from `a5b63eb`),
  `systemctl --user restart`, confirmed via a genuinely new PID (`3532955`, started
  at the restart timestamp, clean startup log) and `git rev-parse HEAD` on ZGX
  matching `c4ac69d` exactly — not inferred from a health check alone.
- Both deployments directly exercised post-deploy (§7's last two rows), not merely
  confirmed present.

## 10. Git safety

- Branch/HEAD/worktree inspected before any change (`git status`, `git log`,
  `git diff --stat`) at the start of this phase.
- No destructive command used at any point: no `reset --hard`, no `clean`, no
  force-push, no amend, no broad `add -A`/`add .`. Every stage was an explicit
  `git add <exact path>` for the two reviewed files only.
- A transient recovery used during the hive-mcp deploy: `docker rm -f hive-mcp` was
  run from the wrong directory (agno-hive, where `docker-compose.hive.yml` does not
  exist) immediately before the matching `up -d`, which failed with "file not
  found" — the container was briefly absent (not the repository, not any file; a
  running container only) until `docker compose -f docker-compose.hive.yml up -d
  hive-mcp` was re-run from the correct directory (EkamApp) seconds later,
  recreating it successfully. No data loss: `hive-mcp` is a stateless service
  container rebuilt from a committed image.
- Full staged diff for the commit was read in full before committing (§6's content
  is a direct description of the actual reviewed diff, not a summary from memory).
- Every pre-existing dirty/untracked file (T24's own work, the training-track files,
  the two untracked T18/T19 test files) was confirmed present, unmodified, and
  unstaged both before and after this phase's commit.

## 11. Remaining risks

1. **`DETERMINISTIC_COMPARISON_GATE`'s wholesale prose-discard** (§1, §4) is a
   separate, pre-existing, now-identified mechanism that overrides the Coordinator's
   entire final answer — including this phase's correctly-forwarded content — for
   any two-sided comparison task, confirmed firing in both the original T13a/T13b
   defect and today's post-fix validation run. Not traced with the same
   reproduce-before-fix rigor applied to A1–A4/T13 in this phase; explicitly left
   unfixed per the user's own instruction this phase (report PARTIAL and stop,
   rather than extend scope). This is the actual reason the full, user-visible
   T13a/T13b "Database Tables" symptom is not resolved end-to-end for this specific
   task shape even though the originally-hypothesized accumulator defect is fixed
   and live-confirmed.
2. **`test_unavailable_tool_handling.py::test_tier4_reports_the_unavailable_tool_instead_of_a_spent_budget`**
   remains a known, pre-existing test/implementation mismatch (flagged by T50's own
   commit message, unrelated to and unchanged by this phase) — still present in both
   the 55-failure hive-root baseline and this phase's own post-fix run, identically.
3. This phase's T13 fix is validated for the specific 3-sequential-same-member shape
   observed in production; a theoretical case with a very large number of distinct
   forwards to one member (unbounded `#N` growth) was not stress-tested, though
   nothing in the implementation imposes a different limit than the existing
   `_MAX_CLAIMS`-style bounds elsewhere in this codebase.

## 12. Artifact

- Code: commit `c4ac69d4acc3d3aa765118548aa329ad4832dfdd`, `hive-mcp/tools/verify.py`
  and `swarm/team.py`, pushed to `origin/main`.
- This report: `docs/runbooks/architecture-acceptance-runs/T51-report.md` (new file;
  `T48-report.md` and `T48-rerun-report.md` untouched).
- Telemetry: `docs/runbooks/architecture-acceptance-runs/runs.jsonl` (appended this
  phase; all prior phases' own lines untouched).
- Scratchpad validation harnesses used this phase (not tracked, not test files):
  reproduction and preservation scripts for A1–A4, and
  `t13_fix_validation.py` for the T13 fix, all under this session's own scratchpad
  directory — reusable for a future session but not part of the repository.
