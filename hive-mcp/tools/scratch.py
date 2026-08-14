"""Scratch-file offload for oversized tool results that have no overflow protection
of their own (agno-hive session/context-overflow pipeline, part #2, 2026-08-14 --
mirrors the harness's own "Output too large... saved to... Preview" pattern).

Deliberately narrow in scope. get_file_content() and search_files() (tools/context.py)
already reduce oversized results a different, better way -- a structural skeleton
(AST-based for Python) or head+tail for get_file_content, capped truncation with
narrowing guidance for search_files. Wiring this module into either would be
redundant and worse: a skeleton preserves structure a raw truncated dump does not.
The confirmed gap is run_command() (tools/files.py) -- zero size protection today,
returns raw unbounded stdout/stderr.

Cleanup is TTL-based, not "delete when the call that wrote it finishes" -- a run in
this system can be SIGKILLed at any point (process-boundary cancellation, the
liveness auto-kill in agno-hive's swarm/team.py), and a cleanup step that only runs
on graceful completion would leak a file every time a run doesn't end gracefully,
which here is common by design, not an edge case. Same shape as verify.py's own
_staged_files() recency cutoff in agno-hive, except that one just ignores old files
-- this one actually deletes them, since the whole point is not making the user
clean up scratch files by hand.
"""
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT

_SCRATCH_DIRNAME = ".hive_scratch"
_GITIGNORE_COMMENT = "# hive-mcp scratch files (oversized tool output, auto-swept) — machine-local"
_GITIGNORE_ENTRY = f"{_SCRATCH_DIRNAME}/"

# Hard ceiling before a tool result gets offloaded instead of returned inline --
# same default as config.SEARCH_MAX_OUTPUT_CHARS for consistency with the existing
# "hard ceiling on characters returned" precedent in this codebase.
_OFFLOAD_THRESHOLD_CHARS = int(os.getenv("SCRATCH_OFFLOAD_THRESHOLD_CHARS", "20000"))
_PREVIEW_CHARS = 2000
# Generous margin above every real run duration observed live (90-370s normal,
# 300s worst-case now that agno-hive's liveness auto-kill bounds a stall) -- nothing
# legitimate gets swept mid-use, nothing lingers for days either.
_TTL_SECONDS = int(os.getenv("SCRATCH_TTL_SECONDS", str(2 * 60 * 60)))

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _scratch_dir() -> Path:
    return PROJECT_ROOT / _SCRATCH_DIRNAME


def ensure_scratch_dir() -> Path:
    """Create the scratch directory if missing, and self-register a .gitignore entry
    for it the first time. Matches the existing convention every other hive-owned
    dot-artifact already follows -- .hive_pending_actions/, .hive-index-state/, each
    with its own explicit commented line; no broad .hive* wildcard exists to piggyback
    on (confirmed by reading EkamApp's actual .gitignore, not assumed). Best-effort on
    the .gitignore write: bookkeeping must never block the actual scratch write."""
    d = _scratch_dir()
    d.mkdir(parents=True, exist_ok=True)
    gitignore = PROJECT_ROOT / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if _GITIGNORE_ENTRY not in existing:
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            with gitignore.open("a", encoding="utf-8") as f:
                f.write(f"{sep}{_GITIGNORE_COMMENT}\n{_GITIGNORE_ENTRY}\n")
    except OSError:
        pass
    return d


def sweep_stale_scratch_files(ttl_seconds: int | None = None) -> int:
    """Delete scratch files older than the TTL. Called opportunistically before each
    new write (see maybe_offload) rather than as a separate background job. Returns
    the count of files deleted. A missing scratch directory (nothing offloaded yet
    this project) is a safe no-op, not an error."""
    ttl = _TTL_SECONDS if ttl_seconds is None else ttl_seconds
    d = _scratch_dir()
    if not d.is_dir():
        return 0
    cutoff = time.time() - ttl
    deleted = 0
    for p in d.iterdir():
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


def _safe_filename(hint: str) -> str:
    """A filesystem-safe scratch filename derived from `hint` (e.g. the command run)
    plus a UUID -- readable enough for a human skimming the directory to guess what
    produced it, unique enough that two offloads in the same call, same second, same
    process never collide (a timestamp+pid alone is not enough for that)."""
    base = _SAFE_NAME_RE.sub("-", hint.strip())[:60].strip("-") or "output"
    ts = time.strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{base}-{uuid.uuid4().hex[:8]}.txt"


def maybe_offload(result: str, hint: str, threshold: int | None = None) -> str:
    """If `result` exceeds the size threshold, sweep stale scratch files, write the
    FULL result to a new scratch file under PROJECT_ROOT/.hive_scratch/, and return a
    preview + the file's relative path instead. Otherwise returns `result` unchanged.

    The returned path is always readable via get_file_content() -- is_excluded()
    carries an explicit exception for .hive_scratch/ (tools/exclusions.py) specifically
    so this never dead-ends; without it, a model could see the preview but never read
    the rest.

    Args:
        result:    the full tool output to check
        hint:      short human-readable label for the filename (e.g. the command run)
        threshold: override the default char threshold (mainly for tests)
    """
    limit = _OFFLOAD_THRESHOLD_CHARS if threshold is None else threshold
    if len(result) <= limit:
        return result
    ensure_scratch_dir()
    sweep_stale_scratch_files()
    name = _safe_filename(hint)
    path = _scratch_dir() / name
    path.write_text(result, encoding="utf-8")
    rel = f"{_SCRATCH_DIRNAME}/{name}"
    preview = result[:_PREVIEW_CHARS]
    return (
        f"Output too large ({len(result):,} chars). Full output saved to: {rel}\n"
        f"Read the rest with get_file_content('{rel}') if you need more than this preview.\n\n"
        f"Preview (first {_PREVIEW_CHARS:,} chars):\n{preview}"
    )
