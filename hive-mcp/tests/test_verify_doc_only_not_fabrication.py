"""Regression test: a SYMBOLS claim found ONLY in project documentation (DOC ONLY)
must never be treated as fabrication -- it is evidence the thing IS real, just not
independently confirmed as a literal string in code.

Live incident (2026-08-15, T1e engineering-team groundedness retest): asked about
the "Module Settings Panel pattern", the swarm correctly located and cited
patterns/ekam-frontend.md:1109, which documents the real symbol
`inventory.party_module_settings`. verify_claims flagged that symbol DOC ONLY (a
real doc hit, not independently confirmed via a literal code grep -- it may be a
dynamically-constructed table name or a documented naming convention rather than a
source-literal string). Before this fix, DOC ONLY counted toward the SAME
`problems` counter as NOT FOUND, which set `bad=True` and produced a VERDICT
telling the model "Fix the answer before returning it -- ... is fabrication, not a
near miss." swarm/team.py's one-shot correction retry then instructed the model
"a previous attempt referred to inventory.party_module_settings, which a
repository-wide grep shows does not exist here. Do not mention it again" -- the
model discarded its own correct citation and answered that an entire real,
documented pattern "does not exist in the EkamApp codebase", with the raw
verify_claims debug block leaking into the final user-facing answer.

DOC ONLY is now tracked separately (doc_only_count) and never increments
`problems` -- a report containing ONLY DOC ONLY items must read as a clean pass
(VERDICT: no fabricated claims found...), never trigger `bad=True`, and never
reach swarm/team.py's retry path at all.
"""
import pytest

from tools import verify

_DOC_HIT = ["patterns/ekam-frontend.md:1109:inventory.party_module_settings ("]


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    """Several tests below reuse the same answer text -- verify.py's repeat-answer
    short-circuit (verify_claims STOPPED: this exact answer text was already
    checked...) would otherwise fire on the second+ test and mask what's actually
    being tested here. Same reset pattern as test_verify_orm_dotted_fallback.py."""
    verify._last_checked_answer = None
    verify._repeat_count = 0


def test_symbol_found_only_in_docs_is_reported_as_doc_only(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: _DOC_HIT)
    answer = "The Module Settings Panel pattern uses `inventory.party_module_settings`."

    report = verify.verify_claims(answer)

    assert "DOC ONLY" in report
    assert "inventory.party_module_settings" in report


def test_doc_only_symbol_does_not_produce_a_not_found_line(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: _DOC_HIT)
    answer = "The pattern uses `party_module_settings`."

    report = verify.verify_claims(answer)

    assert "NOT FOUND" not in report


def test_report_with_only_doc_only_items_reads_as_a_clean_pass(monkeypatch):
    """The exact live-incident shape: a report whose ONLY finding is a real
    documentation citation must never contain the literal substring
    "could NOT be found" -- that is what swarm/team.py's _verify_claims greps for
    to decide whether to trigger a correction retry."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: _DOC_HIT)
    answer = "The pattern uses `party_module_settings`."

    report = verify.verify_claims(answer)

    assert "could NOT be found" not in report
    assert "VERDICT: no fabricated claims found" in report


def test_doc_only_verdict_explicitly_says_not_fabrication_and_no_fix_needed(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: _DOC_HIT)
    answer = "The pattern uses `party_module_settings`."

    report = verify.verify_claims(answer)

    assert "not fabrication" in report
    assert "does not need to be fixed" in report


def test_a_real_not_found_symbol_still_counts_as_a_problem_alongside_doc_only(monkeypatch):
    """DOC ONLY must not launder an ACTUAL fabricated symbol in the same report --
    only DOC ONLY items are exempt, real NOT FOUND items still trigger the normal
    fabrication verdict."""
    def fake_rg(tok, **k):
        return _DOC_HIT if tok == "party_module_settings" else []
    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "Uses `party_module_settings` and `totallyMadeUpSymbolXyz`."

    report = verify.verify_claims(answer)

    assert "DOC ONLY" in report
    assert "NOT FOUND" in report
    assert "totallyMadeUpSymbolXyz" in report
    assert "could NOT be found" in report
    assert "VERDICT: 1 claim(s) could NOT be found" in report


def test_mixed_report_note_clarifies_doc_only_items_are_not_part_of_the_verdict(monkeypatch):
    def fake_rg(tok, **k):
        return _DOC_HIT if tok == "party_module_settings" else []
    monkeypatch.setattr(verify, "_rg", fake_rg)
    answer = "Uses `party_module_settings` and `totallyMadeUpSymbolXyz`."

    report = verify.verify_claims(answer)

    assert "NOTE:" in report
    assert "DOC ONLY item(s) above are NOT part of this verdict" in report


def test_symbol_found_in_code_is_still_reported_found_not_doc_only(monkeypatch):
    code_hit = ["API/inventory-service/models.py:42:    party_module_settings = Column(...)"]
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: code_hit)
    answer = "Uses `party_module_settings`."

    report = verify.verify_claims(answer)

    assert "FOUND" in report
    assert "DOC ONLY" not in report
    assert "NOT FOUND" not in report
