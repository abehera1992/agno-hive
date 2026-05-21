"""File write tools with WRITE_REVIEW support.

When WRITE_REVIEW=true every write is staged as a .hive_proposed file.
The hive CLI detects it, shows the diff, and applies/discards locally.
confirm_write and reject_write are NOT exposed — the CLI handles them.
"""
import difflib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT, WRITE_REVIEW

_PROPOSED_SUFFIX = ".hive_proposed"
_IN_DOCKER = Path("/.dockerenv").exists()

# Shell commands that write to files — blocked when WRITE_REVIEW=true
_WRITE_CMD_RE = re.compile(
    r"\s>>?\s"
    r"|\bsed\s+\S*-i"
    r"|\bperl\s+\S*-i"
    r"|\btee\b"
    r"|\btruncate\b"
    r"|\bdd\b.*of="
)


def _proposed_path(target: Path) -> Path:
    return target.with_name(target.name + _PROPOSED_SUFFIX)


def _inline_diff(original: Path, proposed: Path) -> str:
    """Unified diff for Docker mode (no VS Code available inside container)."""
    try:
        a = original.read_text(encoding="utf-8").splitlines(keepends=True)
        b = proposed.read_text(encoding="utf-8").splitlines(keepends=True)
        lines = list(difflib.unified_diff(
            a, b,
            fromfile=f"original/{original.name}",
            tofile=f"proposed/{original.name}",
            lineterm="",
        ))
        return "".join(lines) if lines else "(no differences)"
    except Exception as exc:
        return f"(diff unavailable: {exc})"


def write_file(relative_path: str, content: str) -> str:
    """
    Write content to a file. Use ONLY for brand-new files that do not exist yet.
    For existing files always use apply_diff() instead — this function is BLOCKED
    on existing files to prevent accidental full rewrites.
    Creates parent directories if needed.

    When WRITE_REVIEW=true the change is staged as a .hive_proposed file.
    STOP after receiving 'review_pending' — the human confirms via hive CLI.

    Args:
        relative_path: Path relative to project root (e.g. 'src/api/new_route.py')
        content:       Full file content to write
    """
    target = PROJECT_ROOT / relative_path
    if target.exists():
        return (
            f"write_file blocked: '{relative_path}' already exists. "
            f"Use apply_diff() to make surgical edits to existing files — "
            f"call get_file_content('{relative_path}') first to get the exact text to replace."
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        if WRITE_REVIEW:
            is_new = not target.exists()
            proposed = _proposed_path(target)
            proposed.write_text(content, encoding="utf-8")
            action = "new file" if is_new else "changes"
            diff = _inline_diff(target, proposed) if _IN_DOCKER and not is_new else ""
            diff_section = f"\n\nProposed diff:\n```diff\n{diff}\n```" if diff else ""
            return (
                f"review_pending: {relative_path} ({action}){diff_section}\n"
                f"The user will confirm or reject via the hive CLI."
            )

        target.write_text(content, encoding="utf-8")
        return f"written: {relative_path}"
    except Exception as e:
        return f"write_file failed: {e}"


def apply_diff(relative_path: str, old_string: str, new_string: str) -> str:
    """
    Surgical edit — replace an exact string in an existing file.
    Always call get_file_content() first to obtain the exact text to replace.
    Fails safely if old_string is not found or appears more than once.

    When WRITE_REVIEW=true the proposed result is staged for human review.
    You MAY make multiple apply_diff calls to the SAME file — each call
    accumulates into the same .hive_proposed file so the human sees one
    combined diff. STOP and report 'review_pending' only after ALL changes
    to a file are complete, or before modifying a DIFFERENT file.

    IMPORTANT rules:
    - To APPEND after a line: include the anchor in BOTH old_string AND new_string.
      old_string = 'last_line'
      new_string = 'last_line\\nnew_content'
    - To REPLACE a line: put only that line in old_string.
    - Never omit content from new_string unless intentionally deleting it.
    - When adding a new symbol (class, function, variable): update the import
      line FIRST, then add the usage — both as separate apply_diff calls.

    Args:
        relative_path: Path relative to project root
        old_string:    Exact text to replace (must appear exactly once)
        new_string:    Replacement text
    """
    target = PROJECT_ROOT / relative_path
    if not target.exists():
        return f"File not found: {relative_path}"
    try:
        proposed = _proposed_path(target)

        # If a staged version already exists, apply the diff on top of it
        # so multiple apply_diff calls accumulate into one proposed file.
        source = proposed if (WRITE_REVIEW and proposed.exists()) else target
        content = source.read_text(encoding="utf-8")

        count = content.count(old_string)
        if count == 0:
            return f"apply_diff failed: old_string not found in {relative_path}"
        if count > 1:
            return f"apply_diff failed: old_string appears {count} times — be more specific"

        proposed_content = content.replace(old_string, new_string, 1)

        if WRITE_REVIEW:
            proposed.write_text(proposed_content, encoding="utf-8")
            diff = _inline_diff(target, proposed) if _IN_DOCKER else ""
            diff_section = f"\n\nProposed diff:\n```diff\n{diff}\n```" if diff else ""
            return (
                f"review_pending: {relative_path}{diff_section}\n"
                f"You may continue with more apply_diff calls to this same file.\n"
                f"STOP and report 'review_pending' only after ALL changes to this file are staged."
            )

        target.write_text(proposed_content, encoding="utf-8")
        return f"applied: {relative_path}"
    except Exception as e:
        return f"apply_diff failed: {e}"


def run_command(command: str, timeout: int = 120) -> str:
    """
    Run a shell command in the project root. Returns stdout + stderr + exit code.

    READ-ONLY use only: tests, linters, build checks, grep, git status.
    NEVER use to write files — no >, >>, sed -i, tee, perl -i.
    Use apply_diff() or write_file() for ALL file modifications.

    Args:
        command: Shell command (e.g. 'pytest tests/ -v', 'npm run lint', 'git status')
        timeout: Seconds before timeout (default 120)
    """
    import subprocess

    if WRITE_REVIEW and _WRITE_CMD_RE.search(command):
        return (
            "blocked: run_command cannot write files when WRITE_REVIEW is enabled. "
            "Use apply_diff() to edit an existing file or write_file() to create a new one."
        )
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=timeout,
        )
        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr.strip():
            parts.append(f"[stderr]\n{result.stderr.strip()}")
        parts.append(f"[exit {result.returncode}]")
        return "\n".join(parts)
    except __import__("subprocess").TimeoutExpired:
        return f"run_command timed out after {timeout}s"
    except Exception as e:
        return f"run_command failed: {e}"
