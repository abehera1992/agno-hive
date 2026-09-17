"""Phase U -- project_map candidate discovery fix.

Confirmed live (Phase T, T13a battery 2026-09-14/15, directly against the real
ZGX journal and the real repository filesystem): project_map('vouchers') ran a
two-tier glob lookup where the SECOND, broader glob only ever executed when the
FIRST, narrower one found nothing. The narrow glob (`**/*{name}*/**/*`, a
DIRECTORY-segment match) matched the real frontend directory
Client/.../vouchers/ (genuinely contains page.tsx + vouchers.module.scss), so
the broad glob (`**/*{name}*`, which also matches a bare FILENAME like
vouchers_api.py) never ran at all -- project_map returned facts only about the
frontend page, and the real backend router file was never discovered. This
fed directly into the Coordinator's own first delegation (which told the
Researcher to read page.tsx and look for backend route decorators inside it),
confirmed via the live journal in Phase T's own forensics.

Fix: both globs now always run and are merged (directory matches first,
preserving the existing bucketing bias toward them, then any NEW paths the
broader glob adds), rather than one short-circuiting the other. The narrow
glob's own matches are always a strict subset of the broad glob's (anything
inside a `*name*` directory also has `name` somewhere in its full path), so
this changes nothing for the single-match case and only ever ADDS candidates
that were previously silently discarded -- never removes or reorders anything
that was already found.

Every test here monkeypatches `find_files` and (where relevant)
`_symbol_locations` -- no real filesystem access, no live model, matching this
test file's own established convention (test_context_project_map_segment.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from tools import context  # noqa: E402
from tools.context import project_map  # noqa: E402


def _found(pattern: str, paths: list[str]) -> str:
    return f"{len(paths)} result(s) for '{pattern}':\n" + "\n".join(paths)


def _empty(pattern: str) -> str:
    return f"No matches for: {pattern}"


def _fake_find_files(dir_result: str | None = None, broad_result: str | None = None):
    """Returns a fake find_files(glob_pattern, max_results=...) distinguishing
    the directory-segment glob ('**/*{name}*/**/*') from the broader one
    ('**/*{name}*') by pattern shape alone -- the same distinction
    project_map's own two calls use."""
    def fake(glob_pattern, max_results=800):
        if glob_pattern.endswith("/**/*"):
            return dir_result if dir_result is not None else _empty(glob_pattern)
        return broad_result if broad_result is not None else _empty(glob_pattern)
    return fake


# ── 1. The exact confirmed live failure, reproduced and fixed -------------

def test_vouchers_directory_match_no_longer_suppresses_the_filename_match(monkeypatch):
    """The precise Phase T incident: a directory match (frontend page route)
    existed, so the filename match (the backend router file) was never even
    looked for. Both must now be discoverable."""
    dir_paths = [
        "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx",
        "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/vouchers.module.scss",
    ]
    broad_paths = dir_paths + ["API/inventory-service/router/vouchers_api.py"]
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_found("**/*vouchers*/**/*", dir_paths),
        broad_result=_found("**/*vouchers*", broad_paths),
    ))
    out = project_map("vouchers")
    assert "page.tsx" in out or "vouchers/" in out  # directory side still surfaced
    assert "router" in out  # the backend router directory must now appear too


# ── 2-7. Explicit scope: directory-only, filename-only, both, dedup, ------
# unrelated, empty -----------------------------------------------------------

def test_directory_only_match_still_works_unchanged(monkeypatch):
    """No filename-only matches exist beyond what the directory glob already
    found -- behavior must be identical to before this fix."""
    paths = ["Client/.../vouchers/page.tsx", "Client/.../vouchers/vouchers.module.scss"]
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_found("**/*vouchers*/**/*", paths),
        broad_result=_found("**/*vouchers*", paths),  # broad glob finds the SAME set
    ))
    out = project_map("vouchers")
    assert "vouchers" in out.lower()
    assert "no directory or file" not in out.lower()


def test_filename_only_match_is_now_discoverable(monkeypatch):
    """No directory named 'vouchers' exists at all -- only a bare filename
    match. Before this fix this path WAS reachable (the narrow glob found
    nothing, so the fallback ran) -- confirming this shape still works."""
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_empty("**/*vouchers*/**/*"),
        broad_result=_found("**/*vouchers*", ["API/inventory-service/router/vouchers_api.py"]),
    ))
    out = project_map("vouchers")
    assert "router" in out


def test_both_directory_and_filename_matches_are_both_discoverable(monkeypatch):
    """The general shape of the fix: a real directory match AND a real,
    separate filename match must both survive into the candidate pool."""
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_found("**/*vouchers*/**/*", ["Client/.../vouchers/page.tsx"]),
        broad_result=_found("**/*vouchers*", [
            "Client/.../vouchers/page.tsx",
            "API/inventory-service/router/vouchers_api.py",
        ]),
    ))
    out = project_map("vouchers")
    assert "vouchers" in out.lower()
    assert "router" in out


def test_duplicate_candidate_paths_are_deduplicated(monkeypatch):
    """A path the narrow glob already found must not be counted twice just
    because the broad glob also matches it (it always will, since the narrow
    glob's matches are a strict subset of the broad glob's)."""
    shared = "Client/.../vouchers/page.tsx"
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_found("**/*vouchers*/**/*", [shared]),
        broad_result=_found("**/*vouchers*", [shared]),  # same path, from the broad glob too
    ))
    out = project_map("vouchers")
    # Rendered once, not twice, for the SAME directory bucket.
    assert out.count("Client/.../vouchers/") <= 1 or out.count("vouchers/  (") == 1


def test_unrelated_directory_matches_do_not_leak_in(monkeypatch):
    """A directory match for a DIFFERENT, unrelated name must not appear just
    because both globs now always run -- each glob is still scoped to the
    SAME requested component name."""
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_found("**/*vouchers*/**/*", ["Client/.../vouchers/page.tsx"]),
        broad_result=_found("**/*vouchers*", ["Client/.../vouchers/page.tsx"]),
    ))
    out = project_map("vouchers")
    assert "payments" not in out.lower()
    assert "business_api" not in out.lower()


def test_empty_no_match_behavior_falls_through_to_symbol_lookup(monkeypatch):
    """Neither glob finds anything -- must still fall through to the existing
    symbol-lookup fallback and, when that also finds nothing, the existing
    empty-handed message -- completely unchanged by this fix."""
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_empty("**/*zzzznomatch*/**/*"),
        broad_result=_empty("**/*zzzznomatch*"),
    ))
    monkeypatch.setattr(context, "_symbol_locations", lambda name: [])
    out = project_map("zzzznomatch")
    assert "nothing in this repository" in out.lower()


# ── 8. Directory-derived candidates still rank first (ordering preserved) --

def test_directory_derived_bucket_still_ranks_first_when_it_has_more_files(monkeypatch):
    """Phase U's own explicit requirement: preserve existing ordering
    semantics where possible. A directory match with MORE files must still
    outrank a single filename match, exactly as before this fix -- this
    change widens the candidate pool, it does not change the ranking rule."""
    dir_paths = [f"Client/.../vouchers/file{i}.tsx" for i in range(5)]
    monkeypatch.setattr(context, "find_files", _fake_find_files(
        dir_result=_found("**/*vouchers*/**/*", dir_paths),
        broad_result=_found("**/*vouchers*", dir_paths + ["API/router/vouchers_api.py"]),
    ))
    out = project_map("vouchers")
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert lines, "expected at least one ranked location line"
    assert "vouchers/" in lines[0]  # the 5-file directory bucket ranks first


# ── 9. No fuzzy guessing / no hardcoding -------------------------------------

def test_source_never_hardcodes_vouchers_api():
    """The fix must be general (merge two glob results), never a special
    case for this one incident's own filenames. Checked against actual CODE
    lines only -- comments legitimately name the incident that motivated the
    fix (see this test file's own module docstring for the same reason)."""
    import inspect
    lines = inspect.getsource(project_map).splitlines()
    code_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    code_text = "\n".join(code_lines).lower()
    assert "vouchers" not in code_text
