"""Critical regression test -- the exact Phase V fabrication run, reproduced.

Run b52f2825af38, session ee2f6cea-2771-49bb-b03e-7b0123eb71d4 (2026-09-17), T13a
vouchers-audit: the final answer's "Backend Database Tables (8)" section named 8
backticked `class X(BaseModel)` declarations. All 8 are fabricated -- 6 have no
real declaration anywhere in the repo at all; the other 2 (`Voucher`,
`VoucherSeries`) are real classes, but the repository's actual declaration is
`class Voucher(Base):` / `class VoucherSeries(Base):`, not `(BaseModel)`.
Authoritative evidence (the real `models.py`) was fully read by the Coordinator
before synthesis, in the same run -- this was not a truncation, relay-loss, or
evidence-corruption failure (see this phase's own forensic report). Replaying the
PRE-W1 production tokenizer against this exact text showed every one of the 8
spans silently discarded before ever becoming a checkable claim.

This fixture reproduces the repository shape via mocks (no live model, no ZGX,
consistent with every other test in this suite), and demonstrates the full W1+W2
chain end-to-end:
  1. W1 extracts all 8 declarations.
  2. W2 sends them through the existing verifier.
  3. None of the 8 fabricated claims is silently invisible any more.
  4. The verification result reflects their actual (unsupported) status.
  5. The report still carries "could NOT be found" -- the exact substring
     swarm/team.py's `_verify_claims` checks to set `bad=True`, so
     `_verified_answer()` does not ship these as verified factual claims without
     at least the disclaimer/retry path firing, unchanged from every other
     fabrication this tool already catches.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._checked_answer_counts = {}


# The exact fabricated section, verbatim from the real delivered answer
# (session_messages, session ee2f6cea-2771-49bb-b03e-7b0123eb71d4).
PHASE_V_ANSWER = """### Audit of the Vouchers Module

**Backend Database Tables (8):**
- `class Voucher(BaseModel)`
- `class VoucherSeries(BaseModel)`
- `class VoucherSeriesConfig(BaseModel)`
- `class VoucherSeriesConfigHistory(BaseModel)`
- `class VoucherSeriesConfigHistoryEntry(BaseModel)`
- `class VoucherSeriesConfigHistoryEntryDiff(BaseModel)`
- `class VoucherSeriesConfigHistoryEntryDiffField(BaseModel)`
- `class VoucherSeriesConfigHistoryEntryDiffFieldChange(BaseModel)`
"""

_REAL_DECLARATION_LINE = {
    "Voucher": "API/inventory-service/models.py:350:class Voucher(Base):",
    "VoucherSeries": "API/inventory-service/models.py:326:class VoucherSeries(Base):",
}
_REAL_BASES = {"Voucher": ["Base"], "VoucherSeries": ["Base"]}

_FABRICATED_NAMES = [
    "VoucherSeriesConfig", "VoucherSeriesConfigHistory",
    "VoucherSeriesConfigHistoryEntry", "VoucherSeriesConfigHistoryEntryDiff",
    "VoucherSeriesConfigHistoryEntryDiffField",
    "VoucherSeriesConfigHistoryEntryDiffFieldChange",
]


def _repo_fixture():
    """Real repo shape for the two genuinely-existing classes; the 6 purely
    fictional ones are absent everywhere, exactly as in the real repository --
    reproduced by simply never adding them to this fixture's hit map."""
    hits_by_pattern = {}
    for name, decl_line in _REAL_DECLARATION_LINE.items():
        hits_by_pattern[name] = [decl_line]
        hits_by_pattern[rf"^\s*class\s+{name}\b"] = [decl_line]

    def fake_rg(pattern, fixed=True, glob_filter="", whole_word=False):
        return hits_by_pattern.get(pattern, [])

    def fake_rg_batch(patterns, glob_filter="", whole_word=False, per_pattern_cap=8):
        return {p: hits_by_pattern.get(p, []) for p in patterns}

    def fake_class_bases(rel_path, class_name):
        return _REAL_BASES.get(class_name)

    return fake_rg, fake_rg_batch, fake_class_bases


def test_all_eight_fabricated_declarations_are_extracted(monkeypatch):
    """Requirement 1 (W1 extracts the declarations) and requirement 3 (no longer
    silently invisible) -- every one of the 8 names must appear in the report at
    all, which the pre-W1 tokenizer never achieved for any of them."""
    fake_rg, fake_rg_batch, fake_class_bases = _repo_fixture()
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr("tools.symbol_index.class_bases", fake_class_bases)

    report = verify.verify_claims(PHASE_V_ANSWER)

    assert "no checkable claims found" not in report
    for name in ["Voucher", "VoucherSeries", *_FABRICATED_NAMES]:
        assert name in report, f"{name} never appears in the report -- W1 regressed"


def test_purely_fictional_declarations_are_marked_not_found(monkeypatch):
    """Requirement 4 -- the 6 names with no real declaration anywhere must be
    marked NOT FOUND, the same as any other fabricated symbol this tool catches."""
    fake_rg, fake_rg_batch, fake_class_bases = _repo_fixture()
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr("tools.symbol_index.class_bases", fake_class_bases)

    report = verify.verify_claims(PHASE_V_ANSWER)

    not_found_lines = "\n".join(
        ln for ln in report.splitlines() if ln.strip().startswith("NOT FOUND")
    )
    for name in _FABRICATED_NAMES:
        assert name in not_found_lines, f"{name} not reported NOT FOUND"


def test_real_classes_with_wrong_claimed_base_are_rejected_not_certified(monkeypatch):
    """Requirement 4, the harder half -- `Voucher`/`VoucherSeries` are REAL, so a
    bare existence check alone would certify them as FOUND. W2 must instead report
    them as unsupported because the claimed base class (BaseModel) contradicts the
    real one (Base) -- this is the exact case a name-only verifier cannot catch,
    and the whole reason W2 exists on top of W1."""
    fake_rg, fake_rg_batch, fake_class_bases = _repo_fixture()
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr("tools.symbol_index.class_bases", fake_class_bases)

    report = verify.verify_claims(PHASE_V_ANSWER)

    wrong_base_lines = "\n".join(
        ln for ln in report.splitlines() if "WRONG BASE" in ln
    )
    assert "Voucher" in wrong_base_lines
    assert "VoucherSeries" in wrong_base_lines
    # Never silently certified as a clean, supported FOUND.
    for ln in report.splitlines():
        assert not ln.strip().startswith("FOUND      Voucher ")
        assert not ln.strip().startswith("FOUND      VoucherSeries ")


def test_verdict_still_reads_as_fabrication_for_the_orchestrator(monkeypatch):
    """Requirement 5 -- swarm/team.py's `_verify_claims` classifies a report as
    `bad=True` via the literal substring check `"could NOT be found" in report`
    (see verify.py's own STOPPED-message comment for why this exact phrase is
    load-bearing). All 8 fabricated declarations must drive this verdict, so
    `_verified_answer()` does not ship them unflagged -- exactly the property that
    silently failed in the real Phase V run before W1/W2."""
    fake_rg, fake_rg_batch, fake_class_bases = _repo_fixture()
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr("tools.symbol_index.class_bases", fake_class_bases)

    report = verify.verify_claims(PHASE_V_ANSWER)

    assert "could NOT be found" in report
    assert "VERDICT: every checked claim exists" not in report


def test_problem_count_covers_all_eight_fabricated_claims(monkeypatch):
    """Every one of the 8 -- not a subset -- must count toward `problems`: 6 as
    NOT FOUND, 2 as WRONG BASE. A verdict that only caught some of the 8 would
    still leave the Coordinator free to ship the rest unflagged."""
    fake_rg, fake_rg_batch, fake_class_bases = _repo_fixture()
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr("tools.symbol_index.class_bases", fake_class_bases)

    report = verify.verify_claims(PHASE_V_ANSWER)

    import re
    m = re.search(r"VERDICT: (\d+) claim\(s\) could NOT be found", report)
    assert m is not None, report
    assert int(m.group(1)) == 8
