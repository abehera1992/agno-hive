# PHASE T42 FINAL REPORT — F6 TEST-OUTCOME DISCREPANCY + RUN_ID CORRELATION FALLBACK SAFETY

## 1. Executive Verdict

**Track A (F6): PASS.** F6 is fully, causally explained with direct evidence. No production defect exists; no code change was needed or made. This report serves as the explicit linked correction to both T40's and T41's own mischaracterizations of these 3 tests.

**Track B (fallback safety): PARTIAL.** The fallback's safety precondition is now established and documented (safe only for in-order completion, which cannot be distinguished from the unsafe out-of-order case using the information available at that point). A stricter fix was built, tested, and explicitly rejected because it broke existing, protected tests. A non-behavioral diagnostic was added instead.

## 2. Exact F6 Test Names and Baseline/Post-Fix Outcomes

- `tests/test_evidence_fidelity.py::test_delegated_researcher_evidence_resolves_to_researcher_not_coordinator`
- `tests/test_evidence_fidelity.py::test_multiple_delegated_members_each_resolve_to_their_own_identity`
- `tests/test_evidence_fidelity.py::test_relay_retention_no_longer_forced_to_zero_for_delegated_evidence`

**None of these three tests exist in any committed revision of this repository** — confirmed by checking out commit `2df251e` (T40's end state) in an isolated `git worktree` and listing every `test_` function actually present in that commit's `test_evidence_fidelity.py`: all 3 are absent. The file at that commit contains only 14 other, unrelated tests. Independently re-confirmed on ZGX's own checkout (a separate machine, same remote): `pytest tests/test_evidence_fidelity.py::test_delegated_researcher_evidence_resolves_to_researcher_not_coordinator` there returns `ERROR: not found`.

## 3. Causal Explanation for F6

**Root cause: a confound in T41's own comparison methodology, not a real code relationship.**

These 3 tests exist only in the perpetually-uncommitted, T24-owned working-tree version of `tests/test_evidence_fidelity.py` (confirmed: `git diff --stat tests/test_evidence_fidelity.py` has shown the identical "153 lines changed, uncommitted" delta throughout this entire multi-phase session, T37 through T42 — this file's content has never varied). They specifically exercise T24's own fix to `_evidence_fidelity_report`'s `agent_name` resolution — also never committed: `git show HEAD:swarm/team.py | grep "T24: prefer the identity"` returns no match at any commit checked, and the committed `HEAD` version unconditionally contains the old, pre-T24 line `agent_name = _fidelity_agent_name(run_context, ev.execution_id)` — the exact bug T24 exists to fix (collapsing a delegated member's evidence identity to "Coordinator").

T41's own re-verification of T40's claim used `git show HEAD:swarm/team.py > swarm/team.py` to reconstruct a "pure baseline" for comparison. Because `git show` returns **committed content only**, this operation also, inadvertently, **temporarily stripped T24's own uncommitted hunk** from the working tree for the duration of that measurement — not because of anything in T40's or T41's own Track A/B changes. Restoring the saved backup afterward (which included T24's hunk again, alongside T41's own changes) is what made the 3 tests pass again in the *next* measurement. The pattern T41 observed — "fails without my fix, passes with it" — was real and reproducible, but the true independent variable being toggled was **T24's own hunk's presence**, confounded with T41's own fix because both happened to be absent/present together across that specific swap-based comparison.

## 4. Exact First Causal Divergence

- **File**: `swarm/team.py`
- **Function**: `_evidence_fidelity_report`
- **Line (committed `HEAD`)**: `agent_name = _fidelity_agent_name(run_context, ev.execution_id)` — the old, pre-T24 resolution.
- **Line (actual working tree, uncommitted, T24's own)**: `agent_name = (getattr(ev, "agent_name", None) or _fidelity_agent_name(run_context, ev.execution_id))` — T24's fix.
- **Divergence point**: any operation that reconstructs `swarm/team.py` from `git show`/a bare checkout (rather than the live working tree) loses this one line, and with it, the only thing these 3 tests actually verify.

## 5. Run_id Correlation Lifecycle and Fallback Safety Conclusions

Traced the complete lifecycle in `_finalise_member_chunks` (per B1): dispatch → `_claim_pending_entry_for_run` (at `RunStarted`) → concurrent execution (agno's own `asyncio.gather`) → completion (`RunContentCompleted`) → `run_id`-based resolution, falling back to FIFO-pop only when no `run_id` match exists.

**Audited the fallback (B2/B3) against the stated safety invariant** ("a result may be attached to a target only when there is evidence it originated from that delegation"). Found: when the fallback is reached with **exactly one** pending entry, FIFO position *is* evidence (no other candidate exists) — safe. When reached with **two or more** pending entries and no `run_id` match, popping "the oldest" is a guess — proven unsafe under adversarial out-of-order completion (T41's own `Case 3`, re-confirmed this phase, unmodified).

**A stricter fix was built and rejected.** Restricting the fallback to the single-entry case (leaving ambiguous multi-entry cases unresolved) was implemented, then run against the *existing, unmodified* test suites — it broke 3 pre-existing, protected tests from T40/T41 (`test_case2_overlapping_dispatch_in_order_completion`, `test_interleaved_dispatches_do_not_cross_tag_each_others_result`, `test_three_interleaved_dispatches_each_keep_their_own_text`) that deliberately exercise the no-`run_id`, in-order-completion shape and expect FIFO-pop to resolve it correctly. Per this phase's explicit instruction, these tests could not be modified to accommodate the stricter rule.

**Resolved as Outcome B3-C**: the ambiguity is fundamental, not merely unaddressed. With no `run_id` and 2+ pending entries, in-order completion (FIFO-correct) and out-of-order completion (FIFO-wrong) are **indistinguishable** from the information available at that exact point in the code — there is no additional signal to consult without a broader architectural change this phase's own scope does not justify. The fallback was left as FIFO-pop (T40's original, unmodified behavior), reported honestly as *not proven safe* rather than forced into an unvalidatable change.

## 6. Exact Cases Validated

Using only existing, unmodified tests (`tests/test_t41_fifo_correlation_integrity.py`, `tests/test_t40_pending_entry_cross_delegation_leak.py`) and direct code inspection — no test file was added or changed:

| Case | Outcome |
|---|---|
| Valid `run_id` with a matching pending entry | resolves correctly (unchanged from T41) |
| Missing `run_id` (fallback path) | FIFO-pop, correct when exactly 1 pending entry |
| Missing `run_id`, 2+ pending entries, in-order completion | FIFO-pop, correct *by observed convention*, not provable from available evidence |
| Missing `run_id`, 2+ pending entries, reverse completion order | FIFO-pop, **proven incorrect** (T41's own Case 3) — now logged as ambiguous via the new diagnostic, not silently hidden |
| Multiple concurrent delegations to the same member | handled correctly via `run_id` when available (Case 4) |
| Duplicate-delegation block | never serves unrelated text (Case 5, re-confirmed) |
| Failure/empty delegation | does not inherit a later success's text (Case 6, re-confirmed) |
| Stale/cross-instance state | no leak (Case 10, re-confirmed) |

Timeout/cancellation-specific cleanup was not separately exercised — no distinct code path for it was found beyond the empty-buffer early-return already covered by Case 6; reported as a gap, not fabricated.

## 7. Production Files Changed

- `swarm/team.py` — one change: a `print`-only diagnostic inside `_finalise_member_chunks`'s existing fallback branch, firing only when 2+ entries are pending and no `run_id` resolved. **No behavior change.** Necessary because the fallback's own safety could not be proven or improved without breaking protected tests, and the mission explicitly values visibility into how often production actually reaches the ambiguous case over a speculative, unvalidatable behavior change.

No other production file was touched this phase for either track.

## 8. Confirmation: No Test Files Touched

`git diff --cached --stat` for the Track B commit (`def9cb4`) shows exactly one file: `swarm/team.py`, 37 insertions, 0 deletions, 0 other files. No test file was added, modified, deleted, or skipped. `git status --short -uall` before and after this phase's entire body of work shows the identical set of pre-existing dirty/untracked files (see §13).

## 9. Commits and Remote Push Confirmation

- `def9cb4` — "T42 Track B: audit run_id correlation fallback safety, add ambiguity diagnostic" — pushed to `origin/main`, confirmed via `git rev-parse origin/main` matching local `HEAD`.
- Track A required no commit (no code change; this report itself, committed separately as telemetry, is the linked correction).

## 10. ZGX Pull Confirmation and Deployed Source Identity

`git pull --ff-only origin main` on ZGX: fast-forward `3e1ce58..def9cb4`, confirmed via `git log --oneline -3` showing `def9cb4` as `HEAD`. ZGX's own pre-existing dirty/untracked files (`training/train.py`, `zgx-ai-setup/docker-compose.yml`, `zgx-ai-setup/litellm-config.yaml`, numerous `.env.bak-*` and training/eval artifacts — all ZGX-local, unrelated to this phase) were confirmed present before the pull and preserved after it. `agno-api.service` restarted cleanly; `grep -c "T42 (Track B)" swarm/team.py` on ZGX returned `1`, confirming the deployed source genuinely contains this phase's change.

## 11. Exact Targeted Commands and Outcomes in ZGX's Production Python Environment

```
~/miniforge3/envs/zgx/bin/python -m pytest tests/test_evidence_fidelity.py::test_delegated_researcher_evidence_resolves_to_researcher_not_coordinator ... -v
  -> ERROR: not found (3x) -- confirms F6's own root cause independently on a second machine

~/miniforge3/envs/zgx/bin/python -m pytest tests/test_t41_fifo_correlation_integrity.py -v
  -> 10 passed

~/miniforge3/envs/zgx/bin/python -m pytest tests/test_t41_fifo_correlation_integrity.py::test_case3_reverse_completion_order_without_run_id_tracking_would_misattribute -s
  -> diagnostic line confirmed present in stdout: "[team] T42: FIFO fallback resolving an AMBIGUOUS completion for 'Researcher' -- 2 entries pending, no run_id correlation available, popping the oldest without positive evidence it is the correct one"
```

## 12. New vs. Pre-Existing Failures by Exact Test Identity

Full local suite after Track B's change: **54 failed, identical to T41's own end-state failing-test-**name** set** (exact `diff` against the saved T41 checkpoint list returns empty, exit code 0) — zero new regressions. The same pre-existing 54 (10 from `test_duplicate_delegation_gate_hook.py`'s own unrelated `UnboundLocalError`, 2 from `test_team_read_cache_hook.py`'s stale hook-count assertion, and the remainder carried forward unchanged from T38–T41) remain exactly as before.

## 13. T24 and Unrelated Dirty-File Preservation Evidence

`git diff --stat` on T24's own files, identical before this phase and after:

```
docs/runbooks/groundedness-battery.md |  39 ++
swarm/execution_context.py            |  32 ++-
swarm/team.py                         |  33 ++-   (T24's own hunks only -- this phase's own 1 hunk committed separately)
tests/test_evidence_fidelity.py       | 153 ++++
training/train.py                    | 416 ++++++++++---
zgx-ai-setup/litellm-config.yaml      |   6 +-
```

Staged `swarm/team.py` via hunk-by-hunk `git add -p` (1 of 6 hunks); `git diff --cached | grep -c "T24:"` returned 0. No `git add -A`/`git add .` used. No stray artifacts found this phase. ZGX's own unrelated dirty files (§10) were inspected before the pull and confirmed unchanged after it.

## 14. Telemetry/Runbook Records Added

`docs/runbooks/architecture-acceptance-runs/runs.jsonl` — 4 new records this phase (1 `phase_baseline`, 2 `task_attempt` — `F6-EXPLAINED-METHODOLOGY-ARTIFACT`, `FALLBACK-AMBIGUITY-DIAGNOSTIC1` — 1 `phase_summary`), bringing the file to 34 total lines, all validated as parseable JSON. Same schema and six-value guard-outcome vocabulary as T36.1–T41, no schema changes, no historical record rewritten. This file.

## 15. Remaining Risks and Smallest Justified Next Step

- The fallback's genuine safety gap (Outcome B3-C) remains unresolved by design this phase — it is now *visible* (via the new diagnostic) rather than *fixed*, because fixing it correctly would require either a broader architectural change (explicit per-entry identity beyond `run_id`, propagated through a path this phase found no evidence for) or breaking protected existing tests, neither of which this phase's own scope permits.
- **Next recommended phase**: let the new diagnostic accumulate real production signal (how often, if ever, the ambiguous 2+-entry fallback path is actually reached), then decide whether a more invasive fix — this time potentially requiring a coordinated update to the existing tests that currently assume blind FIFO-pop is acceptable — is actually warranted by real frequency data, rather than by the theoretical adversarial case alone.
- Track A requires no further action — F6 is closed.
