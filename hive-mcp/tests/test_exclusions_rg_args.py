"""Regression test: rg_args() must translate EXCLUDE_DIRS into ripgrep excludes, not
just EXCLUDE_GLOBS.

Confirmed live 2026-08-11: EkamApp's own project env lists a vendored directory the
way this module's own docstring documents -- EXCLUDE_DIRS=mailcow,signoz,graphify-out,
graphify-cache -- but rg_args() only ever iterated EXCLUDE_GLOBS. is_excluded()
(the Python-level check get_file_content()/write_file()/apply_diff() call directly)
correctly refused a direct read of a signoz path, but every ripgrep-backed tool
sharing rg_args() (find_files, _find_by_basename, search_files, count_matches) still
freely listed and searched signoz's own files -- confirmed by a live disambiguation
list for the ambiguous basename 'index.tsx' offering
signoz/frontend/src/hooks/useDarkMode/index.tsx as if it were a real project
candidate.
"""
from tools import exclusions


def test_rg_args_excludes_a_directory_listed_only_in_exclude_dirs(monkeypatch):
    monkeypatch.setattr(exclusions, "EXCLUDE_DIRS", {"signoz"})
    monkeypatch.setattr(exclusions, "EXCLUDE_GLOBS", [])

    args = exclusions.rg_args()

    assert "--glob" in args
    assert "!**/signoz/**" in args


def test_rg_args_still_covers_exclude_globs_too(monkeypatch):
    monkeypatch.setattr(exclusions, "EXCLUDE_DIRS", set())
    monkeypatch.setattr(exclusions, "EXCLUDE_GLOBS", ["**/infra/mailcow/**"])

    args = exclusions.rg_args()

    assert "!**/infra/mailcow/**" in args


def test_rg_args_covers_both_exclude_dirs_and_exclude_globs_together(monkeypatch):
    monkeypatch.setattr(exclusions, "EXCLUDE_DIRS", {"signoz", "graphify-out"})
    monkeypatch.setattr(exclusions, "EXCLUDE_GLOBS", ["**/infra/mailcow/**"])

    args = exclusions.rg_args()

    assert "!**/signoz/**" in args
    assert "!**/graphify-out/**" in args
    assert "!**/infra/mailcow/**" in args


def test_rg_args_already_negated_glob_is_not_double_negated(monkeypatch):
    monkeypatch.setattr(exclusions, "EXCLUDE_DIRS", set())
    monkeypatch.setattr(exclusions, "EXCLUDE_GLOBS", ["!docs/generated/**"])

    args = exclusions.rg_args()

    assert "!docs/generated/**" in args
    assert "!!docs/generated/**" not in args
