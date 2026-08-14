"""Tests for tools/scratch.py -- oversized-tool-result offload (agno-hive session-
context-overflow pipeline, part #2, 2026-08-14). See the module's own docstring for
which tools need this (run_command -- zero existing protection) and which don't
(get_file_content/search_files already reduce oversized results a different way).
"""
from tools import scratch


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(scratch, "PROJECT_ROOT", tmp_path)
    return tmp_path


# ── ensure_scratch_dir ───────────────────────────────────────────────────────────

def test_ensure_scratch_dir_creates_the_directory(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    d = scratch.ensure_scratch_dir()

    assert d.is_dir()
    assert d == tmp_path / ".hive_scratch"


def test_ensure_scratch_dir_is_idempotent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    scratch.ensure_scratch_dir()
    scratch.ensure_scratch_dir()  # must not raise

    assert (tmp_path / ".hive_scratch").is_dir()


def test_ensure_scratch_dir_registers_a_gitignore_entry(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    scratch.ensure_scratch_dir()

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".hive_scratch/" in gitignore


def test_ensure_scratch_dir_appends_to_an_existing_gitignore(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (tmp_path / ".gitignore").write_text("node_modules/\n.env\n", encoding="utf-8")

    scratch.ensure_scratch_dir()

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in gitignore  # existing content preserved
    assert ".env" in gitignore
    assert ".hive_scratch/" in gitignore


def test_ensure_scratch_dir_does_not_duplicate_the_gitignore_entry_on_repeat_calls(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    scratch.ensure_scratch_dir()
    scratch.ensure_scratch_dir()

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count(".hive_scratch/") == 1


# ── sweep_stale_scratch_files ─────────────────────────────────────────────────────

def test_sweep_deletes_files_older_than_the_ttl(tmp_path, monkeypatch):
    import os
    import time

    _setup(tmp_path, monkeypatch)
    d = scratch.ensure_scratch_dir()
    old_file = d / "old.txt"
    old_file.write_text("stale", encoding="utf-8")
    old_time = time.time() - 10_000
    os.utime(old_file, (old_time, old_time))

    deleted = scratch.sweep_stale_scratch_files(ttl_seconds=100)

    assert deleted == 1
    assert not old_file.exists()


def test_sweep_keeps_files_within_the_ttl(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    d = scratch.ensure_scratch_dir()
    fresh_file = d / "fresh.txt"
    fresh_file.write_text("still relevant", encoding="utf-8")

    deleted = scratch.sweep_stale_scratch_files(ttl_seconds=3600)

    assert deleted == 0
    assert fresh_file.exists()


def test_sweep_on_a_nonexistent_directory_is_a_safe_noop(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # ensure_scratch_dir() never called -- dir doesn't exist

    deleted = scratch.sweep_stale_scratch_files(ttl_seconds=100)

    assert deleted == 0


def test_sweep_only_touches_files_not_subdirectories(tmp_path, monkeypatch):
    import os
    import time

    _setup(tmp_path, monkeypatch)
    d = scratch.ensure_scratch_dir()
    sub = d / "some_subdir"
    sub.mkdir()
    old_time = time.time() - 10_000
    os.utime(sub, (old_time, old_time))

    deleted = scratch.sweep_stale_scratch_files(ttl_seconds=100)  # must not raise on the dir

    assert deleted == 0
    assert sub.is_dir()


# ── maybe_offload ─────────────────────────────────────────────────────────────────

def test_small_result_passes_through_unchanged(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    result = scratch.maybe_offload("short output", hint="run_command", threshold=100)

    assert result == "short output"
    assert not (tmp_path / ".hive_scratch").exists()  # no scratch dir needed at all


def test_oversized_result_is_written_to_a_scratch_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    big = "x" * 500

    result = scratch.maybe_offload(big, hint="pytest -v", threshold=100)

    assert "Output too large" in result
    assert ".hive_scratch/" in result
    scratch_files = list((tmp_path / ".hive_scratch").iterdir())
    assert len(scratch_files) == 1
    assert scratch_files[0].read_text(encoding="utf-8") == big


def test_oversized_result_response_includes_a_readable_preview(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    big = "A" * 50 + "B" * 500

    result = scratch.maybe_offload(big, hint="pytest -v", threshold=100)

    assert "A" * 50 in result  # preview starts from the real beginning of the content


def test_oversized_result_response_names_a_path_get_file_content_can_actually_read(tmp_path, monkeypatch):
    """The whole point: the returned path must be something get_file_content() can
    read back without being refused by is_excluded() -- see
    test_exclusions_scratch_allowance.py for the exclusions-side half of this."""
    import re

    _setup(tmp_path, monkeypatch)
    big = "z" * 500

    result = scratch.maybe_offload(big, hint="run_command", threshold=100)

    m = re.search(r"saved to: (\.hive_scratch/\S+)", result)
    assert m, f"no scratch path found in: {result!r}"
    rel_path = m.group(1)
    assert (tmp_path / rel_path).is_file()

    from tools import exclusions
    assert exclusions.is_excluded(rel_path) is False


def test_maybe_offload_sweeps_stale_files_before_writing_a_new_one(tmp_path, monkeypatch):
    import os
    import time

    _setup(tmp_path, monkeypatch)
    d = scratch.ensure_scratch_dir()
    old_file = d / "ancient.txt"
    old_file.write_text("long gone", encoding="utf-8")
    old_time = time.time() - 10_000
    os.utime(old_file, (old_time, old_time))
    monkeypatch.setattr(scratch, "_TTL_SECONDS", 100)

    scratch.maybe_offload("y" * 500, hint="cmd", threshold=100)

    assert not old_file.exists()


def test_concurrent_calls_with_the_same_hint_do_not_collide(tmp_path, monkeypatch):
    """Two offloads in quick succession (e.g. two run_command calls in the same
    session) must not overwrite each other's scratch file."""
    _setup(tmp_path, monkeypatch)

    r1 = scratch.maybe_offload("a" * 500, hint="pytest -v", threshold=100)
    r2 = scratch.maybe_offload("b" * 500, hint="pytest -v", threshold=100)

    scratch_files = list((tmp_path / ".hive_scratch").iterdir())
    assert len(scratch_files) == 2
    assert r1 != r2
