# Architecture Acceptance Run Telemetry

Durable, append-only record of live/integration/unit validation attempts for
AgnoHive architecture-acceptance phases (T36, T36.1, and future phases). Built
so a later evaluator can compare runs numerically instead of re-reading prose
summaries.

## Files

- `runs.jsonl` — one JSON object per line, append-only. Never rewritten or
  truncated; a correction is a NEW line referencing the original `attempt_id`
  via `supersedes`, never an edit in place.
- `T36.1-report.md` — the human-readable report for the T36.1 phase
  specifically (one such file per phase; see that phase's own report for the
  narrative).
- Future phases append more lines to the same `runs.jsonl` and add their own
  `<phase>-report.md` alongside this one.

## `runs.jsonl` schema

Each line is one JSON object. Fields absent from a given record are either
genuinely not applicable (omitted) or not exposed by the runtime, in which
case they are present with value `null` and a sibling `<field>_reason` string
explaining why (never fabricated).

### Record kinds

`record_type` is one of:
- `"phase_baseline"` — one per phase, written before any task attempt. Captures
  git/runtime identity at phase start.
- `"task_attempt"` — one per task attempt (including failed/orphaned ones).
- `"phase_summary"` — one per phase, written at the end.

### `phase_baseline` fields

```
record_type, phase, timestamp_utc, git_branch, git_head,
git_dirty_tracked_files, git_dirty_untracked_files,
hive_mcp_image_digest, hive_mcp_container_id, hive_mcp_health,
hive_mcp_verify_py_sha256,
zgx_agno_api_commit, zgx_agno_api_service_status,
zgx_team_py_sha256, zgx_verify_py_sha256,
notes
```

### `task_attempt` fields

**Identity/reproducibility**: `record_type="task_attempt"`, `phase`, `task_id`
(e.g. "T13a"), `attempt_id` (unique, e.g. "T13a-A1"), `supersedes` (prior
attempt_id this corrects, or null), `run_session_id` (the swarm's own
`[session: ...]` id, or null), `start_ts_utc`, `end_ts_utc`,
`elapsed_seconds`, `git_branch`, `git_head`, `deployed_runtime` (which
runtime/commit actually served this attempt), `task_prompt_sha256` (stable
hash of the exact prompt text, so prompt drift across runs is detectable),
`task_prompt_source` (where the prompt came from — original runbook line,
verbatim repeat, or a controlled fixture), `swarm_team`, `read_only`,
`fresh_session` (bool), `ground_truth_ref` (what was used to judge the
result).

**Execution/resource**: `final_status` (`completed|failed|timed_out|
orphaned|cancelled|inconclusive`), `accuracy_verdict`
(`PASS|FAIL|PARTIAL|NOT_VERIFIED`), `containment_verdict` (same enum),
`tool_call_count` (or null+reason), `tool_call_breakdown` (dict or
null+reason), `delegation_count` (or null+reason), `retry_count`,
`recovery_events`, `repetition_loop_events`, `input_tokens`, `output_tokens`,
`estimated_cost_usd` (or null+reason — this runtime does not expose per-call
cost, see note below), `wall_clock_seconds`, `phase_timing_breakdown` (or
null+reason), `verification_findings` (list of {category, severity, text}),
`guard_coverage` (list of {guard_name, state} — state is one of
`FIRED_EXPECTED|NOT_FIRED_EXPECTED|NOT_EXERCISED|FIRED_FALSE_POSITIVE|
MISSED_CONDITION|INCONCLUSIVE`), `error_type`, `error_summary_sanitized`,
`evidence_refs` (list of paths — transcript excerpt files under this
directory, never full raw logs with secrets), `final_disposition`,
`confidence_and_limitations`.

**Monitoring (for long-running live attempts)**: `progress_polls` (list of
{ts_utc, observed_state} — honest record of what polling actually saw, e.g.
"N worker processes running", not a claim of continuous visibility),
`monitoring_method` (prose — how progress was actually observed, e.g.
"journalctl + ps aux polling via zgx-workstation execute_command, no
tool-level progress API available for this runtime").

### `phase_summary` fields

`record_type="phase_summary"`, `phase`, `timestamp_utc`, `tracks` (dict of
track → verdict), `commits` (list), `total_attempts`, `evidence_refs`.

## Why no cost/token figures in T36.1

The EkamApp MCP (`agno_run`) result string includes a footer
(`[team: ... | ... | <seconds>s | turn: N | context: N msgs]`) but no
token/cost figures, and no tool was available this phase to query
per-request token/cost accounting from the underlying Ollama/vLLM/LiteLLM
stack for a specific run ID. Every `task_attempt` record with token/cost
fields sets them to `null` with `*_reason: "not exposed by agno_run's
result string; no per-run token/cost query tool available this phase"`
rather than guessing.

## Secrets / sanitization

No credentials, tokens, `.env` contents, or raw multi-hundred-KB logs are
stored here. `evidence_refs` point at short, hand-trimmed excerpt files
(this directory, `*-excerpt.md`) containing only the relevant lines, or at
paths outside this repo (e.g. ZGX's own journalctl) that a future evaluator
with access to that machine can re-query — never a dump of their content.
