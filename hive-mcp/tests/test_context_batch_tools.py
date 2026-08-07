"""Unit tests for the batch concurrency tools -- real files under tmp_path,
same style as test_context_get_file_content.py (PROJECT_ROOT monkeypatched,
no mocking of get_file_content/search_files themselves)."""
import asyncio

import pytest

from tools import context


def test_get_files_batch_reads_multiple_files_and_labels_each_section(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('b')", encoding="utf-8")

    result = asyncio.run(context.get_files_batch(["a.py", "b.py"]))

    assert "=== a.py ===" in result
    assert "print('a')" in result
    assert "=== b.py ===" in result
    assert "print('b')" in result


def test_get_files_batch_includes_the_not_found_message_for_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")

    result = asyncio.run(context.get_files_batch(["a.py", "missing.py"]))

    assert "print('a')" in result
    assert "File not found: missing.py" in result


def test_get_files_batch_preserves_input_order_in_output(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    for name in ("z.py", "a.py", "m.py"):
        (tmp_path / name).write_text(f"# {name}", encoding="utf-8")

    result = asyncio.run(context.get_files_batch(["z.py", "a.py", "m.py"]))

    assert result.index("=== z.py ===") < result.index("=== a.py ===") < result.index("=== m.py ===")


def test_get_files_batch_runs_reads_concurrently_not_sequentially(tmp_path, monkeypatch):
    """Real concurrency check: if 3 reads ran sequentially with a 0.1s delay each,
    total wall time would be >=0.3s. Concurrent (asyncio.gather + to_thread) keeps
    it well under that."""
    import time
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    def _slow_get_file_content(relative_path, offset=0, limit=0):
        time.sleep(0.1)
        return f"content of {relative_path}"

    monkeypatch.setattr(context, "get_file_content", _slow_get_file_content)

    t0 = time.perf_counter()
    result = asyncio.run(context.get_files_batch(["a.py", "b.py", "c.py"]))
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.25   # well under the 0.3s a sequential run would take
    assert "content of a.py" in result
    assert "content of c.py" in result


def test_get_files_batch_output_is_capped_by_max_output_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "_MAX_OUTPUT_CHARS", 100)
    (tmp_path / "big.py").write_text("x" * 500, encoding="utf-8")

    result = asyncio.run(context.get_files_batch(["big.py"]))

    assert len(result) <= 200   # capped text + the "TRUNCATED" message, well under 500
    assert "TRUNCATED" in result
