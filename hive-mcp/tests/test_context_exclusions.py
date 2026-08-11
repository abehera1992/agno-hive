"""Regression test: every read-side tool in context.py must honor is_excluded(),
not just the write-side tools in files.py.

Found 2026-08-10 during a live groundedness test: a task scoped to the Party
Master frontend wandered into `signoz/` — a bundled, vendored, unrelated
observability tool — and get_file_content() served its contents without any
refusal, even though `signoz` was correctly listed in the project's own
EXCLUDE_DIRS env var. Root cause: get_file_content() only ever checked the
CLAUDE.md special-case (_EXCLUDED_ASSISTANT_FILES), never is_excluded() itself.
write_file()/apply_diff() in tools/files.py already called is_excluded() before
every write — this was a read-side gap, not a missing config entry (the env var
was already correct; see hive-mcp/config.py's EXCLUDE_DIRS/EXCLUDE_GLOBS/
EXCLUDE_ALLOW, sourced purely from a local, gitignored .env file, never
hardcoded in this repo).

Same audit found three more read-side gaps sharing the identical root cause
(a function reads/lists a path without ever calling is_excluded()):
  - _rg_glob() (used by both find_files() and _find_by_basename()) built its
    `rg --files --glob ...` command with no exclusion globs at all.
  - count_matches() built its `rg --count-matches --glob ...` command the same
    way — could count occurrences inside a vendored tree.
  - list_directory() filtered a *listed directory's children* by name against
    _IGNORE_DIRS, but never checked whether the *target path itself* was
    excluded — so list_directory('signoz') listed straight into it even though
    listing the parent would never have shown 'signoz/' as an entry.

search_files() already passed _RG_EXCLUDES / used _walk_project() correctly on
both its rg and Python-fallback paths, and list_directory_tree() already pruned
_IGNORE_DIRS for directory listing — both left as-is, no fix needed.
"""
from tools import context


def test_get_file_content_refuses_a_path_under_an_excluded_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "is_excluded", lambda rel: rel.startswith("signoz/"))
    sub = tmp_path / "signoz" / "frontend" / "src"
    sub.mkdir(parents=True)
    (sub / "pageComponent.tsx").write_text("vendored observability UI", encoding="utf-8")

    result = context.get_file_content("signoz/frontend/src/pageComponent.tsx")

    assert "blocked" in result
    assert "excluded path" in result
    assert "vendored observability UI" not in result


def test_get_file_content_still_serves_a_non_excluded_path(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "is_excluded", lambda rel: rel.startswith("signoz/"))
    (tmp_path / "models.py").write_text("class Party(Base): pass", encoding="utf-8")

    result = context.get_file_content("models.py")

    assert "class Party(Base): pass" in result


def test_rg_glob_passes_rg_excludes_to_the_files_command(monkeypatch):
    captured_cmd = {}

    class _FakeResult:
        stdout = "a.py\n"
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeResult()

    monkeypatch.setattr(context, "_RG_EXCLUDES", ["--glob", "!signoz/**"])
    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_run)

    context._rg_glob("rg", "**/*.py", 200)

    assert "--glob" in captured_cmd["cmd"]
    assert "!signoz/**" in captured_cmd["cmd"]


def test_count_matches_passes_rg_excludes_to_the_command(monkeypatch):
    captured_cmd = {}

    class _FakeResult:
        stdout = "a.py:2\n"
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeResult()

    monkeypatch.setattr(context, "_RG_EXCLUDES", ["--glob", "!signoz/**"])
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rg")
    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_run)

    context.count_matches("TODO")

    assert "!signoz/**" in captured_cmd["cmd"]


def test_list_directory_refuses_an_excluded_directory_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "is_excluded", lambda rel: rel == "signoz" or rel.startswith("signoz/"))
    sub = tmp_path / "signoz" / "frontend"
    sub.mkdir(parents=True)
    (sub / "index.tsx").write_text("x", encoding="utf-8")

    result = context.list_directory("signoz")

    assert "blocked" in result
    assert "excluded path" in result
    assert "index.tsx" not in result


def test_list_directory_still_serves_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "is_excluded", lambda rel: rel == "signoz" or rel.startswith("signoz/"))
    (tmp_path / "src").mkdir()

    result = context.list_directory("")

    assert "[DIR]  src" in result


def test_list_directory_still_prunes_an_excluded_child_by_name(tmp_path, monkeypatch):
    # Child-level pruning is via _IGNORE_DIRS (bare directory-name membership),
    # a separate, pre-existing mechanism from the is_excluded() target-path check
    # this fix adds -- confirm the fix didn't disturb it.
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "is_excluded", lambda rel: False)
    monkeypatch.setattr(context, "_IGNORE_DIRS", {"signoz"})
    (tmp_path / "src").mkdir()
    (tmp_path / "signoz").mkdir()

    result = context.list_directory("")

    assert "[DIR]  src" in result
    assert "signoz" not in result
