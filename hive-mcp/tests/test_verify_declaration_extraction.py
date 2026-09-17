"""W1 — declaration-shaped backtick claim extraction.

Regression context: `verify_claims`'s claim extraction tokenized a backticked span
by splitting at the first "(" — `class Voucher(BaseModel)` became the two-word
string "class Voucher", which matches neither `_IDENT_RE` nor `_DOTTED_RE` (both
reject any space), so the whole span was silently dropped and never became a
checkable claim at all. Confirmed live, Phase V, run b52f2825af38 (2026-09-17): an
answer's "Backend Database Tables" section named 8 backticked `class X(BaseModel)`
declarations, none of which exist, and every one slipped past verify_claims this
exact way.

W1 fixes ONLY the extraction step: a declaration-shaped span (`class X(Y)`,
`class X(Y):`, `class X`, `def f(...)`, `async def f(...)`) now yields the declared
symbol name as a checkable claim, flowing through the SAME downstream pipeline
(negation, proposed-new-code framing, noise/MCP-tool exclusion, dedup) as any other
backticked identifier. These tests exercise extraction only — no mocking of `_rg`/
`_rg_batch` is needed for the NOT FOUND cases, since ripgrep is not installed in
this sandbox and a genuinely fabricated symbol is correctly reported absent by both
the real (empty) batch pass and any per-token fallback either way.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._checked_answer_counts = {}


def test_class_with_parens_and_base_is_extracted_and_reported():
    """`class Voucher(BaseModel)` -- the exact Phase V shape -- must become a
    checkable SYMBOLS claim for the bare name "Voucher", not be silently dropped."""
    answer = "- `class Voucher(BaseModel)`"

    report = verify.verify_claims(answer)

    assert "no checkable claims found" not in report
    assert "Voucher" in report
    # Genuinely absent in this fixture-less run (no real project files match) --
    # proves the claim was actually CHECKED, not merely mentioned in the report text.
    assert "NOT FOUND" in report


def test_class_with_parens_and_trailing_colon_is_extracted():
    answer = "- `class Voucher(Base):`"

    report = verify.verify_claims(answer)

    assert "no checkable claims found" not in report
    assert "Voucher" in report


def test_bare_class_declaration_is_extracted():
    answer = "- `class Voucher`"

    report = verify.verify_claims(answer)

    assert "no checkable claims found" not in report
    assert "Voucher" in report


def test_def_declaration_is_extracted():
    answer = "The function `def get_voucher(...)` handles retrieval."

    report = verify.verify_claims(answer)

    assert "no checkable claims found" not in report
    assert "get_voucher" in report
    assert "NOT FOUND" in report


def test_async_def_declaration_is_extracted():
    answer = "The endpoint calls `async def get_voucher(...)` internally."

    report = verify.verify_claims(answer)

    assert "no checkable claims found" not in report
    assert "get_voucher" in report


def test_existing_bare_identifier_form_still_works_unchanged():
    """A plain backticked name with no declaration keyword must be tokenized
    exactly as before -- W1 must not touch this path."""
    answer = "The class `Voucher` represents a single voucher record."

    report = verify.verify_claims(answer)

    assert "Voucher" in report
    assert "NOT FOUND" in report


def test_existing_dotted_claim_form_still_works_unchanged():
    answer = "The table `inventory.vouchers` stores every voucher."

    report = verify.verify_claims(answer)

    assert "inventory.vouchers" in report


def test_ordinary_prose_is_not_mistaken_for_a_declaration():
    """`not a declaration` has no class/def keyword at all and must not be
    treated as one -- it falls through to the ordinary bare-identifier path,
    which itself rejects it (three words, contains spaces)."""
    answer = "This is `not a declaration` of anything in particular."

    report = verify.verify_claims(answer)

    # Neither "not", "a", nor "declaration" alone is ever extracted as a claim --
    # the whole three-word span fails _IDENT_RE exactly as it did before W1.
    assert "no checkable claims found" in report


def test_word_class_alone_is_still_noise_not_a_declaration():
    """A bare `class` mention (the Python keyword itself, no name after it) must
    not match the declaration regex (which requires a name) and must still be
    excluded as noise, exactly as before W1."""
    answer = "Use the `class` keyword to define a model."

    report = verify.verify_claims(answer)

    assert "no checkable claims found" in report


def test_lookalike_word_starting_with_class_is_not_a_false_positive():
    """`classroom` textually starts with "class" but is not followed by
    whitespace + a name in the declaration shape -- must not match _DECL_RE."""
    answer = "The `classroom` module is unrelated."

    report = verify.verify_claims(answer)

    assert "classroom" in report
    assert "WRONG BASE" not in report


def test_multiple_declarations_in_one_answer_are_all_extracted():
    answer = (
        "- `class Voucher(BaseModel)`\n"
        "- `class VoucherSeries(BaseModel)`\n"
        "- `def get_voucher(...)`\n"
    )

    report = verify.verify_claims(answer)

    assert "Voucher" in report
    assert "VoucherSeries" in report
    assert "get_voucher" in report
    not_found_lines = [ln for ln in report.splitlines() if ln.strip().startswith("NOT FOUND")]
    assert len(not_found_lines) == 3


def test_declaration_inside_a_proposed_new_code_framing_is_not_flagged():
    """A declaration the answer itself frames as NEW code to add must land in
    PROPOSED, not be checked as an existence claim -- the same discipline every
    other identifier shape already gets (_is_proposed_new_claim)."""
    answer = "We should add a new model:\n`class Voucher(BaseModel)` to represent a voucher."

    report = verify.verify_claims(answer)

    assert "PROPOSED" in report
    proposed_section = report.split("PROPOSED", 1)[1]
    assert "Voucher" in proposed_section
    assert "NOT FOUND" not in report


# ── W1 validation: replay the exact 8 Phase V fabricated declarations ──────────

_PHASE_V_DECLARATIONS = [
    "class Voucher(BaseModel)",
    "class VoucherSeries(BaseModel)",
    "class VoucherSeriesConfig(BaseModel)",
    "class VoucherSeriesConfigHistory(BaseModel)",
    "class VoucherSeriesConfigHistoryEntry(BaseModel)",
    "class VoucherSeriesConfigHistoryEntryDiff(BaseModel)",
    "class VoucherSeriesConfigHistoryEntryDiffField(BaseModel)",
    "class VoucherSeriesConfigHistoryEntryDiffFieldChange(BaseModel)",
]
_EXPECTED_NAMES = [
    "Voucher", "VoucherSeries", "VoucherSeriesConfig", "VoucherSeriesConfigHistory",
    "VoucherSeriesConfigHistoryEntry", "VoucherSeriesConfigHistoryEntryDiff",
    "VoucherSeriesConfigHistoryEntryDiffField",
    "VoucherSeriesConfigHistoryEntryDiffFieldChange",
]


def test_before_after_extraction_table_all_eight_phase_v_declarations():
    """W1's own required validation: every one of the 8 real fabricated spans from
    the Phase V run must now extract to its declared symbol name. BEFORE this fix
    (see the module docstring), _DECL_RE did not exist and every one of these spans
    tokenized to a two-word string that matched neither _IDENT_RE nor _DOTTED_RE --
    zero of the 8 ever reached `idents`. AFTER: all 8 extract cleanly."""
    import re
    from tools.verify import _DECL_RE

    before_after = []
    for span, expected in zip(_PHASE_V_DECLARATIONS, _EXPECTED_NAMES):
        old_tok = span.split("(", 1)[0].strip().rstrip("()").strip()
        old_extracted = bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]{2,}$", old_tok))
        decl = _DECL_RE.match(span)
        new_tok = decl.group("name") if decl else None
        before_after.append((span, old_extracted, new_tok))
        assert old_extracted is False, f"old tokenizer unexpectedly extracted {span!r}"
        assert new_tok == expected, f"W1 extraction mismatch for {span!r}: got {new_tok!r}"

    # Full report against the actual answer text, byte-identical section header to
    # the real Phase V run.
    answer = (
        "Backend Database Tables (8)\n\n"
        + "\n".join(f"- `{d}`" for d in _PHASE_V_DECLARATIONS)
    )
    report = verify.verify_claims(answer)
    for name in _EXPECTED_NAMES:
        assert name in report, f"{name} never appears in the verify_claims report"
    assert "no checkable claims found" not in report
