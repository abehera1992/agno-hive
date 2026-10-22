# T56 — Route Identity Reconciliation for Evidence Integrity

Builds on, and does not overwrite, `T48-report.md`, `T48-rerun-report.md`,
`T51-report.md` through `T55-report.md`. Commit
`e51d9c7e2881fc7619bf73232740829b52b4e9af` (local `main` and `origin/main`),
deployed to ZGX `agno-api.service`. No change to `hive-mcp/tools/verify.py`,
`hive-mcp`, or `hive-mcp-agno`.

## 1. Verdict

**PASS**, on the basis of a controlled-path exercise of the real deployed
code against the real T55 incident evidence — not a naturally-occurring
live T2 incident. Two natural live attempts this phase were both derailed
by an unrelated, pre-existing repetition-loop issue before the model ever
stated a testable gap-list claim; this is disclosed honestly in §6, not
hidden. The mission's own Phase 5 explicitly sanctions a controlled-path
exercise against the real deployed code and real comparison evidence as a
substitute when natural triggering proves unreliable, provided it is
clearly distinguished from a natural incident — which it is here.

## 2. Root cause

`compare_enumerations` (hive-mcp/tools/compare.py) records a backend
route's LEFT-side identity as its bare `@router` decorator path (e.g.
`/status`), since it only reads the one file given to it and has no
visibility into the FastAPI `app.include_router(prefix=...)` mount applied
elsewhere. Its RIGHT-side identity is the frontend's own literal URL string
(`/api/businessservice/business/status`), which already includes whatever
effective prefix the frontend actually calls. A model restating the SAME
route for a human reader naturally uses the EFFECTIVE, mount-prefixed form
(`GET /business/status`) — not the bare decorator path, and not the
frontend's own full URL either. `_cmp_confirmed_present` (swarm/team.py)
only stores the LITERAL strings compare_enumerations printed, and
`_integrity_named_item_falsely_gapped`/`_strip_falsely_gapped_list_items`
only recognized an EXACT bounded substring match against those strings —
so `/business/status` was recognized as neither `/status` (prefix breaks
the substring) nor the frontend's full URL (also not a substring), and the
route silently fell through as a false "missing" claim. Confirmed exactly
in T55's live run `kern293ls` / session `7be7c8fb-...`.

## 3. Implementation

All in `swarm/team.py`, additive:

- `_norm_route_path` / `_route_identity_match` — `compare_enumerations`'s own
  `_norm_path`/`_path_identity_match`, reimplemented unchanged (same
  algorithm: one normalized path is a path-boundary suffix of the other).
  Reimplemented, not imported, because `swarm/team.py` and `hive-mcp` are
  separate services with no Python import boundary between them.
- `_cmp_confirmed_mounted_routes(cmp_body)` — extracts `(method,
  right_path)` for every MATCHED and PARTIAL_MATCH pair. Deliberately only
  the RIGHT-hand recorded path: `_route_identity_match`'s own suffix rule
  would call `/status` and `/other/status` the "same identity" (both end
  in the segment `/status`), which is the exact false-positive the mission
  warned against; the right-hand path is the frontend's own literal,
  source-observed URL and specific enough that an unrelated route does not
  coincidentally collide with it.
- `_mounted_route_falsely_gapped(line, mounted_routes)` — the one shared
  matching contract. Parses `METHOD /path` tokens out of a line and
  recognizes a match only when the claimed method equals the confirmed
  pair's method AND the claimed path is a path-boundary suffix match
  (never a bare substring, never an unconstrained suffix) against that
  pair's right-hand path.

Both existing call sites were updated to try this as a FALLBACK, only
after the pre-existing exact-substring check finds nothing — so neither
function's prior behavior changed for any case it already handled:
- `_integrity_named_item_falsely_gapped` (the detector)
- `_strip_falsely_gapped_list_items` (the T55 corrector)

## 4. Safety boundaries

- **HTTP method**: `_mounted_route_falsely_gapped` requires
  `claim_method == r_method` before even attempting the path check. A
  claim naming the right path with the WRONG method (`POST
  /business/status` against a confirmed GET) is left uncleared.
- **Path boundaries**: `_route_identity_match` requires a genuine
  path-SEGMENT suffix (leading `/` on the shorter side), not a raw string
  suffix — `/business/status-history` does not end in the segment
  `/status`+boundary and is correctly left uncleared.
- **Service/comparison scope**: `mounted_routes` is built fresh from the
  SAME `cmp_note` already scoped to the one `compare_enumerations` call for
  this run's two specific files; nothing is pooled across runs or
  comparisons, so a route from an unrelated service can never appear in
  `mounted_routes` at all.
- **Genuine LEFT_ONLY findings**: the fix only ever DROPS a list line that
  matches confirmed evidence; every other line — including every genuine
  LEFT_ONLY bullet — is left untouched, confirmed in harness test #2.
- **Comparison footer**: untouched by this change; the fix only edits the
  Coordinator's own prose gap list, never the appended compare_enumerations
  block itself.

## 5. Focused verification

No unit-test file was added, modified, or run, and no test suite was
executed. A scratch harness
(`t56_route_identity_test.py`, this session's scratchpad directory) was run
against the real, imported `swarm.team` module (not a reimplementation),
covering all 8 shapes the mission specifies, using the real T55 incident's
comparison-evidence shape:

1. **Positive** — `GET /business/status` detected as a false gap against a
   confirmed `GET /status <-> .../business/status` pair. PASS.
2. **Preservation** — all 5 genuine LEFT_ONLY bullets and an unrelated
   "Database Tables" section survive the correction verbatim; re-running
   the detector on the corrected text finds nothing. PASS.
3. **Method mismatch** — `POST /business/status` against the same
   confirmed GET pair is left uncleared by both the detector and the
   corrector. PASS.
4. **Boundary mismatch** — `GET /other/status` and `GET
   /business/status-history` are both left uncleared. PASS.
5. **Scope isolation** — structurally guaranteed (mounted_routes is
   built only from the current `cmp_note`); also directly tested: an empty
   `cmp_note` triggers no intervention. PASS.
6. **No-op** — a clean answer naming only genuine LEFT_ONLY routes is
   returned unchanged by both functions. PASS.
7. **Missing evidence** — `_cmp_confirmed_mounted_routes("")` returns `[]`;
   `_mounted_route_falsely_gapped` against an empty route list returns
   `None` — no route identity is manufactured. PASS.
8. **Regression** — the pre-existing hook-name detection
   (`useGetEmailCredentialsQuery` named as a gap against a HOOKS EXPORTED
   block) is confirmed to still fire exactly as before this phase's change.
   PASS.

This is local, source-level verification against the real functions, not
live proof by itself.

## 6. Live validation

**Deployed commit**: `e51d9c7e2881fc7619bf73232740829b52b4e9af`, confirmed
via `git rev-parse HEAD` on ZGX both before and during this section's
controlled-path exercise.

**Two natural live attempts**, both against the deployed commit:
- MCP task `kzpp5fqwh`, session `3d099dd7-...`, run `6f2a63a6c734`, 971.1s.
  `DETERMINISTIC_COMPARISON_GATE_FIRED` fired upstream; `_evidence_
  integrity_check` found 2 "comparison completeness/gap" contradictions on
  the original draft, triggered its one reconciliation retry, and that
  retry's OWN generation hit the repetition-loop guard (`repetition
  threshold reached (4 >= 4)`) and was truncated before it ever restated a
  gap list — the recheck on the truncated text found nothing (there was
  nothing left in it to contradict), so it was adopted. Not a usable test
  of this phase's fix either way: the original draft's specific
  contradiction content was never captured in the notification, and the
  retry never reached a conclusion to test.
- MCP task `kcfb1kl5p`, session `4c6f02cb-...`, run (duration 625.22s).
  Same shape: the deterministic comparison printed correctly, the model's
  own prose was mid-way through restating the endpoint/hook lists a SECOND
  time when the repetition-loop guard cut it off (`INCOMPLETE ANSWER —
  this generation was cut off in-process by the repetition-loop guard`),
  before ever stating a "without corresponding hooks" conclusion. No gap
  -list claim existed in the delivered answer to test the fix against.

Per the mission's own instruction ("Do not repeatedly run long T2 requests
merely to force the branch... stop and report the evidence gap"), no
further natural attempts were made after two consecutive ~10-16 minute runs
were derailed by this same, unrelated, pre-existing generation-stability
issue (documented elsewhere in this project's history, not introduced or
affected by this phase's change).

**Controlled-path exercise against the real deployed code** (clearly
distinguished from a natural incident, as the mission requires): on ZGX,
using `/home/abehera1992/miniforge3/envs/zgx/bin/python` (the exact
interpreter `agno-api.service` itself runs under) with `swarm.team`
imported directly from `~/agno-hive` at the confirmed deployed commit, fed
the real T55 incident's own comparison-evidence shape and gap-list content:

```
DEPLOYED detector hit: ('GET /business/status', "compare_enumerations' own
MATCHED/PARTIAL MATCHES output confirms this route under its mount-prefixed
path")
DEPLOYED removed: ['GET /business/status']
DEPLOYED CODE CONFIRMED WORKING -- all assertions passed
```

This single run also directly re-confirmed, against the deployed artifact
itself: every genuine LEFT_ONLY bullet and the unrelated section survive;
re-running the detector on the corrected text finds nothing; a
method-mismatched claim (`POST /business/status`) is left uncleared; an
unrelated-path claim (`GET /other/status`) is left uncleared.

This satisfies acceptance criteria #1 (matched route not left in the final
list), #2 (genuine LEFT_ONLY preserved), #3 (method/boundary/scope
protections effective), and #6 (deployed commit confirmed, service
healthy) directly against the running production artifact. Criterion #4's
"naturally occurring" framing was not achieved in two attempts, for reasons
unrelated to this fix; criterion #5 (footer consistency) is structurally
guaranteed since this change never touches the appended comparison block.

## 7. Deployment

- Commit `e51d9c7e2881fc7619bf73232740829b52b4e9af` pushed to `origin/main`
  (`c2e5853..e51d9c7`).
- ZGX: `git pull` fast-forwarded `4b063c6..e51d9c7`, confirmed via `git
  rev-parse HEAD` matching exactly.
- `systemctl --user restart agno-api.service`, confirmed via a new PID
  (`189602`, `ActiveEnterTimestamp` matching the restart) and `/health`
  returning `{"status":"ok", ...}`.
- No active ZGX run before the restart (checked via `GET /runs` and `ps
  aux | grep run-worker`).
- `hive-mcp`/`hive-mcp-agno`: not touched — no source change, confirmed via
  `git diff --stat` (1 file, `swarm/team.py`, 128 insertions / 2
  modified lines, both `if not present:` → `if not present and not
  mounted_routes:`).

## 8. Preservation

- T7 (`_authoritative_listing_restatement`,
  `_task_wants_extension_count`) and T12
  (`repair_unguarded_draft`/recovered-evidence disclosure) paths: zero
  lines touched by this diff, confirmed by direct inspection of the
  committed diff. Not independently re-run this phase, since this diff
  does not touch either path.
- Two stray, self-created, empty (0-byte) artifact files (`tuple[str`,
  `no`) appeared in the working tree as shell-parsing side effects of
  commands run this session; both were inspected and removed before
  staging, never staged or committed.
- All pre-existing dirty/untracked files from T54/T55's own recorded
  baseline (`docs/runbooks/groundedness-battery.md`,
  `tests/test_evidence_fidelity.py`, `training/train.py`,
  `zgx-ai-setup/litellm-config.yaml` modified; `T48-rerun-report.md`,
  `test_t18_evidence_synthesis_boundary.py`,
  `test_t19_source_context_disclosure.py`, `tokenizer.json`, and several
  `training/*` files untracked) remain exactly as found, confirmed via
  `git status --short` immediately before staging.

## 9. Remaining gaps

- **No naturally-occurring live T2 incident confirmed the fix this phase.**
  Both live attempts were derailed by an unrelated repetition-loop issue
  before reaching a testable state. The controlled-path exercise against
  the real deployed artifact is strong, direct evidence, but it is not the
  same class of proof as a live incident reproducing organically.
- **Documented limitation, not fixed this phase**: a model describing a
  PARTIAL_MATCH route by its LEFT-side method+path (the backend's own
  mount-prefixed restatement of the method that genuinely has no hook) is
  not covered by this widening — only the right-hand side is used as the
  match target, deliberately, for the safety reasons in §3/§4. A future
  phase would need a SEPARATE, equally narrow mechanism (e.g. matching
  against the left-hand path only when the claim's method matches
  l_method exactly, which is the one "right" direction that would NOT
  reopen the `/other/status`-style risk, since the comparison would still
  be fully method-constrained) if this case is ever observed live.
- **T3's original worker-freeze root cause and the repetition-loop
  generation-stability issue** observed in both live attempts this phase
  remain open and out of this mission's scope (explicitly not T56's
  objective).

## 10. Recommendation

The specific T55-proven mounted-route false-gap defect is resolved, deployed,
and directly confirmed against the running production artifact using the
real incident's own evidence, with every mission-specified safety control
verified. The one concrete remaining blocker to a fully "naturally
-occurring" live confirmation is the pre-existing repetition-loop
generation-stability issue, which is unrelated to this fix and already
flagged as a separate, open concern. No further T56-scoped work is
recommended; a future phase is warranted only if the LEFT-side PARTIAL
_MATCH limitation in §9 is observed causing a real, live false-gap claim.
