"""Regression tests: a line citation whose path comes AFTER the number must be checked.

`_LABELED_LINE_RE` pairs each "line N" with the nearest backticked path, but the search
was backward-only. "defined at line 102 in `models.py`" puts the path a few characters
AFTER the number, so `nearest` stayed None and the citation was skipped entirely -- not
flagged, not checked, simply never a claim.

Live-caught 2026-08-20 during a post-deploy probe battery. An answer said:

    The `sku_prefix` column ... is defined at line 102 in `API/inventory-service/models.py`

Real line: 129. Line 102 is an unrelated `name = Column(String(100), ...)`. verify_claims
RAN on that answer and reported clean, because this phrasing produced zero checkable
citations. The run had also answered from db_schema rather than reading the file, so the
line number could only have been invented -- db_schema returns no line numbers at all.
"""
from tools import verify


def _rg_noop(tok, **k):
    return []


def test_citation_with_the_path_after_the_number_is_checked(tmp_path, monkeypatch):
    """The exact live phrasing. Before the fix this produced no CITATIONS section."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    (tmp_path / "models.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    report = verify.verify_claims("The column is defined at line 102 in `models.py`.")

    assert "CITATIONS" in report, "citation was skipped entirely"
    assert "BAD" in report, "line 102 does not exist in a 2-line file"


def test_path_before_the_number_still_works(tmp_path, monkeypatch):
    """The pre-existing backward pairing must be unchanged -- it remains preferred."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    (tmp_path / "models.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    report = verify.verify_claims("In `models.py`, the column is defined at line 102.")

    assert "CITATIONS" in report
    assert "BAD" in report


def test_a_preceding_path_is_preferred_over_a_following_one(tmp_path, monkeypatch):
    """Backward stays the primary rule: the forward look is only a fallback, so a
    sentence naming its file first must not be re-pointed at the next sentence's file."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")          # 1 line
    (tmp_path / "b.py").write_text("\n".join(["y = 2"] * 50), encoding="utf-8")

    # "line 40" belongs to a.py (which precedes it), not b.py (which follows).
    report = verify.verify_claims("In `a.py` at line 40. Separately, see `b.py`.")

    assert "a.py:40" in report, "should have paired with the PRECEDING path"


def test_a_distant_following_path_is_not_paired(tmp_path, monkeypatch):
    """The forward window is deliberately tight. A path far later in the answer belongs
    to a different claim, and pairing with it would invent a citation nobody made."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    (tmp_path / "far.py").write_text("x = 1\n", encoding="utf-8")

    filler = "and then a good deal of unrelated prose follows here " * 4   # >80 chars
    report = verify.verify_claims(f"Something happens at line 900. {filler} See `far.py`.")

    assert "far.py:900" not in report
