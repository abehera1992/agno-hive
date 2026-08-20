"""Tests: run_command must be read-only about the ENVIRONMENT, not just about files.

_WRITE_CMD_RE only ever matched file-writing syntax (>, sed -i, tee, ...), so
run_command's documented "READ-ONLY use only" was true of files and false of
everything else.

Live-verified 2026-08-20 against the running hive-mcp: `pip install` and `apt-get
install` both executed unblocked, while an `echo hi > file` control was correctly
blocked -- the guard worked exactly as written, just far narrower than the docstring
claimed. It matters because the container runs as root (uid 0), and a read-only
groundedness probe was observed running `pip install psycopg2-binary` and
`apt-get update && apt-get install -y postgresql-client` (5.24s, a real install) while
trying to answer a question about row counts.

The false-negative half of these tests is the important half: run_command exists to run
tests, linters and greps, and a guard that blocked those would be worse than the gap it
closes.
"""
import pytest

from tools import files


def _blocked(command: str) -> bool:
    """True when run_command would refuse the command (WRITE_REVIEW enabled)."""
    return bool(
        files._WRITE_CMD_RE.search(command) or files._MUTATING_CMD_RE.search(command)
    )


@pytest.mark.parametrize("command", [
    "pip install psycopg2-binary",
    "pip3 install requests",
    "pip uninstall psycopg2",
    "apt-get install -y postgresql-client",
    "apt-get update && apt-get install -y postgresql-client",   # the exact live case
    "apt install curl",
    "apt-get remove postgresql-client",
    "apt-get purge something",
    "npm install",
    "npm ci",
    "yarn add lodash",
    "pnpm remove lodash",
    "systemctl restart agno-api",
    "systemctl stop postgres",
])
def test_environment_mutation_is_blocked(command):
    assert _blocked(command), f"should be blocked: {command}"


@pytest.mark.parametrize("command", [
    # The read-only uses this tool exists for -- blocking any of these would be a
    # worse regression than the gap being closed.
    "pytest tests/ -v --tb=short",
    "npm run lint",
    "npm test",
    "pip show psycopg2",          # exactly what the live run did BEFORE installing
    "pip list",
    "pip --version",
    "apt-get --version",
    "git status",
    "git log --oneline -5",
    "git diff HEAD",
    "grep -rn 'def run_command' hive-mcp/",
    "ls -la",
    "python3 -c \"import psycopg2; print('ok')\"",
    "docker ps",
])
def test_read_only_commands_are_not_blocked(command):
    assert not _blocked(command), f"should NOT be blocked: {command}"


def test_file_write_guard_is_unchanged():
    """The pre-existing guard must keep behaving exactly as before."""
    assert _blocked("echo hi > /tmp/x")
    assert _blocked("sed -i 's/a/b/' file.py")
    assert _blocked("cat x | tee out.txt")
    assert not _blocked("cat file.py")


def test_run_shell_is_deliberately_not_given_the_mutation_guard():
    """run_shell is the tool explicitly documented for environment changes ('npm
    install', 'pip install', 'docker compose up'). Pushing genuine installs there is
    the entire point of tightening run_command, so guarding both would defeat it."""
    import inspect

    from tools import shell

    assert not hasattr(shell, "_MUTATING_CMD_RE")
    assert "_MUTATING_CMD_RE" not in inspect.getsource(shell.run_shell)


def test_blocked_message_names_the_right_alternative():
    """A refusal has to tell the model what to do instead, or it just retries."""
    original = files.WRITE_REVIEW
    files.WRITE_REVIEW = True
    try:
        out = files.run_command("pip install psycopg2-binary")
    finally:
        files.WRITE_REVIEW = original

    assert out.startswith("blocked:")
    assert "run_shell()" in out
