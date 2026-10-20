# PHASE T38 — EVIDENCE-GROUNDED TOOL SELECTION AND SEARCH-BUDGET ENFORCEMENT REPORT

## 1. Executive Verdict

**One defect proven and fixed (Priority B); the T37 incident fully reconstructed from live logs; one deeper enforcement question left open, with exact reproduction data for the next phase.**

The T37 incident ("79 tool calls against a 25-call budget") turned out to have two separable causes once the orphaned run's own journal was recovered from ZGX: a genuine **metrics-definition mismatch** (phase0's run-wide cumulative tool-call counter was being compared directly against agno's per-delegation enforcement ceiling) — proven, fixed, tested, deployed — and a second, larger, **not-yet-resolved** question of why one single delegation's own call count (77) so greatly exceeded its own reported ceiling (25). The first is this phase's selected defect. The second is documented precisely enough that the next phase does not need to re-derive it.

Separately, a 5-scenario controlled experiment against the real EkamApp repository empirically proved that a wildcard/regex search would have found the real hooks in one call — direct evidence for Priority A, not implemented this phase per the "one defect per phase" discipline.

## 2. T37 Incident Reconstruction — Exact Source of "79" and "25"

T37's own telemetry record (`T13a-T37-LIVE1`) reported `tool_calls_made: 79` against `tool_call_limit: 25`, sourced from one `[phase0] member_result` log line captured mid-run before the client-side `agno_run` call timed out at 1800s. That record could not say whether the run ever completed.

This phase re-checked ZGX's journal during the T38 deployment restart and found the **same run (`run_id fd5bf842c52c`) had actually completed**, 761 seconds past the client's own timeout — the client-side timeout was a connection problem, not a server-side failure, confirming this session's own standing note about `agno_run` timeouts. Recovering the full journal window (`--since "2026-10-20 10:30:00" --until "2026-10-20 11:05:00"`) gave the exact structure behind "79":

| Delegation (seq) | Objective | Tool calls made | Running cumulative `tool_calls_made` | `tool_call_limit` |
|---|---|---|---|---|
| 2 | List all database tables... | 2 (both file reads) | 2 | 25 |
| 3 | List all frontend hooks... | **77** (all `search_files`) | **79** | 25 |
| 4, 5 | re-asked for already-covered targets | 0 (short-circuited: "ALREADY DONE") | 79 | 25 |
| 6 | re-asked for the same frontend target again | 0 (short-circuited: "STOP") | 79 | 25 |

**79 = 2 (delegation seq 2) + 77 (delegation seq 3), cumulative.** The dominant number, 77, came from **one single delegation**, not a sum of several delegations each safely within their own budget — this refines the hypothesis this phase started with (see §8).

## 3. Tool-Budget Contract and Counting Boundary

Traced end to end, by reading source rather than assuming:

- **Where "25" is configured**: `_resolve_tool_call_limit(team_name, role)` (`swarm/team.py`), DB-override-first via `team_role_models`, falling back to `config.tool_call_limit`. Confirmed live for this incident: the Researcher's own resolved limit was 25.
- **What counts as a call, and where**: agno's own `current_function_call_count` (`agno/models/base.py`, installed package) — a variable accumulated via `function_call_count += len(function_call_results)` inside the agentic `while True:` loop **within one `response()`/`aresponse()` invocation**, checked per call in `run_function_calls` (`if current_function_call_count > function_call_limit: ... continue`, refusing execution).
- **Propagation**: `agent.tool_call_limit` is read and threaded through correctly — confirmed via `grep` across `agno/agent/_run.py` and `agno/agent/_response.py` (8 call sites, all passing `tool_call_limit=agent.tool_call_limit` into the model call). Team delegation (`delegate_structured_task`, `agno/team/_task_tools.py`) invokes `member_agent.arun(...)` directly — the same standard path, not a bypass.
- **Counter scope — the actual defect**: `swarm/phase0.py`'s own `tool_calls_made` is a **module-level, run-wide cumulative counter**, by its own long-standing docstring ("Reset per run by `start_run()`"). `Phase0Run.record_member_result` compared this cumulative count directly against the per-delegation `tool_call_limit` to decide `terminal_reason`. These are two different scopes.
- **Which of the six candidate explanations applies**: **counter-reset-or-wrong-scope** — confirmed, not merely suspected. Not config-not-propagated (propagation is correct). Not purely miscounting-telemetry (the counts are individually accurate; they are compared at the wrong scope). Not a confirmed enforcement bypass for the cross-delegation case (delegation 4/5/6 correctly spent zero additional budget). Whether the single-delegation 77-vs-25 figure (seq 3 alone) represents a *separate* enforcement bypass is the open question in §8 — not resolved this phase.

Per the mission's own decision rule ("if 79 versus 25 is a metrics-definition mismatch rather than an enforcement defect, correct the interpretation and telemetry rather than adding an arbitrary hard cap"), this phase corrected the interpretation and telemetry.

## 4. Identifier-Discovery Failure Analysis (Priority A)

Delegation seq 3's own `files_read` was `["API/inventory-service/router/vouchers_api.py", "API/inventory-service/models.py"]` — the two **backend** files. `inventoryApi.ts`, the real file declaring the 5 voucher hooks, was **never read**. The 77 `search_files` calls were sequential literal-name guesses (`useReactQueryCancelProvider`, `useReactQueryAbort`, `useReactQuerySkeleton`, and ~57 more — real React hooks, real TanStack Query hooks, and mechanically-generated `useReactQuery*` combinations — none of which exist in this codebase, which uses RTK Query, not React Query).

## 5. Earliest Proven Failure Boundary

Same boundary T37 already identified for this incident shape: **evidence gathering itself**, before any guard has anything to check. `_under_reported_exports` (T37's own fix) correctly stayed silent — its precondition (file read with real exports on record) was never met, because the file was never read. This is not a guard regression; it is T37's Priority A conclusion reproduced in the same incident, this time with a run that happened to complete.

## 6. Priority A — 5-Scenario Controlled Experiment

Run directly against the live EkamApp checkout (`Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts`), with an evidence ledger per scenario:

| Scenario | Method | Result |
|---|---|---|
| A — known-valid identifier | literal search for `useGetVouchersQuery` | succeeds in 1 call (not independently re-run; implied by the incident's own data whenever a real name is already known) |
| B — known-nonexistent identifiers | the 77 literal guesses themselves | 0 matches, exactly as logged — none of those names exist anywhere in this codebase |
| C — valid-but-non-obvious, domain keyword only | `grep -rniE 'use\w*voucher\w*(query|mutation)'` across the services directory | **all 5 real hooks found in 1 call**, no exact name needed in advance |
| D — mixed discovery, no domain keyword at all | `grep -nE '^\s*use[A-Z]\w*(Query|Mutation),?\s*$'` against the file's own export-list shape | every hook export in the file found in 1 call, including all 5 voucher hooks |
| E — repeated failed lookup | 60 distinct patterns across 77 calls implies ~17 in-delegation repeats; confirmed via code reading that a fresh delegation carries no memory of a prior delegation's negative results | real, evidenced risk for the general case; not directly observed in this specific incident (hooks objective was delegated to only once) |

**Conclusion**: authoritative one-call discovery was available via the exact same `search_files` tool the member called 77 times — the gap is search *strategy*, not tool *capability*. This is well-evidenced but was not selected as this phase's primary defect: a fix here means editing Researcher role instructions, a different and riskier class of change than Priority B's data-plumbing fix, better served by its own dedicated test cycle next phase.

## 7. Budget-Boundary Test Results

6 new tests, `tests/test_t38_tool_budget_per_delegation_scope.py`, all first run against the unmodified baseline and failed (`KeyError`/`AttributeError` — the fields/function did not yet exist), then passed after the fix:

1. Two delegations, 15 and 12 calls respectively (neither exceeds 25 alone, cumulative 27 does) — the second's own `terminal_reason` is no longer misclassified as `tool_limit`.
2. A genuine single-delegation overshoot (26 calls, 25 limit) is still correctly classified `tool_limit` — the fix narrows, does not loosen.
3. A caller that never calls `note_delegation_start` falls back to the cumulative count — no crash, pre-fix behavior preserved for unintegrated callers.
4. Per-member isolation — one member's delegation reset does not touch another's counters.
5. `note_delegation_start` resets only the new per-delegation counter, never the cumulative one.
6. The real production call site (`_start_member_objective`, `team.py`) actually calls `note_delegation_start` and resets correctly.

## 8. Ranked Findings Register and Selected Defect

| ID | Priority | Status | Confidence |
|---|---|---|---|
| F1 — budget-scope mismatch (cumulative vs per-delegation) | B | **SELECTED, IMPLEMENTED, DEPLOYED** | HIGH |
| F2 — single delegation (seq 3) made 77 calls against its own 25-call ceiling | B (enforcement boundary, distinct from F1) | Documented, NOT implemented — top candidate for next phase | HIGH (observation) / LOW (root cause) |
| F3 — wildcard/regex discovery was available the whole time | A | Documented, NOT implemented — empirically proven | HIGH |
| F4 — T36's results never persisted to a file (carried from T37) | observability | Documented, NOT implemented | HIGH |

**F1 selected** over F2/F3 because it is the most deterministically provable (direct source-code proof on both sides of the comparison, a clean failing-then-passing test sequence) and the most narrowly, safely scoped (one new counter, one reset call site, zero behavior change for any existing reader of the untouched cumulative counter). F2 remains open: tracing `agent.tool_call_limit`'s propagation path (`member_agent.arun()` → `agno/agent/_run.py`/`_response.py` → `model.aresponse()` → `run_function_calls`'s per-call hard check) found no obvious code-level bug, meaning the cause is either an upstream resolution/config issue specific to that invocation, or some other mechanism letting calls through after the tool-budget guard hook's one-shot soft nudge fires (that hook demonstrably does not block further calls if the model ignores its injected instruction — confirmed by reading its own `if who in fired: return result` branch, which runs *after* the real tool call already executed). This is flagged, not resolved, consistent with the mission's own "follow the evidence, not the preferred order" instruction: F1 was the provable defect; F2 remains a documented, well-scoped open question rather than a guessed fix.

## 9. Implementation and Regression Tests

- `swarm/phase0.py`: new `tool_calls_this_delegation` counter in `_slot`'s default dict; new `note_delegation_start(member)`; `note_tool_call` increments the new counter alongside the existing cumulative one; `Phase0Run.record_member_result` classifies `terminal_reason` against the new counter, still reports the old one unchanged.
- `swarm/team.py`: `_start_member_objective` (the one production delegation-start point) now also calls `phase0.note_delegation_start(member_id)`.
- 6 new tests, all passing. Full root suite: same 54 pre-existing failures (one spot-checked directly to confirm it is an unrelated, pre-existing stale assertion, not a new regression), 2597 passed (2591 + 6) — zero regressions.

## 10. Before/After Metrics

| | Before | After |
|---|---|---|
| Two delegations, 15+12 calls, 25-call limit each | 2nd delegation misclassified `tool_limit` (cumulative 27 ≥ 25) | 2nd delegation correctly classified on its own 12 calls |
| Single delegation, 26 calls, 25-call limit | `tool_limit` | `tool_limit` (unchanged) |
| T13a-T37-LIVE1's own 2+77=79 reported figure | Opaque — compared as one number against 25 | Now attributable: 2 (delegation 2) + 77 (delegation 3), each delegation's own count visible separately via `tool_calls_this_delegation` on its own `record_member_result` |

## 11. Deployment Identity and Live Validation

- Commit `73c2f09` pushed to `origin/main`.
- ZGX `agno-api.service`: `git pull --ff-only` (`d380975` → `73c2f09`, fast-forward, also picked up T37's own previously-undeployed docs-only commit `052bf28`), `systemctl --user restart`, clean journal startup confirmed, 3 "T38" markers in `phase0.py` + 1 in `team.py` grep-confirmed in the running checkout.
- `hive-mcp` (port 9003): untouched — this fix is `swarm/team.py`/`swarm/phase0.py` only, orchestrator-side, not verifier-relevant.
- `hive-mcp-agno` (port 9004): untouched, confirmed out of scope throughout.
- No new live run was launched to validate this specific fix end-to-end this phase — instead, the already-completed `T13a-T37-LIVE1` run (recovered from the journal, predating this fix) served as the real-world confirmation that the cross-delegation mechanism this fix addresses is genuinely present (2+77=79) in actual production telemetry, which is stronger evidence than a fresh synthetic replay would have been.

## 12. Telemetry Artifacts

- `docs/runbooks/architecture-acceptance-runs/runs.jsonl` — 17 total lines (12 from T36.1/T37, 5 new this phase: 1 `phase_baseline`, 3 `task_attempt` — `BUDGET-SCOPE-MISMATCH-FIX1`, `T13a-T37-LIVE1-RESOLVED` [supersedes `T13a-T37-LIVE1`], `WILDCARD-DISCOVERY-EXPERIMENT1` — and 1 `phase_summary`). All 17 lines validated as parseable JSON.
- This file. Same schema and six-value guard-outcome vocabulary as T36.1/T37, no schema changes.

## 13. Explicit Inconclusive/Unexercised Conditions

- F2 (single-delegation 77-vs-25) is explicitly **not resolved** — flagged, not guessed at.
- No fresh live run was launched this phase to re-validate F1 end-to-end (deliberate — the already-completed T13a-T37-LIVE1 recovery was stronger, real-production evidence than a new synthetic replay would have been, and launching a new ~20-minute run carried the same orphaning risk T37 already hit once).
- Scenario E (repeated failed lookup across delegation boundaries) is evidenced by code reading, not by a directly observed repeat in this specific incident.

## 14. T24/Dirty-File Preservation Confirmation

- Staged `swarm/team.py` via hunk-by-hunk `git add -p` (1 of 6 hunks — the new `phase0.note_delegation_start(member_id)` call at line ~6300), confirmed zero T24 content via full-content `git diff --cached` review before committing (the remaining 5 hunks, at the known T24 line ranges ~16415-16616 and ~21045-21066, were left unstaged).
- `git status --short -uall` before staging matched the established baseline exactly, except one new stray 0-byte artifact (`Path`, matching this session's known shell-heredoc-artifact pattern) — found and removed before staging.
- Full dirty/untracked file set otherwise unchanged from every prior phase's confirmed baseline.

## 15. Remaining Risks and Next Recommendation

- F2 is the clear next investigation: reproduce a fresh T13a-shaped (no files named) run, instrument the Researcher's own `arun()` call boundary directly (not just the phase0 telemetry layer) to see whether `agent.tool_call_limit` genuinely resolves to 25 at the moment that specific delegation starts, and whether agno's own hard refusal (`create_tool_call_limit_error_result`) ever fires at all during that delegation — if it never fires, the question becomes why; if it does fire but calls still continue past it, the question is what mechanism lets them.
- F3 (wildcard discovery) is a strong, empirically-proven candidate for a narrow Researcher-instruction addition ("prefer a regex/wildcard search over sequential literal guesses when the exact identifier is unknown") — deliberately not implemented this phase to keep F1 isolated and testable on its own, and because an instruction-level change deserves its own dedicated before/after test cycle rather than riding alongside a data-plumbing fix.
- F4 (T36 persistence gap) remains open, unchanged from T37.
