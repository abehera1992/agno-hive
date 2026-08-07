"""Tests for tools/bash.py -- persistent-cwd bash sessions (Phase 1: blocking only).

House style matches test_files.py: bash.py does `from config import PROJECT_ROOT,
WRITE_REVIEW, HIVE_BASH_*` (direct name imports), so tests monkeypatch the
module-local bound names directly (monkeypatch.setattr(bash, "X", ...)), not
config.X -- patching config.X would have no effect here.
"""
import re
import sys

from tools import bash


def _sid(result: str) -> str:
    """Extract the session_id from bash_session_start()'s 'session_id: <id>\\ncwd: ...' result."""
    m = re.match(r"session_id: (\S+)", result)
    assert m, f"expected a session_id line, got: {result!r}"
    return m.group(1)


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(bash, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bash, "WRITE_REVIEW", False)
    monkeypatch.setattr(bash, "HIVE_BASH_TOOL_ENABLED", True)
    monkeypatch.setattr(bash, "HIVE_BASH_MAX_SESSIONS", 10)
    monkeypatch.setattr(bash, "HIVE_BASH_MAX_OUTPUT_CHARS", 20000)
    monkeypatch.setattr(bash, "HIVE_BASH_DEFAULT_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(bash, "HIVE_BASH_MAX_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(bash, "HIVE_BASH_SESSION_TTL_SECONDS", 1800)
    bash._sessions.clear()


# ── bash_session_start ──────────────────────────────────────────────────────────

def test_session_start_returns_new_id_and_default_cwd(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)

    result = bash.bash_session_start()

    assert result.startswith("session_id: ")
    assert str(tmp_path) in result
    sid = _sid(result)
    assert sid in bash._sessions
    assert bash._sessions[sid].cwd == tmp_path


def test_session_start_relative_cwd_resolves_against_project_root(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "sub").mkdir()

    result = bash.bash_session_start(cwd="sub")

    sid = _sid(result)
    assert bash._sessions[sid].cwd == (tmp_path / "sub").resolve()


def test_session_start_nonexistent_cwd_returns_error_no_session_created(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)

    result = bash.bash_session_start(cwd="does_not_exist")

    assert "not a directory" in result
    assert bash._sessions == {}


def test_max_sessions_cap_rejects_new_session_at_limit(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(bash, "HIVE_BASH_MAX_SESSIONS", 1)

    first = bash.bash_session_start()
    second = bash.bash_session_start()

    assert first.startswith("session_id: ")
    assert "limit" in second.lower()
    assert len(bash._sessions) == 1


# ── bash_run: session lookup, cwd persistence ───────────────────────────────────

def test_bash_run_unknown_session_id_returns_clear_error(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)

    result = bash.bash_run("nonexistent-session", "echo hi")

    assert "unknown session_id" in result


def test_bash_run_second_call_sees_cwd_persisted_from_first_cd(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "marker.txt").write_text("found it", encoding="utf-8")
    sid = _sid(bash.bash_session_start())

    cd_result = bash.bash_run(sid, "cd sub")
    read_result = bash.bash_run(
        sid, f'"{sys.executable}" -c "print(open(\'marker.txt\').read())"'
    )

    assert "[exit 0]" in cd_result
    assert "found it" in read_result
    assert bash._sessions[sid].cwd == (tmp_path / "sub").resolve()


def test_bash_run_bare_cd_to_nonexistent_dir_does_not_change_cwd(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "marker.txt").write_text("still here", encoding="utf-8")
    sid = _sid(bash.bash_session_start())

    cd_result = bash.bash_run(sid, "cd does_not_exist")
    read_result = bash.bash_run(
        sid, f'"{sys.executable}" -c "print(open(\'marker.txt\').read())"'
    )

    assert "[exit 0]" not in cd_result
    assert "still here" in read_result
    assert bash._sessions[sid].cwd == tmp_path


def test_bash_run_compound_cd_in_subshell_does_not_leak_into_session_cwd(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "sub").mkdir()
    (tmp_path / "marker.txt").write_text("base dir marker", encoding="utf-8")
    sid = _sid(bash.bash_session_start())

    compound_result = bash.bash_run(sid, f'cd sub && "{sys.executable}" -c "print(1)"')
    read_result = bash.bash_run(
        sid, f'"{sys.executable}" -c "print(open(\'marker.txt\').read())"'
    )

    assert "[exit 0]" in compound_result
    assert "base dir marker" in read_result  # still resolves from tmp_path, not tmp_path/sub
    assert bash._sessions[sid].cwd == tmp_path


# ── bash_run: output cap, timeout, WRITE_REVIEW gating ──────────────────────────

def test_bash_run_output_truncated_at_max_chars_with_marker(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(bash, "HIVE_BASH_MAX_OUTPUT_CHARS", 50)
    sid = _sid(bash.bash_session_start())

    result = bash.bash_run(sid, f'"{sys.executable}" -c "print(\'x\' * 500)"')

    assert "TRUNCATED" in result
    assert len(result) < 500


def test_bash_run_timeout_returns_message_not_hang(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    sid = _sid(bash.bash_session_start())

    result = bash.bash_run(sid, f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout=1)

    assert "timed out after 1s" in result


def test_bash_run_caller_timeout_clamped_to_configured_max(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(bash, "HIVE_BASH_MAX_TIMEOUT_SECONDS", 30)
    sid = _sid(bash.bash_session_start())
    captured = {}

    class _FakeCompleted:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(command, shell, capture_output, text, timeout, cwd):
        captured["timeout"] = timeout
        return _FakeCompleted()

    monkeypatch.setattr(bash.subprocess, "run", _fake_run)

    bash.bash_run(sid, "echo hi", timeout=9999)

    assert captured["timeout"] == 30


def test_bash_run_blocked_by_write_review_regex_like_run_shell(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(bash, "WRITE_REVIEW", True)
    sid = _sid(bash.bash_session_start())

    result = bash.bash_run(sid, "echo hi >> out.txt")

    assert "blocked" in result
    assert not (tmp_path / "out.txt").exists()


def test_bash_run_background_true_returns_not_available_message(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    sid = _sid(bash.bash_session_start())

    result = bash.bash_run(sid, "echo hi", background=True)

    assert "not available yet" in result


# ── bash_session_close ───────────────────────────────────────────────────────────

def test_bash_session_close_frees_slot_and_invalidates_id(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    sid = _sid(bash.bash_session_start())

    close_result = bash.bash_session_close(sid)
    run_after_close = bash.bash_run(sid, "echo hi")

    assert "session closed" in close_result
    assert sid not in bash._sessions
    assert "unknown session_id" in run_after_close


def test_bash_session_close_unknown_id_returns_clear_error(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)

    result = bash.bash_session_close("nonexistent-session")

    assert "unknown session_id" in result


# ── idle-TTL reaper ──────────────────────────────────────────────────────────────

def test_idle_reaper_sweep_closes_session_past_ttl(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(bash, "HIVE_BASH_SESSION_TTL_SECONDS", 10)
    sid = _sid(bash.bash_session_start())
    started_at = bash._sessions[sid].last_used_at

    closed_count = bash._reap_once(now=started_at + 20)

    assert closed_count == 1
    assert sid not in bash._sessions


def test_idle_reaper_sweep_leaves_recently_used_session(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(bash, "HIVE_BASH_SESSION_TTL_SECONDS", 10)
    sid = _sid(bash.bash_session_start())
    started_at = bash._sessions[sid].last_used_at

    closed_count = bash._reap_once(now=started_at + 5)

    assert closed_count == 0
    assert sid in bash._sessions


# ── cleanup_all (shutdown hook) ───────────────────────────────────────────────────

def test_cleanup_all_closes_every_session(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    bash.bash_session_start()
    bash.bash_session_start()

    closed_count = bash.cleanup_all()

    assert closed_count == 2
    assert bash._sessions == {}
