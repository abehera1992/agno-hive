"""Regression tests: a chunker-version mismatch must force reprocessing even when
a file's own content is unchanged.

Confirmed live 2026-08-03/04 on a 730-file project: a `hive --bootstrap --force`
run hit its pass cap partway through (only ~255 files force-touched before
returning Partial). The follow-up plain `hive --bootstrap` correctly resumed and
eventually reported "Done" -- but content-hash/mtime comparison alone had no way
to know the chunker itself had changed (a new "Lines:" header was added to every
chunk), so the remaining ~475 files were legitimately "skipped: unchanged" and
never re-embedded, even though "Done" implied full completion.

Fix: state entries gained a 4th field, chunker version. A stored version that
doesn't match the current _CHUNKER_VERSION is treated the same as changed
content -- never a safe skip, in either the fast (mtime+size) path or the
metadata-only-churn (sha-confirms-unchanged) path.
"""
import asyncio

from tools import index


def _touch_state_file(tmp_path, project_id, entries: dict):
    state_dir = tmp_path / ".hive-index-state"
    state_dir.mkdir(exist_ok=True)
    import json
    (state_dir / f"{project_id}.json").write_text(json.dumps(entries), encoding="utf-8")


def test_current_version_and_unchanged_content_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(index, "_STATE_DIR", tmp_path / ".hive-index-state")
    monkeypatch.setattr(index, "is_excluded", lambda rel: False)

    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    key = index._file_key(f)
    sha = index._sha256(f)
    _touch_state_file(tmp_path, "proj", {"sample.py": f"{key}|{sha}|1|{index._CHUNKER_VERSION}"})

    result = asyncio.run(index.index_project("proj", "http://127.0.0.1:1/mcp"))

    assert "Files skipped:   1" in result
    assert "Done" in result  # to_process was empty -- never attempted the network


def test_stale_version_forces_reprocessing_despite_unchanged_content(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(index, "_STATE_DIR", tmp_path / ".hive-index-state")
    monkeypatch.setattr(index, "is_excluded", lambda rel: False)

    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    key = index._file_key(f)
    sha = index._sha256(f)
    stale_version = index._CHUNKER_VERSION + 999  # guaranteed mismatch
    _touch_state_file(tmp_path, "proj", {"sample.py": f"{key}|{sha}|1|{stale_version}"})

    result = asyncio.run(index.index_project("proj", "http://127.0.0.1:1/mcp"))

    # Not skipped -- it reached the network-connection attempt and failed there,
    # proving the skip-check let it through despite identical content.
    assert "Error connecting to LightRAG" in result  # to_process was non-empty -- it reached the network attempt


def test_missing_version_field_forces_reprocessing(tmp_path, monkeypatch):
    """A pre-this-fix 3-field entry (key|sha|chunk_count, no version) must be
    treated as stale, not silently trusted as current."""
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(index, "_STATE_DIR", tmp_path / ".hive-index-state")
    monkeypatch.setattr(index, "is_excluded", lambda rel: False)

    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    key = index._file_key(f)
    sha = index._sha256(f)
    _touch_state_file(tmp_path, "proj", {"sample.py": f"{key}|{sha}|1"})  # no 4th field

    result = asyncio.run(index.index_project("proj", "http://127.0.0.1:1/mcp"))

    assert "Error connecting to LightRAG" in result  # to_process was non-empty -- it reached the network attempt


def test_metadata_churn_path_also_respects_stale_version(tmp_path, monkeypatch):
    """mtime/size differ (e.g. git checkout) but content-sha matches -- normally a
    'metadata-only churn, skip' case. A stale version must override that and force
    reprocessing anyway, since the file's indexed FORM (not its content) is stale."""
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(index, "_STATE_DIR", tmp_path / ".hive-index-state")
    monkeypatch.setattr(index, "is_excluded", lambda rel: False)

    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    sha = index._sha256(f)
    # A fast-key that does NOT match the file's real current key (simulates
    # mtime/size churn), but the SHA is genuinely correct -- and the version is stale.
    fake_old_fast_key = "0:0"
    stale_version = index._CHUNKER_VERSION + 999
    _touch_state_file(tmp_path, "proj", {"sample.py": f"{fake_old_fast_key}|{sha}|1|{stale_version}"})

    result = asyncio.run(index.index_project("proj", "http://127.0.0.1:1/mcp"))

    assert "Error connecting to LightRAG" in result  # to_process was non-empty -- it reached the network attempt
