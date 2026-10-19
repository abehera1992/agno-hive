"""T31 -- citation-anchor filename misclassification.

The hypothesis T31 set out to test was the mirror image of T30's symptom 1:
could `_find_anchor_symbols()` mistake a bare filename (`inventoryApi.ts`) for
a genuine dotted code symbol and anchor a citation to it? Reproduction showed
this specific direction is ALREADY safe -- `_find_anchor_symbols` has its own,
older guard (`_PATHLIKE_RE`, "a path is the citation's file, not its subject")
that already excludes any bare token ending in a short alphanumeric suffix
after a dot, which covers every realistic bare filename.

But that same guard is a BROADER heuristic than "this is a filename" -- it
excludes ANY token matching `<name>.<1-6 alnum chars>`, which also matches a
genuine SHORT dotted symbol claim (`Voucher.save`, `Voucher.id`, `Model.ok`).
Reproduced directly: a sentence naming both a bare filename and a genuine
short dotted symbol lost the symbol too, leaving the citation with ZERO anchor
candidates. Traced to the consequence: the CITATIONS loop's
`anchors = symbol_claims.get((path, num)) or []` then feeds an empty list into
`for a in anchors: ...` (verify_claims' own citation-verdict loop), which
never executes -- a SILENT false negative, not a visible warning. A citation
whose only nearby candidate is a short real symbol was never checked for
MISMATCH at all.

Fix: reuse T30's own `_looks_like_bare_filename`/`_is_generic_decorator_reference`
(the real, finite extension list and the narrow decorator-call correlation
check) in place of `_PATHLIKE_RE` at this ONE call site only. `_PATHLIKE_RE`
itself, and its two OTHER call sites (the quoted-content exclusion in
`_find_nearby_quote`/`_find_preceding_quote`), are unmodified.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_guard():
    verify._checked_answer_counts = {}
    yield
    verify._checked_answer_counts = {}


def _pos_at(answer: str, marker: str) -> int:
    return answer.index(marker)


# ── The reproduced defect: a real short symbol lost alongside a filename ────

def test_genuine_short_dotted_symbol_is_not_lost_alongside_a_bare_filename():
    answer = "See `models.py` for the schema, and `Voucher.save` is defined at line 50."
    anchors = verify._find_anchor_symbols(answer, _pos_at(answer, "line 50"))
    assert "Voucher.save" in anchors
    assert "models.py" not in anchors


def test_genuine_short_dotted_symbol_alone_still_anchors():
    """Regression guard for the exact symptom: `Voucher.id` (a 2-char suffix)
    used to be excluded by _PATHLIKE_RE purely because "id" is short -- it
    must now anchor like any other genuine dotted claim."""
    answer = "The field `Voucher.id` is defined at line 50."
    anchors = verify._find_anchor_symbols(answer, _pos_at(answer, "line 50"))
    assert anchors == ["Voucher.id"]


# ── The originally-hypothesized direction: still safe ───────────────────────

def test_bare_filename_alone_never_becomes_an_anchor():
    answer = "The hooks are defined in `inventoryApi.ts`, at line 50."
    anchors = verify._find_anchor_symbols(answer, _pos_at(answer, "line 50"))
    assert anchors == []


def test_bare_filename_does_not_outrank_a_nearby_genuine_symbol():
    answer = (
        "The frontend file `inventoryApi.ts` exposes `InventoryApi.getVouchers`, "
        "defined at line 50 in `inventoryApi.ts`."
    )
    anchors = verify._find_anchor_symbols(answer, _pos_at(answer, "line 50"))
    assert anchors == ["InventoryApi.getVouchers"]


def test_py_filename_is_still_excluded_from_anchoring():
    answer = "See `vouchers_api.py` for the endpoints, line 50."
    anchors = verify._find_anchor_symbols(answer, _pos_at(answer, "line 50"))
    assert anchors == []


# ── Control: ambiguous/unresolved filename context does not leak either ────

def test_ambiguous_filename_context_still_produces_no_filename_anchor():
    answer = "Both `models.py` and `other_models.py` are relevant, line 50."
    anchors = verify._find_anchor_symbols(answer, _pos_at(answer, "line 50"))
    assert anchors == []


# ── End-to-end: the MISMATCH detection this anchor feeds is restored ───────

def test_end_to_end_mismatch_is_detected_when_short_symbol_anchor_is_wrong(
    tmp_path, monkeypatch,
):
    """Before the fix, this citation had zero anchors (the filename AND the
    short symbol were both excluded) and the MISMATCH below was silently
    never checked. After the fix, `Voucher.save` anchors the citation, the
    file says it's really at a different line, and MISMATCH fires."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    real_file = tmp_path / "models.py"
    lines = ["# filler"] * 60
    lines[9] = "    def save(self):"  # line 10 -- NOT line 50
    real_file.write_text("\n".join(lines), encoding="utf-8")

    answer = (
        "See `models.py` for the schema, and `Voucher.save` is defined at "
        "`models.py`, line 50."
    )
    report = verify.verify_claims(answer)
    assert "MISMATCH" in report or "`Voucher.save` verified" not in report


def test_end_to_end_correct_citation_with_a_short_symbol_anchor_is_accepted(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    real_file = tmp_path / "models.py"
    lines = ["# filler"] * 60
    lines[49] = "    def save(self):"  # line 50 -- matches the citation
    real_file.write_text("\n".join(lines), encoding="utf-8")

    answer = (
        "See `models.py` for the schema, and `Voucher.save` is defined at "
        "`models.py`, line 50."
    )
    report = verify.verify_claims(answer)
    assert "MISMATCH" not in report


# ── Preserved: ordinary valid/fabricated quotation acceptance unaffected ───

def test_valid_fenced_quotation_unaffected_by_the_anchor_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    real_file = tmp_path / "models.py"
    real_file.write_text("class Voucher(Base):\n    status = Column(String)\n", encoding="utf-8")
    answer = "Defined in `models.py`:\n```python\nclass Voucher(Base):\n    status = Column(String)\n```\n"
    assert verify._blocks_not_in_their_file(answer) == []
