"""Regression test: verify_claims must not flag a genuine ORM `table.column` claim
as fabrication just because the joined literal string never appears in source.

Confirmed live 2026-08-14: a Phase-1 gap-analysis run claimed `item_categories.
sku_prefix` already exists. `ItemCategory.sku_prefix` (EkamApp models.py:129,
`__tablename__ = "item_categories"`) genuinely does -- but SQLAlchemy declares the
table name and the column on separate lines, so the literal string
"item_categories.sku_prefix" never appears anywhere in the repo. `rg -F` for the
joined dotted token correctly found nothing, and the tool reported a real,
correct claim as "NOT FOUND ... fabrication, not a near miss" -- a false
negative in the checker, not a fabrication by the model.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._last_checked_answer = None
    verify._repeat_count = 0


def test_dotted_claim_falls_back_to_bare_attribute_when_joined_string_not_found(monkeypatch):
    def fake_rg(pattern, fixed=True, glob_filter="", whole_word=False):
        if pattern == "item_categories.sku_prefix":
            return []
        if pattern == "sku_prefix" and whole_word:
            return ["API/inventory-service/models.py:129:    sku_prefix = Column(String(8), nullable=True)"]
        return []

    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "The column `item_categories.sku_prefix` already exists."

    report = verify.verify_claims(answer)

    assert "NOT FOUND" not in report
    assert "sku_prefix" in report
    assert "VERDICT: every checked claim exists" in report


def test_dotted_claim_stays_not_found_when_bare_attribute_also_absent(monkeypatch):
    """The fallback must not launder every dotted miss -- a genuinely fabricated
    attribute (absent under any name) is still real fabrication."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The column `item_categories.totally_made_up_column` already exists."

    report = verify.verify_claims(answer)

    assert "NOT FOUND" in report
    assert "totally_made_up_column" in report


def test_dotted_fallback_does_not_count_a_doc_only_attribute_match(monkeypatch):
    """Same DOC ONLY standard as the rest of this tool -- a bare attribute name
    that only appears in documentation (not code) must not launder the claim."""
    def fake_rg(pattern, fixed=True, glob_filter="", whole_word=False):
        if pattern == "item_categories.sku_prefix":
            return []
        if pattern == "sku_prefix" and whole_word:
            return ["docs/inventory.md:45:sku_prefix"]
        return []

    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "The column `item_categories.sku_prefix` already exists."

    report = verify.verify_claims(answer)

    assert "NOT FOUND" in report


def test_non_dotted_claims_are_unaffected_by_this_fallback(monkeypatch):
    """A bare (non-dotted) NOT FOUND symbol must never attempt the split fallback
    -- there is nothing to split."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The function `totallyMadeUp` handles this."

    report = verify.verify_claims(answer)

    assert "NOT FOUND" in report
    assert "SPLIT-FOUND" not in report
