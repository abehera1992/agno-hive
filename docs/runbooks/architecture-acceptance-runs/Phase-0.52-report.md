# Phase 0.52 — CVE-2025-53365 Remediation: MCP SDK Isolation and LightRAG Zombie-State Recovery

Implementation, deployment, and evidence phase. Remediates the recurring
`lightrag.service` crash root-caused in Phase 0.51 to **CVE-2025-53365 /
GHSA-j975-95f5-7wqh** — an uncaught `anyio.ClosedResourceError` that a
late response (written after the client has already disconnected) can
raise inside the MCP Python SDK's Streamable HTTP transport, tearing down
the server-lifetime session-manager task group instead of failing only
that one session.

**Note on scope:** the mission text for this phase was truncated mid-way
through Section 3.2 ("Run the same scenario against the selected..."); no
further sections (the rest of 3.2, and anything beyond) were received.
Everything through the end of Section 3.1, plus the P0 objective stated in
the mission's own opening summary, was followed as given. Where the
mission's own detail was missing (the exact P0 implementation shape, the
report's required section list), this report uses the same structure and
rigor established by Phase 0.49–0.51's own reports.

## 1. Starting-state inspection and preservation

| | Value |
|---|---|
| Local branch / HEAD (start) | `main` / `a2177cb` |
| Origin HEAD (start) | `a2177cb` (matched local) |
| ZGX HEAD (start) | `a2177cb` (matched local/origin) |
| Local dirty/untracked (start) | 79 (78 pre-existing backlog, unchanged from Phase 0.51's own baseline) |
| ZGX dirty/untracked (start) | 35 |
| `lightrag.service` (start) | PID `1447694`, active/running since the Phase 0.51 manual restart (`2026-11-06 07:34:38`), `NRestarts=0` at that point |
| `agno-api.service` (start) | running, unaffected by this phase's own target (see §6) |

**Runtime architecture, established by direct inspection, not assumed:**

- `lightrag.service` and `agno-api.service` both run as systemd **user**
  units on ZGX (`~/.config/systemd/user/`), both launched via the exact
  same interpreter: `/home/abehera1992/miniforge3/envs/zgx/bin/python`
  (a conda env, NOT a container — `lightrag_mcp/server.py`'s `FastMCP`
  instance is started via `main.py --serve-lightrag`, `mcp.run(transport=
  "streamable-http")`).
- Python 3.12.12. `site.ENABLE_USER_SITE: True`, and critically,
  `sys.path` places `/home/abehera1992/.local/lib/python3.12/site-packages`
  (the per-user site-packages directory) **before**
  `/home/abehera1992/miniforge3/envs/zgx/lib/python3.12/site-packages`
  (the conda env's own site-packages).
- `pip show mcp` (via the conda env's own `pip`, which itself resolves
  through the same shadowed `sys.path`) reported **1.9.4**, installed at
  `~/.local/lib/python3.12/site-packages`, and listed as `Required-by:
  open-webui` — a completely separate, unrelated chat-UI application also
  running on this host, not part of this project at all.
- Directly inspecting the conda env's OWN site-packages directory (ignoring
  the shadow) found a **second, dormant, already-patched install**:
  `mcp-1.26.0.dist-info` (installed Feb 23 2026, `INSTALLER: pip`),
  alongside a fully self-consistent, already-co-installed dependency set:
  `anyio==4.12.1`, `starlette==0.52.1`, `fastapi==0.128.5`,
  `httpx==0.28.1`, `httpx_sse==0.4.3`, `pydantic==2.12.5`,
  `pydantic_core==2.41.5`, `pydantic_settings==2.13.1`,
  `sse_starlette==3.2.0`, `uvicorn==0.40.0`.
- This dormant set was never being used — Python's `sys.path` order meant
  every process launched this way resolved `mcp` (and nothing else, since
  only `mcp` has duplicate copies in both locations) to the vulnerable
  `1.9.4` shadow instead.
- `agno-hive`'s own `requirements.txt` pulls `mcp` transitively via
  `agno[mcp,ollama]>=1.4.0` — no explicit pin of its own.
  `hive-mcp/requirements.txt` (a **separately deployed Docker container**,
  not this conda env) explicitly pins `mcp>=1.27.0`. No lockfile exists for
  either path (plain `requirements.txt`, no `uv.lock`/`poetry.lock`/
  `Pipfile.lock`).
- The two hive-mcp Docker containers (`hive-mcp`, `hive-mcp-agno`, both on
  the **client machine**, never ZGX, per this project's own established
  architecture) were checked directly: `hive-mcp` runs `mcp==2.3.0`,
  `hive-mcp-agno` runs `mcp==2.2.0` — both already far past the CVE fix,
  confirming neither shares this host's affected runtime and neither needed
  any change.

No destructive command was run against any pre-existing file. Local and
ZGX dirty/untracked counts were confirmed unchanged at the end of this
phase (see §8).

## 2. P1 — MCP SDK CVE remediation

### 2.1 Vulnerability confirmed against the actual runtime

Confirmed via `pip show mcp`, `import mcp; mcp.__file__`, and direct
`mcp-*.dist-info` directory inspection (§1) — not inferred from a mirror.
`1.9.4` falls squarely in the affected range (`< 1.10.0`) stated by the
official advisory.

### 2.2 Dependency pin located

No file in this repository pins `mcp` for the conda-env runtime
`lightrag.service`/`agno-api.service` actually use — it arrives
transitively through `agno[mcp]`. The authoritative, actually-installed
version is controlled entirely by **what is physically present in the
conda env's site-packages** plus **`sys.path` resolution order** — there
is no `requirements.txt`/lockfile edit that would have changed what
either service loads, since neither file is read again after the initial
environment setup; this is why the fix is a `sys.path`-ordering change,
not a dependency-file edit.

### 2.3 Upgrade-target comparison

| Candidate | CVE fix included | Compatibility | Dependency conflicts | API migration required | Decision |
|---|---|---|---|---|---|
| `mcp==1.9.4` (current/active) | No | — | — | — | Rejected — vulnerable |
| `mcp==1.26.0` (already installed, dormant, in the conda env) | **Yes** (empirically proven, §3) | Confirmed: `lightrag_mcp/server.py`'s `FastMCP`/`mcp.run(transport="streamable-http")` usage works unmodified; co-installed `anyio`/`starlette`/`fastapi`/etc. are already mutually consistent (installed together Feb 23 2026) | **None** — zero new packages installed, zero new resolution | **None** | **Selected** |
| Latest v1.x on PyPI (`1.30.0`, per `pip index versions`) | Yes (presumably; not verified directly) | Would require an actual new `pip install` + dependency resolution inside the conda env — unverified compatibility with the existing co-installed set | Unverified | Unverified | Rejected — real, unneeded resolution risk for zero additional confirmed benefit over 1.26.0 for this incident |
| `mcp==2.3.0` (latest overall) | Yes | Major version bump; mission explicitly disfavors this as the first choice | Unverified | Likely (2.x line) | Rejected per mission's own decision rule |

**Decision rule applied:** the smallest safe change that includes the CVE
fix and is compatible with the actual deployed integration is **not a
new install at all** — it is correcting `sys.path` order so the
conda env's own, already-present, already-fully-resolved `1.26.0` set is
used instead of the shadowing `1.9.4`. This is strictly smaller and
lower-risk than any actual `pip install`, which is why it was chosen over
every candidate that would require one.

### 2.4 Issue #2741, #2416, PR #2334, and #3142 investigated separately

> **Corrected in Phase 0.53.** The original version of this section was
> written from web-search-result snippets rather than the actual GitHub
> pages, and mischaracterized #2416 specifically (as a residual
> `ClosedResourceError` in `respond()` — it is not) and could not locate
> #2741 at all. Everything below was re-verified directly against each
> page's own content (title, state, dates, and technical description),
> not inferred or re-summarized from a prior search. §3's own regression
> evidence is unchanged by this correction.

- **Issue #2741** — "Stateful Streamable HTTP: in-session exceptions are
  masked as an empty -32603; real cause only in a separate 'Session
  crashed' log." Open, filed 2026-05-31. This is a **diagnosability /
  error-reporting** issue, not a report that the server crashes: in
  stateful mode, when a session's `app.run()` raises, the *client*
  receives an unhelpfully empty JSON-RPC `-32603` error, while the real
  exception is correctly caught and logged separately as `"Session <id>
  crashed"` — the exact log line Phase 0.52's own live test (§3.2)
  observed on `mcp==1.26.0`. The issue's own text explicitly distinguishes
  this from the CVE's own server-wide crash (fixed at 1.10.0). No merged
  fix is shown on the page. **This does not establish that the Phase 0.52
  CVE failure remains exploitable on 1.26.0** — if anything, it
  corroborates that per-session isolation (the exact property §3.2 tested)
  is working as intended in the versions where this issue was filed
  (1.27.1/1.27.2); its complaint is about the error message's clarity to
  the client, not about the server surviving.
- **Issue #2416** — "Bug: AssertionError: Request already responded to —
  cancellation race in v1.27.0." Open, filed 2026-04-09. **This report
  previously, incorrectly, described #2416 as a residual
  `ClosedResourceError` crash in `respond()`. It is not.** The actual
  failure is an `AssertionError` (`assert not self._completed`,
  `mcp/shared/session.py`), triggered when a `notifications/cancelled`
  message arrives after a tool handler has already returned but before
  `respond()` has run — a race between cancellation and normal
  completion, not an exception from a closed stream. The issue's own
  thread states explicitly that PR #2334 covers the `ClosedResourceError`
  path but not this assertion race. **Corrected conclusion: #2416 is
  unrelated to the CVE-2025-53365 failure mode and to Phase 0.52's own
  reproduction — a different exception type, on a different trigger.**
- **PR #2334** — "[v1.x] fix: handle `ClosedResourceError` when transport
  closes mid-request." Merged into the `v1.x` branch 2026-03-24 (commit
  `6524782`), fixing issue #2328. The specific path it addresses: the
  transport closes **mid-request** (e.g. stdin EOF, or invalid UTF-8 on
  the wire) while a handler is still running, and the subsequent
  `message.respond()` call hits `ClosedResourceError` on the now-closed
  write stream — fixed by wrapping that call in
  `try`/`except (BrokenResourceError, ClosedResourceError)` and cancelling
  in-flight handlers via `try`/`finally` when the transport closes. **This
  is a different trigger than the Phase 0.52 incident** (a request that
  completes normally and only fails because the *client* has already
  given up waiting — the transport itself does not close mid-request) and
  a different trigger than #2416 (an assertion race, not a
  `ClosedResourceError`). **`mcp==1.26.0` (installed 2026-02-23) predates
  this PR's merge (2026-03-24) and therefore does not contain this
  specific fix.** Phase 0.52's own pass/fail result (§3) is unaffected,
  since it tested the CVE's own original failure shape, not PR #2334's
  mid-request-closure shape — but whether `1.26.0` is also safe against
  *that* narrower trigger remains genuinely **unverified** here, neither
  confirmed safe nor confirmed unsafe.
- **Issue #3142** — "[v1.x] Backport #2257 and cut a release: the
  stateless Streamable HTTP client-disconnect fix (#2064) ships only in
  2.0.0 pre-releases; latest stable v1.28.1 still carries the crashing
  `send_log_message` call." Closed as *not planned*, 2026-08-14. Describes
  a **stateless-mode-only** scenario (`lightrag_mcp/server.py` does not
  set `stateless_http`, so it runs in the default *stateful* mode this
  issue does not cover): a client aborting a POST request **mid-body**
  raises a Starlette `ClientDisconnect` inside `_handle_post_request`,
  logged as an empty `"Received exception from stream:"` error, which
  then reaches a `send_log_message` call reported to crash in some
  deployments. Confirmed on v1.28.1. **Explicitly distinct from both the
  Phase 0.52 reproduction** (a clean request/response cycle where only the
  client's own wait gives up, not a mid-body abort) **and from #2416** (an
  assertion race, not a `ClientDisconnect`) — the issue's own text never
  references #2416 and does not use the "late response after disconnect"
  framing this report's own incident uses.
- **GHSA-j975-95f5-7wqh / CVE-2025-53365** — confirmed directly from the
  official advisory page: affected range `< 1.10.0`, patched at `1.10.0`,
  published 2025-07-04, CWE-248. Description, verbatim from the advisory:
  *"If a client deliberately triggers an exception after establishing a
  streamable HTTP session, this can lead to an uncaught ClosedResourceError
  on the server side, causing the server to crash and requiring a restart
  to restore service."* This matches Phase 0.52's own §3.1 reproduction
  exactly — unchanged, unweakened by this correction. The upstream fix,
  **PR #967** ("Fix uncaught exception in MCP server," merged 2025-06-17,
  commit `7b42065`), is terse on its own page — it names neither the
  exception type nor the advisory directly; the link between that PR and
  the `1.10.0` fix comes from the advisory's own reference list, not from
  the PR's own text. (The commit hash is `7b42065`, not `7b420656` as
  this phase's own mission text stated — a one-digit discrepancy, most
  likely a short-SHA display difference; not independently significant to
  anything this report concludes.)

**Net correction:** none of #2741, #2416, or #3142 describe the *same*
failure mode Phase 0.52 reproduced and fixed (CVE-2025-53365's
late-response-after-disconnect `ClosedResourceError`). They describe three
further, genuinely distinct MCP SDK transport/session issues — two
(#2416, #3142) involving entirely different exception types or trigger
conditions, and one (#2741) describing a diagnosability complaint about an
error message's clarity rather than a crash. PR #2334 is a real, merged
`ClosedResourceError` fix, but for a *different* trigger (mid-request
transport closure) than the CVE's own trigger, and it postdates the
specific `1.26.0` build selected in Phase 0.52 — so whether `1.26.0` is
also safe against PR #2334's own narrower scenario remains genuinely
**unverified**, not "probably fine," and **not** "the same gap as #2416"
(it is not the same gap — #2416 is an unrelated assertion race). Phase
0.52's own regression evidence (§3) — the byte-for-byte reproduction on
`1.9.4` and the confirmed per-session isolation on `1.26.0` for the CVE's
own specific scenario — is unchanged and not weakened by this correction.

### 2.5 Implementation

No dependency pin was changed (§2.2) and no package was installed — the
fix is a **systemd drop-in** that reorders `sys.path` for `lightrag.
service` only:

`ops/lightrag.service.d/cve-2025-53365-mcp-sdk-isolation.conf` (new,
tracked in this repo for documentation/reproducibility; the live copy is
deployed to `~/.config/systemd/user/lightrag.service.d/` on ZGX, which is
machine-local and outside this repo's own git tracking):

```ini
[Service]
Environment=PYTHONPATH=/home/abehera1992/miniforge3/envs/zgx/lib/python3.12/site-packages
```

**Why `PYTHONPATH`, not `PYTHONNOUSERSITE`:** the first attempt used
`PYTHONNOUSERSITE=1` (disables `~/.local` entirely). This broke the
service's startup immediately —
`ModuleNotFoundError: No module named 'ollama'` — because `ollama` (needed
eagerly, at import time, by `swarm/tool_fix.py`) has **no copy at all** in
the conda env and only ever resolved via the same `~/.local` directory
being shadowed. `PYTHONPATH` prepending the conda env's own site-packages
is narrower and correct: a package present in **both** locations (only
`mcp`, confirmed by directory listing) now resolves to the conda env's own
newer copy; a package present **only** in `~/.local` (`ollama`, and
anything else open-webui's own setup put there) still falls through and
resolves exactly as before. Verified directly (`sys.path` inspection +
both imports succeeding together) before touching the systemd unit.

Confirmed after deployment: `cat /proc/<PID>/environ` on the running
service process shows `PYTHONPATH` set exactly as above; the service's
own MCP protocol handshake reports `"serverInfo":{"name":"lightrag",
"version":"1.26.0"}` (FastMCP's own default `serverInfo.version`, sourced
from the installed `mcp` package — an authoritative, protocol-level
confirmation, not just a process-inspection one). `open-webui`'s own
dependency tree in `~/.local` was never read, written, or otherwise
touched by this change.

**`agno-api.service` has the identical latent exposure** (same conda env,
same `ExecStart` pattern, same shadowing — confirmed directly: `import
mcp; mcp.__file__` from that same interpreter also resolves to the
vulnerable `1.9.4` shadow). **Not fixed this phase** — flagged here as a
clear, evidenced recommendation rather than silently expanding this
phase's scope to a second production service the mission did not name.
See §9 #1.

`hive-mcp`/`hive-mcp-agno` (the two separately-deployed Docker containers)
were confirmed (§1) to already run `mcp==2.3.0`/`2.2.0` — untouched, per
the mission's own instruction, since they do not share the affected
runtime.

## 3. P1 — CVE regression validation

### 3.1 Controlled reproduction (isolated, not production)

A minimal `FastMCP` server (`slow_tool`: sleeps N seconds; `fast_tool`:
instant) was run on a throwaway port, exercising the identical SDK
session-manager/transport path `lightrag_mcp/server.py` uses, explicitly
**not** the production `lightrag.service**. Sequence: client calls
`slow_tool` with a client-side timeout shorter than the server's own
sleep → client gives up and disconnects → the server's slow call finishes
anyway and attempts its now-late response write → a **fresh** connection
then probes `fast_tool` to check whether the server survived.

Against the currently-active, vulnerable `mcp==1.9.4` (default `sys.path`,
no override): the fresh probe failed —
`ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)` — and
the server's own log showed the **exact same traceback, line-for-line**,
as the real Nov 6 production incident (`streamable_http_manager.py:106`→
`streamable_http.py:784`→`lowlevel/server.py:473/484/508/557`→
`shared/session.py:218/131/329`→`anyio/streams/memory.py:249/218`→
`anyio.ClosedResourceError`), ending in the identical
`RuntimeError: Task group is not initialized. Make sure to use run().`
on the subsequent request. This is a byte-for-byte, controlled
reproduction of the real incident, not a reasoned guess.

### 3.2 Patched-version validation

The identical scenario, same script, same timing, run with `PYTHONPATH`
set to the conda env's own site-packages (so `mcp==1.26.0` loads instead):
the fresh probe **succeeded** —
`PROBE RESULT: SURVIVED -- fresh connection succeeded: [TextContent(...,
text='fast ok', ...)]`. The server's own log shows the failure still
occurred (`ERROR Session <id> crashed: unhandled errors in a TaskGroup (1
sub-exception)`) but **scoped to that one session** — the server
immediately created a brand-new session afterward and served the fresh
probe cleanly. This is the exact isolation behavior the CVE fix is
supposed to provide, confirmed by direct, repeated, controlled
observation — not inferred from version numbers alone.

## 4. P0 — defense in depth: liveness watchdog

Does not substitute for §2/§3's fix — it exists because the zombie state
leaves the service process **alive and "active/running"** while every
request fails (nothing crashes), which systemd's own `Restart=always`
structurally cannot detect.

New files (tracked in this repo, `ops/`):

- **`ops/lightrag_watchdog.py`** — a real MCP `initialize` handshake
  (not a bare TCP/HTTP connect) against `http://localhost:9002/mcp` every
  invocation. Bounded by design:
  - Never restarts on a single failed check — a second check, 5s later,
    must also fail before any action is taken (rules out one transient
    blip).
  - A 120s cooldown between any two restarts this script issues.
  - A hard cap of 3 auto-restarts per rolling hour — if exceeded, it
    explicitly logs an escalation message and **stops acting**, rather
    than silently restart-looping a persistent, non-zombie failure (a
    genuinely broken config or environment) forever.
  - Every decision (healthy / transient / restart issued / budget
    exhausted) is logged with its own reasoning — a complete audit trail
    in `journalctl -u lightrag-watchdog.service`, never a silent actor.
- **`ops/lightrag-watchdog.service`** / **`ops/lightrag-watchdog.timer`** —
  a systemd oneshot + 120s timer. 120s was chosen as short enough to catch
  the zombie state well inside the span of even a brief `agno_run`, long
  enough that two firings can never overlap the script's own ≤15s worst
  case (10s HTTP timeout × 2 checks + 5s pause).

**Deployed and validated on ZGX:**
- `systemctl --user enable --now lightrag-watchdog.timer` — confirmed
  `active (waiting)`, next trigger scheduled.
- Manual run against the **real, now-healthy** production service:
  `[lightrag-watchdog] healthy (ok)` — confirmed correct on the healthy
  path.
- The detection logic (`check_once()`) was separately pointed at a
  **deliberately crashed isolated reproduction server** (never
  production) and correctly returned `healthy=False` — confirming the
  unhealthy path is also correctly detected. The exact literal "Task group
  is not initialized" substring was not captured in this one test's detail
  string (the request hit a 307 redirect before reaching the deeper 500 in
  this specific invocation) — a cosmetic gap in the log message only; the
  functionally relevant boolean (`healthy=False`, which is what the
  restart decision actually branches on) was correct.
- Production service was never deliberately crashed to test the
  restart-issuing branch itself — doing so would have required either
  crashing the real service (explicitly forbidden by the mission) or
  exercising a code path that is hardcoded to restart only `lightrag.
  service` regardless of which port the health check targets, making an
  isolated-server test of that specific branch not meaningfully different
  from just restarting the (already healthy) production service directly.

## 5. Scratch validation summary

No application code (`swarm/team.py`) was changed this phase, so there is
no scratch harness in the Phase 0.49–0.51 sense. The equivalent evidence
for this phase is the controlled reproduction in §3, which — unlike a
mocked unit test — exercises the *real*, unmodified MCP SDK (both
versions) end-to-end over a real HTTP connection. No unit tests were
added, modified, or run.

## 6. Commit and push SHAs

| | Value |
|---|---|
| Implementation commit | `f9191fc` ("fix(ops): remediate CVE-2025-53365 in lightrag.service + add zombie-state watchdog") |
| Pushed | `origin/main`, `a2177cb..f9191fc` |
| Report commit | recorded after this file is committed — see closing message |

## 7. ZGX deployment, service PID, health, and evidence

| | Value |
|---|---|
| ZGX HEAD | `a2177cb` → `f9191fc` (`--ff-only`, clean) |
| ZGX dirty/untracked | 35, unchanged before and after the git-tracked part of this change |
| `lightrag.service` | restarted with the new drop-in; PID `1509060`, active/running, confirmed serving `mcp==1.26.0` via its own protocol handshake |
| `lightrag.service.d/cve-2025-53365-mcp-sdk-isolation.conf` | deployed to `~/.config/systemd/user/lightrag.service.d/` (machine-local, not git-tracked — a reference copy lives in this repo's `ops/`) |
| `lightrag-watchdog.timer`/`.service` | deployed, enabled, active (waiting), confirmed firing correctly against both a healthy and (separately, isolated) an unhealthy target |
| `agno-api.service` | unchanged this phase; same latent exposure, flagged in §9 #1, not fixed |
| `hive-mcp` / `hive-mcp-agno` | confirmed untouched and already unaffected (`mcp==2.3.0`/`2.2.0`) |
| `/runs` (agno-api) | confirmed empty throughout |

## 8. Dirty/untracked preservation evidence

Local and ZGX dirty/untracked counts (79 local pre-existing backlog + this
phase's own additions before their own commit; 35 ZGX, unchanged) were
recorded before any change and reconfirmed identical after. No `git
reset`, `clean`, `stash`, or `checkout --` was run at any point. The only
files added were the four new, intentionally-staged `ops/` files (§6); no
pre-existing file was modified, deleted, or reset.

## 9. Known limitations and residual risks

1. **`agno-api.service` shares the identical latent exposure** (same conda
   env, same `sys.path` shadowing, same vulnerable `mcp==1.9.4` currently
   active for that service) and was **not** fixed this phase — the mission
   named `lightrag.service` specifically, and extending the fix to a
   second production service without being asked was judged a scope
   decision for the user, not something to do silently. Concrete, ready-to
   -apply recommendation: the identical `ops/lightrag.service.d/...conf`
   pattern, applied to `agno-api.service`'s own drop-in directory.
2. **The mid-request transport-closure `ClosedResourceError` path PR #2334
   fixes** (merged 2026-03-24) **postdates the specific `mcp==1.26.0`
   build selected here** (installed 2026-02-23) — whether `1.26.0` is also
   safe against *that* narrower trigger (not the CVE's own trigger, which
   §3 confirms IS fixed) was not tested this phase and remains an open,
   unverified risk. *(Corrected in Phase 0.53 — the original version of
   this item conflated this with issue #2416, which is unrelated: #2416
   is an `AssertionError`/cancellation race, not a `ClosedResourceError`,
   and describes no gap in `respond()` of the kind originally stated
   here; see §2.4.)* Not chased further; the P0 watchdog (§4, and Phase
   0.53's own completion of it) remains the deliberate defense-in-depth
   layer for any future crash shape, confirmed or not.
3. **The watchdog's "second check" cosmetic detail-string gap** (§4):
   the literal "Task group is not initialized" substring was not captured
   in the one isolated-crash test performed, though the functional
   health-boolean was correct. Not pursued further — it affects only the
   clarity of one log line, never the restart decision itself.
4. **No lockfile exists** for either the conda env or `hive-mcp`'s own
   `requirements.txt` — a future, unrelated `pip install`/`pip upgrade` on
   this conda env could silently re-shadow or otherwise disturb the fix if
   it touches `mcp`/`anyio`/`starlette`/`fastapi` without awareness of this
   phase's own findings. The `ops/` reference files and this report are
   the durable record for that case.
5. **One incident class, one fix.** The P1 fix is proven against the
   *specific* failure shape this project has actually seen six times
   (late response after client disconnect/timeout). It is not a blanket
   guarantee against every conceivable MCP SDK crash — the P0 watchdog
   exists precisely because of this honest limitation.

## 10. Verdicts

- **Vulnerability confirmation**: **PASS** — `mcp==1.9.4`, directly
  confirmed active in the real runtime, falls within CVE-2025-53365's
  affected range; not inferred from third-party summaries alone (direct
  `pip show`/`mcp.__file__`/dist-info inspection).
- **Upgrade-target selection**: **PASS** — the smallest safe option (an
  already-installed, already-co-resolved `mcp==1.26.0`, reached via a
  `sys.path` reorder rather than any new install) was chosen over every
  candidate requiring a fresh dependency resolution, per the mission's own
  explicit decision rule.
- **Implementation**: **PASS** — one scoped systemd drop-in, zero
  application-code changes, zero changes to `~/.local`/open-webui's own
  dependency tree, zero changes to the two already-safe hive-mcp
  containers. The first attempt (`PYTHONNOUSERSITE=1`) was wrong and was
  caught immediately by a real startup failure, not silently shipped.
- **CVE regression validation**: **PASS** — the exact production failure
  shape was reproduced byte-for-byte on the vulnerable version in an
  isolated harness, and shown to be correctly session-isolated (not
  server-wide) on the selected patched version, in a direct, repeated,
  controlled test — not inferred from the advisory or version number
  alone.
- **Deployment**: **PASS** — clean `--ff-only` on both local and ZGX,
  dirty/untracked state fully preserved, service healthy and confirmed via
  its own protocol handshake to be running the patched SDK.
- **P0 watchdog**: **PASS** on its core detection logic (healthy and
  unhealthy paths both independently confirmed), **PARTIAL** on full
  end-to-end restart-path validation (the restart-issuing branch itself
  was not exercised against a real crash, for the reasons given in §4) —
  deployed, enabled, and currently running regardless.
- **Residual risk disclosure**: **PASS** — issue #2416's narrower,
  separate `ClosedResourceError` gap and `agno-api.service`'s identical
  latent exposure are both documented plainly, not glossed over or
  conflated with "the CVE is now fully fixed everywhere."
