"""get_env_info() must report the actual current working directory, not just
PROJECT_ROOT (a configured constant, not a measurement).

Phase 7/7-followup (AGNOHive Reliability Program, T9 forensics): T9's task
explicitly asks for "the current working directory", but get_env_info()
never called os.getcwd() and never emitted any field named "current working
directory" -- only "Project root: {PROJECT_ROOT}". No agent, however
faithful, could have answered the actual question from this tool's real
output, since the authoritative fact was never captured. Confirmed directly
from hive-mcp/tools/shell.py's source (no test previously existed for this
function at all).

These tests use a mocked os.getcwd() (never the real developer machine's
cwd) and a PROJECT_ROOT deliberately set to a DIFFERENT value, so a
regression that collapses the two fields together (or infers one from the
other) is caught rather than accidentally passing because both happen to
match in this environment.
"""
import os

from tools import shell


def test_a_reports_the_actual_cwd(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: "/runtime/actual/cwd")

    out = shell.get_env_info()

    assert "Current working directory: /runtime/actual/cwd" in out


def test_b_project_root_and_cwd_remain_separately_labeled(monkeypatch):
    monkeypatch.setattr(shell, "PROJECT_ROOT", "/configured/project/root")
    monkeypatch.setattr(os, "getcwd", lambda: "/runtime/actual/cwd")

    out = shell.get_env_info()

    assert "Project root: /configured/project/root" in out
    assert "Current working directory: /runtime/actual/cwd" in out
    # Neither line's value leaks into the other's line.
    lines = out.splitlines()
    project_root_line = next(l for l in lines if l.startswith("Project root:"))
    cwd_line = next(l for l in lines if l.startswith("Current working directory:"))
    assert "/runtime/actual/cwd" not in project_root_line
    assert "/configured/project/root" not in cwd_line


def test_c_existing_os_python_project_root_fields_remain_present(monkeypatch):
    monkeypatch.setattr(shell, "PROJECT_ROOT", "/configured/project/root")
    monkeypatch.setattr(os, "getcwd", lambda: "/runtime/actual/cwd")

    out = shell.get_env_info()

    assert out.startswith("OS: ")
    assert "\nPython: " in out
    assert "Project root: /configured/project/root" in out


def test_d_cwd_value_comes_from_the_runtime_not_inferred_from_project_root(monkeypatch):
    """The defect this pins: a fix that silently reused PROJECT_ROOT's value
    for the cwd field (rather than actually calling os.getcwd()) would make
    tests a/b/c above pass too, as long as they happened to use the same
    string -- this test specifically sets them to DIFFERENT values and
    proves the cwd field tracks the mocked os.getcwd() call, not
    PROJECT_ROOT."""
    monkeypatch.setattr(shell, "PROJECT_ROOT", "/configured/project/root")
    calls = {"n": 0}

    def fake_getcwd():
        calls["n"] += 1
        return "/a/completely/different/runtime/path"

    monkeypatch.setattr(os, "getcwd", fake_getcwd)

    out = shell.get_env_info()

    assert calls["n"] >= 1, "get_env_info() must actually call os.getcwd()"
    assert "Current working directory: /a/completely/different/runtime/path" in out
    assert "/a/completely/different/runtime/path" not in [
        l for l in out.splitlines() if l.startswith("Project root:")
    ][0]


def test_e_existing_behaviour_otherwise_unchanged(monkeypatch):
    """Available-tools and environment-variable sections still render, and
    OS/Python lines keep their exact pre-existing format -- this change adds
    one line, it does not restructure the rest of the output."""
    monkeypatch.setattr(shell, "PROJECT_ROOT", "/configured/project/root")
    monkeypatch.setattr(os, "getcwd", lambda: "/runtime/actual/cwd")
    monkeypatch.setattr(os, "environ", {"SOME_VAR": "some_value"})

    out = shell.get_env_info()

    assert "── Available tools ──────────────────────────────────" in out
    assert "── Environment variables (non-sensitive) ────────────" in out
    assert "SOME_VAR=some_value" in out
    # Field order preserved: OS, Python, Project root, Current working
    # directory, then the section divider -- the new line was inserted
    # immediately after Project root, not appended elsewhere.
    head = out.split("── Available tools")[0]
    field_lines = [l for l in head.splitlines() if l]
    assert field_lines[0].startswith("OS: ")
    assert field_lines[1].startswith("Python: ")
    assert field_lines[2].startswith("Project root: ")
    assert field_lines[3].startswith("Current working directory: ")
