"""T33 -- malformed-fence verification coverage and false-negative prevention.

T32's own report flagged a residual risk: `_blocks_not_in_their_file`'s two
boundary-ambiguity guards (`_MD_HEADING_IN_BLOCK_RE` for T30's incident,
`_EMBEDDED_FENCE_OPEN_RE` for T32's own) both ended in a bare `continue` --
silently discarding the whole span with zero finding. Reproduced directly:
an answer whose ENTIRE fenced "evidence" is one such ambiguous span (a
fabricated function definition sitting as literal, un-backticked code inside
the merged span -- never extracted as a checkable SYMBOLS claim by anything
else in this module, since verify_claims only ever examines backticked spans
and fenced blocks, never arbitrary code text) produced a clean
"VERDICT: every checked claim exists in the project" -- identical wording to
a genuinely, fully-examined answer. A skipped check must never render the
same as a passed check.

Fix: `_blocks_not_in_their_file` now returns `(kind, resolved, sample, n)`
4-tuples. `kind` is "ABSENT" (examined, genuinely not present -- the
pre-existing fabrication-detection behavior, unchanged) or "AMBIGUOUS" (T33:
the fence's own boundary could not be reliably determined, so its content
was NEVER compared against the file at all). AMBIGUOUS is rendered visibly
("AMBIGUOUS   <file> <-- ...") and counted toward `problems`, exactly like
an AMBIGUOUS file:line citation already is elsewhere in this module --
"could not be checked" is surfaced, never silently treated as "checked and
clean".

This does NOT attempt to extract and check arbitrary, non-backticked code
text for fabricated symbols (that would be a much larger, separate change to
what counts as a "checkable claim" at all, out of this phase's scope) --
it ensures the verifier's OWN confidence in its overall verdict accurately
reflects what it actually examined.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_guard():
    verify._checked_answer_counts = {}
    yield
    verify._checked_answer_counts = {}


def _mk_models_py(tmp_path, monkeypatch, content=None):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    real_file = tmp_path / "models.py"
    real_file.write_text(
        content or "class Voucher(Base):\n    status = Column(String)\n",
        encoding="utf-8",
    )
    return real_file


def _rg_blank(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})


# ── Case A -- fabrication inside an ambiguous merged span ──────────────────

def test_case_a_ambiguous_span_is_disclosed_not_silently_passed(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    _rg_blank(monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\ncla\n"
        "def completely_fabricated_function_t33_xyz():\n"
        "    pass\n"
        "I will continue describing the voucher model shortly.\n\n"
        "```python\nclass Voucher(Base):\n    status = Column(String)\n```\n"
    )
    # Causal trace, asserted directly:
    blocks = list(verify._iter_fenced_blocks(answer))
    assert len(blocks) == 1  # the scanner merges into one span, as designed
    assert verify._EMBEDDED_FENCE_OPEN_RE.search(blocks[0][1])  # fires
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "AMBIGUOUS"

    report = verify.verify_claims(answer)
    assert "AMBIGUOUS" in report
    assert "every checked claim exists in the project" not in report
    assert "VERDICT: 1 claim(s) could NOT be found" in report


# ── Case B -- fabrication after a genuinely independent later block ────────

def test_case_b_fabrication_in_a_properly_closed_later_block_is_checked_independently(
    tmp_path, monkeypatch,
):
    """A second block that IS properly, unambiguously closed (no embedded
    fence-open, no heading) must still be checked on its own merits, even
    when it follows a SEPARATE, genuinely ambiguous first block."""
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\nclass Voucher(Base):\n"
        "    status = Column(String)\n```\n"
        "Also defined in `models.py`:\n```python\n"
        "completely_fabricated_second_block_call_one()\n"
        "completely_fabricated_second_block_call_two()\n```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "ABSENT"


# ── Case C -- ordinary truncation, no fabrication ───────────────────────────

def test_case_c_ordinary_truncation_with_no_fabrication_is_handled_conservatively(
    tmp_path, monkeypatch,
):
    """An unclosed fence running straight to end of input, quoting content
    that IS genuinely present, must not invent a mismatch."""
    _mk_models_py(tmp_path, monkeypatch)
    answer = "Defined in `models.py`:\n```python\nclass Voucher(Base):\n    status = Column(String)\n"
    findings = verify._blocks_not_in_their_file(answer)
    assert findings == []


# ── Case D -- valid complete blocks retain existing correct behavior ───────

def test_case_d_two_valid_complete_blocks_are_both_accepted(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\nclass Voucher(Base):\n"
        "    status = Column(String)\n```\n"
        "Also defined in `models.py`:\n```python\nclass Voucher(Base):\n"
        "    status = Column(String)\n```\n"
    )
    assert verify._blocks_not_in_their_file(answer) == []


# ── Case E -- valid content must not launder adjacent fabricated content ───

def test_case_e_valid_quotation_does_not_launder_an_adjacent_fabricated_one(
    tmp_path, monkeypatch,
):
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\nclass Voucher(Base):\n"
        "    status = Column(String)\n```\n"
        "Also claimed to be in `models.py`:\n```python\n"
        "completely_fabricated_adjacent_call_one()\n"
        "completely_fabricated_adjacent_call_two()\n```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "ABSENT"


# ── Case F -- T30 heading regression, preserved exactly (no reintroduced FP) ─

def test_case_f_t30_heading_regression_is_disclosed_ambiguous_not_falsely_absent(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    frontend = tmp_path / "inventoryApi.ts"
    frontend.write_text("export const useGetVouchersQuery = () => {};\n", encoding="utf-8")
    answer = (
        "### Frontend Hooks (from `inventoryApi.ts`):\n"
        "```typescript\n"
        "use\n\n"
        "I'll continue from where the previous answer left off and complete "
        "the comparison.\n\n"
        "### Frontend Hooks (from `inventoryApi.ts`):\n"
        "```typescript\n"
        "useGetVouchersQuery,\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "AMBIGUOUS"
    assert "BLOCK NOT IN" not in str(findings)


# ── Case G -- genuine symbol/route controls remain active ──────────────────

def test_case_g_genuine_symbol_and_route_checks_unaffected_by_a_malformed_fence(
    tmp_path, monkeypatch,
):
    """A malformed fence elsewhere in the answer must not suppress the
    independent SYMBOLS/ROUTES checks for claims made outside it."""
    _mk_models_py(tmp_path, monkeypatch)
    _rg_blank(monkeypatch)
    answer = (
        "Backend: `@router.get(\"/vouchers/this-route-does-not-exist-at-all\")`.\n"
        "Field: `Voucher.id` is defined at line 1 in `models.py`.\n"
        "Defined in `models.py`:\n```python\ncla\n"
        "def completely_fabricated_function_t33_g():\n    pass\n"
        "More prose continuing the thought.\n\n"
        "```python\nclass Voucher(Base):\n    status = Column(String)\n```\n"
    )
    report = verify.verify_claims(answer)
    assert "ROUTES (" in report
    assert "AMBIGUOUS" in report  # the malformed fence is still disclosed


# ── Preserved invariants: T29, T31 ──────────────────────────────────────────

def test_t29_bare_filename_path_resolution_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    _rg_blank(monkeypatch)
    monkeypatch.setattr(verify.shutil, "which", lambda name: "rg")

    class _FakeRgFiles:
        def __init__(self, paths):
            self.stdout = "\n".join(paths)

    monkeypatch.setattr(
        verify.subprocess, "run",
        lambda *a, **k: _FakeRgFiles(["Client/inventoryApi.ts"]))
    frontend = tmp_path / "Client" / "inventoryApi.ts"
    frontend.parent.mkdir(parents=True)
    frontend.write_text("export const useGetVouchersQuery = () => {};\n", encoding="utf-8")
    answer = "Frontend hooks (from `inventoryApi.ts`): `useGetVouchersQuery`."
    report = verify.verify_claims(answer)
    assert "EXISTS" in report


def test_t31_genuine_short_dotted_symbol_anchoring_unaffected():
    answer = "See `models.py` for the schema, and `Voucher.save` is defined at line 50."
    anchors = verify._find_anchor_symbols(answer, answer.index("line 50"))
    assert anchors == ["Voucher.save"]
