# T55 — Evidence-to-answer lifecycle integrity and regeneration-safe correctness

Builds on, and does not overwrite, `T48-report.md`, `T48-rerun-report.md`,
`T51-report.md`, `T52-report.md`, `T53-report.md`, or `T54-report.md`. Commit
`4b063c6958ffef318488ccdd1be276fe1988dcdf` (local `main` and `origin/main`),
deployed to ZGX `agno-api.service`. No change to `hive-mcp/tools/verify.py`
this phase, so the `hive-mcp`/`hive-mcp-agno` containers were not touched.

## 1. Overall verdict

**PARTIAL.**

Priority A's implementation is real, narrowly scoped, deterministic, and
harness-proven on the exact historical incident shape T54 demonstrated
failing to correct. It is deployed and confirmed not to regress a run that
needed no intervention. It has not yet been directly observed firing live,
and live validation surfaced a genuine, newly-traced, more fundamental
limitation — a path-representation mismatch between compare_enumerations'
own raw route output and a model's full-URL prose — that silently defeats
both the pre-existing detector and this phase's new corrector in exactly
the shape it occurred live. Priority B made no code change, by deliberate,
evidence-based choice. Priority C is preserved by construction (zero lines
touched in that path).

## 2. Initial repository and deployed-runtime state

- Local HEAD at mission start: `177c8ea66382afd3007d20ba24a06cf6de61cc40`
  (T54's final-report commit), identical to `origin/main`.
- Working tree at mission start: the same pre-existing dirty/untracked files
  T54's own report recorded (`docs/runbooks/groundedness-battery.md`,
  `tests/test_evidence_fidelity.py`, `training/train.py`,
  `zgx-ai-setup/litellm-config.yaml` modified; `T48-rerun-report.md`,
  `test_t18_evidence_synthesis_boundary.py`,
  `test_t19_source_context_disclosure.py`, `tokenizer.json`, and several
  `training/*` files untracked). None staged, none touched, confirmed again
  at the end of this phase.
- ZGX `agno-api.service` at mission start: HEAD `4935dc67dbd6ccde9f2309051235aa7ab73cbcf6`
  (T54's code commit — correct, since T54's report commit is docs-only and
  needs no deployment), active, `/health` ok, zero active runs.
- Two stray, self-created, empty artifact files (`tuple[str`, and earlier
  `str`) appeared in the working tree from shell-parsing side effects of
  commands run during this and the prior phase; both were inspected
  (confirmed 0 bytes, created by this session), removed, and never staged.

## 3. Priority A — root cause, implementation, and how protection survives regeneration

### Root cause, traced directly in source (not re-assumed from T54's report)

`_evidence_integrity_check` (new name confirmed at `swarm/team.py:8187`,
unchanged from T54) is the final choke point applied at `_verified_answer`'s
own single call site, run strictly after `_verified_answer`'s own retry
chain has finished. It independently detects the exact T2 contradiction
shape via `_integrity_named_item_falsely_gapped` (category 3b inside
`_evidence_integrity_findings`) — a matched/partial-matched item named as
missing, checked against the SAME `compare_enumerations` call. Its own
one-shot reconciliation retry (`_stream_team_run` with a correction prompt)
is a SEPARATE budget from `_reconcile_completeness_claim_with_comparison`'s
own one-shot gate (`_COMPARISON_RECONCILE_FLAG`) — confirming this really is
a later, independent regeneration stage, not the same mechanism firing
twice. Previously, when this retry did not resolve the contradiction, the
ONLY disposition available was `_force_uncertainty_answer`'s disclosure-only
banner — confirmed exactly in T54's live run `kbxxxr147`
(`useGetEmailCredentialsQuery` named a gap after the retry, despite
`compare_enumerations`' own MATCHED output).

### Implementation

New `_strip_falsely_gapped_list_items(content, cmp_note)` (`swarm/team.py`,
inserted immediately after `_integrity_named_item_falsely_gapped`): a
deterministic, zero-LLM-call correction. Reuses the exact scan shape
`_gap_claim_block_lines` already uses (`_GAP_CLAIM_LINE_RE`,
`_LIST_ITEM_LINE_RE`, the same bounded lookahead) — only the SELECTION
(which specific list-item lines to drop) is new. Removes only the bulleted/
numbered line(s) naming something `_cmp_confirmed_present` confirms exists;
never the gap-declaring line itself (sibling bullets may name genuinely
missing items), never an unrelated section.

Wired into `_evidence_integrity_check` at both points the root-cause trace
identified:
1. On the FIRST findings pass (`swarm/team.py` ~8230-8256), before spending
   a whole regeneration round on a shape that does not need one. Safe
   without a `verify_claims` re-check because `content` reaching this
   function is already `verify_claims`-clean (checked by `_verified_answer`
   before this function is ever called), and removing lines from clean text
   cannot introduce a new fabrication.
2. On the POST-RETRY recheck (`swarm/team.py` ~8327-8359) — exactly where
   T54's run shipped the disclosure-only banner instead. Here the stripped
   candidate IS re-verified with `verify_claims` before adoption, since
   `retried` is a fresh, never-verified draft (same bar the existing
   "resolved" branch already holds a clean candidate to).

Both sites adopt only on FULL resolution (recomputing
`_evidence_integrity_findings` on the stripped text and requiring it to
come back empty); a partial strip or no match falls through to the
existing retry/disclosure logic completely unchanged. The invariant now
holds regardless of which stage first saw the contradiction, without
touching `_COMPARISON_RECONCILE_FLAG`'s one-shot behavior at all —
satisfying the mission's explicit requirement not to relax that flag.

### Harness evidence (source-confirmed)

Scratchpad script against the real `swarm.team` module (not a
reimplementation), reproducing the exact T54 incident shape: a
`compare_enumerations`-style `cmp_note` with `useGetEmailCredentialsQuery`/
`GET /business/email-credentials` in the MATCHED block, a `content` naming
it as a gap alongside 6 genuinely LEFT_ONLY endpoints and an unrelated
"Database Tables" section.
- The falsely-gapped bullet is removed; the exact identifying string
  (`/business/email-credentials`) is reported as the removed item.
- All 6 genuinely-missing bullets survive verbatim.
- The unrelated section survives verbatim.
- Re-running `_integrity_named_item_falsely_gapped` on the stripped content
  returns `None` — confirms the fix actually resolves the shape the
  detector itself flags, not a different string.
- Negative controls: a clean gap list with nothing falsely gapped returns
  `None` (no intervention); no `cmp_note` at all returns `None`.

### Live evidence — three runs against the deployed commit (`4b063c6`)

1. **MCP task `kpz4hqmnr`, session `0b924646-dafa-4691-8ff7-9d64fe8cb863`,
   run `749c84fedcca`, 1292.1s.** `DETERMINISTIC_COMPARISON_GATE_FIRED` with
   `model_made_completeness_claim=False`, "preserving 5 unrelated
   section(s)" — zero exclusions logged by either T54's or this phase's
   logging. No `evidence-integrity` log lines at all. The delivered answer
   correctly lists only the 5 genuine LEFT_ONLY endpoints as gapped,
   correctly excludes the MATCHED `email-credentials` endpoint and the
   PARTIAL_MATCH `{business_id}/documents` endpoint. This phase's new code
   had nothing to do — the draft was already clean by the time it reached
   either choke point. Confirms no regression, not confirmation of firing.
2. **MCP task `kern293ls`, session `7be7c8fb-c750-42ff-a421-f3b0d2efa162`,
   run `3b430f48b171`, 791.8s.** Same gate outcome
   (`model_made_completeness_claim=False`, 0 exclusions, 0
   evidence-integrity activity) — **but the delivered answer's own final
   "Backend Endpoints Without Frontend Hooks" list wrongly includes
   `GET /business/status`, while the SAME answer's own appended
   `compare_enumerations` footer shows `GET /status <-> .../business/status`
   in its MATCHED(7) block.** A real, live occurrence of the T2
   contradiction shape that neither `_integrity_named_item_falsely_gapped`
   nor this phase's new `_strip_falsely_gapped_list_items` caught. Traced
   to a concrete cause, not left as an unexplained miss — see §7.

Given the cost of each run (≈13-22 minutes) and the mission's own
instruction against spending extra calls once the mechanism's behavior is
understood, three live attempts this phase (five total across T54+T55) were
judged sufficient evidence rather than continuing to search for a run that
happens to exercise this exact retry/recheck boundary.

### Acceptance criteria applied to Priority A

Not fully met. The harness proves the new mechanism correctly resolves the
exact historical shape when the path representations align, and live
evidence confirms no regression on a clean run. It does not meet "live-
confirmed across the relevant regeneration boundary" — no live run this
phase reached that boundary with a shape the new matching logic could
recognize, and live run 2 demonstrates a real, traced case the underlying
matching logic (shared with the pre-existing detector) does not yet cover.

## 4. Priority B — T7 generalization

T54's `_task_wants_extension_count`/`_authoritative_listing_restatement`
confirmed intact at `swarm/team.py:23819-23857`, unchanged, and confirmed
deployed on ZGX (`4b063c6`, same commit as this phase's own change).

Evaluated the one visible next candidate: a bare "number of files" task
(no extension named) resolving to `listing["files"]`. Declined it. The
existing code's own comment at this exact site (`swarm/team.py:23815-23818`)
already documents the reasoning: "'how many files' alone (no extension) is
still ambiguous (all files? just one kind?)" — a directory can hold a
non-`.py` file (a stray `README.md`, `.gitignore`) alongside its modules,
and a bare "files" question has no wording-level signal distinguishing
"all files, whatever extension" from "all the files of the kind this
conversation is really about." No new evidence this phase overrides that
deliberate prior caution, and the mission explicitly warns against
broadening T7 "just for the sake of adding code." No code change.

## 5. Priority C — T12 recovery preservation

No code in `repair_unguarded_draft`, the recovery-path filename-disclosure
logic, or either T52/T53 mechanism was touched by this phase's diff (113
insertions, 0 deletions, confined to the two `_evidence_integrity_check`
sites and the one new helper function above). Preserved by construction,
not independently re-exercised — per the mission's own instruction not to
repeat the controlled staleness exercise merely to reconfirm an already
-demonstrated behavior, and because neither Priority A nor B touched the
recovery path.

## 6. Deployment evidence

- Commit `4b063c6958ffef318488ccdd1be276fe1988dcdf` pushed to `origin/main`
  (`177c8ea..4b063c6`).
- ZGX `agno-api.service`: `git pull` fast-forwarded `4935dc6..4b063c6`
  (also picking up T54's report commit, docs-only), confirmed via `git
  rev-parse HEAD` on ZGX matching `4b063c6` exactly.
- `systemctl --user restart agno-api.service`, confirmed via a genuinely new
  PID (`4116165`, `ActiveEnterTimestamp` matching the restart) and
  `/health` returning `{"status":"ok", ...}`.
- No active ZGX run before either restart this phase (checked via
  `GET /runs` and `ps aux | grep run-worker` immediately before).
- `hive-mcp`/`hive-mcp-agno`: not touched (no source change; confirmed via
  `git diff --stat`, 1 file changed, `swarm/team.py` only).
- Live production runs executed against the deployed commit, with their own
  session/run identifiers, cited in §3 above.

## 7. Outstanding risks

- **Newly-traced, concrete limitation: mount-prefix mismatch defeats the
  present-item match.** `compare_enumerations` reports LEFT-side routes as
  the bare `@router` decorator path (e.g. `GET /status`), since it has no
  visibility into the `app.include_router(prefix=...)` mount applied in
  `main.py`. A model's own prose, when it restates an endpoint for a human
  reader, naturally includes that mount prefix (`GET /business/status`).
  `_cmp_confirmed_present`'s `present` set therefore never contains a string
  that is a bounded substring of the model's own full-path phrasing, so
  BOTH `_integrity_named_item_falsely_gapped` (pre-existing) and this
  phase's own `_strip_falsely_gapped_list_items` silently fail to recognize
  the match — confirmed live in run `kern293ls` (`GET /business/status`
  shipped as a gap, directly contradicting the same answer's own appended
  MATCHED(7) block showing `GET /status <-> .../business/status`).
  Deliberately not patched this phase: the mission explicitly warns
  against "global substring matching that conflates endpoints, hooks,
  services, or unrelated claims," and a hasty prefix-stripping or
  suffix-matching widening risks exactly that without careful, separately
  -scoped design and testing. This is the single most concrete, actionable
  finding from this phase's live validation and should be the first item
  in a T56-style follow-up: teach `_cmp_confirmed_present`'s consumers
  (or `_cmp_confirmed_present` itself) to recognize a model's fully
  -prefixed path as a path-boundary suffix match against compare_
  enumerations' own bare path, mirroring `compare_enumerations`' own
  documented join strategy ("path-boundary suffix match, exact string, no
  inference") rather than inventing a new matching rule.
- **Priority A's own new branch remains live-unconfirmed** after three
  attempts this phase (five total across T54+T55). The harness proof is
  solid and directly exercises the real functions on the exact historical
  shape; what remains unproven is a live run reaching the post-retry
  recheck boundary with a contradiction the current matching logic can
  recognize. Not papered over as a PASS.
- **T2's broader lifecycle gap is narrowed, not closed.** This phase closes
  the specific "retry/recheck gives up with disclosure-only" gap T54 traced
  and reported. The newly-found prefix-mismatch limitation above shows the
  underlying matching logic itself has a real blind spot independent of
  which stage runs it — a deeper issue than regeneration-timing alone.
- **T3's original worker-freeze root cause remains fully open**, unchanged
  and not re-investigated this phase, per the mission's own scope (T3 was
  not a T55 priority).
- **No regression found in T10/T13/T52/T53 behavior**, confirmed by
  construction (zero lines of this phase's diff touch any of those paths)
  rather than by independent re-run, per the mission's own instruction to
  report plainly when a protected path was not independently re-exercised.

## Acceptance criteria applied (overall)

PASS was not met: Priority A's live evidence is inconclusive (one run
needed no intervention, one run surfaced a related-but-distinct matching
limitation neither the old nor the new mechanism catches). FAIL does not
apply: no regression was introduced (confirmed both by construction for
T10/T13/T52/T53 and live for the one clean run), no genuinely-missing
finding was suppressed, and deployment is verified with the intended code
active. PARTIAL is the honest verdict: the code is deployed and the harness
-proven mechanism is real, but the specific live-confirmation this phase
sought remains unproven, and a more fundamental, now precisely-traced
limitation was found in the process.
