"""Regression tests for the LightRAG basename collision fix.

Confirmed 2026-08-02, two layers deep, against a live LightRAG instance
(disposable test project "hive-mcp-debug-test", cleaned up after):

1. LightRAG (1.5.4) derives an unspecified document's id from
   `normalize_document_file_path(file_path)`, which collapses to the
   BASENAME only, dropping the directory. This looked like the whole bug —
   but passing a distinct explicit doc_id per call does NOT fix it.

2. The real gate is a separate, hard-coded "filename-based dedup" step in
   `apipeline_enqueue_documents` (lightrag/pipeline.py): it looks up any
   EXISTING doc_status row with the same basename and rejects the new
   document as a duplicate regardless of doc_id. Verified: two distinct
   files sharing a basename ("Client/routes/home/page.tsx" vs ".../about/
   page.tsx"), and separately two chunks of the SAME file ("def foo()" /
   "def bar()" under "app/utils.py"), both still got rejected even with
   distinct explicit doc_ids — "Duplicate document detected (filename)",
   `status: failed`, zero chunks in storage for the second arrival.

Only making file_path itself basename-unique per (file, chunk_index) fixed
it — confirmed by re-running the same two scenarios through
_chunk_citation_path's folding scheme and seeing both inserts succeed.
EkamApp has 39 files named page.tsx, 17 __init__.py, 11 main.py — a large,
real blast radius before this fix.

index_project() now passes both: _chunk_citation_path (the actual fix, sent
as file_path) and _chunk_doc_id (an explicit id, so _delete_stale doesn't
need to replicate LightRAG's own id-hash algorithm) on every insert.
"""
from tools import index


def test_chunk_doc_id_differs_for_different_files_with_the_same_basename():
    # This is the exact collision that silently dropped content: two distinct
    # files sharing a basename must never produce the same id.
    home = index._chunk_doc_id("ekamweb", "Client/routes/home/page.tsx", 0)
    about = index._chunk_doc_id("ekamweb", "Client/routes/about/page.tsx", 0)
    assert home != about


def test_chunk_doc_id_differs_for_different_chunks_of_the_same_file():
    # Every chunk of one multi-chunk file shares a file_path, which is exactly
    # what used to collide when no explicit id was passed.
    chunk_0 = index._chunk_doc_id("agno-hive", "app/utils.py", 0)
    chunk_1 = index._chunk_doc_id("agno-hive", "app/utils.py", 1)
    assert chunk_0 != chunk_1


def test_chunk_doc_id_is_deterministic():
    # _delete_stale must be able to reconstruct a file's PAST ids purely from
    # its relative path + a stored chunk count -- no id storage needed beyond
    # that count, which only works if this is a pure, stable function.
    first = index._chunk_doc_id("ekam", "training/RUNBOOK.md", 2)
    second = index._chunk_doc_id("ekam", "training/RUNBOOK.md", 2)
    assert first == second


def test_chunk_doc_id_differs_across_projects_for_the_same_path():
    # Belt-and-suspenders: even though LightRAG already isolates projects via
    # separate workspaces, the id itself shouldn't accidentally collide too.
    ekam = index._chunk_doc_id("ekam", "config.py", 0)
    agno = index._chunk_doc_id("agno-hive", "config.py", 0)
    assert ekam != agno


def test_chunk_citation_path_differs_for_different_files_with_the_same_basename():
    # This is THE actual fix — LightRAG's dedup keys on Path(file_path).name,
    # so two files sharing a basename must produce different citation paths
    # or the second one gets silently rejected as a duplicate at insert time.
    home = index._chunk_citation_path("Client/routes/home/page.tsx", 0)
    about = index._chunk_citation_path("Client/routes/about/page.tsx", 0)
    assert home != about


def test_chunk_citation_path_survives_basename_extraction():
    # LightRAG extracts Path(file_path).name -- everything before the last
    # "/" is discarded. The fix only works if no "/" survives into the
    # folded path, or LightRAG's own basename extraction would silently
    # undo it and reintroduce the exact collision this exists to prevent.
    from pathlib import PurePosixPath
    folded = index._chunk_citation_path("Client/routes/home/page.tsx", 0)
    assert "/" not in folded
    assert PurePosixPath(folded).name == folded


def test_chunk_citation_path_differs_for_different_chunks_of_the_same_file():
    chunk_0 = index._chunk_citation_path("app/utils.py", 0)
    chunk_1 = index._chunk_citation_path("app/utils.py", 1)
    assert chunk_0 != chunk_1


def test_chunk_citation_path_is_deterministic():
    first = index._chunk_citation_path("training/RUNBOOK.md", 2)
    second = index._chunk_citation_path("training/RUNBOOK.md", 2)
    assert first == second


def test_state_entry_round_trips_chunk_count_through_the_walk_parser(tmp_path, monkeypatch):
    # A legacy 3-field entry (key|sha|chunk_count, no chunker_version -- written
    # before _CHUNKER_VERSION existed) must parse without crashing, and the
    # missing 4th field must NOT break the existing key/sha comparison logic.
    #
    # This test originally asserted the file gets SKIPPED here -- that was
    # correct before _CHUNKER_VERSION existed, but is now the wrong expectation:
    # a state entry with no recorded chunker version is exactly the case
    # test_index_chunker_version.py's test_missing_version_field_forces_reprocessing
    # covers, and must force reprocessing, not a skip (see that file's module
    # docstring for why: a chunker upgrade needs to reach files whose CONTENT
    # is unchanged too, and content-hash comparison alone can never know that).
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(index, "_STATE_DIR", tmp_path / ".hive-index-state")
    monkeypatch.setattr(index, "is_excluded", lambda rel: False)

    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    key = index._file_key(f)
    sha = index._sha256(f)

    state_dir = tmp_path / ".hive-index-state"
    state_dir.mkdir()
    (state_dir / "proj.json").write_text(
        '{"sample.py": "' + key + "|" + sha + '|1"}', encoding="utf-8"
    )

    import asyncio
    result = asyncio.run(index.index_project("proj", "http://127.0.0.1:1/mcp"))

    # Not skipped: no chunker_version in the stored entry -- reached the network
    # attempt and failed there, proving the legacy entry was NOT trusted as current.
    assert "Error connecting to LightRAG" in result
