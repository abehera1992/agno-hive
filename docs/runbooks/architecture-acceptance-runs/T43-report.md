# T43 — Evidence-Driven Delegation Correlation, Native Agno Capability Audit, and Live ZGX Validation

## 1. Executive verdict

| Dimension | Verdict |
|---|---|
| Investigation | **PASS** |
| Implementation | **PASS** |
| Deployment | **PASS** |
| Live validation | **PARTIAL** |

T43 closed out the remaining open question from T40–T42: whether the `run_id`-based
correlation fallback (reached only when `run_id` claiming fails) could be made safer
without reintroducing T42's already-rejected stricter guard. The answer, established
by direct reading of the installed Agno package rather than assumption, is that a
fully race-free fix is **not available without modifying Agno itself** (out of
scope). The smallest safe, directly justified change was implemented instead:
enriching the existing ambiguity diagnostic (T42) with each candidate's own `target`
identity, so a future occurrence is diagnosable rather than merely confirmed
ambiguous. No correlation *behavior* changed. A new, real-but-narrow race window was
also discovered (RunStarted can itself fire out of dispatch order, under two awaits
inside Agno's own `arun()`) and is reported honestly as an open, unexercised risk —
consistent with the mission's instruction not to invent a fix without strong,
directly-justified evidence.

Live validation is marked **PARTIAL, not PASS**: a real 24.4-minute production run on
the deployed T43 commit confirmed the correlation mechanism operating correctly under
normal (sequential) multi-delegation load, but did not exercise either the specific
fallback path T43 changed or genuine concurrent/out-of-order dispatch — both remain
NOT_EXERCISED live, as in every prior phase.

## 2. Root cause and exact source locations

There was no new defect to fix this phase — T41 already closed the primary
(completion-order) race via `run_id` correlation, and T42 already explained the F6
discrepancy as a methodology artifact (not revisited here, per the mission's explicit
instruction).

What T43 investigated and found:

- **`swarm/team.py:18162-18174`** (`_finalise_member_chunks`'s fallback branch): the
  path reached only when `run_id` correlation does not resolve. Its own T42
  diagnostic (`print(... T42: FIFO fallback ...)`) could confirm *that* an ambiguous
  pop happened but not *which* candidates were in contention. T43 enriched this one
  print statement to include each pending entry's own `target` field.
- **`agno/agent/_run.py` (installed package, ~lines 1954–1983)**: `await
  aregister_run(run_context.run_id)` and `agent_session = await
  aread_or_create_session(...)` both execute **before** `create_run_started_event`
  is constructed/yielded. This is a new finding: T41's "claim at RunStarted" design
  assumes RunStarted fires in dispatch order; these two awaits give concurrently
  dispatched delegations' own session-setup work a real (if small) window to
  complete, and therefore emit RunStarted, out of that order.
- **`agno/team/_default_tools.py:538+`** (`delegate_task_to_member`) and
  **`agno/team/_task_tools.py:508-610`** (`aexecute_task`, the structured-task
  path): both construct their calls to `member_agent.run()/arun()` with no `run_id`
  kwarg at all. Confirmed by reading both functions' full bodies — this codebase's
  own delegation wrapper (`delegate_structured_task` → `original_entrypoint`) has no
  native Agno parameter to pass a caller-supplied `run_id` through.

## 3. Native Agno capability audit (Track B)

| Capability | Classification | Evidence |
|---|---|---|
| Agent/Team `run_id` | **Native, sufficient for uniqueness** | `run_id = run_id or str(uuid4())` (`agno/agent/_run.py` ~1255, ~2488) — always freshly generated since this codebase never supplies one |
| `run_id` as a guaranteed-order signal under concurrency | **Native, but insufficient** | RunStarted emission follows two real awaits (`aregister_run`, `aread_or_create_session`) that can themselves resolve out of dispatch order under `asyncio.gather` |
| Caller-supplied/pre-assigned `run_id` | **Not available in the installed version** | Neither native delegation entrypoint (`_default_tools.py`, `_task_tools.py`) accepts or forwards one |
| Session/workflow identifiers | **Application-specific** | `session.session_id` is shared across all members in a team run — not a per-delegation identity |
| Delegation/task identity (this codebase's own) | **Application-specific** | `canonical_task_hash` / `delegation_key` (this repo's own hashing in `delegate_structured_task`) — not an Agno-native concept |
| `asyncio.gather` completion semantics | **Native, confirmed concurrent** | `agno/models/base.py`: "Create and run all function calls in parallel" |
| Tool execution/result metadata | **Native, but not identity-bearing** | tool-call events carry no cross-referenceable delegation identity beyond `run_id` |

**Conclusion**: `run_id` remains the best available native signal — real, unique, and
correctly adopted by T41 — but it is not a native, caller-controllable mechanism, and
its *emission order* is not a hard guarantee under concurrency. No better native
mechanism was overlooked; none exists in this installed Agno version.

## 4. Production code changed, and why

**File**: `swarm/team.py`, `_finalise_member_chunks`'s ambiguous-fallback branch
(single hunk, 19 insertions / 4 deletions).

**Change**: the existing diagnostic print statement now includes each pending
entry's own `target` field (never the full objective/evidence_required prose, never
a tool argument) and names which one was chosen. No change to which entry gets
popped, when the fallback fires, or any other behavior.

**Why this and not something deeper**: two deeper fixes were evaluated and rejected
on evidence, not preference:
1. A caller-supplied `run_id` threaded through to Agno's own `arun()` — confirmed
   infeasible without modifying the installed Agno package (Track B).
2. T42's own stricter fail-closed fallback — already proven (in T42) to break 3
   protected tests this phase may not touch.

Per the mission's own Track C preference order ("don't invent a correlation
algorithm... implement only an independently safe, directly justified improvement"),
diagnostic enrichment is the only change that is both safe and justified by this
phase's own evidence.

## 5. Before/after identity-correlation behavior

**Before (T42 end-state)**: on reaching the ambiguous fallback (2+ pending, no
`run_id` match), the system logged only a count (`N entries pending`) and popped the
oldest, with no way to tell afterward which candidates were involved or whether the
guess was right.

**After (T43)**: identical pop behavior. The log now reads, e.g.:
```
[team] T43: FIFO fallback resolving an AMBIGUOUS completion for 'Researcher' --
2 entries pending (targets: ['a.py', 'b.py']), choosing 'a.py' (oldest) without
positive evidence it is the correct one -- no run_id correlation was available
for this completion
```
A future occurrence is now diagnosable against the actual candidates, not just
confirmed ambiguous.

## 6. Targeted ZGX validation evidence

A single, narrow live scenario was run against the deployed T43 commit (`4028bfa`):
`agno_run("Audit the vouchers module: ...", swarm_team="engineering",
read_only=True)`. Session `ee281c5c-a2df-499e-b21b-fd92400748ee`, run_id
`3009468428cf`, duration 1466.5s, outcome `ok`.

Directly observed via `journalctl --user -u agno-api.service`:

- **4 real delegations** dispatched to the Researcher member (endpoints, database
  tables, frontend hooks, gap analysis). Each one's own `RunStarted` event was
  observed and claimed; each completion's own result text stayed correctly scoped to
  its own objective — zero cross-contamination.
- **3 duplicate attempts** on already-answered targets were correctly intercepted by
  the pre-existing duplicate-delegation gate (2 served the prior result, 1
  hard-stopped after a 3rd repeat) — none of these reached the correlation machinery
  at all, confirming the two layers do not interfere.
- **Zero occurrences** of either the T42 or the T43-enriched ambiguity diagnostic —
  `run_id` correlation resolved every real completion this run; the fallback path
  T43 changed was **not reached**.
- **Dispatch/completion order was strictly sequential** throughout — this run did not
  (and, consistent with T39–T42's own finding, does not naturally) exercise genuine
  overlapping/out-of-order concurrent dispatch to the same member.
- A separate, pre-existing, unrelated issue was observed: the Coordinator's own
  final-answer generation hit two nested repetition loops, handled by existing guards
  and ultimately delivered with an honest "UNRESOLVED/still incomplete" disclosure.
  Out of scope for T43 — noted only as an observation (echoes the previously-logged,
  still-open "T12 runaway-generation loop" finding).
- `agno-api.service` was confirmed healthy throughout and after (same main process,
  same start time before and after the run — no mid-measurement restart).

**Regression confidence**: the full existing test suite was re-run, unmodified,
twice — locally and directly against the deployed ZGX source (via
`~/miniforge3/envs/zgx/bin/python -m pytest`). The 15 directly relevant tests
(`test_t41_fifo_correlation_integrity.py`, `test_t40_pending_entry_cross_delegation_leak.py`)
pass in both places. The full suite's failing-test-name set is byte-identical to
T42's own end-state baseline (54 failures, zero new) — confirmed via exact diff, not
just a count.

## 7. Git / push / deployment status

- Commit `4028bfa` on `main`, containing exactly one file (`swarm/team.py`), zero
  test files, zero T24 content (`git add -p` used to skip all 6 of T24's own hunks;
  `git diff --cached | grep -c "T24:"` returned 0 before commit).
- Pushed: `6b57fb5..4028bfa main -> main`. `origin/main` verified to match local HEAD
  exactly.
- No stray 0-byte shell artifacts found before commit (`git status --short -uall`
  showed only pre-existing, unrelated untracked files).

## 8. ZGX deployed commit / service status

- ZGX's own dirty/untracked files (training scripts, `.env.bak-*`, litellm configs,
  etc.) confirmed unchanged before and after the pull — none touched.
- `git pull --ff-only origin main` on ZGX: fast-forward `def9cb4..4028bfa`, clean,
  no conflicts.
- `agno-api.service` restarted once (to pick up the change), confirmed active and
  healthy (`systemctl --user status`), then left running unmodified through the
  full 24.4-minute live validation — same PID/start-time before and after.
- `grep -c "T43 (Track A)" swarm/team.py` on ZGX confirmed the new comment marker
  present in the deployed source.

## 9. Confirmation no test files were touched

`git status --short -uall` before commit showed zero new or modified files under
`tests/`. The staged diff (`git diff --cached --stat`) touched exactly
`swarm/team.py`. No test file was added, modified, deleted, renamed, or reformatted
at any point in this phase.

## 10. Remaining risks / unexercised branches

1. **RunStarted-ordering race** (Track B's own new finding): real but narrow — not
   fixed this phase, since no safe, directly-justified fix exists without modifying
   installed Agno. Unexercised by any test or live run. A future phase could build a
   deliberate concurrent-dispatch integration test to probe it (a new test file would
   be that future phase's own decision, not a violation of *this* phase's
   constraint).
2. **The ambiguous fallback's own behavior is unchanged** — only its diagnostic was
   enriched. If a real misattribution ever occurs, it is now diagnosable after the
   fact, not prevented. Same boundary T42 called Outcome B3-C, carried forward
   honestly.
3. **No native caller-supplied `run_id` exists in the currently installed Agno** — if
   Agno is upgraded, re-verify rather than assume this still holds.
4. Genuine overlapping/out-of-order dispatch to the same member remains
   **NOT_EXERCISED** live, across every phase from T39 through T43 — correctness
   there continues to rest on the unit test suite (`test_case3`/`case3b` etc.), not
   on any observed production occurrence.

## 11. Telemetry locations

- `docs/runbooks/architecture-acceptance-runs/runs.jsonl` — 40 lines total after
  this phase (append-only; 1 phase_baseline, 3 task_attempt records for Tracks A/B/C,
  1 task_attempt for the live validation, 1 phase_summary). T40–T42's own 34 lines
  untouched.
- This file (`T43-report.md`).
