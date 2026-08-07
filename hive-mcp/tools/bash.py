"""Persistent-cwd bash sessions + background jobs.

Session-scoped, in-memory state layered on plain subprocess.Popen — not a PTY, no
persisted environment variables (only the working directory persists across calls).
State lives in module-level dicts, matching tools/files.py's _last_failed_call /
_consecutive_failures pattern; it does not survive a container restart, and isn't
meant to.

Phase 1 (this file, current): bash_session_start, bash_run (blocking only --
background=True is rejected with a clear message), bash_session_close.
Phase 2 (follow-up): bash_run's background=True path, bash_job_status, bash_job_kill.

Why session-scoped state, not a global mutable cwd: hive-mcp is one long-lived
process shared by every concurrent tool call across every swarm agent run. A global
cwd would let one task's `cd` bleed into an unrelated concurrent task. A per-session
id mirrors the one existing precedent for "id chains state across otherwise-stateless
calls" in this codebase -- agno_run's session_id.
"""
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    PROJECT_ROOT,
    WRITE_REVIEW,
    HIVE_BASH_TOOL_ENABLED,
    HIVE_BASH_MAX_OUTPUT_CHARS,
    HIVE_BASH_DEFAULT_TIMEOUT_SECONDS,
    HIVE_BASH_MAX_TIMEOUT_SECONDS,
    HIVE_BASH_MAX_SESSIONS,
    HIVE_BASH_SESSION_TTL_SECONDS,
    HIVE_BASH_REAP_INTERVAL_SECONDS,
)

# Same write-detection pattern as run_command in files.py / run_shell in shell.py --
# duplicated here rather than imported, matching this codebase's existing precedent
# of each shell-executing module carrying its own copy of this regex.
_WRITE_CMD_RE = re.compile(
    r"\s>>?\s"
    r"|\bsed\s+\S*-i"
    r"|\bperl\s+\S*-i"
    r"|\btee\b"
    r"|\btruncate\b"
    r"|\bdd\b.*of="
)

_BARE_CD_RE = re.compile(r"^\s*cd\s+(\S+)\s*$")


@dataclass
class _Session:
    id: str
    cwd: Path
    created_at: float
    last_used_at: float
    jobs: set = field(default_factory=set)


_sessions: dict = {}
_state_lock = threading.Lock()
_REAPER_STARTED = False


def _cap(text: str, limit: "int | None" = None) -> str:
    """Clamp output to `limit` chars -- same convention as tools/context.py's _cap.
    run_command/run_shell have NO equivalent cap; this tool must not repeat that gap.
    `limit` defaults to the CURRENT value of HIVE_BASH_MAX_OUTPUT_CHARS, read at call
    time -- a plain default-argument value would be snapshotted once at import time
    and never see a later monkeypatch/config change."""
    if limit is None:
        limit = HIVE_BASH_MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    return (text[:limit]
            + f"\n... TRUNCATED at {limit} chars. Redirect verbose output to a file "
              f"and read it with get_file_content instead.")


def _resolve_cwd(raw: str, base: Path) -> "Path | None":
    """Resolve `raw` against `base` (absolute paths pass through). Returns None if
    the result doesn't exist or isn't a directory -- caller decides what that means."""
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    return candidate if candidate.is_dir() else None


def bash_session_start(cwd: str = "") -> str:
    """
    Create a persistent-cwd bash session. Returns a session_id -- pass it to every
    subsequent bash_run() call to keep working in the same directory: `cd` persists
    across calls in the same session, nothing else does (no environment variables
    carry over between calls -- each bash_run is still its own subprocess).

    Use this only when a task needs cwd to persist across multiple commands (e.g.
    cd into a subpackage, then run several commands there), or a command needs to
    run in the background (see bash_run). For a single one-off command, run_command
    or run_shell is simpler and needs no session/cleanup.

    Args:
        cwd: Directory to start in, relative to the project root (or absolute).
             Empty/omitted defaults to the project root.

    Sessions expire after HIVE_BASH_SESSION_TTL_SECONDS of inactivity, or on server
    restart -- do not treat a session_id as durable across a long gap. Call
    bash_session_close(session_id) when done with a multi-step shell workflow.
    """
    if not HIVE_BASH_TOOL_ENABLED:
        return "bash sessions are disabled on this server (HIVE_BASH_TOOL_ENABLED=false)"

    resolved = _resolve_cwd(cwd, PROJECT_ROOT) if cwd else PROJECT_ROOT
    if resolved is None:
        return f"cannot start session: '{cwd}' is not a directory under {PROJECT_ROOT}"

    with _state_lock:
        if len(_sessions) >= HIVE_BASH_MAX_SESSIONS:
            return (f"cannot start session: at the limit of {HIVE_BASH_MAX_SESSIONS} "
                     f"concurrent sessions. Close one with bash_session_close() first.")
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        _sessions[sid] = _Session(id=sid, cwd=resolved, created_at=now, last_used_at=now)

    _ensure_reaper_started()
    return f"session_id: {sid}\ncwd: {resolved}"


def bash_run(session_id: str, command: str, timeout: "int | None" = None,
             background: bool = False) -> str:
    """
    Run a command in an existing session's persisted working directory.

    Blocking (default): waits up to `timeout` seconds, returns stdout + stderr +
    exit code (capped at HIVE_BASH_MAX_OUTPUT_CHARS). A bare `cd <path>` (the ENTIRE
    command, nothing else chained) updates the session's cwd for FUTURE calls, only
    if the cd actually succeeds; `cd` inside a larger command (`cd sub && pytest`)
    runs in that command's own subshell and does not leak out -- same as real shell
    scoping, since each call is its own subprocess.

    background=True: not available yet in this phase -- use blocking mode.

    Args:
        session_id: id returned by bash_session_start()
        command: shell command string
        timeout: seconds to wait, default 120 (clamped to HIVE_BASH_MAX_TIMEOUT_SECONDS)
        background: run detached and return a job_id immediately (Phase 2, not yet available)
    """
    if not HIVE_BASH_TOOL_ENABLED:
        return "bash sessions are disabled on this server (HIVE_BASH_TOOL_ENABLED=false)"

    with _state_lock:
        session = _sessions.get(session_id)
    if session is None:
        return f"unknown session_id: {session_id!r} (expired, closed, or never created)"

    if background:
        return ("background execution is not available yet -- this server only supports "
                 "blocking mode (background=False) at this time.")

    if WRITE_REVIEW and _WRITE_CMD_RE.search(command):
        return (
            "blocked: bash_run cannot write files when WRITE_REVIEW is enabled. "
            "Use apply_diff() to edit an existing file or write_file() to create a new one."
        )

    if timeout is None:
        timeout = HIVE_BASH_DEFAULT_TIMEOUT_SECONDS
    timeout = max(1, min(timeout, HIVE_BASH_MAX_TIMEOUT_SECONDS))

    try:
        r = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=session.cwd,
        )
    except subprocess.TimeoutExpired:
        with _state_lock:
            session.last_used_at = time.time()
        return f"timed out after {timeout}s"
    except Exception as e:
        with _state_lock:
            session.last_used_at = time.time()
        return f"bash_run failed: {e}"

    with _state_lock:
        session.last_used_at = time.time()
        m = _BARE_CD_RE.match(command)
        if m and r.returncode == 0:
            new_cwd = _resolve_cwd(m.group(1), session.cwd)
            if new_cwd is not None:
                session.cwd = new_cwd

    parts = []
    if r.stdout.strip():
        parts.append(r.stdout.strip())
    if r.stderr.strip():
        parts.append(f"[stderr]\n{r.stderr.strip()}")
    parts.append(f"[exit {r.returncode}]")
    return _cap("\n".join(parts))


def bash_session_close(session_id: str) -> str:
    """
    Close a session, freeing its slot immediately instead of waiting for TTL expiry.
    Call this when done with a multi-step shell workflow, especially if several
    sessions were opened in one task.
    """
    with _state_lock:
        existed = _sessions.pop(session_id, None) is not None
    if not existed:
        return f"unknown session_id: {session_id!r} (already closed, expired, or never created)"
    return f"session closed: {session_id}"


def _reap_once(now: "float | None" = None) -> int:
    """Close sessions idle past HIVE_BASH_SESSION_TTL_SECONDS. Returns count closed.
    Callable directly (tests, with an explicit `now`) or from the background reaper
    thread (real time)."""
    now = now if now is not None else time.time()
    with _state_lock:
        expired = [sid for sid, s in _sessions.items()
                   if now - s.last_used_at > HIVE_BASH_SESSION_TTL_SECONDS]
        for sid in expired:
            del _sessions[sid]
    return len(expired)


def _reap_loop() -> None:
    while True:
        time.sleep(HIVE_BASH_REAP_INTERVAL_SECONDS)
        try:
            _reap_once()
        except Exception as e:
            print(f"[bash] reaper sweep failed: {e}", flush=True)


def _ensure_reaper_started() -> None:
    global _REAPER_STARTED
    if _REAPER_STARTED or not HIVE_BASH_TOOL_ENABLED:
        return
    _REAPER_STARTED = True
    threading.Thread(target=_reap_loop, daemon=True).start()


def cleanup_all() -> int:
    """Close every live session. Called on server shutdown (atexit/SIGTERM in
    main.py) so a restart doesn't need to wait out the TTL reaper. Phase 1 has no
    live subprocesses to kill (blocking calls complete before returning) -- Phase 2
    extends this to also terminate live background job processes. Returns count
    closed."""
    with _state_lock:
        count = len(_sessions)
        _sessions.clear()
    return count
