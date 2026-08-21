"""Regression tests: a correct answer with TWO citations in adjacent sentences.

Live false positive, 2026-08-21 (T1-T13 re-run). This answer was correct in every
particular and both of its citations were reported MISMATCH:

    The `delete_party` function is defined in `API/inventory-service/router/
    parties_api.py` at line 194. It performs a soft delete by setting
    `party.is_active = False` on line 205.

Line 194 really is `async def delete_party(`. Line 205 really is
`party.is_active = False`. verify_claims' own report confirmed both while
flagging them. Two independent causes, both from fixed-size windows that cannot
express what actually bounds a citation's subject matter -- the next citation:

  * 194's forward quote-scan ran 200 chars, swallowed 205's quote, and checked
    `party.is_active = False` against line 194.
  * 205's backward anchor-scan skipped that same span (not a bare identifier) and
    kept walking back to `delete_party`, anchoring 205 to the function.

A checker that flags correct answers gets ignored, and takes its true positives
with it -- so these tests pin the false-positive side as hard as the real ones.
"""
from tools import verify


# The live answer, verbatim apart from the path (kept short for the fixture).
_T3 = (
    "The `delete_party` function is defined in `router/parties_api.py` at line 194. "
    "It performs a soft delete by setting `party.is_active = False` on line 205."
)


def _parties_api(tmp_path):
    """A file whose real content matches the T3 answer's claims exactly."""
    d = tmp_path / "router"
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# filler {i}" for i in range(1, 221)]
    lines[193] = "async def delete_party("          # line 194
    lines[204] = "    party.is_active = False"      # line 205
    (d / "parties_api.py").write_text("\n".join(lines), encoding="utf-8")


def _rg_noop(tok, **k):
    return []


def test_the_live_two_citation_answer_is_not_flagged(tmp_path, monkeypatch):
    """The whole incident in one assertion."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _parties_api(tmp_path)

    report = verify.verify_claims(_T3)

    assert "MISMATCH" not in report, report


def test_a_quote_preceding_its_own_line_number_is_not_stolen_by_the_prior_citation(tmp_path):
    """Cause 1, isolated. The span sits ~38 chars after citation 194 but only ~4
    before citation 205, so it belongs to 205 -- the clamp alone cannot decide this,
    because the span is before the next line number rather than after it."""
    pos = _T3.index("at line 194.") + len("at line 194")
    assert verify._find_nearby_quote(_T3, pos) is None


def test_the_anchor_does_not_walk_back_past_the_previous_citation(tmp_path):
    """Cause 2, isolated. `party.is_active = False` is not a bare identifier, so the
    scan skips it; without the clamp it keeps going and grabs `delete_party`."""
    pos = _T3.index("on line 205")
    assert verify._find_anchor_symbol(_T3, pos) != "delete_party"


# ── The true positives must survive ───────────────────────────────────────────

def test_a_genuinely_wrong_quoted_citation_is_still_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _parties_api(tmp_path)

    report = verify.verify_claims(
        "See `router/parties_api.py` line 12 -- `party.is_active = False`."
    )

    assert "MISMATCH" in report


def test_a_genuinely_wrong_symbol_anchored_citation_is_still_caught(tmp_path, monkeypatch):
    """The 2026-08-20 symbol-anchor check, which these clamps must not disarm."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _parties_api(tmp_path)

    report = verify.verify_claims(
        "The `delete_party` function is defined at line 12 in `router/parties_api.py`."
    )

    assert "MISMATCH" in report
    assert "actually appears at line(s) 194" in report


def test_a_single_citation_answer_still_pairs_its_quote(tmp_path, monkeypatch):
    """With no neighbouring citation there is nothing to clamp against, so the
    original behaviour must be untouched."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _parties_api(tmp_path)

    report = verify.verify_claims(
        "See `router/parties_api.py` line 205 -- `party.is_active = False`."
    )

    assert "MISMATCH" not in report
    assert "content verified" in report or "verified within" in report


def test_citation_bounds_are_the_neighbouring_citations():
    """Called the way the real code calls it: with the citation match's own start,
    so the citation never clamps itself."""
    matches = list(verify._LABELED_LINE_RE.finditer(_T3))
    assert len(matches) == 2, [m.group(0) for m in matches]
    first, second = matches

    lo, hi = verify._citation_bounds(_T3, second.start())
    assert lo == first.end(), "should stop at the previous citation"
    assert hi == len(_T3), "nothing follows the last citation"

    lo2, hi2 = verify._citation_bounds(_T3, first.start())
    assert lo2 == 0
    assert hi2 == second.start(), "should stop at the next citation"
