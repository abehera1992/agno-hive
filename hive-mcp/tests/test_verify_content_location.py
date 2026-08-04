"""Regression tests: verify_claims must check WHERE a quoted citation actually is,
not just that the cited line number is within the file's bounds.

Confirmed live 2026-08-04 against the ekam project, AFTER the labeled-citation fix
(test_verify_labeled_line_citations.py) was already deployed: an answer wrote

    - **Source**: `models.py`, line 450-497
      > the docstring "L1 -- Legal entity / PAN-based." (triple-quoted in source)

The quoted docstring is real -- it exists verbatim in the file -- but at line 236,
not 450. The old citation check only confirmed the file had >= 450 lines (it has
774), so this passed cleanly with "VERDICT: every checked claim exists". Existence
of the line number is not existence of the claimed content AT that line.

Fix: when a citation is immediately followed by a backticked quote, read the real
lines around the cited line number and confirm the quote is actually there.
"""
from tools import verify


def test_quoted_content_far_from_cited_line_is_flagged_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    lines = ["x = 1"] * 235 + ['    """L1 -- Legal entity / PAN-based."""'] + ["x = 1"] * 600
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")

    # Real docstring text (line 236), but cited at line 450 -- the exact live failure.
    answer = '**File:** `models.py`, **Line:** 450  \n> `"""L1 -- Legal entity / PAN-based."""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" in report
    assert "models.py:450" in report
    assert "VERDICT: 1 claim" in report


def test_quoted_content_at_cited_line_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    lines = ["x = 1"] * 235 + ['    """L1 -- Legal entity / PAN-based."""'] + ["x = 1"] * 5
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")

    answer = '**File:** `models.py`, **Line:** 236  \n> `"""L1 -- Legal entity / PAN-based."""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
    assert "content verified" in report
    assert "VERDICT: every checked claim exists" in report


def test_quoted_content_within_tolerance_of_cited_line_passes(tmp_path, monkeypatch):
    # Citing the class line when the docstring is one line below is a normal,
    # legitimate citation style -- must not be flagged just because it isn't exact.
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    lines = ["x = 1"] * 234 + ["class Party(Base):", '    """L1 docstring."""'] + ["x = 1"] * 5
    (tmp_path / "models.py").write_text("\n".join(lines), encoding="utf-8")

    # Cites the class line (235); the quoted docstring is actually on the next line (236).
    answer = '**File:** `models.py`, **Line:** 235  \n> `"""L1 docstring."""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
    assert "content verified" in report


def test_citation_without_a_nearby_quote_is_unaffected(tmp_path, monkeypatch):
    # No quoted content following the citation -- behavior must be identical to
    # before this fix: existence-only, no MISMATCH machinery engaged.
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    (tmp_path / "models.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")

    answer = "**File:** `models.py`, **Line:** 5 defines the config."

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report
    assert "content verified" not in report
    assert "LINE 5" in report


def test_next_citations_path_is_not_mistaken_for_this_ones_content(tmp_path, monkeypatch):
    # A bare path in the quote-search window belongs to the NEXT citation, not this
    # one's content -- must not be swallowed as a false "quote" to check.
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    (tmp_path / "a.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")
    (tmp_path / "b.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")

    answer = "See `a.py`, line 3, then `b.py`, line 7 for the continuation."

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report


def test_quoted_content_absent_from_file_entirely_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: ["models.py:1:x"])
    (tmp_path / "models.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")

    answer = '**File:** `models.py`, **Line:** 5  \n> `"""this docstring was never written"""`'

    report = verify.verify_claims(answer)

    assert "MISMATCH" in report
