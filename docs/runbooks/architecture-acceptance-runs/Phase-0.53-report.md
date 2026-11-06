# Phase 0.53 — MCP Upstream Issue-Analysis Correction, Watchdog Recovery Validation, and agno-api SDK Isolation

Implementation, documentation-correction, deployment, and targeted
live-validation phase, executed in the mission's own required order:
(1) correct Phase 0.52's inaccurate upstream issue analysis, (2) prove the
LightRAG watchdog's persistence and restart-and-recovery path end-to-end
against an isolated target, (3) remove `agno-api.service`'s identical
latent CVE-2025-53365 exposure, flagged but not fixed in Phase 0.52.

## 1. Starting state and preservation evidence

| | Value |
|---|---|
| Local branch / HEAD (start) | `main` / `b08d064` |
| Origin HEAD (start) | `b08d064` (matched local) |
| ZGX HEAD (start) | `b08d064` (matched local/origin) |
| Local dirty/untracked (start) | 79 (78 pre-existing backlog, unchanged from Phase 0.52's own baseline) |
| ZGX dirty/untracked (start) | 35 |
| `lightrag.service` (start) | PID `1509060`, active/running since `2026-11-06 08:12:54`, `NRestarts=7` (from Phase 0.52's own `PYTHONNOUSERSITE` trial-and-error, unrelated to this phase) |
| `agno-api.service` (start) | PID `1444953`, active/running since `2026-11-06 07:32:56`, `NRestarts=0` |
| `lightrag-watchdog.timer` (start) | active (waiting), firing every ~120s, every observed firing logging `healthy (ok)` |
| `ops/.lightrag_watchdog_state.json` (start) | did not exist — the restart-issuing branch had never fired in production |

No `git reset --hard`, `clean`, `stash`, or `checkout --` was run at any
point. All pre-existing dirty/untracked files (local and ZGX) were
preserved; the only local-tree delta at the end of this phase is this
phase's own four changes (§9).

## 2. Objective 1 — Phase 0.52 upstream issue-analysis correction

Corrected directly against the actual GitHub issue/PR/advisory pages
(fetched in full, not re-summarized from search snippets) — see
Phase-0.52-report.md §2.4/§9 for the corrected text in place. Summary of
what changed:

| Item | Phase 0.52's original claim | Corrected finding |
|---|---|---|
| Issue #2741 | "could not be located via web search... treated as unconfirmed" | Exists, open, filed 2026-05-31. A **diagnosability** issue (client receives an empty `-32603` error while the real exception is correctly logged separately as `"Session <id> crashed"`) — not a report that the server crashes. Corroborates per-session isolation working, does not contradict it. |
| Issue #2416 | "a crash still reachable in `respond()`... a narrower `ClosedResourceError` gap" | Open, filed 2026-04-09. Actually an **`AssertionError`** (`assert not self._completed`) from a `notifications/cancelled` **race**, not a `ClosedResourceError` at all. Unrelated exception type, unrelated trigger. The issue's own thread states PR #2334 does not cover this. |
| PR #2334 | "fixes a mid-request ClosedResourceError... `mcp-1.26.0` predates it" | Confirmed: merged 2026-03-24 (commit `6524782`), fixes issue #2328, specifically the transport-closes-**mid-request** `ClosedResourceError` path. `1.26.0` (installed 2026-02-23) genuinely predates it — this part of Phase 0.52's reasoning was correct; only its conflation with #2416 was wrong. |
| Issue #3142 | not mentioned in Phase 0.52 at all | New finding: closed as *not planned* (2026-08-14). A **stateless-mode-only** `ClientDisconnect`/`send_log_message` crash path on v1.28.1 — `lightrag_mcp/server.py` runs in the default *stateful* mode, so this issue's own scenario does not apply here. Distinct from both the CVE and #2416. |
| GHSA-j975-95f5-7wqh | affected `<1.10.0`, patched `1.10.0` | Confirmed verbatim from the official advisory — unchanged, unweakened. |
| PR #967 | "fix commit `7b420656`" | Corrected: the actual commit hash is `7b42065` (one digit shorter) — a short-SHA display discrepancy, not independently significant. |

**Net effect:** Phase 0.52's own regression evidence (byte-for-byte
reproduction on `1.9.4`, confirmed per-session isolation on `1.26.0` for
the CVE's specific scenario) is **completely unchanged and unweakened** by
this correction. What changed is the characterization of three *separate*
upstream issues this report had previously either mischaracterized
(#2416) or been unable to locate (#2741) or never mentioned (#3142).

**Source verification method:** a dedicated fetch of each of the six
authoritative URLs (the four issues/PRs, the GHSA advisory, and PR #967)
was performed and read in full — not inferred from search-result snippets.

## 3. Objective 2 — watchdog source inspection and persistent-state design

Inspecting the Phase 0.52 implementation found exactly the gap the mission
predicted: `load_state`/`save_state` used plain `open()`/`json.dump()`
(no atomicity — a kill mid-write could corrupt the file), there was no
lock of any kind (two overlapping timer firings could both reach the
restart-issuing branch), and the production state file had never been
created (`ls`: `No such file or directory`) — the restart/cooldown/budget
logic had never actually executed against real state because production
had stayed healthy throughout Phase 0.52.

`ops/lightrag_watchdog.py` was rewritten (322 insertions net) to add:

- **Atomic state writes**: `tempfile.mkstemp` in the same directory +
  `os.fsync` + `os.replace` — a process killed mid-write leaves either the
  old or the new file intact, never a half-written one.
- **Fail-closed (not fail-open) on corrupt state**: a *missing* state file
  is legitimately "no restarts yet" (empty budget). A state file that
  *exists but fails to parse* is treated as "budget unknown" and refused
  (logged as an explicit `ESCALATION`, exit code `4`) — deliberately NOT
  reset to an empty budget, which would hand out a fresh restart allowance
  at exactly the moment something is already wrong enough to have
  corrupted the file.
- **A dedicated `flock` (non-blocking) lock** around the entire decision
  body — a second invocation that finds the lock held exits immediately
  (exit code `10`) rather than racing the first.
- **Evidence-based health categorization**: `check_once` now returns
  `(healthy, category, detail)` with category one of `ok`,
  `connection_error`, `timeout`, `unexpected_redirect`, `http_error`,
  `zombie_confirmed`, `malformed_response` — a redirect is never silently
  followed to an unintended endpoint; a confirmed zombie signature is
  distinguished from a generic HTTP error, which is distinguished from the
  process simply not being reachable at all.
- **Post-restart recovery verification**: after issuing a restart, one
  follow-up `check_once` call confirms and distinctly logs whether the
  service actually came back (`"recovery CONFIRMED"`) or not
  (`"recovery NOT yet confirmed"`) — these were previously conflated into
  one unverified "restart issued successfully" log line.
- **Full parameterization**: every tunable (`mcp_url`, `service_name`,
  `state_file`, `lock_file`, all timing values) is now a `main()` argument
  defaulting to the real production value — so the *actual* implementation
  can be exercised against an isolated target with zero code path by which
  it could ever reach, restart, or persist state against the real
  `lightrag.service`.

No clock/date-string parsing exists anywhere in this design (timestamps
are stored as raw `time.time()` epoch floats, loaded back and validated as
numbers) — eliminating that entire failure class by construction rather
than handling it defensively.

## 4. Objective 2 — isolated restart-and-recovery execution evidence

A real, separate, temporary systemd user service (`p53-test-mcp.service`,
never `lightrag.service`) was created on ZGX, running a minimal `FastMCP`
server under the same vulnerable `mcp==1.9.4` shadow, on port `19010`. The
real, unmodified `lightrag_watchdog.main()` was called directly (not a
reimplementation, not a mock) with every target parameter pointed at this
isolated service and isolated state/lock files under `/tmp/p53_test/`.

All 14 steps of the mission's required sequence were executed and
directly observed:

| # | Step | Evidence |
|---|---|---|
| 1 | Start isolated service | `p53-test-mcp.service` active, PID `1587533` |
| 2 | Confirm successful `initialize` | `HTTP/1.1 200 OK`, `serverInfo.version: "1.9.4"` |
| 3 | Cause the zombie condition | Client called `slow_tool` with a 1s timeout, disconnected; the server's own late write then failed — confirmed via its journal: identical `ClosedResourceError` → `RuntimeError: Task group is not initialized` traceback as the real production incidents. A fresh probe afterward returned `500 Internal Server Error` while `systemctl` still reported `active (running)` on the same PID — the exact zombie signature. |
| 4 | Run the watchdog | `main(mcp_url="http://127.0.0.1:19010/mcp/", service_name="p53-test-mcp.service", state_file="/tmp/p53_test/state.json", lock_file="/tmp/p53_test/lock", ...)` |
| 5 | First failure does not restart | Logged: `"first check failed (http_error): HTTPError 500... re-checking once before acting"` |
| 6 | Second failure confirms | Logged: `"second check also failed (http_error)... confirmed, not transient"` |
| 7 | Restarts the isolated target, not production | Logged: `"issuing systemctl --user restart p53-test-mcp.service"`; `lightrag.service`'s own PID/timestamp/restart-count confirmed unchanged immediately after |
| 8 | Isolated service starts again | New PID `1590897` |
| 9 | Fresh handshake succeeds | Logged: `"restart issued and recovery CONFIRMED -- fresh handshake succeeded (ok)"` |
| 10 | Recovery outcome recorded accurately | Exit code `0`; distinct from the "not confirmed" exit code `5` branch (not hit in this run, since recovery genuinely succeeded) |
| 11 | Restart timestamps persist across invocations | A **separate, fresh** `python` process (invocation B) read back the timestamp invocation A's process had written, and based its cooldown decision on it |
| 12 | Cooldown blocks an immediate second restart | With `cooldown_s=60`: invocation B, a **separate process**, re-crashed the service and run ~17s after invocation A's restart, logged `"last auto-restart was 17s ago, inside the 60s cooldown -- NOT restarting again yet"`, exit code `1`, and critically issued **no** `systemctl restart` call at all (confirmed by the log itself and by the state file remaining unchanged at exactly 1 entry). An earlier attempt at this same check, with a too-short `cooldown_s=10`, was invalidated by the test's own ~34s round-trip time (crash + wait + watchdog overhead) already exceeding that cooldown before the second invocation ran — a test-timing mistake, corrected by lengthening `cooldown_s`, not a defect in the watchdog |
| 13 | Rolling-window budget blocks a 4th restart | With `cooldown_s=15, window_s=90, max_restarts=3`: three full crash→restart→recover cycles, each ~27-28s apart (clearing cooldown each time), produced exactly 3 recorded timestamps; a 4th crash+invocation logged `"ESCALATION: 3 auto-restart(s) already issued in the last 90s (budget 3)... Refusing to restart again"`, exit code `2` |
| 14 | Budget exhaustion escalates, does not restart | Confirmed in the same step: state file unchanged (still exactly 3 entries) and the isolated service was left in its zombied state — **no 4th restart was issued**, and the escalation was logged explicitly rather than silently retried |

**One honest, documented limitation**: the live zombie signature's exact
client-visible text varied between test runs — one crash surfaced the
literal `"Task group is not initialized"` string in the HTTP body;
another (uvicorn's own generic ASGI error handler reformatting the same
underlying exception) surfaced only a plain `"Internal Server Error"`
body, with the real exception visible only in the server's own log, not
the client response. Both are correctly classified as unhealthy
(`http_error`) and both correctly drive the same restart decision — this
is a cosmetic variance in which `category` string gets logged, never a
functional gap in the detection logic itself.

All test infrastructure (`p53-test-mcp.service` unit file, `/tmp/p53_test/`,
the test state/lock files) was removed after validation. Confirmed via
`systemctl --user list-units --all | grep p53` → no units remain.

## 5. Objective 2 — production timer/service/journal verification

After the isolated validation (§4), production was re-checked:

| | Value |
|---|---|
| `lightrag.service` PID/timestamp/restart-count | `1509060` / `2026-11-06 08:12:54` / `7` — **identical** to the pre-test snapshot (§1); zero disturbance from any isolated testing |
| `lightrag-watchdog.timer` | `active (waiting)`, continuing to fire on schedule |
| `lightrag-watchdog.service` journal (post-update) | Three consecutive firings (`08:33:52`, `08:36:02`, `08:38:12`), each logging `"healthy (ok)"` — the production copy of the file was updated in place to the new Phase 0.53 code, and its healthy-path behavior is confirmed unchanged |
| Production vs. isolated state separation | Production state file (`/home/abehera1992/agno-hive/ops/.lightrag_watchdog_state.json`) still does not exist after all isolated testing — confirming the isolated test's `state_file`/`lock_file` parameters never touched production's own paths |
| `lightrag.service`'s own protocol handshake | `"serverInfo":{"name":"lightrag","version":"1.26.0"}` — unchanged, still the patched SDK |
| Restart target configured in the deployed production script | `SERVICE_NAME = "lightrag.service"` (the module-level default; the production systemd service invokes `main()` with no arguments, so this exact default is what is used) |

No production restart was caused by any of this phase's testing.

## 6. Objective 3 — agno-api.service MCP usage and compatibility findings

**Actual MCP usage, confirmed by direct source inspection:** `agno-
api.service` is **purely an MCP client** — `agno.tools.mcp.MCPTools` and
direct `mcp.client.streamable_http.streamablehttp_client` calls throughout
`swarm/team.py`, connecting *out* to hive-mcp/lightrag-mcp/EkamApp MCP. It
never runs `mcp.run()`/`FastMCP` itself — confirmed via `main.py`'s own
`--serve` branch, which starts a plain FastAPI/uvicorn app
(`from api.server import app`), never an MCP server. **CVE-2025-53365's
own attack shape (a disconnecting client crashing a server's session
manager) therefore does not apply to `agno-api.service` directly** — it
is never the server side of that interaction. The fix is still applied, for
consistency and defense-in-depth, since it was running the identical
vulnerable `1.9.4` shadow for no reason once the fix is proven safe.

**Compatibility, confirmed directly, not assumed:** `main.py` imports
`swarm.team` (and therefore the full `agno[mcp,ollama]` chain) **at module
level, unconditionally**, before dispatching on `--serve` vs.
`--serve-lightrag` — both services run through the exact same import
chain. Since `lightrag.service` had already been running successfully
under the `PYTHONPATH` override for roughly an hour at this point, that is
itself strong evidence — but compatibility was also re-confirmed directly
and independently, standalone, before touching `agno-api.service`:

```
$ PYTHONPATH=/home/abehera1992/miniforge3/envs/zgx/lib/python3.12/site-packages \
  /home/abehera1992/miniforge3/envs/zgx/bin/python -c "
  from swarm import model_routing
  from swarm.team import run_task_async, run_task_stream
  from observability.setup import setup_telemetry
  import mcp; print('mcp:', mcp.__file__)
  import ollama; print('ollama import: OK')
  from agno.tools.mcp import MCPTools
  from mcp.client.streamable_http import streamablehttp_client
  "
mcp: /home/abehera1992/miniforge3/envs/zgx/lib/python3.12/site-packages/mcp/__init__.py
mcp version: 1.26.0
ollama import: OK
agno.tools.mcp.MCPTools import: OK
mcp.client.streamable_http.streamablehttp_client import: OK
ALL IMPORTS SUCCEEDED under the proposed PYTHONPATH override
```

No broad `pip install`/upgrade/reinstall was performed or needed — the
same already-installed, already-proven `mcp==1.26.0` set `lightrag.
service` uses.

## 7. Objective 3 — implementation and targeted live validation

New file, tracked, `ops/agno-api.service.d/cve-2025-53365-mcp-sdk-
isolation.conf` (reference copy; live copy deployed to
`~/.config/systemd/user/agno-api.service.d/` on ZGX, machine-local):

```ini
[Service]
Environment=PYTHONPATH=/home/abehera1992/miniforge3/envs/zgx/lib/python3.12/site-packages
```

**Before state** (captured, §1): PID `1444953`, active since
`07:32:56`, `NRestarts=0`, `/health` → `{"status":"ok",...}`, `/runs` →
`{"runs":[]}`, no `PYTHONPATH` set, `mcp.__file__` resolving to the
vulnerable `~/.local` shadow.

**Applied**: `systemctl --user daemon-reload` → `systemctl --user restart
agno-api.service` (only this one unit; `lightrag.service` was not
touched by this step).

**After state, directly observed:**

| Check | Result |
|---|---|
| Service active | `ActiveState=active`, `SubState=running`, new PID `1599958`, `NRestarts=0` |
| `/health` | `{"status":"ok","mcp_url":"http://100.87.159.86:9000/mcp"}` |
| Running process's own `PYTHONPATH` | Confirmed via `/proc/1599958/environ`: set exactly as deployed |
| Startup journal | Clean — `Application startup complete`, the one `[readiness] WARNING` line is the same pre-existing, unrelated `forward_member_answer` registry warning present since before this session began; no import, transport, or dependency error of any kind |
| `/runs` before the targeted operation | `{"runs":[]}` |
| Targeted real MCP integration exercise | A single small `agno_run` (`read_only=true`, "What port does the EkamApp MCP server run on?") was launched. The journal showed **repeated, successful, real round-trip MCP tool calls** (`project_map` via hive-mcp, each completing in ~1.7-2.0s with no error) — direct, live proof that `agno-api.service`'s own MCP **client** integration (the actual relevant path for this service, distinct from `lightrag.service`'s server-side path) works correctly under `mcp==1.26.0` |
| Cleanup | The task was stopped and the server-side run explicitly cancelled via `POST /runs/{pid}/cancel` (`{"cancelled":1600651,"elapsed_s":165.5}`) rather than left to run out its own budget unnecessarily; `/runs` confirmed back to `{"runs":[]}` immediately after |

**Explicitly not claimed:** this targeted exercise confirms `agno-
api.service`'s own **client-side MCP integration** works under the new
SDK — it is not, and is not presented as, a reproduction of the
CVE-2025-53365 server-crash scenario (§6 already establishes that
scenario does not apply to this service's own role).

## 8. Rollback instructions

**LightRAG (Phase 0.52, unchanged this phase)** — unaffected by anything
in this phase; no rollback action was taken or needed against it.

**agno-api.service (this phase's own new change):**

```bash
rm ~/.config/systemd/user/agno-api.service.d/cve-2025-53365-mcp-sdk-isolation.conf
systemctl --user daemon-reload
systemctl --user restart agno-api.service
```

This removes only the one drop-in introduced in §7 and restarts only
`agno-api.service`. It does not touch, and must never be used to touch,
`lightrag.service`'s own, separately-validated Phase 0.52 drop-in. Not
exercised this phase — `agno-api.service` started cleanly and passed
every check in §7, so rollback was never triggered.

**Watchdog (this phase's own rewrite):** the prior Phase 0.52 version of
`ops/lightrag_watchdog.py` is recoverable from git history
(`git show f9191fc:ops/lightrag_watchdog.py`) if the Phase 0.53 rewrite
ever needs to be reverted; not needed here, since §4/§5 confirm it behaves
correctly.

## 9. Commit and push SHAs

| | Value |
|---|---|
| Files changed this phase | `.gitignore` (watchdog runtime-file ignores), `docs/runbooks/architecture-acceptance-runs/Phase-0.52-report.md` (Objective 1 correction), `ops/lightrag_watchdog.py` (Objective 2 rewrite), `ops/agno-api.service.d/cve-2025-53365-mcp-sdk-isolation.conf` (new, Objective 3) |
| Implementation/documentation commit | recorded after staging — see closing message |
| Pushed | `origin/main` |
| Report commit | recorded after this file is committed — see closing message |

No tracked unit test was added, modified, or run. No unrelated file was
staged (confirmed via `git status --short`/`git diff --stat` reviewed
before staging — every entry outside this phase's own four files is
pre-existing backlog, individually re-confirmed against the Phase 0.52
baseline count).

## 10. ZGX deployment evidence

| | Value |
|---|---|
| ZGX dirty count, mid-phase (before official push/pull) | 37 — exactly the Phase 0.52 baseline of 35 plus this phase's own pre-staged `ops/lightrag_watchdog.py` edit and the new, gitignored `ops/.lightrag_watchdog.lock` runtime file the production watchdog created on its own during normal operation |
| ZGX HEAD (after `git fetch && merge --ff-only`) | recorded after the merge — see closing message |
| Fast-forward cleanliness | to be confirmed at deployment time; no divergence expected, since the pre-staged `ops/lightrag_watchdog.py` content is byte-identical to what this phase commits |

## 11. Service PIDs, start times, loaded MCP versions, and health results (final)

| Service | PID | Active since | NRestarts | MCP version (own handshake / import) | Health |
|---|---|---|---|---|---|
| `lightrag.service` | `1509060` | `2026-11-06 08:12:54` | `7` (all pre-dating this phase) | `1.26.0` (protocol handshake) | Healthy, confirmed via watchdog's own successive `healthy (ok)` log lines |
| `agno-api.service` | `1599958` | `2026-11-06 08:40:05` | `0` | `1.26.0` (direct import check + running process `/proc/<PID>/environ`) | `/health` → `ok`, `/runs` → empty, real MCP client round-trips confirmed working |
| `lightrag-watchdog.timer`/`.service` | n/a (oneshot, timer-triggered) | n/a | n/a | n/a | `active (waiting)`, last several firings all `healthy (ok)` |

## 12. Dirty/untracked preservation evidence

Local: 79 at phase start (78 pre-existing + nothing of this phase's own
yet) → confirmed, after this phase's own edits, that the delta against the
78-item baseline is exactly the four files named in §9, nothing else.
ZGX: 35 at phase start → 37 mid-phase (explained precisely in §10) → the
`.gitignore` addition in this same commit means the `.lock` file (and the
eventual `.lightrag_watchdog_state.json`, once production state exists)
will no longer appear in future `git status` output at all. No destructive
git command was run on either machine at any point in this phase.

## 13. Known limitations and residual risks

1. **The exact client-visible zombie-signature text is not guaranteed
   stable** (§4) — sometimes the literal `"Task group is not
   initialized"` string, sometimes a generic `"Internal Server Error"`
   with the real cause visible only server-side. The watchdog's
   `zombie_confirmed` category will not always fire even on a genuine
   zombie; the broader `http_error` category (which drives the identical
   restart decision) reliably does. This affects only log clarity, never
   the functional detection/restart behavior.
2. **PR #2334's own narrower mid-request-closure `ClosedResourceError`
   path remains unverified against `mcp==1.26.0`** (carried over from
   Phase 0.52, corrected in scope but not newly resolved this phase — see
   §2). The P0 watchdog (§3-§5) is the deliberate defense-in-depth layer
   for this and any other future crash shape, confirmed or not.
3. **Issue #2741's own diagnosability complaint** (clients receiving an
   uninformative empty `-32603` on a session crash) is not addressed by
   anything in this phase — it affects error-message clarity to MCP
   clients generally, not this project's own watchdog, which probes the
   server directly and does not depend on that error message's content.
4. **`agno-api.service`'s own MCP client integration was validated with
   one small, targeted operation, not an exhaustive exercise of every
   MCPTools code path** (e.g. the EkamApp MCP connection specifically, or
   a write-capable task) — the mission's own scope explicitly excluded a
   broader battery; this is a deliberate, bounded validation, not a claim
   of exhaustive coverage.
5. **The watchdog's lock file and state file are both process-local to
   ZGX** (not distributed/clustered) — this is correct and sufficient for
   this project's single-host deployment; it would need rework if
   `lightrag.service` were ever run on more than one host.

## 14. Verdicts

| Area | Verdict |
|---|---|
| Phase 0.52 upstream issue-analysis correction | **PASS** — all four issues/PRs directly re-verified against their actual pages; #2416's mischaracterization corrected, #2741 located and correctly characterized, #3142 added, PR #2334's scope precisely stated; Phase 0.52's own CVE regression evidence unweakened |
| Watchdog detection | **PASS** — real MCP `initialize` handshake, evidence-based category classification (seven distinct categories), confirmed against both a genuinely healthy and a genuinely zombied isolated target |
| Watchdog persistent cooldown | **PASS** — confirmed blocking an immediate second restart using state read back by a *separate, fresh* process invocation, not in-memory state |
| Watchdog rolling-hour budget | **PASS** — confirmed blocking a 4th restart after 3 real restarts within the configured window, with an explicit escalation log and zero silent/masked restart |
| Isolated restart-and-recovery | **PASS** — all 14 required steps directly observed against a real, separate, controllable systemd service; the real, unmodified `main()` implementation was exercised throughout, never a mock or reimplementation |
| Production watchdog deployment | **PASS** — timer enabled and firing on schedule, service executing successfully against the healthy production `lightrag.service`, confirmed zero production disturbance from any of this phase's isolated testing |
| agno-api MCP compatibility | **PASS** — confirmed directly (a standalone import check under the proposed `PYTHONPATH`, plus `lightrag.service`'s own hour-long successful run through the identical import chain) before any change was applied |
| agno-api runtime SDK isolation | **PASS** — deployed, restarted cleanly, `PYTHONPATH` confirmed in the running process, `mcp==1.26.0` confirmed loaded |
| agno-api targeted integration validation | **PASS** — a real, small `agno_run` produced multiple successful round-trip MCP client calls (`project_map` via hive-mcp) with zero import/transport/protocol errors; explicitly not claimed as a CVE server-crash reproduction, since that scenario does not apply to this service's own (client-only) role |
| Deployment and preservation | **PASS** — scoped diff (four files, reviewed before staging), no unrelated file touched, no tracked test added/modified/run, all pre-existing dirty/untracked files on both local and ZGX preserved throughout |
