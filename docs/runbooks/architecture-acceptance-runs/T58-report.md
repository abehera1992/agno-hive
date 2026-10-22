# T58 — Semantic Non-Progress Detection and Bounded Recovery

Builds on, and does not overwrite, prior reports through `T57-report.md`.
Commit `7b9cf74644e7884446e62cbc1c2fc65563c1776f` (local `main` and
`origin/main`), deployed to ZGX `agno-api.service`. No change to
`hive-mcp/tools/verify.py`, `hive-mcp`, or `hive-mcp-agno`.

## 1. Verdict

**PASS**, on harness evidence against the real functions (both locally
and directly against the deployed artifact on ZGX) that demonstrates the
specific, precisely-traced defect is closed, plus one natural live run
that confirmed non-regression of the pre-existing mechanisms this phase
builds on. That natural run's own ending was caused by an unrelated client
-side disconnect (this session's own MCP tool timeout, not a model or
server failure), so it is reported as non-regression evidence only, not
as natural confirmation of the new detector — disclosed honestly, not
overstated.

## 2. Confirmed findings from `kpj33b99v` vs. unresolved hypotheses

**Confirmed, from the still-retained ZGX journal** (not reconstructed from
memory — the raw per-event log for this exact run, session `247bc117`,
was still present and was read directly):

- The retry spent roughly 5 minutes 45 seconds of its ~19-minute run
  (15:59:26–16:05:11) issuing `search_files` calls against
  `business_api.py` and then `businessApi.ts`, each with a **different**
  regex pattern, narrated each time as "let me try a different
  approach"/"let me try a different pattern"/"let me try to get the
  endpoints by looking for X".
- The **last six consecutive** `search_files` calls (16:03:53 through
  16:05:11), despite five different patterns (`use\w+Query|use\w+Mutation`
  then four separate `use\w+` attempts), **all returned the byte-identical
  200-char result preview**: `businessApi.ts:196:   useGetEmailCredentialsQuery,`.
  The model never advanced past the first match in its own tool's
  truncated preview.
- **Why T16 never saw this**: `_looks_like_repetition_loop`/
  `_looks_like_repetition_decay` scan only the narrated TEXT content
  stream, in 10-second windows. The narration bridging these calls is
  different wording every time (confirmed: no two "let me try..."
  sentences in this run are verbatim or near-verbatim repeats of each
  other), so the text-level detector correctly found nothing to flag —
  the actual non-progress is entirely inside the TOOL-CALL event stream,
  which that detector never inspects.

**Unresolved, not speculated on**: why the model kept retrying patterns
against the same file instead of widening its own search or reading the
file directly once a pattern match confirmed the target. This phase does
not claim to know the model's own internal reasoning for choosing that
strategy — only that the tool-call stream shows no evidence it was
working, and a deterministic signal exists to recognize that without
needing to know why.

## 3. Root cause of the control gap

`_evidence_integrity_check`'s reconciliation retry (and, more generally,
any call through `_stream_team_run`) had no mechanism that looked at
consecutive tool-call results at all — only at the narrated text and, via
T57, at whether the Z16 textual-repetition flag was set. A sustained
"keep trying different search patterns, get the same answer" loop,
expressed almost entirely through tool calls rather than repeated
narration, was therefore invisible end to end: not to the detector, and
(before T57) not to the completion-integrity gate either.

## 4. Files and functions changed

All in `swarm/team.py`:

- New module constants `_EXPLORATORY_TOOLS` (`search_files`,
  `count_matches`, `find_files` — discovery tools only; never
  `get_file_content`/`get_files_batch`, which retrieve a known target and
  are not prone to this failure mode) and `_SEMANTIC_STALL_THRESHOLD = 4`
  (reuses `config.liveness_repetition_threshold`'s own "max observed
  benign streak 3, so 4 leaves one firing of margin" philosophy —
  not independently re-derived; the live incident ran six identical
  -preview calls before its own outer budget ended it, so 4 stops two
  firings earlier).
- `_stream_team_run`: new local state (`last_exploratory_preview`,
  `semantic_stall_count`, `semantic_stall_stopped`) alongside the existing
  Z16 `repetition_stopped` state. On each tool "end" event for an
  exploratory tool, compares `result_preview` against the immediately
  preceding exploratory call's preview; a byte-identical match increments
  the counter (logged each time, with the running count and threshold),
  any different preview resets it to 0. At threshold, stops the generation
  in-process (same `last_good_len` truncation shape as Z16's own branch)
  and sets a **distinct** flag, `team._semantic_stall_truncated` — never
  merged into `_repetition_truncated`, so the reason stays diagnosable.
- `_complete_repetition_truncated_answer` (T57's existing bounded
  -completion mechanism, reused again, not duplicated): entry check now
  recognizes either `_repetition_truncated` or `_semantic_stall_truncated`
  as a trigger, computes a `truncation_reason` string ("repetition" or
  "semantic stall"), and threads it through every one of its own
  diagnostic print statements so the two reasons are distinguishable in
  the logs. Both flags share the SAME one-shot `_REPETITION_COMPLETION_FLAG`
  budget — not a second, parallel one. The nested-truncation check (the
  completion's own `_stream_team_run` call tripping a second stop) now
  also recognizes either flag.
- `_evidence_integrity_check` (T57's own wrapper block): `was_truncated`
  now checks either flag before calling `_complete_repetition_truncated_answer`,
  so the same budget-already-exhausted gap T57 closed for textual
  repetition is also closed for a semantic-stall trigger.

## 5. How legitimate progress is distinguished from semantic non-progress

Deliberately the narrowest deterministic signal the evidence supports, per
the mission's own preference for runtime signals over a second model
-judge: **the same exploratory tool returning the exact same result
preview it returned on its own immediately preceding call, regardless of
whether the arguments (typically the search pattern) differ.** This needs
no args parsing, no semantic classification, and no model call. A
genuinely different preview — even from the same tool, even one call after
a stall streak started — immediately resets the counter, so exploration
that keeps finding something new is never penalized (confirmed in harness
Scenario 2 below). The detector is scoped to three discovery-tool names
only; `get_file_content`/`get_files_batch` calls, however repeated, never
contribute to or reset this counter (confirmed in Scenario 4).

Explicitly NOT implemented, per the mission's own list of insufficient
signals: no scoring of "different wording," no counting of distinct
approach-announcements, no flagging based on identifier/filename mentions
alone, and no model-based semantic judge — a second LLM call to classify
"is this progress" would add real latency and cost on every retry, and its
own judgment could itself hallucinate progress that was not made; the
deterministic preview-equality check has neither failure mode and was
sufficient to explain the recorded incident without it.

## 6. Recovery behavior, limits, completion-gate decisions, diagnostics

Recovery reuses T57's existing `_complete_repetition_truncated_answer`
unmodified in its own mechanics: one bounded completion pass, built from
`_checkpoint_block(team)` (the run's own already-gathered evidence and
member results, never the raw repetitive transcript), sharing the single
one-shot completion budget across both trigger reasons. If that pass
itself stalls or repeats, it is disclosed as still incomplete
(`_TRUNCATED_ANSWER_NOTE`) rather than adopted silently — unchanged from
T57's own design, now also reachable via the semantic-stall trigger.
Diagnostics: every print statement in both the detector and the completion
path names the specific reason (`"semantic stall"` vs. `"repetition"`),
the running stall count and threshold, and (in the detector) the actual
truncated preview that repeated — enough to reconstruct, from logs alone,
why an intervention fired without needing to re-read the full transcript.

The completion gate remains authoritative, per the mission's Section 5:
neither this phase's new detector nor the existing textual one can mark a
result COMPLETE on their own — they can only request the bounded
completion pass; `_TRUNCATED_ANSWER_NOTE`'s presence or absence after that
pass is what every downstream caller actually checks.

## 7. Focused validation — local and deployed evidence, clearly separated

No unit-test file was added, modified, or run; no test suite was executed.

**Local scratch harness** (`t58_semantic_stall_test.py`,
`t58_completion_provenance_test.py`, this session's scratchpad directory),
run against the real, imported `_stream_team_run` and
`_complete_repetition_truncated_answer` (not reimplementations), with a
fake `team.arun()` async generator constructing the exact event sequence
`_stream_event_to_chunk` expects:

1. **Positive — the real incident shape**: five different `search_files`
   patterns, the last four returning the byte-identical preview from the
   real incident. Correctly trips at the 4th identical preview;
   `_semantic_stall_truncated=True`, `_repetition_truncated=False`. PASS.
2. **Legitimate exploration**: five `search_files` calls, each with a
   genuinely different preview. Never flagged. PASS.
3. **Below threshold**: three identical-preview calls (threshold is 4).
   Not yet flagged. PASS.
4. **Non-exploratory tool**: `get_file_content` called six times with an
   identical preview. Never flagged — confirms the detector is scoped to
   discovery tools only. PASS.
5. **Completion provenance**: a team with only `_semantic_stall_truncated`
   set correctly triggers `_complete_repetition_truncated_answer`, labels
   its own diagnostics `"semantic stall-truncated"` (not
   `"repetition-truncated"`), and clears both flags on completion. PASS.
6. **No-trigger no-op**: neither flag set — confirmed `_stream_team_run`/
   `_complete_repetition_truncated_answer` are never even called into
   their trigger branch. PASS.

T16's own textual-repetition/decay branch was **not** independently
re-exercised in this harness: it is gated on a real 10-second
`time.monotonic()` window, which a fast, synchronous fake-event harness
cannot trigger without faking the clock. Its preservation is confirmed by
diff inspection instead (zero lines changed in
`_looks_like_repetition_loop`/`_looks_like_repetition_decay` or the
periodic-check branch that calls them) and, independently, by the natural
live run in §8, where it fired twice and was handled correctly.

**Deployed-path exercise**, clearly distinguished from the harness above:
on ZGX, using `/home/abehera1992/miniforge3/envs/zgx/bin/python` (the exact
interpreter `agno-api.service` runs under) with `swarm.team` imported
directly from `~/agno-hive` at the confirmed deployed commit, the same
positive and negative-control scenarios were re-run against the real
deployed module:

```
DEPLOYED semantic_stall_truncated: True
DEPLOYED repetition_truncated: False
DEPLOYED negative control stall (must be False): False
DEPLOYED CODE CONFIRMED WORKING -- both scenarios passed
```

## 8. Natural live run

One natural attempt: MCP task `ktn6qc6gh`, session `e174f022a540`
(confirmed from `/phase0` log lines). It genuinely ran for over 11
minutes against the deployed commit and surfaced three separate, real
events in sequence, all independently confirmed from the journal:

1. Z16's textual-repetition detector fired on the **main generation**
   (`[team] repetition threshold reached (4 >= 4)`), cutting it off at
   12,992 chars.
2. The resulting completion pass (`[repetition-completion]` log label —
   `_complete_repetition_truncated_answer`'s own retry) **itself** hit
   Z16's textual repetition a second time and was itself cut short —
   exactly the nested-truncation case T57 built handling for, confirmed
   firing correctly.
3. `_evidence_integrity_check`'s own retry (`[evidence-integrity]` log
   label) then began a clean, steadily-progressing re-listing of the
   endpoints and hooks with no repetition and no stall detected by either
   mechanism — until this session's own MCP client call to `agno_run`
   hit its own timeout and disconnected, which `api/server.py` correctly
   logs as `[api] client disconnected — worker process killed, GPU work
   aborted`. This is a SELF-inflicted interruption of this validation
   session's own tooling, not a server or model failure, and not evidence
   about this phase's fix either way.

**This run did not naturally exercise the semantic-stall detector** — its
own failure mode this time was textual repetition, not the tool-call
-preview staleness this phase targets — but it is valuable non-regression
evidence: T16 and T57's existing mechanisms fired correctly, twice, with
this phase's new code present and deployed, and the new detector
correctly stayed silent throughout since the actual symptom this run hit
was not the one it watches for.

## 9. Deployment, service health, preservation

- Commit `7b9cf74644e7884446e62cbc1c2fc65563c1776f` pushed to
  `origin/main` (`8385c34..7b9cf74`).
- ZGX: `git pull` fast-forwarded `bfb3a34..7b9cf74`, confirmed via `git
  rev-parse HEAD` matching exactly, both before deployment and during the
  §7 deployed-path exercise.
- `systemctl --user restart agno-api.service`: new PID `655519`,
  `/health` returning `{"status":"ok", ...}`.
- No active run before the restart (`GET /runs` empty, no `run-worker`
  process).
- `hive-mcp`/`hive-mcp-agno`: not touched — no source change.
- `git status --short` before and after this phase's work shows only
  `swarm/team.py` newly dirty; the same pre-existing dirty/untracked
  files recorded in every prior report remain exactly as found. Two
  stray, self-created, empty (0-byte) artifact files
  (`_SEMANTIC_STALL_THRESHOLD`, `str`) appeared from shell-parsing side
  effects of commands run this session; both were inspected and removed
  before staging, never staged or committed.

## 10. Remaining risks and smallest justified follow-up

- **`run_task_async`'s own separate copy of the Z16 logic** (not involved
  in the `kpj33b99v` incident, which went through `_stream_team_run`
  specifically) does not have the semantic-stall signal. If a future
  incident shows the SAME tool-call-preview-staleness pattern occurring in
  the main top-level generation rather than inside a retry, the smallest
  justified follow-up is porting this exact signal (not a redesign) to
  that copy.
- **No naturally-occurring live run reproduced the exact semantic-stall
  trigger this phase closes.** The deployed-path exercise is strong,
  direct evidence against the real artifact, but is not the same class of
  proof as an organic reproduction; the one natural attempt available this
  phase hit a different (textual) failure mode instead, and ended via an
  unrelated client disconnect before it could run long enough to possibly
  reach a semantic-stall scenario of its own.
- **The threshold (4) is reused from an existing, differently-grounded
  constant, not independently tuned against a corpus of semantic-stall
  incidents** — there is exactly one recorded incident to calibrate
  against. If a future incident shows 4 is too eager (flags legitimate,
  slow-but-real exploration) or too lax (lets a longer stall run), that is
  the evidence a narrow threshold adjustment would need, not a reason to
  broaden the detector's scope.
