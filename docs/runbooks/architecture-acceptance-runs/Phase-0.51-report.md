# Phase 0.51 — Chain-Coverage Disclosure Verification and Evidence-Accurate Delegation Hints

Implementation phase. Fixes the real verification false positive the Phase
0.50 live T11 run exposed: `_chain_coverage_unresolved_note` named the
internal AgnoHive classifier `_is_chain_question` in backticks, and
`verify_claims` correctly — by its own unweakened rules — flagged it `NOT
FOUND` against the EkamApp repository, triggering an unnecessary repair/
re-delegation cascade that discarded an otherwise-honest disclosure.

## 1. Starting and ending commits

| | Value |
|---|---|
| Local HEAD (start) | `9fa7cc8` |
| Origin HEAD (start) | `9fa7cc8` (matched local) |
| ZGX HEAD (start) | `9fa7cc8` (matched local/origin) |
| Local/origin dirty+untracked count (start) | 79 (78 pre-existing backlog + this phase's own `swarm/team.py` edit, before commit) |
| ZGX dirty+untracked count (start) | 35 |
| Implementation commit | `6e16c72` ("fix(swarm): verifier-safe chain-coverage disclosure + evidence-accurate move-on hint (T11)") |
| Pushed | `origin/main`, `9fa7cc8..6e16c72` |
| ZGX HEAD (end) | `6e16c72` (fast-forwarded, `--ff-only`) |
| Local/origin dirty+untracked count (end) | 79 (78 pre-existing + this report, before its own commit) |
| ZGX dirty+untracked count (end) | 35, unchanged |

## 2. Dirty/untracked preservation evidence

Local: 78 pre-existing entries before this phase's own edit, confirmed
identical (31 deleted `T*-report.md` files, 4 modified files, 43 untracked
files/dirs) to the baseline Phase 0.50 recorded. One accidental, empty,
zero-byte debris file (`_CHAIN_COVERAGE_RENDER_CAP`, created by this
session's own earlier shell-escaping mistake in a one-off debugging
command, never part of any commit) appeared mid-phase and was removed —
this was this session's own freshly-created artifact, not pre-existing
work, and removing it is explicitly safe per this project's own cleanup
convention. ZGX: 35 before `git fetch && git merge --ff-only`, 35 after —
the merge touched only `swarm/team.py` (48 insertions, 11 deletions).

## 3. Exact files and functions changed

`swarm/team.py` only, 48 insertions, 11 deletions:

1. **`_chain_coverage_unresolved_note`** (Objective A, reworded in place):
   removed the backtick-quoted `` `_is_chain_question` `` aside; the
   disclosure now says "this question spans multiple components" with no
   internal identifier at all. Added a docstring paragraph tracing the exact
   false-positive mechanism for future readers.
2. **`_move_on_hint`** (Objective D, one line changed + wording tweak): its
   "unseen"/no-usable-result computation now calls
   `_unresolved_chain_components(log, team)` (Phase 0.49/0.50, unmodified)
   instead of the looser `_uncovered_components(log, team)` — a strict
   superset of the same function, so this can only ADD a component that was
   delegated for but produced no usable result, never remove one that
   genuinely has evidence. The reminder's own wording changed from
   "COMPONENTS NOTHING IN THIS RUN HAS OPENED YET" / "you have not looked" to
   "COMPONENTS WITH NO USABLE INVESTIGATION RESULT YET" / "no usable result
   exists yet", for accuracy against the now-broader set it can contain.
   The "covered so far" list (`_covered_targets`, unchanged) and all four
   existing duplicate-delegation call sites are untouched.

No change to `_covered_targets`, `_uncovered_components`,
`_unresolved_chain_components`, `_chain_coverage_unresolved_note`'s list-
rendering logic, `_attempt_chain_coverage_correction`'s own control flow,
`_salvage_truncated_prefix`, `_strip_incomplete_answer_note`,
`_CHAIN_COVERAGE_SALVAGE_MIN_RETENTION`, `_complete_repetition_truncated_
answer`, `_current_truncation_reason`, `_truncation_note_for_reason`,
`_enforce_verification_invariant`, `_repair_verification_failure`,
`verify_claims`/`hive-mcp/tools/verify.py` (not touched at all this phase —
see §5 for why), or any repetition threshold/watchdog/timeout.

## 4. Phase 0.50 false-positive root-cause trace

Traced via direct source inspection (not inferred from the journal alone):

1. `_chain_coverage_unresolved_note` built its disclosure string containing
   `` `_is_chain_question` ``, purely as an explanatory aside naming the
   internal classifier this gate is built on.
2. `verify_claims` (`hive-mcp/tools/verify.py`) extracts every backtick-
   quoted bare identifier via `_IDENT_RE` (`^[A-Za-z_][A-Za-z0-9_]{2,}$`) —
   `_is_chain_question` (18 chars, leading underscore) matches cleanly.
3. It is checked against `_NOISE` (common English words — not a match) and
   `_MCP_TOOL_NAMES` (hive-mcp's own registered TOOL names — also not a
   match: `_is_chain_question` is neither a hive-mcp tool nor an EkamApp
   symbol; it is an internal `swarm/team.py` function, a third, unrelated
   category these two exclusion sets were never meant to cover).
4. Not negated (no "does not exist" phrasing nearby), so it reaches the
   real grep check against the EkamApp repository, finds nothing (correctly
   — it genuinely does not exist there), and is reported `NOT FOUND`.
5. `_enforce_verification_invariant` (called with `op_prefix=
   "enforce_verification_invariant_final"` at its final call site) extracts
   this via `_not_found_claims` (any line starting with `"NOT FOUND"`,
   regardless of report section), builds a `_RepairFinding`, and calls
   `_repair_verification_failure` — the repair/re-delegation cascade.
6. That cascade itself stalled (confirmed in Phase 0.50's own live journal:
   a 180s `lightrag_query` timeout) and gave up, preserving the ORIGINAL,
   still-flagged content with an "unverified" disclosure instead of the
   corrected one — the net effect being the whole, otherwise-honest
   disclosure buried under `VERIFICATION FAILED` / `(no response)` banners.

## 5. The exact disclosure/verifier fix and why it does not weaken verification

The fix is purely textual: `_chain_coverage_unresolved_note` no longer
names any internal identifier at all. This is the narrowest reliable fix
for three concrete reasons:

- It requires **zero changes to `hive-mcp/tools/verify.py`** — a
  separately-versioned, separately-deployed artifact (built into a Docker
  image via CI, pulled and recreated independently of `agno-api.service`).
  A verify.py change would have needed its own build/push/pull/redeploy
  cycle, a larger-blast-radius change for a problem this disclosure's own
  wording fully owns.
- Adding `_is_chain_question` to `_MCP_TOOL_NAMES` (the obvious-looking
  alternative) would have been the wrong exclusion for the wrong reason:
  that set exists specifically for hive-mcp's own registered TOOL names
  (names a model might call and then mention in prose) — `_is_chain_
  question` is neither a hive-mcp tool nor ever called by the model; it is
  an AgnoHive orchestration-internal name this disclosure happened to
  mention. Using that set for it would have muddied its own documented,
  narrow purpose for every future reader.
- Verification itself is completely unweakened: no existing check, exclusion
  list, or rejection path in `verify_claims` was touched. A genuinely
  fabricated symbol is checked by the exact same unmodified machinery that
  caught `_is_chain_question` correctly in the first place (see §7, Scenario
  C/G) — the fix only ensures this ONE disclosure never again hands that
  machinery something to legitimately (if unhelpfully) flag.

## 6. Objective D: implemented

Implemented, not deferred — analysis of all four existing `_move_on_hint`
call sites (lines ~22197/22209/22322/22335, read in full before any edit)
confirmed they are all purely advisory text appended to an *already-decided*
refusal/notice message (`STOP`/`ALREADY DONE`); the refusal decision itself
comes from separate, unmodified target/objective-matching logic earlier in
each branch. `_move_on_hint` itself does not influence whether a delegation
is refused — only what text accompanies that refusal. Substituting
`_unresolved_chain_components` for `_uncovered_components` in its own
"unseen" computation therefore changes only the accuracy of the advisory
reminder text, never any refusal/duplicate-delegation decision — confirmed
directly by Scenario K (§7).

## 7. Scratch-harness scenarios, exact results, and untested branches

Two scratch files, both calling real, unmodified production code. **The
only mock in either file is team.py's own async `_verify_claims` MCP-
transport wrapper**, routed via a subprocess (to avoid a `config`
package-name collision between agno-hive's own `config/` package and
hive-mcp's own `config.py` module landing on the same `sys.path`) to the
REAL, unmodified `hive-mcp/tools/verify.py` `verify_claims()` function
against the real EkamApp repository — not a stand-in for the verification
logic, which runs for real in every scenario below. `_stream_team_run` and
`_repair_verification_failure`'s own deep retry are the two genuinely-
external dependencies mocked for scenario G, clearly labeled at each site.
No unit tests were added, modified, or run; no tracked test file was
touched.

**`test_p51_verify_scenarios.py` — 12 checks, zero mocks of verify_claims
itself:**

| Scenario | What was exercised | Result |
|---|---|---|
| A | The OLD, pre-fix wording (manually reconstructed, NOT production code) DOES trip the false positive — confirms the bug was real. The real, FIXED `_chain_coverage_unresolved_note` no longer contains the string, and its output does not trip `NOT FOUND _is_chain_question` against the real verifier | PASS (3/3) |
| B | The fixed disclosure with real component names, checked alone — zero `NOT FOUND` symbol flags; real paths reported `EXISTS` | PASS (2/2) |
| C | A genuinely fabricated symbol (`totally_fake_symbol_xyz789`) — still correctly flagged `NOT FOUND` | PASS (1/1) |
| D | Real, independently-reconfirmed symbols (`upload_file` at `storage_api.py:33`, `register_documents` at `business_api.py:484`) — not flagged | PASS (1/1) |
| E | Real unresolved component paths remain present in the fixed disclosure | PASS (4/4) |
| (clean case) | No unresolved components → empty disclosure, nothing to verify | PASS (1/1) |

**`test_p51_team_functions.py` — 9 checks, against real `_enforce_
verification_invariant`/`_move_on_hint`, verify_claims routed to the real
function as described above:**

| Scenario | What was exercised | Result |
|---|---|---|
| F | The fixed disclosure attached to otherwise-clean content → `_enforce_verification_invariant` returns content byte-unchanged; `_repair_verification_failure` is never invoked (asserted via a stub that raises if called) | PASS (2/2) |
| G | A genuine, unrelated fabrication (`totally_fake_symbol_xyz789`) → the established repair path IS invoked (confirmed via a call-counting spy) | PASS (1/1) |
| H | A component delegated for with an empty result is NOT represented as completed — appears in "COMPONENTS WITH NO USABLE INVESTIGATION RESULT YET" | PASS (1/1) |
| I | A component with a real, non-empty result IS excluded from that reminder, while still correctly appearing in the unchanged "covered so far" list | PASS (2/2) |
| J | A component never delegated for at all remains in the reminder | PASS (1/1) |
| K | Duplicate-delegation's own "covered so far" list (the refusal-decision input) still names an attempted-but-resultless target — byte-identical, unchanged behavior | PASS (1/1) |
| N | `_is_chain_question(T12_TASK)` is `False` | PASS (1/1) |

**Total: 21/21 new checks passed.** Re-ran the directly relevant pre-
existing suites unchanged: `test_p50_salvage.py` (33/33), `test_p49_chain_
coverage.py` (23/23), `test_p45_t12_router_inventory.py` (18/18), `test_
p46_post_correction_revalidation.py` (20/20), `test_p46_wiring_e2e.py`
(3/3) — zero regressions. Per this phase's own instruction, no broader
historical suite was re-run.

**Untested branches / limitations of the scratch harness:** the exact agno
tool-hook call signature for the four `_move_on_hint` sites (L/duplicate-
delegation itself) was exercised only via direct, hand-built log/team
objects, not through the real tool-hook plumbing — same limitation Phase
0.49/0.50's own harnesses recorded, unaffected by this phase's diff
regardless (confirmed unchanged by diff, §3).

## 8. Commit and push SHAs

| | Value |
|---|---|
| Implementation commit | `6e16c72` |
| Pushed | `origin/main`, `9fa7cc8..6e16c72` |
| Report commit | recorded after this file is committed — see closing message |

## 9. ZGX deployment, service PID, health, and `/runs` evidence

| | Value |
|---|---|
| ZGX HEAD | `9fa7cc8` → `6e16c72` (`--ff-only`, clean) |
| ZGX dirty/untracked | 35, unchanged before and after |
| `agno-api.service` | restarted (code change); new PID `1444953`, active since `2026-11-06 07:32:56 EST` |
| `/health` | `{"status":"ok", ...}` |
| `/runs` | confirmed empty before each launch attempt |
| `hive-mcp-agno` (client-machine container) | confirmed untouched: `Up 2 weeks (healthy)`, predates this session entirely |
| `lightrag.service` (ZGX) | restarted mid-phase — see §14 #1; a pre-existing, unrelated service, not a code change |

## 10. Canonical T11 prompt hash and live run identifiers

Canonical prompt, byte-exact from `docs/runbooks/groundedness-battery.md`:
> When a seller uploads a document, which services are involved end to end,
> and which function in each one handles it? Name every file in the chain.

Independently recomputed SHA-256 before each launch attempt:
`7bb97b1c00b7b116223b4710042e2c0abfd4a151e0a97f49e56ee48423f25566` — matches
the mission's stated hash exactly, both times.

**Attempt 1 (not a counted run):** `POST /run` returned a `500 Internal
Server Error` (`RuntimeError: Attempted to exit a cancel scope...`) before
the Coordinator ever started. Traced directly to `lightrag.service`'s own
journal: `RuntimeError: Task group is not initialized. Make sure to use
run().` — a stuck MCP session-manager state in a separate, pre-existing
service, unrelated to this phase's `swarm/team.py` diff (confirmed: zero
application-level log lines appeared before the crash). `lightrag.service`
was restarted (an operational action, not a code change) and `/runs`
reconfirmed empty before the actual counted run below. This was not a
"repeat to get a better result" — the prior attempt never executed any
application code at all.

**Attempt 2 (the counted canonical run):**

| | Value |
|---|---|
| Task ID (MCP) | `kmy7dhady` |
| Run ID (`phase0`) | `422c40071934` |
| Session ID | `fa582b93-56c0-44b4-bb81-2d47af58034b` |
| Duration | 444.4s (phase0 log) / 446.01s (API footer) |
| `swarm_team` | `engineering` |
| `read_only` | `true` |
| Session | fresh, no `session_id` supplied |
| `/runs` | confirmed empty immediately before launch |
| Run count | run once, not repeated |
| Outcome (process-level) | `ok` |
| Outcome (content-level) | `(no response)` plus disclosure banners |

## 11. Complete journal evidence for verification and finalization

Pulled and read start-to-finish via `journalctl --user -u agno-api.service`:

1. **07:36:08** — the ORIGINAL business-service delegation's own narration
   hits a repetition stall with zero usable content generated yet; Phase
   0.44's no-evidence invariant correctly skips completion ("no usable
   current-generation evidence exists yet").
2. **07:36:15** — `verify (draft): bad=False` and `_enforce_verification_
   invariant:initial` both run on the pre-chain-coverage draft, both clean
   (nothing to verify yet — expected, this is before my disclosure exists).
3. **07:36:31** — `_attempt_chain_coverage_correction ENTER`: 14 discovered
   components, `move-on hint attached — 1 target(s) covered so far:
   'API/business-service'` followed immediately by `move-on hint: 14
   component(s) untouched so far: ... 'API/business-service', ...` —
   **this phase's Objective D fix firing live**: business-service is
   correctly shown in BOTH lists simultaneously (attempted, per the
   unchanged `_covered_targets`; AND unresolved, per the newly-wired
   `_unresolved_chain_components`, because its own delegation never
   produced a usable result).
4. **07:37:22–07:42:03** — the retry re-investigates `API/business-service`
   (re-reads the same four files, no new delegation to any other
   component), builds a genuinely substantive 2,953-char synthesis
   (`register_documents`, `get_seller_documents`, `delete_seller_document`,
   `BusinessDocument`, `DocumentReference`, correctly attributed to their
   real files), then hits a second repetition stall at **07:42:03**.
5. **07:42:03** — `chain-coverage-correction: retry hit a second repetition
   stall, but 2,376 of its 2,953 chars read as a genuinely complete prefix
   -- salvaging that prefix instead of discarding the whole retry` —
   **Phase 0.50's salvage mechanism (unchanged this phase) firing on its
   ADOPTION branch for the first time live** (Phase 0.50's own live run
   only exercised the fail-closed branch).
6. **07:42:03** — immediately after: `chain-coverage-correction: retry made
   no new delegation progress (14 still uncovered, was 14) -- keeping
   original content with an unresolved-boundary disclosure` — the salvage
   succeeded, but since the retry never delegated to any component beyond
   the one it was already given, the coverage-progress gate correctly
   discards it anyway (this gate's entire purpose is coverage improvement,
   not prose quality — exactly as designed, not a defect).
7. **07:42:03–07:42:16** — `_enforce_verification_invariant_final:initial`
   runs on the final content (the original `(no response)` draft plus this
   phase's fixed disclosure plus a separate, pre-existing "ASKED FOR A LIST"
   banner): `verify_claims ok via live-session ... EXIT ... outcome=
   skipped`. **No `repair (verify_claims)` line appears anywhere in this
   run's complete journal** — a direct, observable contrast with Phase
   0.50's incident, which showed that exact line for `_is_chain_question`.
8. **07:42:16** — run completes, `outcome: "ok"` at the process level.

## 12. Independent source-ground-truth comparison

Reconfirmed fresh this phase (not assumed from Phase 0.49/0.50):
`register_documents` at `API/business-service/router/business_api.py:484`,
`get_seller_documents` at `API/business-service/router/business_admin_
api.py:420`, `delete_seller_document` at the same file, line `441` — all
three still exist at the exact lines previously recorded. The final
answer's "28 enumerable lines" banner is raw, tool-produced output (a
direct `get_file_content` read), not model prose, and is therefore
inherently accurate by construction. **No fabricated service, file,
function, or route appears anywhere in the final answer.**

**A discrepancy worth disclosing honestly, not investigated further (out
of this phase's scope):** independently re-running the exact final answer
text through this LOCAL checkout's `verify_claims()` directly (no MCP, no
mocks) reports 6 `ROUTES` section `NOT FOUND` findings (each one, e.g.
`/status/{user_id}`, `/{business_id}/documents`, also separately marked
`PLAUSIBLE` by the `ROUTE VERBS` cross-check as "verb+path declared in the
cited file") — none of which are `_is_chain_question`, and none of which
the live run's own journal shows triggering any repair activity at all (no
`repair (verify_claims)` line appears). Possible causes include the
deployed hive-mcp container running code that has since diverged from this
local checkout, or a call-argument difference this report did not isolate.
This is a **pre-existing, unrelated-to-this-phase ROUTES-matching
behavior** — if real, it predates and is orthogonal to both the `_is_
chain_question` defect this phase fixes and the `_move_on_hint` change —
and the live run itself never actually encountered it as a problem
(confirmed by the absence of any repair line). Flagged as a concrete,
unresolved observation for a future phase; not pursued further here per
this phase's own explicit "do not broaden the mission" instruction.

## 13. Phase 0.44/0.45/0.46/0.49/0.50 preservation evidence

- **Phase 0.44** (no-evidence invariant): unchanged by diff; confirmed
  firing correctly in this live run (§11, step 1).
- **Phase 0.45** (router/model completeness guards): unchanged by diff;
  not exercised by T11.
- **Phase 0.46** (`_revalidate_adopted_correction`): unchanged by diff; its
  own call immediately after `_attempt_chain_coverage_correction` correctly
  logged `outcome=skipped` in the live run.
- **Phase 0.49** (the chain-coverage gate's discovery/delegation design,
  one-shot bound, tool-enabled retry): unchanged by diff; all confirmed
  firing correctly in the live run (§11, steps 3–4).
- **Phase 0.50** (`_salvage_truncated_prefix`, `_strip_incomplete_answer_
  note`, `_CHAIN_COVERAGE_SALVAGE_MIN_RETENTION`, the coverage-progress
  gate): unchanged by diff; its adoption branch fired live for the first
  time this phase (§11, steps 5–6), and the coverage-progress gate
  correctly discarded the salvaged text once it confirmed no real coverage
  progress had been made — both pre-existing mechanisms, working exactly as
  Phase 0.50 designed them, untouched by this phase's own two fixes.

## 14. Known limitations and unresolved risks

1. **`lightrag.service` needed a restart** before any live run could
   execute at all — a pre-existing, unrelated infrastructure issue (a
   stuck MCP session-manager task group), not a code defect this phase
   introduced or is responsible for fixing. Documented transparently rather
   than silently worked around.
2. **The live run's own final audit quality is still FAIL** (`(no
   response)`), but for a genuinely different, pre-existing reason than
   Phase 0.50's incident: the ORIGINAL business-service delegation's own
   generation never produced a usable synthesis even once, on the very
   first attempt, before the chain-coverage gate ever ran. This is a
   repetition-proneness issue in the underlying model/pipeline, explicitly
   out of this phase's scope to chase (mission: "do not change the
   repetition system to compensate for a verification defect"; "do not
   broaden the mission because the live run exposes unrelated issues").
3. **The ROUTES-matching discrepancy** described in §12 is real, observed,
   and unresolved — flagged as a concrete candidate for a future phase's
   investigation, not fixed or further diagnosed here.
4. **One live run is one data point.** Whether the salvage-adoption branch
   firing, the coverage-progress gate correctly discarding it, and the
   original delegation's own repetition-proneness recur on every T11 run or
   vary run-to-run was not established here, and this phase's own "do not
   repeat to get a better result" instruction correctly prohibits pursuing
   that further in this report.

## 15. Verdicts

- **Implementation**: **PASS** — two small, narrowly-scoped wording/logic
  changes (48 insertions, 11 deletions), zero changes to `verify.py`, zero
  changes to any other correction function, every stated non-goal
  respected (confirmed by diff and by the scratch suite).
- **Scratch validation**: **PASS** — 21/21 new checks (12 against the real
  verifier with zero mocks of its logic, 9 against the real invariant/hint
  functions with only the MCP transport hop mocked), 33/33 + 23/23 + 18/18
  + 20/20 + 3/3 pre-existing checks unchanged — zero regressions. No unit
  tests added, modified, or run.
- **Deployment**: **PASS** — clean `--ff-only` on both local and ZGX,
  dirty/untracked state preserved (one accidental, self-created, empty
  debris file removed), service healthy and restarted on the new commit.
- **Live mechanism execution**: **PASS** — Objective A/C confirmed directly:
  zero `repair (verify_claims)` lines anywhere in the complete live journal
  (a direct, observable contrast with Phase 0.50's own incident, which
  showed exactly that line for this same disclosure function). Objective D
  confirmed directly: `API/business-service` correctly appears in BOTH the
  "covered so far" and the "no usable investigation result yet" lists in
  the SAME live run, proving the evidence-accuracy fix fires on a genuine
  live incident where a delegation was attempted but never produced a
  result.
- **Final verification**: **PASS** on this phase's own primary acceptance
  criterion — the fixed disclosure reached the final answer intact, and no
  false-positive repair/re-delegation cascade occurred because of it.
  **PARTIAL** on the broader question of "is the live verifier pipeline
  fully understood" — the §12 ROUTES discrepancy remains open and
  unexplained, though it did not affect this run's outcome.
- **Live T11 audit quality**: **FAIL** — the final answer is still `(no
  response)`, but independently confirmed NOT to be caused by this phase's
  own target defect; root-caused instead to the original delegation's own
  pre-existing repetition-proneness, explicitly out of scope to fix here.
- **Fabrication containment**: **PASS** — zero fabricated service,
  function, file, or route names anywhere in the final answer; every named
  symbol independently reconfirmed against the real repository this phase.
