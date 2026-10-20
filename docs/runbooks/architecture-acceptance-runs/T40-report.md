# PHASE T40 FINAL REPORT — RECONCILIATION INTEGRITY AFTER BLOCKED DELEGATION + T39 HARD-STOP VALIDATION

## 1. Executive Verdict

**Track A (F3): PARTIAL.** Root cause proven to one exact code boundary, fixed, baseline-first unit-tested, deployed, and re-run directly against the deployed source. A fresh live-model trigger of the exact duplicate-block-to-reconciliation sequence was not attempted this phase — reported honestly as PARTIAL, not rounded up to PASS.

**Track B (T39 hard-stop): PARTIAL.** The deterministic production-path check is **PASS** — the hard-stop fires exactly at its documented 20-call threshold, never one call early, confirmed against the real function both locally and on the deployed ZGX source. The live-model dimension is **NOT_EXERCISED** — no live run was launched to force the exact 20-consecutive-literal-probe sequence, consistent with T39's own finding that the model does not reliably reproduce this pattern on demand.

Per the mission's own final decision rule, since neither track achieved a full live-model trigger, **T40 as a whole is PARTIAL**, not fully accepted.

## 2. F3 Incident Timeline and Evidence-Backed Root Cause

Reconstructed from ZGX journalctl (run `d6f4d4a1b3cc`, `--since "2026-10-20 13:48:00" --until "2026-10-20 14:01:15"`, still within the journal's retention window):

| seq | Delegation | Target | Outcome |
|---|---|---|---|
| 1 | "List all endpoints..." | `vouchers_api.py` | dispatched, ran fresh (`no usable prior result — allowing the retry`) |
| 2 | "Identify the database tables..." | `vouchers_api.py` (same) | dispatched, ran fresh |
| 3 | "Identify the frontend hooks..." | `page.tsx` (different) | dispatched, ran fresh, found the 2 real hooks |
| 4 | "Compare backend endpoints against frontend hooks..." | `vouchers_api.py` (repeat, #2) | **served "ALREADY DONE... The frontend hooks used by the vouchers module i[s...]"** — a different delegation's text, for a different target, than what seq4 actually asked about |
| 5 | "List all the endpoints..." (repeat, #3) | `vouchers_api.py` | hard-blocked: `STOP: you have now asked 'researcher' for this same target 3 times` |

The evidence-integrity reconciliation retry that followed seq5's block (triggered by an unrelated "9 routes, not 5" count contradiction) had no usable grounded content to work from for the endpoints/tables sections and generated generic CRUD names from the model's own prior knowledge instead — `verify_claims` correctly flagged 13 of 15 checked symbols as `NOT IN FILE` and the run returned an explicit `UNRESOLVED` banner rather than shipping the fabrication silently.

## 3. Exact Violation Boundary

`swarm/team.py`, `_make_duplicate_delegation_gate_hook` (dispatch site) and `_finalise_member_chunks` (completion site), pre-fix:

- **Dispatch** (`~16013`): `team._pending_log_entry_by_member[_dispatch_member_id] = log[-1]` — a single dict slot keyed by **member only**, unconditionally overwritten on every `delegate_structured_task` call for that member.
- **Completion** (`~17960-17964`): `_pending_entries.pop(key, None)` pops whatever is *currently* in that one slot and tags the completing member's text onto it — not necessarily the entry that dispatch belongs to, if a later dispatch already overwrote the slot.
- **Read** (`~15789-15791`): the duplicate-gate's `prior` resolution prefers the per-entry tag but falls back to `team._member_results.get(member_id)` — also member-only keyed, holding only the single most recent completion — whenever the per-entry tag is missing or wrong.

Net effect, proven by direct trace matching the live incident: dispatching a second delegation to the same member before the first one's result materializes silently reassigns the bridge, so the first delegation's text can land on the second's log entry (or nowhere), and a later repeat of the first delegation's own question falls back to whatever the member most recently finished — which may be for a completely unrelated target.

## 4. Hypothesis Table (Track A)

| Hypothesis | Verdict |
|---|---|
| Reconciliation invents content because it has no fallback for a blocked retry | **Contributing symptom, not the root cause** — reconciliation's own failure is downstream of being handed the wrong "prior result" in the first place |
| The duplicate-delegation gate's matching logic (target/objective selection) is wrong | **Separate, pre-existing, out-of-scope limitation found** (F5, §11) — not this phase's fix |
| The pending-result bridge is a single slot with no target/delegation scoping | **CONFIRMED root cause**, proven via code trace + baseline-first failing tests reproducing the live incident's exact shape |
| `forward_member_answer` mishandles evidence | **Not implicated** — not touched, no evidence found against it |

## 5. Baseline-First Test Evidence and Post-Fix Results

`tests/test_t40_pending_entry_cross_delegation_leak.py` — 5 tests. Against the unmodified baseline: 2 failed, reproducing the exact incident shape (a repeat of the first delegation's own target+objective served a *different* delegation's text). A third test's baseline pass revealed the separate `_objective_names_new_entity` limitation (§11); its wording was corrected to isolate the pending-entry-bridge defect from that different function's own documented scope, rather than conflating the two. Post-fix: 5/5 pass.

## 6. T39 Hard-Stop Threshold Test — Proof the Actuator Fires

`tests/test_t40_literal_probe_exact_threshold.py` — 2 tests, both passed on **first run** (T39's own mechanism needed no fix): 19 consecutive unproductive literal `search_files` calls never force text-only; the 20th does, exactly then, confirmed via `researcher.tool_choice == "none"` immediately after that call and the explanatory note present in that call's own returned result — driven through the real `_make_read_cache_tool_hook` production function, not a reimplementation.

## 7. Productive-Search and Legitimate-Exploration Control Results

Re-ran T39's own 7-test suite (`test_t39_literal_search_probe_escalation.py`) unchanged: a real match resets the streak across repeated near-threshold cycles; a regex/alternation attempt resets the streak even if it also misses; the 9-endpoint legitimate enumeration shape stays silent; per-member isolation holds; interleaved different-tool calls don't interfere. All 7 still pass — Track A's changes did not disturb Track B's mechanism.

## 8. Role-Policy Row and Effective-Budget Verification

Re-ran T39's `test_t39_role_policy_case_mismatch.py` (6 tests) unchanged — all pass. `_resolve_tool_call_limit`'s correct resolution (exact-case primary cache, lowercase-keyed fallback) and `phase0`'s `tool_calls_made` (run-wide cumulative) vs `tool_calls_this_delegation` (per-delegation) distinction remain intact and un-conflated; `test_t38_tool_budget_per_delegation_scope.py` (6 tests) re-confirmed clean.

## 9. Deployment Details and Live-Validation Outcomes

| Component | Commit | Runtime | Method | Verified |
|---|---|---|---|---|
| Track A fix (`swarm/team.py`) | `b0e8760` | ZGX `agno-api.service` (9001) | `git pull --ff-only` + `systemctl --user restart` | clean restart, 2 "T40 (F3)" markers grep-confirmed; 12/12 tests re-run directly against the deployed source via its own production Python env (`~/miniforge3/envs/zgx/bin/python`) |
| Track B tests (no code change) | `d4246db` | ZGX `agno-api.service` (9001) | `git pull --ff-only` only — **no restart performed**, since this commit changes no production code | 2/2 new boundary tests re-run directly against the deployed source |
| `hive-mcp` (9003) | — | untouched | n/a | not implicated by either track |
| `hive-mcp-agno` (9004) | — | untouched | n/a | explicitly out of scope, not touched |

No live-model run was launched this phase for either track — see §1 and §11 for why, and the mission's own explicit allowance for reporting `NOT_EXERCISED`/`PARTIAL` rather than claiming an untriggered live validation.

## 10. Test-Suite Results

Full local suite: **54 failed, 2617 passed**. A **precise failing-test-NAME diff** (not just a count) against the unmodified baseline (git `HEAD` before any T40 edit) found:

- **Zero new failures** introduced by either track.
- **3 pre-existing failures resolved as a side effect**: `tests/test_evidence_fidelity.py::test_delegated_researcher_evidence_resolves_to_researcher_not_coordinator`, `::test_multiple_delegated_members_each_resolve_to_their_own_identity`, `::test_relay_retention_no_longer_forced_to_zero_for_delegated_evidence` — all were failing in the raw, unmodified baseline (confirmed by temporarily restoring `git show HEAD:swarm/team.py` and re-running) and now pass because the shared `_pending_log_entry_by_member` mechanism they depend on is more correct. **T24's own source files (`swarm/team.py`'s T24 hunks, `swarm/execution_context.py`, `tests/test_evidence_fidelity.py` itself) were not modified** — this is confirmed by the exact T24 diff stat matching the pre-T40 baseline byte-for-byte (§11).

This is the first time in this multi-phase session a byte-exact diff of failing test *names* (not just the raw count) was performed; the baseline count itself was 57, not the "54" figure carried forward by assumption across T37–T39's own reports. This phase reports its own measured numbers rather than the older assumed figure, per the mission's own explicit instruction not to report "zero regressions" from a count match alone.

## 11. Commits and Files Changed

- `b0e8760` — Track A fix: `swarm/team.py` (2 hunks: dispatch-time FIFO append, finalize-time FIFO pop), `tests/test_t40_pending_entry_cross_delegation_leak.py` (new, 5 tests).
- `d4246db` — Track B tests: `tests/test_t40_literal_probe_exact_threshold.py` (new, 2 tests). No production code.

**New finding, not fixed this phase (F5):** `_objective_names_new_entity` (pre-existing, unmodified) only recognizes identifier-shaped (CamelCase/snake_case) token differences between two objectives sharing a target, never plain-English subject differences, by its own documented design. This means a repeat of a *non-first* question sharing a target with an earlier, plain-English-only-different question can still match the wrong historical entry even with this phase's FIFO fix in place (the fix guarantees each entry *holds* its own correct text; it does not change *which* entry a later lookup selects among several sharing a target). Documented as a separate, independent candidate for a future phase, not conflated with F3's own narrower fix here.

## 12. T24 and Unrelated Dirty-File Preservation Confirmation

Before (start of T40) and after (this report) `git diff --stat` on T24's own files:

```
docs/runbooks/groundedness-battery.md |  39 ++
swarm/execution_context.py            |  32 ++-
swarm/team.py                         |  33 ++-   (T24's own 5 hunks only — my 2 hunks are committed separately)
tests/test_evidence_fidelity.py       | 153 ++++
training/train.py                    | 416 ++++++++++---
zgx-ai-setup/litellm-config.yaml      |   6 +-
```

Identical before and after this phase's entire body of work. Staged `swarm/team.py` via hunk-by-hunk `git add -p` for the Track A commit (7 total hunks: 2 mine, 5 T24's, confirmed via `git diff --cached | grep -c "T24:"` returning 0 both times this session). No `git add -A`/`git add .` used anywhere. No stray artifacts found this phase (checked `git status --short -uall` before every commit).

## 13. Telemetry/Runbook Entries Added

`docs/runbooks/architecture-acceptance-runs/runs.jsonl` — 4 new records this phase (1 `phase_baseline`, 2 `task_attempt` — `PENDING-ENTRY-FIFO-FIX1`, `LITERAL-PROBE-THRESHOLD-BOUNDARY1` — and 1 `phase_summary`), bringing the file to 26 total lines, all validated as parseable JSON. Same schema and six-value guard-outcome vocabulary as T36.1–T39, no schema changes. This file.

## 14. Remaining Gaps, Risks, and Next Recommended Phase

- **F5** (`_objective_names_new_entity`'s plain-English blind spot) is the clearest, most concretely-evidenced next candidate — it was *discovered* while isolating this phase's own test, not invented speculatively, and it directly affects the same duplicate-delegation gate this phase just repaired one layer of.
- Neither track achieved a genuine live-model trigger this phase. The next phase that touches either mechanism should budget for *one* deliberate live attempt specifically shaped to increase the odds of hitting the real trigger condition (for Track A: a task structurally likely to need a reconciliation retry AND a 3rd same-target ask; for Track B: a task scoped to a directory with no obviously-findable identifier, pressuring literal guessing) — accepting that even a well-shaped attempt may still land as `NOT_EXERCISED`, as T39's own one live sample already demonstrated for Track B.
- The "54 vs 57" baseline-count discrepancy (§10) suggests prior phases' own "zero regressions" claims, while directionally correct (no NEW failures from this session's own fixes), were tracking a remembered round number rather than a freshly re-verified exact list each time. Future phases should prefer a precise before/after failing-test-name diff over a bare count comparison, as done here.

**Next recommended phase:** investigate F5 (`_objective_names_new_entity`'s entity-discrimination scope) as its own, independently-fixable defect, and attempt one deliberately-shaped live trigger for each of Track A and Track B, reporting whichever verdicts the evidence actually supports.
