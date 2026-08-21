"""Regression tests: a citation with NO quoted content is still checkable.

verify.py's own docstring long recorded this hole -- "MISATTRIBUTED SYMBOLS -- NOT caught
for claims with no quoted content" -- and most citations quote nothing, so in practice a
cited line number was only ever bounds-checked. In any file of a few hundred lines,
almost every invented number is in bounds.

Live-caught 2026-08-20 on two consecutive runs of the same probe:

    The `item_categories` table has a `sku_prefix` column. It is defined at line 102
    in `API/inventory-service/models.py`      (first run)
    ... is defined at line 123 in ...          (second run, after a deploy)

Real line: 129. Both 102 and 123 exist in that 700-line file, and neither answer quoted
anything, so verify_claims reported CLEAN both times -- on a line number the run could
only have invented, having answered from db_schema without ever reading the file.

The fix anchors on the identifier the citation is ABOUT (`sku_prefix`), which real
answers name just before locating it, and checks the cited line against where that symbol
actually appears.
"""
from tools import verify


def _rg_noop(tok, **k):
    return []


def _models_py(tmp_path):
    """A file whose target symbol sits well past several plausible wrong lines."""
    lines = [f"    col_{i} = Column(String(50), nullable=True)" for i in range(1, 40)]
    lines[28] = "    sku_prefix = Column(String(8), nullable=True)"   # line 29
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")


def test_wrong_line_with_a_symbol_anchor_is_flagged(tmp_path, monkeypatch):
    """The live shape: in-bounds but wrong, quoting nothing."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `sku_prefix` column is defined at line 12 in `models.py`."
    )

    assert "MISMATCH" in report
    assert "actually appears at line(s) 29" in report


def test_correct_line_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `sku_prefix` column is defined at line 29 in `models.py`."
    )

    assert "MISMATCH" not in report
    assert "verified within" in report


def test_within_tolerance_still_passes(tmp_path, monkeypatch):
    """Citing the class line while the attribute is a few lines below is normal and
    correct -- _LINE_TOLERANCE exists precisely for that."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `sku_prefix` column is defined at line 26 in `models.py`."   # 3 off
    )

    assert "MISMATCH" not in report


def test_absent_symbol_never_manufactures_a_mismatch(tmp_path, monkeypatch):
    """If the anchor is not in the file at all, that is the SYMBOLS section's business.
    Treating it as citation evidence would invent a MISMATCH every time the backward
    anchor guessed wrong -- the failure mode that makes a checker cry wolf."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "The `nonexistent_thing` column is defined at line 12 in `models.py`."
    )

    assert "MISMATCH" not in report


def test_a_path_is_never_used_as_the_anchor(tmp_path, monkeypatch):
    """Backticked paths are the citation's FILE, not its subject."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    assert verify._find_anchor_symbol("see `models.py` at line 12", 26) is None


def test_anchor_prefers_the_nearest_identifier(tmp_path):
    answer = "The `hsn_prefix` column, and also the `sku_prefix` column, at line 12"
    assert verify._find_anchor_symbol(answer, len(answer) - 8) == "sku_prefix"


def test_a_correct_citation_that_also_quotes_content_is_not_flagged(tmp_path, monkeypatch):
    """Whichever check applies -- quote or symbol anchor -- a correct citation carrying
    BOTH must come out clean. Asserting the outcome rather than which mechanism fired:
    the two are alternatives, and pinning the mechanism would make this test fail on a
    future change that is merely a different (still correct) route to the same verdict."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _models_py(tmp_path)

    report = verify.verify_claims(
        "`sku_prefix` at line 29 in `models.py` -- `sku_prefix = Column(String(8), nullable=True)`"
    )

    assert "MISMATCH" not in report
    assert "verified within" in report or "content verified" in report
