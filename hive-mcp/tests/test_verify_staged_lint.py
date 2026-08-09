"""Regression tests: verify_claims' convention linter must also check files that
were actually staged for review, not just fenced code the model chose to echo
back into its prose answer.

Confirmed live 2026-08-05: a write_file() task created a new page.tsx using bare
Tailwind classNames and shadcn/ui components instead of this project's mandatory
SCSS-module convention. The final answer was a plain narrative summary ("I've
created the party detail page...") with no code re-pasted into it, so the
existing fenced-code-block lint had nothing to scan and reported zero
violations -- not because the code was clean, but because the actual code was
never shown to the checker at all.
"""
import os

from tools import verify


def _stage(tmp_path, rel_path: str, content: str):
    p = tmp_path / (rel_path + ".hive_proposed")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_stale_staged_file_from_an_earlier_session_is_ignored(tmp_path, monkeypatch):
    """Confirmed live 2026-08-05: an unscoped rglob across the whole project picked
    up a FOUR-DAY-OLD staged file left over from an unrelated earlier session. Its
    unrelated identifiers filled nearly the entire _MAX_CLAIMS cap, crowding out
    the current task's own staged file and ballooning a single verify_claims call
    from tens of seconds to over 1300s. A staged file old enough to be from a
    forgotten prior session must not be treated as part of the current task."""
    import os
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", ['className="::use styles.x, not a bare className string'])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    stale = _stage(tmp_path, "old_unrelated_file", 'x = <div className="stale-leftover">hi</div>')
    import time
    os.utime(stale, (time.time() - 4 * 24 * 60 * 60,) * 2)

    answer = "I've added the new feature."

    report = verify.verify_claims(answer)

    assert "FORBIDDEN pattern" not in report
    assert "stale-leftover" not in report


def test_forbidden_pattern_in_staged_file_is_caught_with_no_fenced_code_in_answer(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", ['className="::use styles.x, not a bare className string'])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(tmp_path, "page.tsx", '<div className="container mx-auto py-8">hi</div>')

    # Pure narrative -- no fenced code block, nothing for the OLD check to scan.
    answer = "I've created the party detail page following project conventions."

    report = verify.verify_claims(answer)

    assert "FORBIDDEN pattern" in report
    assert "page.tsx.hive_proposed" in report


def test_clean_staged_file_reports_no_violation(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", ['className="::use styles.x, not a bare className string'])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(tmp_path, "page.tsx", 'import styles from "./page.module.scss";\n<div className={styles.container}>hi</div>')

    answer = "I've created the party detail page following project conventions."

    report = verify.verify_claims(answer)

    assert "FORBIDDEN pattern" not in report
    assert "MISSING required pattern" not in report


def test_require_rule_not_applied_to_non_component_staged_file(tmp_path, monkeypatch):
    """A REQUIRE rule about SCSS module usage must not fire against a staged
    backend .py file just because it happens to be staged at the same time --
    that file was never going to reference styles.x and flagging it would be a
    false positive that teaches agents to ignore this checker."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(tmp_path, "backend/parties_api.py", "def get_party(id: str):\n    return db.query(Party).get(id)\n")

    answer = "I've added the new get_party endpoint."

    report = verify.verify_claims(answer)

    assert "MISSING required pattern" not in report


def test_dotted_identifier_in_staged_file_is_checked_with_no_fenced_code_in_answer(
    tmp_path, monkeypatch
):
    """Confirmed live 2026-08-05: a write_file() task referenced styles.card,
    styles.fieldRow, and styles.value in a brand-new page.tsx, none of which exist
    in the .module.scss it imported -- all three would resolve to `undefined` at
    runtime. The answer never pasted the code back into its own text, so
    _code_idents (same fenced-code-only limitation as _lint_code) never saw these
    tokens and the SYMBOLS check reported nothing to verify.
    """
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])  # nothing found anywhere
    _stage(tmp_path, "page.tsx", "import styles from './x.module.scss';\nconst x = styles.card;")

    answer = "I've created the party detail page."

    report = verify.verify_claims(answer)

    assert "styles.card" in report
    assert "NOT FOUND" in report


def test_staged_files_prunes_excluded_dirs_instead_of_walking_them(tmp_path, monkeypatch):
    """Confirmed live 2026-08-05: three verify_claims calls on EkamApp took
    183s/205s/233s -- an order of magnitude past the typical sub-30s -- with no
    staged-file pollution to blame. Root cause: the old rglob() had to visit
    every file name in every directory (including node_modules' 27,405 files
    and .venv's 14,790) before it could tell none of them matched, because
    Path.rglob() offers no way to prune a directory before descending into it.
    This test proves the fix actually prunes -- not just filters after the
    fact -- by making a real file inside node_modules unreadable-if-touched:
    if os.walk ever descended into it, stat() would still succeed (this is a
    normal file), so what actually matters is the count of files visited stays
    small regardless of how many are stuffed under the excluded dir."""
    from tools import verify as verify_module
    monkeypatch.setattr(verify_module, "PROJECT_ROOT", tmp_path)

    real_file = _stage(tmp_path, "src/real_feature", "content")
    noise_dir = tmp_path / "node_modules" / "some-package"
    noise_dir.mkdir(parents=True)
    for i in range(50):
        (noise_dir / f"noise{i}.hive_proposed").write_text("noise", encoding="utf-8")

    walked_dirs = []
    real_walk = os.walk

    def spying_walk(top, *a, **k):
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            walked_dirs.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", spying_walk)

    found = verify_module._staged_files()

    assert real_file in found
    assert not any("node_modules" in d for d in walked_dirs)
    assert not any("noise" in str(p) for p in found)


def test_forbidden_rule_still_applies_to_non_component_staged_file(tmp_path, monkeypatch):
    """FORBID rules have no false-positive risk for unrelated file types (a
    forbidden JSX pattern simply won't appear in a real .py file), so unlike
    REQUIRE they stay generic across every staged file."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [r"TODO::no leftover TODO markers"])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    _stage(tmp_path, "backend/parties_api.py", "def get_party(id: str):\n    # TODO: add auth check\n    return db.query(Party).get(id)\n")

    answer = "I've added the new delete_party endpoint."

    report = verify.verify_claims(answer)

    assert "FORBIDDEN pattern" in report


# ── Fenced-code-block REQUIRE scoping (same false positive, the other code path) ──
# _lint_code() has TWO ways to see code: staged *.hive_proposed files (tests above)
# and fenced code blocks pasted into the answer's own prose. The staged-file path
# was already scoped to component extensions (_COMPONENT_EXTS) above; the fenced-
# block path never got the same treatment. Confirmed live 2026-08-09: a pure-
# backend Python answer with zero frontend code was flagged "MISSING required
# pattern styles\." because every fenced block, regardless of language, was joined
# into one blob and checked against REQUIRE rules unconditionally.

def test_require_rule_not_applied_to_python_fenced_block(monkeypatch):
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_staged_files", lambda: [])
    answer = (
        "Here's the caching implementation:\n\n"
        "```python\n"
        "def get_cached(key: str):\n"
        "    return cache.get(key)\n"
        "```\n"
    )

    report = verify.verify_claims(answer)

    assert "MISSING required pattern" not in report


def test_require_rule_still_applies_to_tsx_fenced_block(monkeypatch):
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_staged_files", lambda: [])
    answer = (
        "Here's the new component:\n\n"
        "```tsx\n"
        "export function Badge() { return <div className=\"badge\">hi</div>; }\n"
        "```\n"
    )

    report = verify.verify_claims(answer)

    assert "MISSING required pattern" in report


def test_require_rule_not_applied_to_untagged_fenced_block(monkeypatch):
    """No language tag at all -- degrades to non-component treatment, same as an
    unrecognized extension would for a staged file."""
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_staged_files", lambda: [])
    answer = "Config:\n\n```\nCACHE_TTL=300\n```\n"

    report = verify.verify_claims(answer)

    assert "MISSING required pattern" not in report


def test_forbid_rule_still_applies_to_python_fenced_block(monkeypatch):
    """FORBID has no false-positive risk across languages -- confirm the fix didn't
    also narrow FORBID scope down to component blocks, only REQUIRE."""
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [r"TODO::no leftover TODO markers"])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_staged_files", lambda: [])
    answer = "```python\n# TODO: add auth check\ndef get_party(id): ...\n```\n"

    report = verify.verify_claims(answer)

    assert "FORBIDDEN pattern" in report


def test_mixed_python_and_tsx_blocks_only_flags_the_tsx_one(monkeypatch):
    """A real mixed-language answer (e.g. a backend endpoint + a frontend hook to
    call it) must not let the compliant Python block's absence of `styles.` mask
    OR get blamed for a genuinely missing pattern in the tsx block -- and must not
    duplicate the FORBID check's findings by running it twice on overlapping text."""
    monkeypatch.setattr(verify.config, "CODE_LINT_FORBID", [r"TODO::no leftover TODO markers"])
    monkeypatch.setattr(verify.config, "CODE_LINT_REQUIRE", [r"styles\.::components must reference SCSS module classes"])
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_staged_files", lambda: [])
    answer = (
        "```python\n"
        "# TODO: add auth check\n"
        "def get_party(id): ...\n"
        "```\n"
        "```tsx\n"
        "export function PartyView() { return <div className=\"card\">hi</div>; }\n"
        "```\n"
    )

    report = verify.verify_claims(answer)

    assert "MISSING required pattern" in report
    assert report.count("FORBIDDEN pattern") == 1  # not duplicated by the second lint pass
