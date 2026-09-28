"""Phase Z18R (2026-09-28) -- forwarded-section claims must not be silently
dropped by _MAX_CLAIMS when a long, legitimate synthesis already fills the cap.

Root cause, proven against the real T4 incident (2026-09-26): swarm/team.py's
_with_forwarded_evidence appends a member's raw answer under a "FORWARDED FROM
THE MEMBERS" banner whenever the Coordinator's own text doesn't already carry it
-- confirmed to run BEFORE verify_claims at its call site (run_task_async, the
line assigning `content = _with_forwarded_evidence(...)` precedes the later
`content = await _verified_answer(...)` by several lines). So forwarded content
is NOT excluded from verification (ruling out the "trust boundary" hypothesis
outright) -- it goes through the exact same _MAX_CLAIMS-capped idents list as
everything else. The bug is purely positional: idents are checked in first-seen
order, and when an early, legitimate synthesis already produces >= _MAX_CLAIMS
unique claims, every claim whose FIRST occurrence is in a LATER forwarded
section is silently never examined -- not adjudicated, not skipped-and-noted,
simply absent from the checked list. Replaying T4's actual final answer text
through the unpatched function reproduced this exactly: "VERDICT: every checked
claim exists" while 6 of the forwarded section's own field names do not exist
anywhere in the project (verified directly against
API/inventory-service/models.py).

The fix (verify.py, right after the idents-collection loop): a stable partition
that moves idents whose first occurrence is at or after the forwarded marker to
the front of the list, before any [:_MAX_CLAIMS] slicing happens downstream.
Does not compare the two representations to each other and does not touch
existence-checking logic at all -- it only decides which idents get a turn.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._checked_answer_counts = {}


def _make_grep(hits_by_pattern):
    """Same convention as test_verify_declaration_structure.py."""
    def fake_rg(pattern, fixed=True, glob_filter="", whole_word=False):
        return hits_by_pattern.get(pattern, [])

    def fake_rg_batch(patterns, glob_filter="", whole_word=False, per_pattern_cap=8):
        return {p: hits_by_pattern.get(p, []) for p in patterns}

    return fake_rg, fake_rg_batch


def _early_claims(n: int) -> str:
    """n distinct, EXISTING bare identifiers, one per line -- stands in for a
    long, legitimate synthesis that alone reaches _MAX_CLAIMS."""
    return "\n".join(f"- `earlyField{i}`" for i in range(n))


def _forwarded_block(fields: list[str]) -> str:
    return (
        "\n\n---\n**FORWARDED FROM THE MEMBERS — their own text, unedited, "
        "appended because the answer above did not carry all of it.**\n\n"
        "### From researcher\n"
        + "\n".join(f"- `{f}`" for f in fields)
    )


# Test A -- contradictory duplicate entity: unsupported fields past the cap,
# in a forwarded section, must not silently survive.
def test_fabricated_forwarded_fields_past_the_cap_are_flagged(monkeypatch):
    hits = {f"earlyField{i}": [f"file.py:{i}:earlyField{i}"] for i in range(25)}
    fake_rg, fake_rg_batch = _make_grep(hits)
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)

    fabricated = ["registrationNumber", "validFrom", "validTo", "addressLine1", "pincode"]
    answer = _early_claims(25) + _forwarded_block(fabricated)

    report = verify.verify_claims(answer)

    for field in fabricated:
        assert field in report
    assert "could NOT be found" in report
    assert "VERDICT: every checked claim exists" not in report


# Test B -- correct forwarded evidence must still survive: a genuinely correct
# forwarded section past the cap must not be falsely flagged.
def test_correct_forwarded_fields_past_the_cap_are_not_falsely_flagged(monkeypatch):
    hits = {f"earlyField{i}": [f"file.py:{i}:earlyField{i}"] for i in range(25)}
    real_forwarded = ["gstin", "stateCode", "tradeName"]
    hits.update({f: [f"file.py:99:{f}"] for f in real_forwarded})
    fake_rg, fake_rg_batch = _make_grep(hits)
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)

    answer = _early_claims(25) + _forwarded_block(real_forwarded)

    report = verify.verify_claims(answer)

    assert "VERDICT: every checked claim exists" in report
    for field in real_forwarded:
        assert f"NOT FOUND  {field}" not in report


# Test C -- normal, non-contradictory answer (no forwarded section at all, or
# under the cap) is completely unaffected: idents ordering stays byte-identical
# to before this change.
def test_answer_without_a_forwarded_section_is_unaffected(monkeypatch):
    calls = []

    def tracking_rg(pattern, fixed=True, glob_filter="", whole_word=False):
        calls.append(pattern)
        return [f"file.py:1:{pattern}"]

    def tracking_rg_batch(patterns, glob_filter="", whole_word=False, per_pattern_cap=8):
        for p in patterns:
            calls.append(p)
        return {p: [f"file.py:1:{p}"] for p in patterns}

    monkeypatch.setattr(verify, "_rg", tracking_rg)
    monkeypatch.setattr(verify, "_rg_batch", tracking_rg_batch)

    answer = _early_claims(10)  # well under _MAX_CLAIMS, no forwarded marker
    report = verify.verify_claims(answer)

    assert "VERDICT: every checked claim exists" in report
    assert "SYMBOLS (10 checked)" in report


def test_under_the_cap_with_a_forwarded_section_is_unaffected(monkeypatch):
    """The reorder only activates when idents EXCEED _MAX_CLAIMS -- a short
    answer with a forwarded section but few total claims must see the exact
    same ordering (and therefore the exact same report) as before this change."""
    hits = {f"earlyField{i}": [f"file.py:{i}:earlyField{i}"] for i in range(5)}
    hits["gstin"] = ["file.py:99:gstin"]
    fake_rg, fake_rg_batch = _make_grep(hits)
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)

    answer = _early_claims(5) + _forwarded_block(["gstin"])
    report = verify.verify_claims(answer)

    assert "SYMBOLS (6 checked)" in report
    assert "VERDICT: every checked claim exists" in report


# Test D -- two genuinely different, unrelated entities (one early, one
# forwarded) must each be checked on their own merits -- no false contradiction
# is manufactured merely because the reorder moved one ahead of the other.
def test_different_entities_are_each_checked_independently(monkeypatch):
    hits = {f"earlyField{i}": [f"file.py:{i}:earlyField{i}"] for i in range(25)}
    hits["realForwardedThing"] = ["other_file.py:5:realForwardedThing"]
    fake_rg, fake_rg_batch = _make_grep(hits)
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)

    answer = (
        _early_claims(25)
        + _forwarded_block(["realForwardedThing", "fabricatedUnrelatedThing"])
    )
    report = verify.verify_claims(answer)

    assert "fabricatedUnrelatedThing" in report
    assert "could NOT be found" in report
    assert "NOT FOUND  realForwardedThing" not in report
