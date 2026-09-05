"""Facts about the target repo, computed at check time -- never hardcoded.

A benchmark whose expected answers are pasted in as constants starts rotting the
moment the repo moves: the 24 router files become 25, every enumeration task fails,
and the failure looks like a model regression. Every fact here is derived from the
checkout at the moment a run is scored, so the task set tracks the code instead of a
snapshot of it.

The one thing this must never do is consult the swarm's own tools or output. Ground
truth is read straight off the filesystem and git, so a task can fail freely on the
failure modes the guards in swarm/team.py were built around -- the guards cannot
influence what "correct" means here.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

# The project under test. Overridable so this can point at another checkout.
PROJECT_ROOT = Path(r"C:\Users\Abhishek Behera\Projects\EkamApp")

_DECOR = re.compile(r"""@router\.(get|post|put|patch|delete)\(\s*["']([^"']*)["']""")
_HOOK = re.compile(r"\buse[A-Z]\w+")


def _read(rel: str) -> str:
    try:
        return (PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


@lru_cache(maxsize=None)
def py_files_in(rel_dir: str) -> tuple[str, ...]:
    """Every .py filename in a directory, sorted. () when the directory is absent."""
    p = PROJECT_ROOT / rel_dir
    if not p.is_dir():
        return ()
    return tuple(sorted(f.name for f in p.glob("*.py")))


@lru_cache(maxsize=None)
def routes_in(rel_file: str) -> tuple[tuple[str, str], ...]:
    """(METHOD, path) for every @router decorator in a file, in source order."""
    return tuple((m.group(1).upper(), m.group(2)) for m in _DECOR.finditer(_read(rel_file)))


@lru_cache(maxsize=None)
def hooks_in(rel_file: str) -> tuple[str, ...]:
    """Every useXxx identifier exported by an RTK Query slice, sorted and unique."""
    return tuple(sorted(set(_HOOK.findall(_read(rel_file)))))


def exists(rel: str) -> bool:
    return (PROJECT_ROOT / rel).exists()


@lru_cache(maxsize=None)
def symbol_line(rel_file: str, symbol: str) -> int | None:
    """First line where `symbol` appears whole-word, 1-indexed. None if absent."""
    pattern = re.compile(rf"(?<!\w){re.escape(symbol)}(?!\w)")
    for i, line in enumerate(_read(rel_file).splitlines(), 1):
        if pattern.search(line):
            return i
    return None


@lru_cache(maxsize=None)
def git_last_commit(rel: str) -> tuple[str, str] | None:
    """(short sha, YYYY-MM-DD) of the most recent commit touching a path."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h|%ad", "--date=short", "--", rel],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception:
        return None
    if "|" not in out:
        return None
    sha, date = out.split("|", 1)
    return sha.strip(), date.strip()


@lru_cache(maxsize=None)
def git_commit_count(rel: str) -> int | None:
    try:
        out = subprocess.run(
            ["git", "rev-list", "--count", "HEAD", "--", rel],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return int(out)
    except Exception:
        return None


def uses_orm(rel_file: str) -> bool:
    return "from sqlalchemy" in _read(rel_file) or "import sqlalchemy" in _read(rel_file)


def mentions(rel_file: str, needle: str) -> bool:
    return needle.lower() in _read(rel_file).lower()
