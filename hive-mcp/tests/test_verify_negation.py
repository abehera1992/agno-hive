"""Regression tests: verify_claims must not treat a correctly-hedged "this does not
exist" statement, or a chained CSS/SCSS class selector picked up from a staged file,
as a fabricated positive existence claim.

Confirmed live 2026-08-05: a Coder wrote "Is named `statusBadge` (not
`statusBadge.success`, etc., as those do not exist)" -- a correct disclaimer -- and
separately staged CSS containing ".statusBadge.success { ... }". Both produced
"statusBadge.success" as a checked claim, both reported NOT FOUND (accurately, since
neither is real, elsewhere-existing code -- one is a correct negative statement, the
other is brand-new CSS that only exists in the very file being staged), and the
resulting false "fabrication" verdict triggered swarm/team.py's correction retry,
which then staged a SECOND, duplicate copy of already-correct content instead of
fixing anything.
"""
from tools import verify


def test_negated_prose_claim_is_not_checked(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Is named `statusBadge` (not `statusBadge.success`, etc., as those do not exist)."

    report = verify.verify_claims(answer)

    assert "statusBadge.success" not in report
    assert "statusBadge" in report  # the un-negated symbol is still checked normally


def test_trailing_does_not_exist_disclaimer_is_not_checked(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The helper `fooBarBaz` does not exist in this project."

    report = verify.verify_claims(answer)

    assert "fooBarBaz" not in report


def test_normal_backtick_claim_without_negation_is_still_checked(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The function is `doTheThing`."

    report = verify.verify_claims(answer)

    assert "doTheThing" in report
    assert "NOT FOUND" in report


def test_chained_css_selector_in_staged_file_is_not_treated_as_a_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    p = tmp_path / "x.module.scss.hive_proposed"
    p.write_text(
        ".statusBadge {\n  display: inline-flex;\n}\n"
        ".statusBadge.success {\n  background: green;\n}\n",
        encoding="utf-8",
    )

    answer = "I've added the statusBadge class and its success variant."

    report = verify.verify_claims(answer)

    assert "statusBadge.success" not in report


def test_genuine_dotted_identifier_in_staged_file_is_still_checked(tmp_path, monkeypatch):
    """The chained-selector guard must only suppress a match preceded by ANOTHER dot
    -- a real member-access expression like styles.warning (no leading dot before
    'styles') must still be caught."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    p = tmp_path / "x.tsx.hive_proposed"
    p.write_text("const x = styles.warningLabel;\n", encoding="utf-8")

    answer = "I've added a new label using an existing style."

    report = verify.verify_claims(answer)

    assert "styles.warningLabel" in report
    assert "NOT FOUND" in report
