"""T35 -- citation symbol resolution: forward-reference false-negative.

T34 established that `_find_anchor_symbols()` searches only BACKWARD from a
citation's start position. A symbol anchor sitting AFTER the citation (the
common "`file:line` -- `symbol`" shape) was invisible to it regardless of
how obviously it was the citation's intended referent. Reproduced directly:
`models.py:10 -- `Voucher.save`` (the real definition is at line
449) produced ZERO anchors and therefore ZERO finding for a genuinely wrong
citation -- a silent false negative, not a visible warning.

Fix: `_find_anchor_symbols` now accepts an optional `cite_end` (the
citation match's own `.end()`, already available at both call sites) and,
when supplied, ALSO scans a bounded forward window using the IDENTICAL
eligibility check the backward scan already uses (`_is_eligible_anchor_token`,
extracted so both directions can never drift apart). The forward scan is
clamped to the next citation's own boundary (`_citation_bounds`), exactly
like `_find_nearby_quote`'s own forward search already is, so it can never
steal a different citation's anchor.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_guard():
    verify._checked_answer_counts = {}
    yield
    verify._checked_answer_counts = {}


def _mk_models_py(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    real_file = tmp_path / "models.py"
    lines = [f"# filler line {i}" for i in range(1, 500)]
    lines[448] = "    def save(self):"  # real line 449
    real_file.write_text("\n".join(lines), encoding="utf-8")
    return real_file


BT = chr(96)


# ── Case A: symbol before citation, correct line -- unchanged behavior ─────

def test_case_a_symbol_before_citation_correct_line_still_verifies(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = f"{BT}Voucher.save{BT} is defined at models.py:449."
    report = verify.verify_claims(answer)
    assert "verified within 5 lines" in report
    assert "MISMATCH" not in report


# ── Case B: symbol AFTER citation, WRONG line -- the reproduced defect ─────

def test_case_b_symbol_after_citation_wrong_line_now_produces_a_finding(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = f"models.py:10 -- {BT}Voucher.save{BT} runs here."
    report = verify.verify_claims(answer)
    assert "MISMATCH" in report
    assert "Voucher.save" in report
    assert "449" in report  # names the real location


# ── Case C: symbol AFTER citation, correct line -- no false mismatch ───────

def test_case_c_symbol_after_citation_correct_line_verifies_not_mismatches(
    tmp_path, monkeypatch,
):
    _mk_models_py(tmp_path, monkeypatch)
    answer = f"models.py:449 -- {BT}Voucher.save{BT} runs here."
    report = verify.verify_claims(answer)
    assert "verified within 5 lines" in report
    assert "MISMATCH" not in report


# ── Case D: filenames/paths remain safe as forward anchors ─────────────────

def test_case_d_filename_reference_does_not_produce_a_false_verdict(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = f"models.py:10 -- {BT}Button.module.scss{BT} styles it."
    report = verify.verify_claims(answer)
    assert "verified within 5 lines" not in report
    # Never falsely asserted as matching this citation (the pre-existing
    # "hits must be non-empty to report" safety net keeps this silent for
    # the citation itself, independent of this phase's own change).
    assert "MISMATCH   models.py:10" not in report


# ── Case E: generic decorator references remain safe as forward anchors ───

def test_case_e_generic_decorator_reference_does_not_produce_a_false_verdict(
    tmp_path, monkeypatch,
):
    _mk_models_py(tmp_path, monkeypatch)
    answer = f"models.py:10 -- {BT}router.post{BT} decorates it."
    report = verify.verify_claims(answer)
    assert "verified within 5 lines" not in report
    assert "MISMATCH   models.py:10" not in report


# ── Case F: fabricated symbol is not falsely validated ─────────────────────

def test_case_f_fabricated_symbol_after_citation_is_not_falsely_validated(
    tmp_path, monkeypatch,
):
    _mk_models_py(tmp_path, monkeypatch)
    answer = f"models.py:10 -- {BT}Voucher.totally_fabricated_method{BT} runs here."
    report = verify.verify_claims(answer)
    assert "verified within 5 lines" not in report
    assert "could NOT be found" in report


# ── Case G: adjacent citations keep their own, correct symbol association ──

def test_case_g_adjacent_citations_do_not_steal_each_others_anchor(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        f"models.py:449 -- {BT}Voucher.save{BT} runs here. "
        f"models.py:10 -- {BT}Voucher.totally_fabricated_method{BT} runs here."
    )
    report = verify.verify_claims(answer)
    # The first (correct) citation must verify ...
    assert "`Voucher.save` verified within 5 lines" in report
    # ... and the second (fabricated) citation must still be flagged, not
    # silently cleared by proximity to the first, correct one.
    assert "could NOT be found" in report
    assert "Voucher.totally_fabricated_method" in report


# ── Multiple plausible symbols near one citation: never arbitrary selection ─

def test_multiple_plausible_symbols_near_one_citation_all_get_checked(tmp_path, monkeypatch):
    """One BACKWARD candidate and one FORWARD candidate both sit near the
    SAME citation -- the citation must be checked against BOTH, not just
    whichever one is nearer, so a correct citation is accepted if either
    one resolves."""
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        f"{BT}Voucher.totally_fabricated_backward{BT} and then "
        f"models.py:449 -- {BT}Voucher.save{BT} runs here."
    )
    report = verify.verify_claims(answer)
    assert "`Voucher.save` verified within 5 lines" in report
    assert "MISMATCH" not in report


# ── Preserved invariants: T31 short dotted-symbol handling ─────────────────

def test_t31_preceding_short_dotted_symbol_still_works(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    anchors = verify._find_anchor_symbols(
        f"See {BT}models.py{BT} for the schema, and {BT}Voucher.save{BT} "
        f"is defined at line 50.",
        (f"See {BT}models.py{BT} for the schema, and {BT}Voucher.save{BT} "
         f"is defined at line 50.").index("line 50"),
    )
    assert anchors == ["Voucher.save"]


# ── Preserved invariants: T33 AMBIGUOUS handling is a separate mechanism ───

def test_t33_ambiguous_fence_handling_unaffected(tmp_path, monkeypatch):
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
