"""Regression tests: run_shell / run_docker output must be size-capped like run_command.

The .hive_scratch offload added 2026-08-14 was scoped to run_command (files.py) and
never reached shell.py, so run_shell and run_docker could return output of unbounded
size directly into the model's context.

Root-caused from production logs 2026-08-20, not hypothesised. A live-DB probe ("how
many rows in the items table, broken down by is_active") never called db_query. It
improvised instead -- `pip show psycopg2`, `pip install psycopg2-binary`, `apt-get
install -y postgresql-client`, `psql` with guessed credentials -- and finally ran
`run_docker('logs ekamapp-postgres-1')`, which returned the postgres container's entire
log. The next model call died with:

    litellm.ContextWindowExceededError: This model's maximum context length is 262144
    tokens. However, you requested 4096 output tokens and your prompt contains at
    least 258049 input tokens

The run then tore down abnormally, and the resulting anyio "Attempted to exit cancel
scope in a different task than it was entered in" RuntimeError was what surfaced to the
caller as a 500 -- the real cause appearing nowhere in it.

The cap is applied inside the shared _run(), so run_shell, run_docker and
list_processes are all covered by construction.
"""
from tools import shell


def test_oversized_docker_output_is_offloaded_not_returned_whole(tmp_path, monkeypatch):
    """`docker logs <container>` on a long-lived container is the exact shape that blew
    the context window."""
    monkeypatch.setattr(shell, "PROJECT_ROOT", tmp_path)
    import tools.scratch as scratch
    monkeypatch.setattr(scratch, "PROJECT_ROOT", tmp_path)

    huge = "postgres log line\n" * 5000          # ~90k chars, well over the 20k threshold

    class _Result:
        stdout, stderr, returncode = huge, "", 0

    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: _Result())

    out = shell.run_docker("logs ekamapp-postgres-1")

    assert len(out) < len(huge), "oversized output was returned whole"
    assert ".hive_scratch" in out, "no scratch path offered for the full output"


def test_small_output_is_returned_unchanged(tmp_path, monkeypatch):
    """No behaviour change for ordinary commands -- the overwhelming majority."""
    monkeypatch.setattr(shell, "PROJECT_ROOT", tmp_path)
    import tools.scratch as scratch
    monkeypatch.setattr(scratch, "PROJECT_ROOT", tmp_path)

    class _Result:
        stdout, stderr, returncode = "CONTAINER ID   IMAGE\nabc123   postgres:16", "", 0

    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: _Result())

    out = shell.run_docker("ps")

    assert "abc123" in out
    assert ".hive_scratch" not in out
    assert "[exit 0]" in out


def test_run_shell_is_capped_too(tmp_path, monkeypatch):
    """Same shared runner, so the cap must apply here as well -- `cat` on a large file
    or a verbose build log reaches it just as easily as docker logs."""
    monkeypatch.setattr(shell, "PROJECT_ROOT", tmp_path)
    import tools.scratch as scratch
    monkeypatch.setattr(scratch, "PROJECT_ROOT", tmp_path)

    huge = "build output line\n" * 5000

    class _Result:
        stdout, stderr, returncode = huge, "", 0

    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: _Result())

    out = shell.run_shell("cat some-enormous-build.log")

    assert len(out) < len(huge)
    assert ".hive_scratch" in out


def test_exit_code_survives_offloading(tmp_path, monkeypatch):
    """A failure must still read as a failure after offloading -- otherwise a huge
    error dump would come back looking like a clean result."""
    monkeypatch.setattr(shell, "PROJECT_ROOT", tmp_path)
    import tools.scratch as scratch
    monkeypatch.setattr(scratch, "PROJECT_ROOT", tmp_path)

    class _Result:
        stdout, stderr, returncode = "x" * 30000, "boom", 1

    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: _Result())

    out = shell.run_docker("logs broken-container")

    assert ".hive_scratch" in out
    # The preview keeps the head of the output; the full text (incl. exit code) is in
    # the scratch file, which the model can read back via get_file_content.
    assert len(out) < 30000
