"""Regression tests: verify_claims must catch labeled-prose line citations.

Confirmed 2026-08-03 via a live groundedness test against the ekam project: an
answer wrote "**File:** `models.py`, **Line:** 389" for a class actually defined at
line 235 -- fabricated, off by ~155 lines. verify_claims' existing _FILE_LINE_RE only
matches the compact "path:123" form, so this labeled-prose shape was never even
recognized as a checkable claim and the fabrication shipped uncaught.

Fix: pair each "line N" mention with the nearest preceding backticked path (within a
bounded window) instead of requiring one exact label phrasing, since models write
this a dozen different ways.
"""
from tools import verify


# The backticked path in these answers ("`models.py`") is ALSO checked as a bare
# symbol claim (pre-existing, unrelated behavior — a backticked token gets checked
# both as a possible identifier AND as a citation-path component). Fake _rg so that
# unrelated check doesn't drown out the CITATIONS-specific assertions these tests
# actually care about.
def _rg_finds_bare_filename(tok, **k):
    if tok == "models.py":
        return ["models.py:1:some content"]
    return []


def test_labeled_prose_citation_with_wrong_line_is_flagged_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_finds_bare_filename)
    f = tmp_path / "models.py"
    f.write_text("class Party(Base):\n    pass\n", encoding="utf-8")  # only 2 lines

    # Exact shape of the real fabricated answer, not the compact "path:123" form.
    answer = "**File:** `models.py`, **Line:** 389"

    report = verify.verify_claims(answer)

    assert "BAD" in report
    assert "models.py:389" in report
    assert "VERDICT: 1 claim" in report


def test_labeled_prose_citation_with_correct_line_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_finds_bare_filename)
    f = tmp_path / "models.py"
    f.write_text("\n".join(["x = 1"] * 5 + ["class Party(Base):"] + ["    pass"]), encoding="utf-8")

    answer = "**File:** `models.py`, **Line:** 6"

    report = verify.verify_claims(answer)

    assert "LINE 6" in report
    assert "class Party" in report
    assert "VERDICT: every checked claim exists" in report


def test_line_citation_pairs_with_nearest_preceding_path_not_an_earlier_one(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")  # 1 line
    (tmp_path / "b.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")  # 3 lines

    # "Line: 3" should pair with the nearer `b.py`, not the farther `a.py`.
    answer = "See `a.py` for context. Later, in `b.py`, **Line:** 3 defines z."

    report = verify.verify_claims(answer)

    assert "b.py:3" not in report  # not reported as BAD/AMBIGUOUS
    assert "LINE 3   " in report or "LINE 3\t" in report or "b.py" in report
    # Confirm it did NOT get paired with a.py (which has no line 3).
    assert "a.py:3" not in report


def test_bare_line_number_with_no_nearby_path_is_ignored(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    # No backticked path anywhere in the answer -- nothing to pair "line 42" with.
    answer = "This behaviour starts around line 42 of the module."

    report = verify.verify_claims(answer)

    assert "no checkable claims found" in report


def test_word_containing_line_as_substring_does_not_false_match(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    (tmp_path / "config.py").write_text("x = 1\n", encoding="utf-8")

    # "pipeline" and "baseline" contain "line" as a substring but must not trigger
    # the \bline label matcher.
    answer = "The `config.py` pipeline establishes a baseline before processing."

    report = verify.verify_claims(answer)

    assert "CITATIONS" not in report
