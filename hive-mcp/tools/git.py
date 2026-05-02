"""Git introspection tools.

Read-only git operations for understanding project state before making
changes. Writing (commit, push, branch) is intentionally excluded —
those decisions belong to the human.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT


def _git(args: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=PROJECT_ROOT,
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        if r.returncode != 0:
            return f"git error: {err or out}"
        return out or "(empty output)"
    except subprocess.TimeoutExpired:
        return "git timed out"
    except FileNotFoundError:
        return "git not found in PATH"
    except Exception as e:
        return f"git failed: {e}"


def git_status() -> str:
    """
    Show the working tree status — modified, staged, and untracked files.
    Call this before making changes to understand the current state.
    """
    return _git(["status", "--short", "--branch"])


def git_log(limit: int = 15) -> str:
    """
    Show recent commit history with author, date, and message.
    Useful for understanding what has changed recently and who made changes.

    Args:
        limit: Number of commits to show (default 15)
    """
    return _git([
        "log",
        f"--max-count={limit}",
        "--pretty=format:%h  %ai  %an  %s",
    ])


def git_diff(ref: str = "") -> str:
    """
    Show changes between working tree and a reference.

    Args:
        ref: Git ref to diff against. Empty string = diff against HEAD (unstaged changes).
             Use 'HEAD' for staged+unstaged, 'main' to compare with main branch,
             or a specific commit hash.

    Examples:
        git_diff()          → unstaged changes vs HEAD
        git_diff('HEAD')    → all changes (staged + unstaged) vs last commit
        git_diff('main')    → current branch vs main
    """
    args = ["diff"]
    if ref:
        args.append(ref)
    return _git(args, timeout=30)


def git_log_file(relative_path: str, limit: int = 10) -> str:
    """
    Show commit history for a specific file.
    Useful for understanding when and why a file was changed.

    Args:
        relative_path: Path relative to project root (e.g. 'src/api/routes.py')
        limit:         Number of commits to show (default 10)
    """
    return _git([
        "log",
        f"--max-count={limit}",
        "--pretty=format:%h  %ai  %an  %s",
        "--",
        relative_path,
    ])


def git_blame(relative_path: str, start_line: int = 1, end_line: int = 50) -> str:
    """
    Show who last modified each line of a file (git blame).
    Useful when you need context about why specific code exists.

    Args:
        relative_path: Path relative to project root
        start_line:    First line to show (default 1)
        end_line:      Last line to show (default 50)
    """
    return _git([
        "blame",
        f"-L{start_line},{end_line}",
        "--date=short",
        relative_path,
    ], timeout=20)
