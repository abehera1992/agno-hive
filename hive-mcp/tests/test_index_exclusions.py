"""Regression test: index_project()'s bootstrap walk must honor EXCLUDE_GLOBS.

Found 2026-08-02 while verifying agno-hive's own training/eval/** data (98
near-identical eval-case JSON files + a 1.3MB training corpus) stayed out of
LightRAG after adding EXCLUDE_GLOBS=training/eval/cases/**,training/eval/captured/**,
training/data/*.jsonl to its project env. is_excluded() correctly returned True
for these paths in isolation, but the files still showed up in the index state
file after a live bootstrap run.

Root cause: the walk's os.walk dir-pruning only consults EXCLUDE_DIRS (directory
NAMES, via _IGNORE_DIRS) to decide which directories to descend into. EXCLUDE_GLOBS
patterns like "training/data/*.jsonl" are file-pattern-scoped, not a directory name,
so they can't prune a directory — and nothing downstream ever called is_excluded()
on the collected file paths either. Every other tool (context.py, files.py) already
routes through is_excluded(); index.py's walk was the one path that bypassed it.

Uses only files that fall under EXCLUDE_GLOBS so to_process ends up empty and
index_project() never attempts the LightRAG network call ("if to_process:" gates
the whole connection block) — keeping this a fast, deterministic unit test of the
walk/filter logic alone.
"""
import asyncio

from tools import index


def test_bootstrap_walk_skips_files_matching_exclude_globs(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        index, "is_excluded",
        lambda rel: rel.startswith("training/eval/cases/")
        or rel.startswith("training/eval/captured/")
        or (rel.startswith("training/data/") and rel.endswith(".jsonl")),
    )
    monkeypatch.setattr(index, "_STATE_DIR", tmp_path / ".hive-index-state")

    (tmp_path / "training" / "eval" / "cases").mkdir(parents=True)
    (tmp_path / "training" / "eval" / "cases" / "A-tool-01.json").write_text("{}", encoding="utf-8")
    (tmp_path / "training" / "eval" / "captured").mkdir(parents=True)
    (tmp_path / "training" / "eval" / "captured" / "E1-stale-knowledge.json").write_text("{}", encoding="utf-8")
    (tmp_path / "training" / "data").mkdir(parents=True)
    (tmp_path / "training" / "data" / "corpus_v2.jsonl").write_text("{}\n", encoding="utf-8")

    result = asyncio.run(index.index_project("agno-hive", "http://unused:9002/mcp"))

    # to_process ended up empty, so the walk/filter stage is the whole story here:
    # no LightRAG connection was attempted and nothing was queued for indexing.
    assert "Files scanned:   0" in result
    assert "Done" in result
