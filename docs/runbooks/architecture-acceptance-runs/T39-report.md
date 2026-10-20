# PHASE T39 — SEARCH-FIRST DISCOVERY AND BOUNDED IDENTIFIER EXPLORATION REPORT

## 1. Executive Verdict

**PARTIAL.** Root-cause proof, implementation, and unit-level validation (baseline-first, all 13 new tests across both fixes) are complete and solid: **PASS**. Live end-to-end validation is **PARTIAL**, honestly reported as such rather than rounded up: a fresh live replay of the exact original T13a task confirmed the *targeted symptom* (a confident false claim that no frontend hooks exist) did **not** recur, but did not exercise the new escalation mechanism itself — this run's Researcher found the real hooks via a direct file read on its first action, never entering the literal-guessing pattern the fix exists to stop. The mechanism's own live firing therefore remains proven only at the unit level (7 tests, baseline-first against the real hook) plus the historical incident reconstruction, not a fresh live reproduction of the exact 77-call pattern.

Reconstructed the T37 incident's 77-call delegation chronologically, call by call, from ZGX journalctl. All 77 calls used a bare literal identifier — never a regex, alternation, or wildcard — against a correctly-scoped search. Ruled out "capability unavailable" and "capability undiscoverable" directly (the tool's own docstring demonstrates wildcard usage) and ruled out "wrong search scope" empirically (a wildcard search in the exact same scope finds 2 of 5 real hooks in one call, via `page.tsx`'s own import line). The earliest proven failure boundary is an **ignored existing control**: `_BATCH_ALTERNATIVE_HINTS` already recommends exactly the right fix and fired 9 times across this exact run — and was never adopted once. Implemented a deterministic escalation of that same mechanism, reusing the same `_force_text_only` actuator the budget guard hook already proves works in this exact incident's own journal. Separately, while reconstructing the incident, found and fixed an independent, smaller defect: the "79 vs 25" framing itself was a telemetry case-sensitivity bug, not a real budget violation — the real, correctly-enforced limit was 80, and the model legitimately used 79 of it. The live replay also surfaced a genuinely new, independent, already-contained defect (§8, §11) for the next phase.

## 2. Baseline Incident Reconstruction

Session `fd5bf842c52c`, delegation seq 3 ("List all frontend hooks in the vouchers module"), 10:34:21–10:52:49 (18m28s), 77 `search_files` calls, all literal, 77 distinct patterns (zero repeats — corrects an earlier hypothesis in this session's own T38 work that assumed ~17 in-delegation repeats). Full chronological trace recovered via a parsing script over the ZGX journal:

| Calls | Shape | Example | Result |
|---|---|---|---|
| 1 | an unrelated literal guess | `useClientHook` | no match |
| 2–4 | generic scan terms, not hook names | `const`, `function`, `use` | hit unrelated lines in page.tsx (PER_PAGE, function decl, 'use client') |
| 5 | a real React hook that IS used | `useState` | hit (the import line) |
| 6–22 | every other real React hook, tried individually | `useEffect`, `useContext`, `useReducer`, ... | no match (not called in this file) |
| 23–30 | real TanStack/React-Query hooks — wrong library for this codebase (RTK Query) | `useQuery`, `useMutation`, ... | no match |
| 31–77 | a purely mechanical `useReactQuery` + suffix combinatorial expansion, then the same 29 suffixes again with `Provider` appended | `useReactQueryCancelProvider`, ... | no match, all 47 calls |

Ground truth (`Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts`): 5 real hooks — `useGetVouchersQuery`, `useGetVoucherQuery`, `useCreateVoucherMutation`, `usePostVoucherMutation`, `useCancelVoucherMutation`. 2 of them (`useGetVouchersQuery`, `useCreateVoucherMutation`) are imported and called directly in `page.tsx` (lines 6/33/34) — **inside the exact `glob_filter` scope all 77 calls already used.**

Final answer (recovered from the journal — this run completed 13 minutes after T37's client-side timeout gave up on it): *"the vouchers module frontend ... does not define any custom hooks beyond standard React hooks like useState."* **Wrong** — this is `T13a-T37-LIVE1`'s definitive **FAIL** verdict, per T39's own mission statement.

## 3. Root Cause

The earliest *provable* failure boundary is not "the model can't search" or "the model doesn't know search_files supports regex" — both are directly falsified (§4). It is: **a correctly-targeted, already-existing advisory control was ignored across its entire firing window.**

`_BATCH_ALTERNATIVE_HINTS["search_files"]` (`swarm/team.py`, built specifically for "a run searching 34 different terms") fires every 8th call (`_BATCH_HINT_EVERY = 8`) with: *"search_files takes a REGEX: one call with an alternation — search_files('first|second|third', glob) — covers many terms at once."* Across this exact 77-call delegation it fired at calls 8, 16, 24, 32, 40, 48, 56, 64, 72 — **9 times**, every time with the literally correct fix — and the model never adopted it, not once.

## 4. Hypothesis Table

| Hypothesis | Verdict | Evidence |
|---|---|---|
| A — search capability unavailable | **REJECTED** | `search_files` is a real, registered, fully regex-capable tool; used by this same role 77 times in this exact run |
| B — capability undiscoverable | **REJECTED** | `search_files`'s own docstring (`hive-mcp/tools/context.py`) explicitly states "Search file contents with a regex pattern" and gives a wildcard example (`'import.*React'`) |
| C — poor tool selection | **CONFIRMED as a contributing factor**, not the earliest boundary | all 77 calls were literal; the model never once tried an alternation on its own initiative |
| D — search results/advice ignored | **CONFIRMED as the earliest-proven boundary** | the existing hint fired 9 times with the exact right fix text and was never adopted |
| E — inadequate search construction | not applicable here | the calls weren't badly-constructed regexes; they were never regexes at all |
| F — evidence/context loss | **REJECTED** | nothing was found to lose; the 2 real hooks present in scope were simply never searched for correctly |
| G — missing exploration control | **REJECTED as "missing"; REFINED as "present but non-deterministic"** | the control existed and fired correctly; it only lacked a hard stop for sustained non-compliance |
| H — task ambiguity | **REJECTED** | the task's own directory scope was correct and sufficient; no broader exploration was genuinely required for the 2 discoverable hooks |

Scope was also directly tested and found **not** to be the problem: `grep -rnE 'use[A-Za-z]*(Query|Mutation)' .../vouchers/` (the EXACT glob_filter the real run used) finds `useGetVouchersQuery` and `useCreateVoucherMutation` in one call.

## 5. Track A vs Track B

**Track A (existing behavior)** is the real incident itself (§2) — not re-run, since it is already fully and precisely reconstructed from production telemetry, and re-running the identical failure live would only re-incur its own ~18-minute cost without adding information already in hand: 77 calls, 0 real hooks found, FAIL answer.

**Track B (search-first / with the fix)**: implemented as a deterministic escalation rather than a prompt-only experiment, per Decision Gate A's explicit preference ("prefer a control intervention... investigate a minimal deterministic control" once prompt-only behavior is shown unreliable — which the 9-times-ignored hint already demonstrates). Unit-tested directly against the real hook (`tests/test_t39_literal_search_probe_escalation.py`):

| | Track A (real incident) | Track B (fixed mechanism, unit-reproduced) |
|---|---|---|
| Calls before stop | 77 | ≤20 (`_LITERAL_SEARCH_PROBE_LIMIT`) |
| Pattern type throughout | 100% literal | — (mechanism forces an answer, doesn't itself change the model's next pattern choice) |
| Real hooks found | 0 of 5 | not directly comparable — see §8 for what the LIVE replay's own hook-discovery behavior does |
| Final answer | "no custom hooks exist" (FALSE) | forced to answer honestly using what was gathered, explicitly instructed to mark unconfirmed items UNRESOLVED rather than claim absence |

A live, fresh replay of the identical task against the deployed fix is recorded in §8.

## 6. Implementation

**Primary fix** (`swarm/team.py`, commit `3134f97`): `_LITERAL_SEARCH_PROBE_LIMIT = 20`, `_LITERAL_PROBE_PATTERN_RE` (matches a bare `\w+` identifier — no regex metacharacter), a new `literal_probe_streak` dict (same keying convention as the existing `batch_counts`) inside `_make_read_cache_tool_hook`. Increments on each fresh, zero-match (`result.startswith("No matches for:")`), literal-pattern `search_files` call; resets on any match or any pattern containing a regex metacharacter (a compliance attempt is never punished for also missing). At the limit, calls `_force_text_only(agent, team=None)` — the same actuator the budget guard hook already uses, and that this exact incident's own journal independently confirms genuinely stops further tool calls (`[budget] Researcher: call 79/80` was immediately followed by zero more calls) — and appends an explanatory note instructing the model to answer now, marking unconfirmed items UNRESOLVED rather than claiming absence.

This is the smallest justified fix: no new denylist, no hardcoded hook names, no new generic guard class, no change to repetition detection, capability checks, or `forward_member_answer`. It is an escalation of an *existing*, already-correctly-targeted mechanism, reusing an *existing*, already-proven actuator.

**Secondary, independent fix** (`swarm/model_routing.py`, commit `66d0114`): found while reconstructing the incident's exact numbers. The real, agno-enforced `tool_call_limit` for the Researcher in this run was **80** (`[budget] Researcher: call 79/80` — directly in the journal), not 25 as every T37/T38 telemetry record reported. Root cause: `phase0.record_member_result`'s own call site resolves the role through `_member_key()` first (lowercased, e.g. `'researcher'`), and the DB-backed `_role_policy_cache`'s exact-string lookup on that lowercased key silently missed the entry keyed by the DB's natural casing (`'Researcher'`), falling through to `config.tool_call_limit`'s global default (25). Fixed with a separate, lowercase-keyed fallback index (`_role_policy_cache_ci`), consulted only when the exact-case lookup misses — the primary cache's own keys, and therefore `reload()`'s admin-facing diff output, are untouched. **This means the T37 incident was never an 80-call-ceiling violation** — the model legitimately used 79 of its real 80-call budget and was correctly stopped one call early, exactly as designed.

## 7. Tests

- `tests/test_t39_role_policy_case_mismatch.py` — 6 tests, all first proven against the raw baseline API with no test helper (`get_role_policy("engineering", "researcher")` returned `None` pre-fix). Full model_routing/phase0/T38 regression set: 73 passed.
- `tests/test_t39_literal_search_probe_escalation.py` — 7 tests, driving the real `_make_read_cache_tool_hook` closure directly (same discipline as the existing `test_team_read_cache_hook.py` suite). All first proven against the pre-fix committed `team.py` (`ImportError: _LITERAL_SEARCH_PROBE_LIMIT` does not exist — confirmed by temporarily swapping in the last-committed file and re-running, then restoring). Covers: the exact incident shape forces text-only strictly before 77 calls; a real match resets the streak across repeated near-threshold cycles; a regex/alternation attempt resets the streak even if it also misses; the 9-endpoint legitimate enumeration shape stays silent; the forcing note's exact content; interleaved different-tool calls don't interfere; per-member isolation.
- Full root suite, both fixes combined: **same 54 pre-existing failures** (one spot-checked directly as a known, unrelated, stale hook-count assertion; another confirmed as T24's own in-progress, intentionally-uncommitted state), **2610 passed** (2597 baseline + 6 role-policy + 7 literal-probe) — zero regressions.

## 8. Live Validation

A fresh, read-only `engineering`-team run of the verbatim original T13a prompt was launched against the deployed fix (`3134f97`) via `mcp__ekamapp__agno_run`. Run ID `d6f4d4a1b3cc`, session `3deacd12-87b9-4bdf-8feb-86c750207b46`, 784.2s, 5 delegations. Full structured result: telemetry record `T13a-T39-LIVE1`.

**The targeted symptom did not recur.** The Researcher's delegation for "list all frontend hooks" opened with `get_file_content` directly on `page.tsx` — not a single `search_files` guess — and both real hooks were in the captured evidence on the first call. The final answer's own "Frontend Hooks" section, and the Researcher's raw, verbatim, unedited forwarded text, both correctly state:

> `useState`, `useGetVouchersQuery` (fetching voucher data), `useCreateVoucherMutation` (creating new vouchers) — "These are the only hook declarations found in the file."

This matches ground truth exactly (2 of 5 real hooks are wired into this specific page; the other 3 genuinely have no frontend consumer). No false "no hooks exist" claim anywhere in the result.

**The new mechanism was not exercised.** Because the model never entered a sustained literal-guessing pattern, `literal_probe_streak` had no occasion to fire. This run confirms the *symptom* is gone in this one sample; it does not independently confirm the *mechanism* fires correctly live (that remains unit-proven only).

**A different, independent, pre-existing defect surfaced and was correctly contained.** A later reconciliation retry (triggered by an unrelated 9-vs-5 endpoint miscount in the first-pass answer) had its own re-delegation blocked by the duplicate-delegation gate ("STOP: already asked 3 times"). Unable to re-read the file again, the Coordinator generated backend endpoint names (`get_vouchers`, `update_voucher`, `delete_voucher`, `get_voucher_by_id`, `apply_voucher`, `validate_voucher`, `deactivate_voucher`, `reactivate_voucher`) and database table names (`vouchers`, `voucher_redemptions`, `voucher_validity`) from the model's own prior knowledge instead of falling back to its own first-pass answer's correct content for those sections. The run's own pre-existing `verify_claims` layer caught this precisely — 13 of 15 checked symbols flagged `NOT IN FILE` — and prefixed the entire returned answer with an explicit `UNRESOLVED` banner naming every contradicted claim, rather than returning the fabrication silently. This is real existing containment working exactly as designed, for a defect this phase did not cause and is not scoped to fix — logged as finding F3 (§11) for the next phase.

## 9. Telemetry

`docs/runbooks/architecture-acceptance-runs/runs.jsonl` — records appended this phase: 1 `phase_baseline`, `ROLE-POLICY-CASE-MISMATCH-FIX1`, `LITERAL-SEARCH-PROBE-ESCALATION-FIX1`, `T13a-T39-LIVE1`, 1 `phase_summary`. Same schema and six-value guard-outcome vocabulary as T36.1/T37/T38 — no schema changes.

## 10. Preservation

- `git status --short -uall` checked before and after every commit this phase; matched the established baseline exactly except 3 new stray 0-byte artifacts (`None`, `tuple[str`, `run_function_calls's`, plus an earlier `Path`), all removed before staging, consistent with this session's long-documented shell-heredoc artifact pattern.
- `swarm/team.py` staged via hunk-by-hunk `git add -p` for both the T38 and T39 commits touching it; T24's own 5 hunks (identified by `# T24:` comments and known line ranges) left unstaged and uncommitted both times, confirmed via `git diff --cached | grep -c "T24:"` returning 0.
- `swarm/model_routing.py`, `swarm/phase0.py`, and all new test files have no T24 content at all (confirmed by file identity — T24's own documented footprint is `swarm/execution_context.py` and specific `swarm/team.py` hunks only).
- `test_evidence_fidelity.py::test_not_called_from_verified_answer_or_any_live_pipeline_function` failing in the full suite is T24's own in-progress, intentionally-uncommitted state (confirmed already present in the pre-T39 baseline), not a T39 regression.

## 11. Remaining Risks

- **F3 — reconciliation-after-duplicate-block fabrication (new this phase, not fixed).** When an evidence-integrity reconciliation retry's own re-delegation is refused by the duplicate-delegation gate, the Coordinator generates replacement content from the model's own prior knowledge rather than falling back to its own earlier, correct, first-pass answer for the blocked section — observed live, caught correctly by `verify_claims`, answer marked `UNRESOLVED` rather than shipped silently. Independent of T39's own mission; not implemented per the one-primary-defect discipline.
- The live replay's own final answer quality (§8) depends on live model behavior this phase's fix does not fully control — the fix guarantees the runaway STOPS early and the model is told to answer honestly with UNRESOLVED markers; it does not guarantee the model then independently discovers the hooks via a smarter search strategy on its own initiative. Both an honest partial/UNRESOLVED answer and a fully-correct answer are acceptable outcomes of this fix; a confidently-wrong "no hooks exist" claim is not.
- `_LITERAL_SEARCH_PROBE_LIMIT = 20` is a judgment call (two full advisory cycles above the 9-endpoint legitimate case, far below the 77-call runaway) — not re-derived from a larger corpus of legitimate-enumeration shapes beyond the ones this codebase's own `_BATCH_HINT_EVERY` comment already names.
- The deeper "why does the model ignore a correctly-targeted hint 9 times in a row" question (an LLM behavior/attention question, not a code defect) is not answered by this phase and is out of scope for a deterministic control-plane fix.
- The primary fix's own live firing is not yet directly observed — only unit-proven and historically reconstructed. A fresh replay that happens to reproduce the literal-guessing pattern would close this gap.

## 12. Next Recommendation

**Investigate F3 next** (reconciliation-after-duplicate-block fabrication): reproduce deliberately by forcing a reconciliation retry into the duplicate-delegation gate on a known file, and confirm whether the Coordinator consistently discards its own correct first-pass content for the blocked section. If confirmed as a recurring pattern (not a one-off), the smallest likely fix is having the reconciliation path fall back to the ALREADY-PRODUCED first-pass answer for any section it cannot re-verify, rather than regenerating it from scratch. Secondary: run a handful of additional live T13a replays to see whether `literal_probe_streak` actually fires in practice and whether `_LITERAL_SEARCH_PROBE_LIMIT` needs tuning from real-world data beyond this one incident.
