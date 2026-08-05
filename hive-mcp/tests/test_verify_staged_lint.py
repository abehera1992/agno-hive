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
