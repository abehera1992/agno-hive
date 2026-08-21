"""Regression tests: a sentence naming BOTH the thing located and its container.

Live false positive, 2026-08-21, on three consecutive runs of a fully CORRECT
answer -- the same probe that had just been fixed to stop fabricating:

    The `sku_prefix` column on the `ItemCategory` model is defined at line 129 in
    `API/inventory-service/models.py`.

Line 129 really is `sku_prefix = Column(String(8), nullable=True)`; the report
even printed that matching line, then flagged the citation anyway:

    LINE 129   API/inventory-service/models.py
              | sku_prefix = Column(String(8), nullable=True)
    MISMATCH   models.py:129 <-- `ItemCategory` is not within 5 lines of 129;
                                 it actually appears at line(s) 116, 208

Cause: the anchor scan took the NEAREST backticked identifier. The citation's
subject is sku_prefix; ItemCategory is a qualifier that happens to sit closer to
the line number. Word order does not reliably separate subject from qualifier,
and a deterministic grep-based checker should not try to infer it from grammar.

Fix: collect every candidate and ask the FILE. If any of them is within tolerance
of the cited line, the citation is anchored. Only when none is does it fail.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def reset_repeat_guard():
    """verify_claims short-circuits an answer whose text it just checked, via
    module-level state. Several tests here deliberately submit the SAME sentence
    against different fixtures, so without this the second one gets the repeat
    notice instead of a real report — a test-isolation problem, not a bug."""
    verify._last_checked_answer = None
    verify._repeat_count = 0
    yield
    verify._last_checked_answer = None
    verify._repeat_count = 0


def _rg_noop(tok, **k):
    return []


def _models_py(tmp_path):
    """sku_prefix at 129, ItemCategory at 116 and 208 — the real file's shape."""
    lines = [f"# filler {i}" for i in range(1, 221)]
    lines[115] = "class ItemCategory(Base):"                      # line 116
    lines[128] = "    sku_prefix = Column(String(8), nullable=True)"   # line 129
    lines[207] = "    category = relationship('ItemCategory')"    # line 208
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")


def test_the_live_correct_answer_is_no_longer_flagged(tmp_path, monkeypatch):
    """The whole incident in one assertion."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `sku_prefix` column on the `ItemCategory` model is defined at line 129 "
        "in `models.py`."
    )

    assert "MISMATCH" not in report, report


def test_the_matching_candidate_is_the_one_reported(tmp_path, monkeypatch):
    """It should name sku_prefix, not the qualifier that happened to be nearer."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `sku_prefix` column on the `ItemCategory` model is defined at line 129 "
        "in `models.py`."
    )

    assert "`sku_prefix` verified within" in report


def test_candidates_are_collected_nearest_first():
    """Called with the citation match's own start, the way the real code calls it —
    passing a position AFTER the citation makes _citation_bounds clamp the window to
    nothing, since the citation then counts as the preceding one."""
    answer = "The `sku_prefix` column on the `ItemCategory` model is defined at line 129"
    pos = next(verify._LABELED_LINE_RE.finditer(answer)).start()
    found = verify._find_anchor_symbols(answer, pos)

    assert found[0] == "ItemCategory", "nearest must still come first"
    assert "sku_prefix" in found, "the subject must not be dropped"


def test_the_singular_helper_still_returns_the_nearest(tmp_path):
    """Kept for its existing callers and tests — the plural is additive."""
    answer = "The `hsn_prefix` column, and also the `sku_prefix` column, at line 12"
    assert verify._find_anchor_symbol(answer, len(answer) - 8) == "sku_prefix"


# ── True positives must survive ───────────────────────────────────────────────

def test_a_wrong_line_still_fails_when_NO_candidate_is_near(tmp_path, monkeypatch):
    """The 2026-08-20 check's whole reason for existing. Both candidates are real
    and both are far from line 12, so this is still fabrication."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `sku_prefix` column on the `ItemCategory` model is defined at line 12 "
        "in `models.py`."
    )

    assert "MISMATCH" in report


def test_a_single_wrong_anchor_still_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `sku_prefix` column is defined at line 12 in `models.py`."
    )

    assert "MISMATCH" in report
    assert "actually appears at line(s) 129" in report


def test_an_absent_candidate_never_manufactures_a_mismatch(tmp_path, monkeypatch):
    """A candidate missing from the file entirely is the SYMBOLS section's business.
    Counting it here would cry wolf every time the backward scan picked up an
    unrelated identifier."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `nonexistent_thing` value is at line 129 in `models.py`."
    )

    assert "MISMATCH" not in report


def test_a_near_candidate_rescues_an_unrelated_nearer_one(tmp_path, monkeypatch):
    """The general shape of the fix: the nearest candidate is real but elsewhere,
    a later one is exactly on the cited line — that is a verified citation."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "`sku_prefix` is declared on the `ItemCategory` class at line 129 in `models.py`."
    )

    assert "MISMATCH" not in report
