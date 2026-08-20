"""Regression tests: verify_claims must catch PLURAL line-RANGE prose citations.

Confirmed 2026-08-20 via a live groundedness probe against the ekam project. An answer
about `API/inventory-service/router/parties_api.py` wrote:

    "- **Lines**: 91-135"

for a claim about `get_party`/`update_party`/`delete_party` -- but 91-109 is
`list_parties` (a different function entirely), `delete_party` actually lives at
193-206, and the range the answer gave for it (209-235) is `add_registration`'s.

`_LABELED_LINE_RE` matched only the SINGULAR "line N" form, so a plural range was
never recognised as a checkable claim at all -- the same class of blind spot the
2026-08-03 labeled-prose fix closed for "**Line:** 389", just one word away. The
citation shipped unchecked even though verify_claims otherwise ran cleanly that run.

Fix: accept `lines?` and an optional range end, registering BOTH endpoints as
citations so a range that runs off the end of the file, or lands on unrelated
content, is caught the same as a single bad line number.
"""
from tools import verify


def _rg_finds_bare_filename(tok, **k):
    """Same helper rationale as test_verify_labeled_line_citations.py: a backticked
    path is also checked as a bare symbol, and that unrelated check would otherwise
    drown out the CITATION assertions these tests are about."""
    if tok == "parties_api.py":
        return ["parties_api.py:1:some content"]
    return []


def test_plural_line_range_beyond_end_of_file_is_flagged(tmp_path, monkeypatch):
    """The live shape: a range whose endpoints don't exist in a short file."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_finds_bare_filename)
    (tmp_path / "parties_api.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    answer = "Tenant ownership checks live in `parties_api.py` — **Lines**: 91-135"

    report = verify.verify_claims(answer)

    assert "BAD" in report
    assert "parties_api.py:91" in report


def test_en_dash_range_is_matched_too(tmp_path, monkeypatch):
    """Models write ranges with an en-dash as often as a hyphen; the real 2026-08-20
    answer used '91–135'. Matching only ASCII '-' would miss the exact live case."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_finds_bare_filename)
    (tmp_path / "parties_api.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    answer = "See `parties_api.py`, **Lines**: 91–135 for the ownership filter."

    report = verify.verify_claims(answer)

    assert "BAD" in report


def test_both_range_endpoints_are_registered_as_citations(tmp_path, monkeypatch):
    """A range is two claims, not one: a start that happens to be valid must not
    launder an end that isn't. Here 2 exists and 400 does not."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_finds_bare_filename)
    (tmp_path / "parties_api.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")

    answer = "In `parties_api.py`, **Lines**: 2-400 hold the validation."

    report = verify.verify_claims(answer)

    assert "parties_api.py:400" in report
    assert "BAD" in report


def test_valid_plural_range_within_the_file_is_not_flagged(tmp_path, monkeypatch):
    """No new false positives: an honest in-bounds range must still pass."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_finds_bare_filename)
    (tmp_path / "parties_api.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 21)), encoding="utf-8"
    )

    answer = "In `parties_api.py`, **Lines**: 3-7 define the schema."

    report = verify.verify_claims(answer)

    assert "BAD" not in report


def test_singular_line_citation_still_works(tmp_path, monkeypatch):
    """The pre-existing 2026-08-03 behaviour must be unchanged by the widening."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_finds_bare_filename)
    (tmp_path / "parties_api.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    answer = "**File:** `parties_api.py`, **Line:** 389"

    report = verify.verify_claims(answer)

    assert "BAD" in report
    assert "parties_api.py:389" in report
