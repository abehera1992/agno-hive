"""Persistent-cwd bash sessions + background jobs.

Session-scoped, in-memory state layered on plain subprocess.Popen — not a PTY, no
persisted environment variables (only the working directory persists across calls).
State lives in module-level dicts, matching tools/files.py's _last_failed_call /
_consecutive_failures pattern; it does not survive a container restart, and isn't
meant to.

Phase 1: bash_session_start, bash_run (blocking mode), bash_session_close.
Phase 2 (this file, current): bash_run's background=True path, bash_job_status,
bash_job_kill -- a background job is a plain subprocess.Popen with a daemon reader
thread pumping merged stdout/stderr into a capped in-memory buffer; polled later via
bash_job_status, never streamed (no push/streaming channel exists in this
request/response MCP server).

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
    HIVE_BASH_MAX_BACKGROUND_JOBS,
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


@dataclass
class _Job:
    id: str
    session_id: str
    command: str
    proc: subprocess.Popen
    started_at: float
    last_used_at: float
    timeout: int                       # hard max-runtime cap, not a wait
    status: str = "running"            # "running" | "exited" | "timed_out" | "killed"
    exit_code: "int | None" = None
    output: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


_sessions: dict = {}
_jobs: dict = {}
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

    background=True: starts the command detached and returns a job_id immediately
    instead of waiting. Poll bash_job_status(job_id) LATER to check on it -- do not
    sleep-poll-retry in a tight loop, there is no push notification here. `timeout`
    becomes the job's max runtime, not a wait -- it is killed and marked timed_out
    if it runs past that. A `cd` in a background command does NOT update the
    session's persisted cwd (cwd tracking is a blocking-mode-only convenience).

    Args:
        session_id: id returned by bash_session_start()
        command: shell command string
        timeout: seconds to wait (blocking) or max runtime (background), default
                 120 (clamped to HIVE_BASH_MAX_TIMEOUT_SECONDS)
        background: run detached and return a job_id immediately instead of blocking
    """
    if not HIVE_BASH_TOOL_ENABLED:
        return "bash sessions are disabled on this server (HIVE_BASH_TOOL_ENABLED=false)"

    with _state_lock:
        session = _sessions.get(session_id)
    if session is None:
        return f"unknown session_id: {session_id!r} (expired, closed, or never created)"

    if WRITE_REVIEW and _WRITE_CMD_RE.search(command):
        return (
            "blocked: bash_run cannot write files when WRITE_REVIEW is enabled. "
            "Use apply_diff() to edit an existing file or write_file() to create a new one."
        )

    if timeout is None:
        timeout = HIVE_BASH_DEFAULT_TIMEOUT_SECONDS
    timeout = max(1, min(timeout, HIVE_BASH_MAX_TIMEOUT_SECONDS))

    with _state_lock:
        session.last_used_at = time.time()

    if background:
        return _start_background_job(session, command, timeout)

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
        return f"timed out after {timeout}s"
    except Exception as e:
        return f"bash_run failed: {e}"

    with _state_lock:
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


def _append_job_output(job: "_Job", text: str) -> None:
    """Append to a job's output buffer under its own lock, keeping the TAIL once
    the buffer exceeds HIVE_BASH_MAX_OUTPUT_CHARS (most-recent output is more
    useful than the earliest for a long-running job) -- read at call time so a
    later monkeypatch/config change is honored, not snapshotted at import time."""
    if not text:
        return
    with job.lock:
        combined = job.output + text
        if len(combined) > HIVE_BASH_MAX_OUTPUT_CHARS:
            combined = (
                f"... EARLIER OUTPUT DROPPED (job buffer capped at "
                f"{HIVE_BASH_MAX_OUTPUT_CHARS} chars) ...\n"
                + combined[-HIVE_BASH_MAX_OUTPUT_CHARS:]
            )
        job.output = combined


def _pump_job_output(job: "_Job") -> None:
    """Daemon-thread target: reads a background job's merged stdout/stderr until
    EOF, then waits for the process to actually exit and records the outcome --
    unless something else (bash_job_kill, the reaper's timeout sweep) already set
    a terminal status first, in which case that status wins."""
    try:
        stream = job.proc.stdout
        if stream is not None:
            for line in stream:
                _append_job_output(job, line)
    except Exception as e:
        _append_job_output(job, f"\n[bash] output reader failed: {e}\n")
    finally:
        job.proc.wait()
        with job.lock:
            if job.status == "running":
                job.status = "exited"
                job.exit_code = job.proc.returncode


def _start_background_job(session: "_Session", command: str, timeout: int) -> str:
    """Spawn a detached command and register it as a job. Holds _state_lock across
    the cap-check + Popen() + registration so two concurrent callers can't both
    slip past the concurrency cap (Popen() itself returns immediately -- it starts
    the process without waiting for it, so holding the lock across it is cheap,
    unlike subprocess.run which would block for the command's whole runtime)."""
    with _state_lock:
        if len(_jobs) >= HIVE_BASH_MAX_BACKGROUND_JOBS:
            return (f"cannot start background job: at the limit of "
                     f"{HIVE_BASH_MAX_BACKGROUND_JOBS} concurrent jobs. "
                     f"Poll and let one finish, or bash_job_kill() one first.")
        jid = uuid.uuid4().hex[:12]
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=session.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            return f"bash_run failed to start background job: {e}"
        now = time.time()
        job = _Job(id=jid, session_id=session.id, command=command, proc=proc,
                   started_at=now, last_used_at=now, timeout=timeout)
        _jobs[jid] = job
        session.jobs.add(jid)

    threading.Thread(target=_pump_job_output, args=(job,), daemon=True).start()
    return f"job_id: {jid}\nstatus: running"


def bash_job_status(job_id: str, tail_chars: int = 4000) -> str:
    """
    Poll a background job started via bash_run(..., background=True). Returns
    status (running/exited/timed_out/killed), exit code once finished, and the
    last tail_chars of accumulated output (the job's whole stored buffer is
    itself capped at HIVE_BASH_MAX_OUTPUT_CHARS -- a very verbose long-running
    job should redirect output to a file and be read via get_file_content
    instead of relying on this buffer). Safe to call repeatedly.

    Args:
        job_id: id returned by bash_run(..., background=True)
        tail_chars: how much of the accumulated output to return (most recent)
    """
    with _state_lock:
        job = _jobs.get(job_id)
    if job is None:
        return f"unknown job_id: {job_id!r} (finished and reaped, or never created)"

    with job.lock:
        job.last_used_at = time.time()
        status = job.status
        exit_code = job.exit_code
        output = job.output

    parts = [f"status: {status}"]
    if exit_code is not None:
        parts.append(f"exit_code: {exit_code}")
    if output:
        tail = output[-tail_chars:] if tail_chars and len(output) > tail_chars else output
        parts.append(tail)
    return "\n".join(parts)


def bash_job_kill(job_id: str) -> str:
    """
    Terminate a running background job. No-op with a clear message if the job
    already finished (exited/timed_out) or was already killed.
    """
    with _state_lock:
        job = _jobs.get(job_id)
    if job is None:
        return f"unknown job_id: {job_id!r} (finished and reaped, or never created)"

    with job.lock:
        if job.status != "running":
            return f"job {job_id} is not running (status: {job.status})"
        job.status = "killed"

    try:
        job.proc.kill()
    except Exception:
        pass
    return f"job killed: {job_id}"


def bash_session_close(session_id: str) -> str:
    """
    Close a session, freeing its slot immediately instead of waiting for TTL expiry.
    Also kills and removes any background jobs still attached to it (running or
    not) -- a session's jobs shouldn't outlive the session itself. Call this when
    done with a multi-step shell workflow, especially if several sessions were
    opened in one task.
    """
    with _state_lock:
        session = _sessions.pop(session_id, None)
        if session is None:
            return f"unknown session_id: {session_id!r} (already closed, expired, or never created)"
        popped_jobs = [_jobs.pop(jid, None) for jid in session.jobs]

    killed = 0
    for job in popped_jobs:
        if job is None:
            continue
        with job.lock:
            was_running = job.status == "running"
            if was_running:
                job.status = "killed"
        if was_running:
            try:
                job.proc.kill()
            except Exception:
                pass
            killed += 1

    suffix = f" ({len(popped_jobs)} background job(s) closed, {killed} killed)" if popped_jobs else ""
    return f"session closed: {session_id}{suffix}"


def _reap_once(now: "float | None" = None) -> int:
    """Close sessions idle past HIVE_BASH_SESSION_TTL_SECONDS. For background jobs:
    a RUNNING job is only ever reaped by its OWN timeout (hard max-runtime cap,
    checked and killed here if exceeded) -- never by idle/no-output alone, since a
    legitimately slow job may go quiet for a while without being stuck. A job that
    just reached "timed_out" stays in _jobs for one more sweep so a caller can still
    poll its final status/output via bash_job_status before it's actually removed.
    A FINISHED job (exited/timed_out/killed) is removed once idle past the same TTL,
    to free its dict slot once nobody's collecting the result. Returns the total
    count of sessions + jobs reaped. Callable directly (tests, with an explicit
    `now`) or from the background reaper thread (real time)."""
    now = now if now is not None else time.time()

    with _state_lock:
        expired_sessions = [sid for sid, s in _sessions.items()
                             if now - s.last_used_at > HIVE_BASH_SESSION_TTL_SECONDS]
        jobs_snapshot = list(_jobs.items())

    jobs_to_remove = []
    for jid, job in jobs_snapshot:
        with job.lock:
            if job.status == "running":
                if now - job.started_at > job.timeout:
                    job.status = "timed_out"
                    try:
                        job.proc.kill()
                    except Exception:
                        pass
            elif now - job.last_used_at > HIVE_BASH_SESSION_TTL_SECONDS:
                jobs_to_remove.append(jid)

    with _state_lock:
        for sid in expired_sessions:
            del _sessions[sid]
        for jid in jobs_to_remove:
            _jobs.pop(jid, None)

    return len(expired_sessions) + len(jobs_to_remove)


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
    """Close every live session and kill every live background job. Called on
    server shutdown (atexit/SIGTERM in main.py) so a restart doesn't leave orphaned
    child processes behind waiting out their own timeout. Returns the total count
    of sessions + jobs cleared."""
    with _state_lock:
        session_count = len(_sessions)
        _sessions.clear()
        jobs_snapshot = list(_jobs.values())
        _jobs.clear()

    for job in jobs_snapshot:
        with job.lock:
            was_running = job.status == "running"
        if was_running:
            try:
                job.proc.kill()
            except Exception:
                pass

    return session_count + len(jobs_snapshot)
