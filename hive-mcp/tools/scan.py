"""Scan the project and generate/update hive.md for session context injection.

hive.md is automatically read by AGNOHive's bootstrap on every session start,
giving the coordinator a grounded project snapshot without repeated MCP calls.

Incremental update strategy (2 layers of change detection):
  1. Committed changes since last scan  (git diff --name-only <hash>..HEAD — object-db only, fast)
  2. Worktree state in ONE walk         (git status --porcelain — covers staged,
     unstaged, and untracked; worktree walks cost ~76-96s on Docker Desktop
     bind mounts, so the three separate commands this replaced exceeded the
     gateway timeout)

If nothing changed across both layers: returns "up to date".
If anything changed — or change detection FAILS (timeout/git error) — rebuilds
the full hive.md (fail-open: a scan that cannot determine changes must never
claim "up to date"). Falls back to full scan when git is unavailable or
hive.md doesn't exist yet.
"""
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT

_HIVE_MD = "hive.md"
_META_RE = re.compile(r"<!--\s*hive-scan:\s*commit=(\S+)\s+timestamp=\S+\s*-->")

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".next", "dist", "build",
    ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", "coverage",
    ".tox", ".eggs", "eggs",
}
_SKIP_EXTENSIONS = {".pyc", ".pyo", ".class", ".so", ".dll", ".exe", ".bin", ".lock"}
_ROOT_DOCS = ["CLAUDE.md", "README.md", "docs.md", "AGENTS.md", ".env.example"]
_DIR_CANDIDATES = ["README.md", "__init__.py", "main.py", "config.py",
                   "index.ts", "index.js", "server.py", "app.py"]


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(args: list[str], timeout: int = 10) -> str | None:
    """Run a git command in PROJECT_ROOT.

    Returns stdout on success ('' is a legitimate empty result), or None on
    any failure/timeout — callers must distinguish 'no changes' from
    'could not determine changes'.
    """
    try:
        r = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# Worktree-walking commands (diff against the working tree, untracked scan)
# stat every tracked file. On Docker Desktop bind mounts this measured 70-80s
# for a ~650-file project — far beyond the old 10s timeout, which made the
# scan silently blind to unstaged/untracked changes ("up to date" lie).
_WORKTREE_TIMEOUT = 180


def _current_head() -> str:
    return _git(["rev-parse", "HEAD"]) or "unknown"


def _read_stored_hash(hive_md: Path) -> str:
    """Extract the commit hash stored in the hive.md header comment."""
    try:
        first_line = hive_md.read_text(encoding="utf-8").split("\n")[0]
        m = _META_RE.match(first_line)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _worktree_status() -> list[str] | None:
    """All staged + unstaged + untracked paths in ONE worktree walk.

    `git status --porcelain` covers what previously took three separate
    commands (diff --cached, diff, ls-files --others), each walking the
    worktree — at ~76s per walk on Docker Desktop bind mounts that made
    scans exceed the API gateway timeout. Returns None on failure.
    """
    out = _git(["status", "--porcelain"], timeout=_WORKTREE_TIMEOUT)
    if out is None:
        return None
    paths: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename: "R  old -> new"
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


def _get_changed_files(last_hash: str, worktree: list[str] | None) -> list[str]:
    """Return all changed paths: committed since last scan + worktree state.

    Fail-open: if detection fails (timeout, git error), a sentinel is
    returned so the caller rebuilds instead of falsely reporting
    'up to date'.
    """
    changed: set[str] = set()

    if last_hash and last_hash != "unknown":
        out = _git(["diff", "--name-only", f"{last_hash}..HEAD"])
        if out is None:
            changed.add("<change-detection-failed>")
        elif out:
            changed.update(out.splitlines())

    if worktree is None:
        changed.add("<change-detection-failed>")
    else:
        changed.update(worktree)

    return [f for f in changed if f]


def _uncommitted_summary(worktree: list[str] | None) -> str:
    """Short human-readable list of files with uncommitted changes."""
    if worktree is None:
        return "unknown — git status failed"
    if not worktree:
        return "none"
    files = sorted(set(worktree))
    if len(files) > 8:
        return ", ".join(files[:8]) + f" … (+{len(files) - 8} more)"
    return ", ".join(files)


# ── Content builders ──────────────────────────────────────────────────────────

def _read_file(rel_path: str, max_chars: int = 2500) -> str:
    try:
        p = PROJECT_ROOT / rel_path
        if p.exists() and p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            if len(content) > max_chars:
                return content[:max_chars] + f"\n… (truncated at {max_chars} chars)"
            return content
    except Exception:
        pass
    return ""


def _directory_tree(max_depth: int = 3) -> str:
    lines: list[str] = [PROJECT_ROOT.name + "/"]

    def _walk(path: Path, depth: int, prefix: str = "") -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            return
        visible = [
            e for e in entries
            if not e.name.startswith(".")
            and e.name not in _SKIP_DIRS
            and (e.is_dir() or e.suffix not in _SKIP_EXTENSIONS)
        ]
        for i, entry in enumerate(visible):
            connector = "└── " if i == len(visible) - 1 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir():
                ext = "    " if i == len(visible) - 1 else "│   "
                _walk(entry, depth + 1, prefix + ext)

    _walk(PROJECT_ROOT, 1)
    return "\n".join(lines)


def _dir_summaries() -> str:
    sections: list[str] = []
    try:
        top_dirs = sorted(
            [d for d in PROJECT_ROOT.iterdir()
             if d.is_dir() and not d.name.startswith(".") and d.name not in _SKIP_DIRS],
            key=lambda d: d.name.lower(),
        )
    except Exception:
        return ""

    for d in top_dirs:
        for candidate in _DIR_CANDIDATES:
            f = d / candidate
            if f.exists():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")[:800]
                    lang = "python" if f.suffix == ".py" else ""
                    sections.append(f"### `{d.name}/`\n```{lang}\n{content}\n```")
                except Exception:
                    pass
                break

    return "\n\n".join(sections)


def _build_content(head: str, uncommitted: str) -> str:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts: list[str] = [
        f"<!-- hive-scan: commit={head} timestamp={now_iso} -->",
        "# Hive Project Context",
        f"**Project:** {PROJECT_ROOT.name}  ",
        f"**Last scanned:** {now_human} (commit `{head[:8]}`)  ",
        f"**Uncommitted changes:** {uncommitted}",
        "",
        "## Project Structure",
        "```",
        _directory_tree(),
        "```",
        "",
    ]

    for doc in _ROOT_DOCS:
        cap = 3000 if doc in ("CLAUDE.md", "docs.md", "AGENTS.md") else 1500
        content = _read_file(doc, max_chars=cap)
        if content:
            parts += [f"## {doc}", content, ""]

    summaries = _dir_summaries()
    if summaries:
        parts += ["## Top-Level Directory Summaries", summaries, ""]

    return "\n".join(parts)


# ── Public tool ───────────────────────────────────────────────────────────────

def scan_project_context(force: bool = False) -> str:
    """
    Scan the project and generate or update hive.md in the project root.

    hive.md is automatically injected into every AGNOHive session by the bootstrap
    phase, giving the coordinator a grounded project snapshot without repeated file
    reads — reducing hallucination on structure and design questions.

    Incremental mode (default, force=False):
      Reads the commit hash stored in the existing hive.md header, then collects
      all files changed since then across four layers: committed changes, staged,
      unstaged, and untracked new files. If nothing changed across all four layers,
      returns immediately ("up to date"). Otherwise rebuilds the full file.

    Full scan (force=True):
      Rebuilds hive.md from scratch regardless of existing state.

    Works for any project — not just agno-hive. Falls back to full scan when git
    is unavailable or hive.md doesn't exist yet.

    Args:
        force: If True, rebuild hive.md from scratch even if it already exists.
    """
    hive_md = PROJECT_ROOT / _HIVE_MD
    head = _current_head()
    # ONE worktree walk per scan — shared by change detection and the
    # uncommitted-files summary (each walk costs ~76s on bind mounts).
    worktree = _worktree_status()
    uncommitted = _uncommitted_summary(worktree)

    # Full scan when forced or file missing
    if force or not hive_md.exists():
        content = _build_content(head, uncommitted)
        hive_md.write_text(content, encoding="utf-8")
        mode = "forced full scan" if force else "first-time full scan"
        return f"written: {_HIVE_MD} ({hive_md.stat().st_size:,} bytes) — {mode}"

    # Incremental: check what changed
    last_hash = _read_stored_hash(hive_md)
    changed = _get_changed_files(last_hash, worktree)

    if not changed and last_hash == head:
        return f"up to date: {_HIVE_MD} — no changes since commit {head[:8]}"

    # Something changed — rebuild full content (fast, all reads are capped)
    content = _build_content(head, uncommitted)
    hive_md.write_text(content, encoding="utf-8")

    if changed:
        # The fail-open sentinel triggers a rebuild but is not a real file —
        # keep it out of the human-facing preview and count.
        detection_failed = "<change-detection-failed>" in changed
        real = [c for c in changed if c != "<change-detection-failed>"]
        preview = ", ".join(real[:5]) + ("…" if len(real) > 5 else "")
        note = " (change detection fell back to full rebuild)" if detection_failed else ""
        body = f"{len(real)} file(s) changed [{preview}]" if real else "rebuilt"
        return f"updated: {_HIVE_MD} ({hive_md.stat().st_size:,} bytes) — {body}{note}"
    return f"updated: {_HIVE_MD} — commit advanced to {head[:8]}"
