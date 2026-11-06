#!/usr/bin/env python3
"""Phase 0.52/0.53 (P0, defense in depth): bounded liveness check +
auto-recovery for lightrag.service's zombie-session-manager failure mode.

This does NOT replace the Phase 0.52 P1 fix (the PYTHONPATH reorder that
gets lightrag.service running the patched mcp==1.26.0, which isolates a
late-response ClosedResourceError per-session instead of tearing down the
whole server). This exists only as a second, independent layer: the exact
failure this project has already seen SIX times (2026-10-23 x5,
2026-11-06 x1) leaves the systemd unit's own process alive and
"active/running" while every MCP request fails -- a state systemd's own
Restart=always cannot detect, because nothing crashes. This script is the
external check that CAN detect it.

Bounded by design:
  - A real MCP `initialize` handshake is the health signal, not just a TCP
    connect or a bare HTTP GET -- the zombie state still accepts TCP
    connections and answers some routes; only a real session-level request
    actually exercises the broken code path.
  - A restart is only ever issued after TWO consecutive failed checks
    (never on a single transient blip -- e.g. the service mid-restart from
    something else, or one slow LLM call saturating the loop for a moment).
  - A COOLDOWN_S floor between any two restarts this script issues, and a
    MAX_RESTARTS_PER_WINDOW hard cap within WINDOW_S -- if the service is
    failing for a DIFFERENT, non-zombie reason (disk full, bad config, the
    conda env itself broken), this refuses to restart-loop forever and
    hide that from whoever is watching; it logs an explicit escalation
    message and stops acting instead.
  - Every decision (healthy / transient failure / restart issued / budget
    exhausted) is logged with its own reasoning, so journalctl -u
    lightrag-watchdog.service is a complete audit trail, never a silent
    actor.

Phase 0.53 additions (the process-memory/persistence gap Phase 0.52 left
open -- this script is invoked fresh by a systemd timer every 120s, so
nothing held only in Python variables survives between invocations):
  - State (prior restart timestamps) is written ATOMICALLY (tempfile in
    the same directory + os.replace) so a kill mid-write can never leave a
    half-written, corrupt JSON file behind.
  - State is read back FAIL-CLOSED, not fail-open: if the file is missing,
    this is legitimately "no restarts yet" (an empty budget, safe to
    start from). But if the file EXISTS and fails to parse, that is
    treated as "budget state unknown" and refused -- NOT silently reset to
    an empty budget, which would hand out a fresh restart allowance at
    exactly the moment something is already wrong enough to have corrupted
    the file. This is a deliberate, narrow exception to "fail open" for
    the one case where failing open would defeat the budget's own purpose.
  - A dedicated lock file (`flock`, non-blocking) guards the ENTIRE
    decision body, so two overlapping invocations (a slow one plus the
    next timer firing) can never both reach the restart-issuing branch at
    once -- the second one logs and exits immediately instead of racing.
  - After issuing a restart, one follow-up health check confirms (and
    explicitly logs) whether the service actually came back -- "restart
    issued" and "restart issued AND confirmed healthy again" are recorded
    as distinct outcomes, never conflated.
  - Every tunable (MCP URL, systemd service name, state/lock file paths,
    timing) is a `main()` parameter with the real production value as its
    default -- so a test can call the SAME, real `main()` against an
    isolated target with zero risk of it ever touching
    `lightrag.service` or production state, instead of a parallel
    reimplementation or a mocked decision.
"""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

MCP_URL = "http://localhost:9002/mcp"
SERVICE_NAME = "lightrag.service"
TIMEOUT_S = 10
STATE_FILE = "/home/abehera1992/agno-hive/ops/.lightrag_watchdog_state.json"
LOCK_FILE = "/home/abehera1992/agno-hive/ops/.lightrag_watchdog.lock"
COOLDOWN_S = 120
WINDOW_S = 3600
MAX_RESTARTS_PER_WINDOW = 3
SECOND_CHECK_DELAY_S = 5
POST_RESTART_WAIT_S = 5


def _init_payload() -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "lightrag-watchdog", "version": "1"}},
    }).encode("utf-8")


def log(msg: str) -> None:
    print(f"[lightrag-watchdog] {msg}", flush=True)


def check_once(mcp_url: str = MCP_URL, timeout_s: float = TIMEOUT_S) -> tuple[bool, str, str]:
    """One real MCP initialize handshake. Returns (healthy, category, detail).

    `category` is one of: ok, connection_error, timeout, unexpected_redirect,
    http_error, zombie_confirmed, malformed_response -- deliberately NOT a
    bare bool-and-string, so every caller can tell "the service process
    itself seems to be gone" (connection_error) apart from "it answered but
    the session manager is the known zombie" (zombie_confirmed) apart from
    "it answered something we don't recognize at all" (malformed_response),
    rather than equating every non-200 with a confirmed zombie.
    """
    req = urllib.request.Request(
        mcp_url, data=_init_payload(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                return False, "http_error", f"HTTP {resp.status}"
            body = resp.read(4096).decode("utf-8", errors="replace")
            if '"serverInfo"' not in body or '"result"' not in body:
                return False, "malformed_response", f"no serverInfo/result: {body[:200]!r}"
            return True, "ok", "ok"
    except urllib.error.HTTPError as exc:
        # A redirect (3xx) or any other non-2xx status lands here when
        # urllib refuses to follow it automatically (e.g. a 307 on a POST,
        # which urllib deliberately does not auto-replay). The expected,
        # correct endpoint for this server is a bare POST to `mcp_url`
        # with NO trailing slash and returns 200 directly (confirmed live,
        # Phase 0.52) -- any redirect back out of THIS exact call is
        # therefore unexpected on its own terms, not something to silently
        # follow to a DIFFERENT endpoint we never asked for.
        if exc.code in (301, 302, 303, 307, 308):
            location = exc.headers.get("Location", "<none>") if exc.headers else "<none>"
            return False, "unexpected_redirect", f"HTTP {exc.code} -> {location}"
        body = exc.read().decode("utf-8", errors="replace")
        if "Task group is not initialized" in body:
            return False, "zombie_confirmed", "CONFIRMED zombie signature: 'Task group is not initialized'"
        return False, "http_error", f"HTTPError {exc.code}: {body[:200]!r}"
    except TimeoutError:
        return False, "timeout", f"no response within {timeout_s}s"
    except urllib.error.URLError as exc:
        # Covers connection-refused (process not listening -- e.g. mid
        # systemd-managed restart from an unrelated crash, or genuinely
        # down) and DNS/socket-level failures. Distinct from a confirmed
        # zombie: here the process itself may not even be there to be a
        # zombie, which is useful context in the log even though this
        # script's own restart action is the same for the "confirmed after
        # two checks" branch either way.
        return False, "connection_error", f"{type(exc.reason).__name__ if exc.reason else 'URLError'}: {exc}"
    except Exception as exc:  # noqa: BLE001 -- this IS the generic health probe
        return False, "connection_error", f"{type(exc).__name__}: {exc}"


class _StateUnreadable(Exception):
    """Raised when the state file exists but cannot be parsed -- distinct
    from "does not exist yet", which is a legitimate empty-budget start."""


def load_state(state_file: str) -> dict:
    if not os.path.exists(state_file):
        return {"restarts": []}
    try:
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("restarts", []), list):
            raise ValueError("state file has the wrong shape")
        # Defensive: every entry must be a real number (a past time.time()
        # epoch float) -- never a string that would need date PARSING (the
        # entire reason this design stores raw epoch floats instead of
        # formatted timestamps: there is no clock/format-parsing failure
        # mode to have at all).
        clean = [t for t in data["restarts"] if isinstance(t, (int, float))]
        if len(clean) != len(data["restarts"]):
            raise ValueError("state file contains a non-numeric restart timestamp")
        return {"restarts": clean}
    except Exception as exc:  # noqa: BLE001
        raise _StateUnreadable(str(exc)) from exc


def save_state(state_file: str, state: dict) -> bool:
    """Atomic write: build the new content in a temp file in the SAME
    directory, then os.replace() it over the real path -- a process killed
    mid-write leaves either the old file or the new one intact, never a
    half-written one, on any POSIX filesystem (os.replace is atomic there).
    Returns True on success, False (logged) on failure -- never raises."""
    directory = os.path.dirname(state_file) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".watchdog_state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, state_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: could not persist state file atomically ({exc}) -- "
            f"rate limiting may be inaccurate until this is fixed")
        return False


def main(
    mcp_url: str = MCP_URL,
    service_name: str = SERVICE_NAME,
    state_file: str = STATE_FILE,
    lock_file: str = LOCK_FILE,
    timeout_s: float = TIMEOUT_S,
    cooldown_s: float = COOLDOWN_S,
    window_s: float = WINDOW_S,
    max_restarts: int = MAX_RESTARTS_PER_WINDOW,
    second_check_delay_s: float = SECOND_CHECK_DELAY_S,
    post_restart_wait_s: float = POST_RESTART_WAIT_S,
) -> int:
    """The real, complete decision body. Every argument defaults to the
    real production target; a caller validating this against an isolated
    service passes isolated values for ALL of mcp_url/service_name/
    state_file/lock_file so there is no code path by which that call could
    ever reach, restart, or persist state against the real
    `lightrag.service` or its real state file."""
    os.makedirs(os.path.dirname(lock_file) or ".", exist_ok=True)
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another invocation is already in progress (lock held) -- "
                "exiting without checking anything, to avoid a racing "
                "restart decision")
            return 10

        healthy, category, detail = check_once(mcp_url, timeout_s)
        if healthy:
            log(f"healthy ({detail})")
            return 0

        log(f"first check failed ({category}): {detail} -- re-checking once "
            f"before acting (never restart on a single transient failure)")
        time.sleep(second_check_delay_s)
        healthy2, category2, detail2 = check_once(mcp_url, timeout_s)
        if healthy2:
            log(f"second check passed ({detail2}) -- treating the first "
                f"failure ({category}) as transient, not restarting")
            return 0

        log(f"second check also failed ({category2}): {detail2} -- confirmed, not transient")

        now = time.time()
        try:
            state = load_state(state_file)
        except _StateUnreadable as exc:
            log(f"ESCALATION: restart-budget state file exists but could not "
                f"be read ({exc}) -- refusing to restart with an unverifiable "
                f"budget rather than silently resetting it to empty. Needs a "
                f"human to inspect {state_file!r} directly.")
            return 4

        recent = [t for t in state["restarts"] if now - t < window_s]

        if recent and (now - recent[-1]) < cooldown_s:
            log(f"last auto-restart was {now - recent[-1]:.0f}s ago, inside "
                f"the {cooldown_s:.0f}s cooldown -- NOT restarting again yet, "
                f"waiting it out")
            return 1

        if len(recent) >= max_restarts:
            log(f"ESCALATION: {len(recent)} auto-restart(s) already issued in "
                f"the last {window_s:.0f}s (budget {max_restarts}) -- this no "
                f"longer looks like the known zombie-session-manager failure "
                f"mode (that one is fixed by a single restart and does not "
                f"recur this fast). Refusing to restart again -- this needs a "
                f"human to look at it directly "
                f"(journalctl -u {service_name}).")
            return 2

        log(f"issuing systemctl --user restart {service_name} "
            f"({len(recent)} prior restart(s) in the last {window_s:.0f}s)")
        result = subprocess.run(
            ["systemctl", "--user", "restart", service_name],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"ESCALATION: systemctl restart itself failed (exit "
                f"{result.returncode}): {result.stderr.strip()} -- needs a human")
            return 3

        recent.append(now)
        if not save_state(state_file, {"restarts": recent}):
            log("WARNING: restart was issued but the new timestamp could not "
                "be persisted -- the budget/cooldown may undercount on the "
                "NEXT invocation until this is fixed")

        time.sleep(post_restart_wait_s)
        recovered, recat, rdetail = check_once(mcp_url, timeout_s)
        if recovered:
            log(f"restart issued and recovery CONFIRMED -- fresh handshake "
                f"succeeded ({rdetail})")
            return 0
        log(f"restart issued but recovery NOT yet confirmed -- post-restart "
            f"check still failing ({recat}: {rdetail}); the service may still "
            f"be starting, or this is a different, non-zombie failure. The "
            f"next scheduled invocation will re-check.")
        return 5
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
