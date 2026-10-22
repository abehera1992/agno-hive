# T57 — Repetition Recovery and Completion Integrity

Builds on, and does not overwrite, prior reports through `T56-report.md`.
Commit `bfb3a348a43f684eda3bcc3ca2d514889e82d78f` (local `main` and
`origin/main`), deployed to ZGX `agno-api.service`. No change to
`hive-mcp/tools/verify.py`, `hive-mcp`, or `hive-mcp-agno`.

## 1. Verdict

**PASS**, on the strength of harness evidence against the real functions
(both locally and against the deployed artifact on ZGX) that directly
demonstrates the specific defect is closed, plus one natural live attempt
that exercised the broader pipeline without regression. The natural
attempt did not reproduce the EXACT T56 repetition-truncation shape (it hit
a different, unrelated exploration-loop issue instead, correctly handled
by pre-existing, unrelated safeguards) — disclosed honestly in §5, not
claimed as direct natural confirmation of this phase's specific fix.

## 2. Confirmed root cause vs. unresolved hypotheses

**Confirmed, by direct source trace:** `_evidence_integrity_check`'s own
one reconciliation retry calls `_stream_team_run`, the same streaming
helper whose Z16 repetition guard can cut a generation short and set
`team._repetition_truncated = True`. Nothing in `_evidence_integrity_check`
ever read or acted on that flag — the retry's text (`retried`) was judged
purely by whether `_evidence_integrity_findings` still found a
contradiction in it. A generation cut off mid-restatement, with no
contradiction surviving in its truncated tail, was therefore adopted as a
complete, correct answer. This is an EXACT, source-confirmed match for
T56's own reported incident (run `kzpp5fqwh`).

A second, closely related gap was found while tracing the fix, not
assumed: `_complete_repetition_truncated_answer` (the EXISTING completion
mechanism this phase reuses, previously only invoked once per run for the
MAIN draft) has its own blind spot — when its one-shot completion budget
(`_REPETITION_COMPLETION_FLAG`) was already spent by an earlier stage in
the same run, it returns the truncated text completely UNCHANGED, with
none of its own usual `_TRUNCATED_ANSWER_NOTE` disclosure, because it never
reaches the code path that would add it. Confirmed by direct read of the
function (lines 7247-7251) and reproduced in harness Scenario 3/B below.

**Unresolved, not speculated on:** the INITIATING cause of the repetition
itself (why the model's generation starts repeating at all) remains
unestablished, exactly as the mission's own framing states, and this phase
does not claim otherwise. The one natural live attempt this phase made
surfaced a DIFFERENT, separate symptom (a "let me try a different
approach" exploration loop that evaded the character-level repetition
detector's exact-repeat/decay thresholds entirely, landing instead on an
incorrect frontend path before self-correcting) — evidence that at least
one class of unproductive-generation pattern exists that T16's detector
does not catch at all. This is noted as a distinct, unresolved finding in
§6, not folded into or conflated with the repetition-truncation defect
this phase fixes.

## 3. Implementation

`swarm/team.py`, `_evidence_integrity_check`, 45 lines inserted
immediately after the retry's own `_stream_team_run` call (within the
existing `try/except/finally` already there), zero existing lines
modified or removed:

- Captures `was_truncated = getattr(team, "_repetition_truncated", False)`
  and `pre_completion = retried` immediately after the retry returns.
- Calls `_complete_repetition_truncated_answer(retried, task, team, [],
  None, liveness_path)` — reused completely unmodified. `result=None` and
  a throwaway `[]` list are safe: `_count_read_calls` (and therefore
  `_more_grounded`/`_adopt_retry`, which that function calls internally)
  treats a `None` result as "undeterminable", its own documented fallback
  that always favors adoption — confirmed by direct read, not assumed.
  `_evidence_integrity_check` has no `result` object of its own to pass in
  the first place; it is content-in, content-out.
- Closes the shared-budget-exhausted gap found in §2: if `was_truncated`
  was true, the returned text is byte-identical to what was passed in, it
  carries no disclosure note, AND it does not already read as a natural
  stopping point (`_ends_at_a_natural_stopping_point`, reused unmodified),
  the disclosure note is appended here.
- If the text carries `_TRUNCATED_ANSWER_NOTE` after this (whether added by
  `_complete_repetition_truncated_answer` itself or by the gap-closing
  check above), the retry is discarded entirely — not adopted, not
  stripped, not recomputed against — falling back to the EXISTING
  disposition the "retry produced nothing usable" branch a few lines below
  already uses: `_force_uncertainty_answer(content, findings)`, keeping the
  pre-retry draft, which was already `verify_claims`-checked and known
  complete before `_evidence_integrity_check` ever ran.

Deliberately reuses `_REPETITION_COMPLETION_FLAG` — the same one-shot
budget `_verified_answer`'s own upfront call already spends from for the
MAIN draft — rather than a second, parallel completion budget. At most one
repetition-completion pass happens per run, whichever stage needs it
first. This directly satisfies the mission's "keep finite retry and
execution budgets; do not introduce unlimited retries" constraint by
construction, not by a separate new limit.

## 4. Safety boundaries / preservation

- **T16** (the character-level repetition/decay detector,
  `_looks_like_repetition_loop`/`_looks_like_repetition_decay`, and the
  `last_progress_at`/`repetition_count` tiering inside `_stream_team_run`
  itself): zero lines touched. This phase only reads a flag that
  mechanism already sets; it changes no detection threshold, no tier, no
  stop condition.
- **T56** (`_route_identity_match`, `_cmp_confirmed_mounted_routes`,
  `_mounted_route_falsely_gapped`, and their call sites inside
  `_integrity_named_item_falsely_gapped`/`_strip_falsely_gapped_list_items`):
  zero lines touched. This phase's insertion is entirely inside the
  retry-handling block of `_evidence_integrity_check`, upstream of where
  those T56 functions are called later in the same function.
- **T7** (`_task_wants_extension_count`/`_authoritative_listing_restatement`,
  in `_verified_answer`'s miscount-handling block) and **T12**
  (`repair_unguarded_draft`'s recovered-member-results disclosure, in
  `api/server.py`): zero lines touched in either.
- **No new retry budget, no raised threshold, no disabled guard**: the fix
  reuses an existing, already-bounded completion mechanism and an existing
  one-shot flag; it never raises `config.liveness_repetition_threshold`,
  never adds a loop, never retries more than the one bounded pass that
  mechanism already allows.

## 5. Focused verification

No unit-test file was added, modified, or run; no test suite was executed.
A scratch harness (`t57_repetition_completion_test.py`, this session's
scratchpad directory) was run against the real, imported
`_complete_repetition_truncated_answer` (not a reimplementation), with
`_stream_team_run` monkey-patched to control its outcome without needing a
live model call:

1. **Clean completion** — a truncated draft, completion succeeds and is
   grounded: no disclosure note; the pre-repeat text and the completion
   are both present in the combined result. PASS.
2. **Completion attempt fails** (raises) — the disclosure note is
   correctly added; the pre-repeat draft is preserved. PASS.
3. **Shared completion budget already exhausted** — confirmed the
   existing function's own gap (returns the truncated text completely
   unchanged, no note, `_stream_team_run` correctly never called a second
   time); confirmed the exact wrapper logic now shipped in
   `_evidence_integrity_check` correctly adds the disclosure note in this
   case. PASS.
4. **No truncation at all** (the common case) — confirmed a complete
   no-op: `_stream_team_run` is never called, the text is returned
   byte-identical. PASS — this is the regression check for every normal
   run that never touches the repetition guard at all.

This is local, source-level verification against the real functions, not
live proof by itself.

## 6. Live validation

**Deployed commit**: `bfb3a348a43f684eda3bcc3ca2d514889e82d78f`, confirmed
via `git rev-parse HEAD` on ZGX both before and during the controlled-path
exercise below. Service restarted with a new PID (`454541`,
`ActiveEnterTimestamp` matching) and `/health` returning
`{"status":"ok", ...}`. No active run before the restart (checked via
`GET /runs` and `ps aux | grep run-worker`).

**One natural live attempt**: MCP task `kpj33b99v`, session
`247bc117-22d7-42e1-b041-0ea5c9fda4ee`, 1143.21s. The retry DID run (the
upstream `_evidence_integrity_check` fired on the original draft's "no
corresponding hook" claim), but the retry's own generation spent most of
its time in a distinct, repeated "let me try a different approach" /
"let me try a different pattern" self-correction loop — semantically
repetitive but not an exact or near-exact textual repeat, so T16's
character-level detector never classified it as a repetition loop or
decay at all, and `team._repetition_truncated` was never set. The retry
eventually did produce text, recheck still found a contradiction
("compare_enumerations found left=13, right=16, matched=7" vs. the
claimed "no corresponding hook"), and a SEPARATE, pre-existing path
(verify_claims finding a genuinely absent `frontend/business-api-slice.ts`
path and a `CONTRADICTED` absence claim) correctly merged into the same
disclosure banner — exactly the existing, unmodified "recheck still
contradicts AND failed verify_claims" branch a few lines below this
phase's own insertion. The final delivered answer was explicitly marked
`UNRESOLVED`, not silently shipped as correct. **This attempt does not
directly exercise this phase's specific fix** (no `_repetition_truncated`
flag was ever set), but it does confirm no regression: the pre-existing
disclosure path this phase's insertion sits just upstream of still fires
correctly and was not disturbed.

**Controlled-path exercise against the real deployed code** (clearly
distinguished from a natural incident, per the mission's own allowance):
on ZGX, using `/home/abehera1992/miniforge3/envs/zgx/bin/python` (the exact
interpreter `agno-api.service` runs under) with `swarm.team` imported
directly from `~/agno-hive` at the confirmed deployed commit,
`_stream_team_run` monkey-patched to reproduce the exact T56 incident
shape (a genuinely repetition-truncated retry) and the shared-budget
-exhausted gap:

```
Scenario A (clean completion) -- note present (must be False): False
Scenario B (budget exhausted) -- unchanged: True | note before wrapper
(must be False): False
Scenario B -- note after wrapper (must be True): True
DEPLOYED CODE CONFIRMED WORKING -- both scenarios passed
```

This directly demonstrates, against the running production artifact
itself: a genuinely truncated retry that completes cleanly is adopted
without a false incomplete marker; a truncated retry whose shared
completion budget was already spent is correctly flagged incomplete by
this phase's own wrapper logic rather than silently falling through.

## 7. Deployment

- Commit `bfb3a348a43f684eda3bcc3ca2d514889e82d78f` pushed to
  `origin/main` (`3e5ad76..bfb3a34`).
- ZGX: `git pull` fast-forwarded `e51d9c7..bfb3a34`, confirmed via `git
  rev-parse HEAD` matching exactly.
- `systemctl --user restart agno-api.service`: new PID `454541`,
  `/health` ok.
- No active run before the restart.
- `hive-mcp`/`hive-mcp-agno`: not touched — no source change, confirmed via
  `git diff --stat` (1 file, `swarm/team.py`, 45 insertions, 0 deletions).

## 8. Preservation

- `git status --short` before and after this phase's work shows only
  `swarm/team.py` newly dirty; the same pre-existing dirty/untracked files
  recorded in every prior report (`docs/runbooks/groundedness-battery.md`,
  `tests/test_evidence_fidelity.py`, `training/train.py`,
  `zgx-ai-setup/litellm-config.yaml` modified; `T48-rerun-report.md`,
  `test_t18_evidence_synthesis_boundary.py`,
  `test_t19_source_context_disclosure.py`, `tokenizer.json`, several
  `training/*` files untracked) remain exactly as found.
- No stray artifact files were created or staged this phase.

## 9. Remaining gaps

- **No naturally-occurring live incident reproduced the exact
  repetition-truncation shape this phase fixes.** The controlled-path
  exercise against the real deployed code is strong, direct evidence, but
  it is not the same class of proof as an organic reproduction.
- **The initiating repetition cause remains unestablished**, as expected
  and explicitly out of this phase's scope. What evidence would establish
  it: a live run whose streamed token sequence (not just the periodic
  10-second-window log snapshots currently captured) is captured in full
  around the onset of the repeat, to see whether the model's own context
  window lost track of what it had already written, versus a sampling
  -level pathology, versus a prompt-structure issue specific to the
  reconciliation retry's prompt shape.
- **A second, distinct unproductive-generation pattern was found live**
  (the "let me try a different approach" semantic loop in run
  `kpj33b99v`) that T16's exact/near-exact repetition detector does not
  catch at all. Flagged as a separate, unresolved observation per the
  mission's own scope discipline — not addressed in this phase, and not
  assumed to be the same mechanism as the repetition-truncation defect
  this phase fixes.
