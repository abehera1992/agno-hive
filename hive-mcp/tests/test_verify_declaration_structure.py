"""W2 — verification of declaration-shaped claims (base-class structure).

W1 (test_verify_declaration_extraction.py) fixed EXTRACTION: a `class X(Y)`-shaped
backtick span now becomes a checkable claim for the bare name X. Existence alone is
not enough to support that claim, though — the answer also asserted a specific base
class Y, and a repo-wide "does X exist anywhere" grep cannot tell a correct
declaration from a real class with the WRONG claimed base. This is the same
structural-vs-existence distinction `_structural_verdict`/`field_of` already apply
to a claimed field's owner (see symbol_index.py's own module docstring, the
`reg_id`/krakend.json incident).

`_declared_bases_mismatch` (verify.py) closes that gap: for a class declaration
claim with an explicit base, it greps repo-wide for the real DECLARATION LINE
(`^\\s*class Tok\\b`), then compares `symbol_index.class_bases` against what was
claimed. It is deliberately repo-wide, not scoped to files the answer named — the
real Phase V fabrication never cited `models.py` by path near its "Database
Tables" section, so a file-scoped check alone would never fire on the one case
this exists to catch.

Mocking note: this sandbox has no `rg` binary, so verify_claims's own batched
SYMBOLS grep pass (`_rg_batch`) always returns an empty hit list for every token
regardless of any `_rg` mock (`_rg_batch` short-circuits before ever calling `_rg`
when the binary is missing) -- confirmed against this repo's OWN pre-existing test
suite, where `test_code_block_and_prose_idents_are_deduplicated` already fails in
this same sandbox for the identical, pre-existing reason (unrelated to W1/W2; see
this phase's own final report). Tests here that need the bare-identifier "the
symbol DOES exist somewhere" path therefore monkeypatch BOTH `_rg` and `_rg_batch`
so results do not depend on whether ripgrep happens to be installed.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._checked_answer_counts = {}


def _make_grep(hits_by_pattern):
    """Deterministic double for both `_rg` and `_rg_batch`, keyed by exact pattern
    string -- same convention this file's neighbours already use for `_rg` alone."""
    def fake_rg(pattern, fixed=True, glob_filter="", whole_word=False):
        return hits_by_pattern.get(pattern, [])

    def fake_rg_batch(patterns, glob_filter="", whole_word=False, per_pattern_cap=8):
        return {p: hits_by_pattern.get(p, []) for p in patterns}

    return fake_rg, fake_rg_batch


def test_fabricated_class_with_no_real_declaration_anywhere_is_not_found():
    """Case 2 -- symbol does not exist. No mocking needed: a genuinely fictional
    name is absent from both the real (empty, no-rg) batch and any grep."""
    answer = "- `class VoucherSeriesConfig(BaseModel)`"

    report = verify.verify_claims(answer)

    assert "NOT FOUND" in report
    assert "VoucherSeriesConfig" in report
    assert "WRONG BASE" not in report  # never reaches the base-class check at all


def test_wrong_inheritance_is_not_reported_as_fully_supported(monkeypatch):
    """Case 1/3 -- `Voucher` is real (repo-wide grep finds it), but the answer
    claims base `BaseModel` while the real declaration's base is `Base`. Must NOT
    read as a clean FOUND merely because the bare name exists."""
    fake_rg, fake_rg_batch = _make_grep({
        "Voucher": ["API/inventory-service/models.py:150:class Voucher(Base):"],
        r"^\s*class\s+Voucher\b": ["API/inventory-service/models.py:150:class Voucher(Base):"],
    })
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr(
        "tools.symbol_index.class_bases",
        lambda rel_path, class_name: ["Base"] if class_name == "Voucher" else None,
    )

    answer = "- `class Voucher(BaseModel)`"
    report = verify.verify_claims(answer)

    assert "WRONG BASE" in report
    assert "Base" in report
    assert "could NOT be found" in report  # still counts toward the fabrication verdict
    assert "FOUND      Voucher" not in report  # never silently certified as supported


def test_correct_declaration_is_not_falsely_rejected(monkeypatch):
    """The verifier must not falsely reject a genuinely correct declaration claim
    just because the structural check now exists."""
    fake_rg, fake_rg_batch = _make_grep({
        "Voucher": ["API/inventory-service/models.py:150:class Voucher(Base):"],
        r"^\s*class\s+Voucher\b": ["API/inventory-service/models.py:150:class Voucher(Base):"],
    })
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr(
        "tools.symbol_index.class_bases",
        lambda rel_path, class_name: ["Base"] if class_name == "Voucher" else None,
    )

    answer = "- `class Voucher(Base)`"
    report = verify.verify_claims(answer)

    assert "WRONG BASE" not in report
    assert "VERDICT: every checked claim exists" in report


def test_multiple_claimed_bases_supported_if_any_one_matches(monkeypatch):
    """A multi-inheritance claim `class Foo(Base, Mixin)` should not be rejected
    just because the check cannot fully re-derive multiple-inheritance ordering --
    matching on ANY claimed base is enough to count as supported, consistent with
    this checker's own stated preference for missed detection over false accusation
    (see verify.py module docstring, MISATTRIBUTED SYMBOLS)."""
    fake_rg, fake_rg_batch = _make_grep({
        "Foo": ["API/inventory-service/models.py:1:class Foo(Base, SomeMixin):"],
        r"^\s*class\s+Foo\b": ["API/inventory-service/models.py:1:class Foo(Base, SomeMixin):"],
    })
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr(
        "tools.symbol_index.class_bases",
        lambda rel_path, class_name: ["Base", "SomeMixin"] if class_name == "Foo" else None,
    )

    answer = "- `class Foo(Base, Mixin)`"
    report = verify.verify_claims(answer)

    assert "WRONG BASE" not in report


def test_undeterminable_declaration_falls_back_to_existence_only(monkeypatch):
    """When no declaration LINE can be located anywhere (class_bases always
    returns None -- e.g. an unindexable language, or the grep for the declaration
    line itself found nothing even though the bare name exists elsewhere as, say,
    an import or a comment), the check must not manufacture a rejection -- the
    weaker, pre-existing FOUND verdict stands unchanged. Never downgrades an
    existing pass into a failure just because structure cannot be determined."""
    fake_rg, fake_rg_batch = _make_grep({
        "Voucher": ["some/other/file.py:5:    return Voucher"],
    })
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr("tools.symbol_index.class_bases", lambda rel_path, class_name: None)

    answer = "- `class Voucher(BaseModel)`"
    report = verify.verify_claims(answer)

    assert "WRONG BASE" not in report
    assert "FOUND      Voucher" in report


def test_bare_class_declaration_with_no_parens_never_triggers_base_check(monkeypatch):
    """`class Voucher` (no explicit base at all) makes no base-class claim -- there
    is nothing for _declared_bases_mismatch to compare, and it must never be
    called for this shape."""
    calls = []

    def tracking_rg(pattern, fixed=True, glob_filter="", whole_word=False):
        calls.append(pattern)
        return ["API/inventory-service/models.py:150:class Voucher(Base):"] \
            if pattern == "Voucher" else []

    def tracking_rg_batch(patterns, glob_filter="", whole_word=False, per_pattern_cap=8):
        return {p: (["API/inventory-service/models.py:150:class Voucher(Base):"]
                     if p == "Voucher" else []) for p in patterns}

    monkeypatch.setattr(verify, "_rg", tracking_rg)
    monkeypatch.setattr(verify, "_rg_batch", tracking_rg_batch)

    answer = "- `class Voucher`"
    report = verify.verify_claims(answer)

    assert "FOUND      Voucher" in report
    assert not any(p.startswith(r"^\s*class") for p in calls)


def test_existing_claim_verification_tests_still_pass():
    """No-op sanity check that this file's own additions did not need to change
    verify_claims' pre-existing behavior for non-declaration claims -- the real
    regression coverage for that lives in test_verify.py and its neighbours, run
    as part of the full suite in this phase's own validation."""
    answer = "The column `item_categories.sku_prefix` already exists."
    report = verify.verify_claims(answer)
    assert "item_categories.sku_prefix" in report
