#!/usr/bin/env python3
"""Phase 0.52 (P0, defense in depth): bounded liveness check + auto-recovery
for lightrag.service's zombie-session-manager failure mode.

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
"""
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error

MCP_URL = "http://localhost:9002/mcp"
TIMEOUT_S = 10
STATE_FILE = "/home/abehera1992/agno-hive/ops/.lightrag_watchdog_state.json"
COOLDOWN_S = 120
WINDOW_S = 3600
MAX_RESTARTS_PER_WINDOW = 3

INIT_PAYLOAD = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "lightrag-watchdog", "version": "1"}},
}).encode("utf-8")


def log(msg: str) -> None:
    print(f"[lightrag-watchdog] {msg}", flush=True)


def check_once() -> tuple[bool, str]:
    """One real MCP initialize handshake. Returns (healthy, detail)."""
    req = urllib.request.Request(
        MCP_URL, data=INIT_PAYLOAD, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            body = resp.read(4096).decode("utf-8", errors="replace")
            if '"serverInfo"' not in body or '"result"' not in body:
                return False, f"malformed response (no serverInfo/result): {body[:200]!r}"
            return True, "ok"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if "Task group is not initialized" in body:
            return False, "CONFIRMED zombie signature: 'Task group is not initialized'"
        return False, f"HTTPError {exc.code}: {body[:200]!r}"
    except Exception as exc:  # noqa: BLE001 -- this IS the generic health probe
        return False, f"{type(exc).__name__}: {exc}"


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 -- missing/corrupt state must never crash the watchdog
        return {"restarts": []}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: could not persist state file ({exc}) -- rate limiting "
            f"may be inaccurate across invocations until this is fixed")


def main() -> int:
    healthy, detail = check_once()
    if healthy:
        log(f"healthy ({detail})")
        return 0

    log(f"first check failed: {detail} -- re-checking once before acting "
        f"(never restart on a single transient failure)")
    time.sleep(5)
    healthy2, detail2 = check_once()
    if healthy2:
        log(f"second check passed ({detail2}) -- treating the first failure "
            f"as transient, not restarting")
        return 0

    log(f"second check also failed: {detail2} -- confirmed, not transient")

    now = time.time()
    state = load_state()
    recent = [t for t in state.get("restarts", []) if now - t < WINDOW_S]

    if recent and (now - recent[-1]) < COOLDOWN_S:
        log(f"last auto-restart was {now - recent[-1]:.0f}s ago, inside the "
            f"{COOLDOWN_S}s cooldown -- NOT restarting again yet, waiting it out")
        return 1

    if len(recent) >= MAX_RESTARTS_PER_WINDOW:
        log(f"ESCALATION: {len(recent)} auto-restarts already issued in the "
            f"last {WINDOW_S}s (budget {MAX_RESTARTS_PER_WINDOW}) -- this no "
            f"longer looks like the known zombie-session-manager failure "
            f"mode (that one is fixed by a single restart and does not "
            f"recur this fast). Refusing to restart again -- this needs a "
            f"human to look at it directly (journalctl -u lightrag.service).")
        return 2

    log(f"issuing systemctl --user restart lightrag.service "
        f"({len(recent)} prior restart(s) in the last {WINDOW_S}s)")
    result = subprocess.run(
        ["systemctl", "--user", "restart", "lightrag.service"],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        log(f"ESCALATION: systemctl restart itself failed (exit "
            f"{result.returncode}): {result.stderr.strip()} -- needs a human")
        return 3

    recent.append(now)
    state["restarts"] = recent
    save_state(state)
    log("restart issued successfully; next health check will confirm recovery")
    return 0


if __name__ == "__main__":
    sys.exit(main())
