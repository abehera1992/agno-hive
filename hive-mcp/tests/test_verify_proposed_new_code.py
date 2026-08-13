"""Regression tests: verify_claims must not treat a symbol the answer itself
introduces as NEW code to add as a fabricated existence claim.

Confirmed live 2026-08-14: a read-only "propose Phase 1 code changes" task
(EkamApp parties module) returned a proposal whose own text said things like
"Add handlers: `openAddLocationModal`, `handleAddRegistration`,
`handleAddLocation`" and "Add a `stateOptions` list", plus a fenced ```tsx block
referencing `styles.registrationsList` and other new SCSS classnames. All 9 of
these were reported NOT FOUND with a "fix the answer before returning it -- this
is fabrication" verdict, even though the answer's own text explicitly framed
every one of them as something to CREATE, not something already there.

Two distinct mechanisms, two distinct fixes:
  1. A "new/add/propose" cue shortly before a backticked PROSE claim (mirrors
     _is_negated_claim's existing shape) -- catches the bare handler/variable
     names, which only ever appear via backtick-prose (table cells), never as
     dotted code-block identifiers _code_idents would otherwise pick up.
  2. Identifiers from a fenced CODE BLOCK whose immediately preceding text (a
     heading like "Proposed Code Insertion") carries the same cue -- catches the
     dotted styles.* classnames referenced directly inside the ```tsx block. An
     earlier version of this fix gated on whether ANYTHING was staged anywhere in
     the project instead of checking each fence's own preceding text, which
     regressed a plain "here is the existing code" quote (no staged file either)
     into a false PROPOSED label -- see test_code_block_without_a_preceding_cue_*
     below for the regression this now guards against.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    """verify_claims hard-stops on a BYTE-IDENTICAL answer checked twice in a row
    (see verify.py's own module docstring for why) -- two tests in this file
    intentionally reuse near-identical TSX snippets, which otherwise collide with
    that unrelated mechanism depending on test order. Same reset test_verify.py's
    own test_identical_answer_checked_twice_hard_stops uses, just autoused here
    since nothing in this file is testing THAT mechanism."""
    verify._last_checked_answer = None
    verify._repeat_count = 0


# ── mechanism 1: backtick-prose "new/add" cue ───────────────────────────────────

def test_add_cue_immediately_before_backtick_is_not_a_fabrication_claim(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Add a new `stateOptions` list for the state-code dropdown."

    report = verify.verify_claims(answer)

    assert "NOT FOUND" not in report
    assert "stateOptions" in report
    assert "PROPOSED" in report


def test_add_cue_applies_across_a_comma_separated_list(monkeypatch):
    """The real incident's exact phrasing: one cue word, three backticked names
    after it. A per-name 20-char window (like negation's) would miss the 2nd/3rd."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = ("Add handlers: `openAddLocationModal`, `handleAddRegistration`, "
              "`handleAddLocation`.")

    report = verify.verify_claims(answer)

    assert "NOT FOUND" not in report
    for name in ("openAddLocationModal", "handleAddRegistration", "handleAddLocation"):
        assert name in report


def test_unrelated_earlier_add_does_not_suppress_a_real_existence_claim(monkeypatch):
    """The window must not be so wide that an unrelated earlier 'add' anywhere in
    the paragraph blanket-suppresses a genuine, unrelated existence claim."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = ("We should add proper validation somewhere in this long sentence "
              "before we ever get close to mentioning the already-existing "
              "helper called `fooBarBaz` right here at the end.")

    report = verify.verify_claims(answer)

    assert "fooBarBaz" in report
    assert "NOT FOUND" in report


def test_plain_backtick_claim_without_a_new_code_cue_is_still_checked(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The function is `doTheThing`."

    report = verify.verify_claims(answer)

    assert "doTheThing" in report
    assert "NOT FOUND" in report


# ── mechanism 2: fenced-code-block identifiers, gated on a preceding cue ────────

def test_code_block_after_a_proposed_heading_is_not_a_fabrication_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = (
        "##### Proposed Code Insertion\n\n"
        "```tsx\n"
        "const x = <div className={styles.registrationsList}>hi</div>;\n"
        "```\n"
    )

    report = verify.verify_claims(answer)

    assert "NOT FOUND" not in report
    assert "styles.registrationsList" in report
    assert "PROPOSED" in report


def test_code_block_without_a_preceding_cue_is_still_checked_strictly(tmp_path, monkeypatch):
    """A plain 'here is the existing code' snippet, with no new/add/propose cue
    before it, must keep being checked as an existence claim exactly as before.
    This is the regression an earlier version of this fix introduced by gating on
    whether ANYTHING was staged anywhere in the project (a read-only Q&A call also
    has no staged file) instead of checking each fence's own preceding text."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = (
        "Here is the code:\n"
        "```tsx\n"
        "const x = <div className={styles.registrationsList}>hi</div>;\n"
        "```\n"
    )

    report = verify.verify_claims(answer)

    assert "styles.registrationsList" in report
    assert "NOT FOUND" in report
    assert "PROPOSED" not in report


def test_code_block_in_a_staged_file_is_unaffected_by_this_mechanism(tmp_path, monkeypatch):
    """*.hive_proposed content never goes through _proposed_code_block_idents (it
    only scans the answer's own prose fences) -- a symbol that made it into an
    actually-staged file stays on the strict path unconditionally, cue or not."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    staged = tmp_path / "other.tsx.hive_proposed"
    staged.write_text("const y = <div className={styles.registrationsList} />;\n", encoding="utf-8")
    answer = "Proposed change: staged in other.tsx."

    report = verify.verify_claims(answer)

    assert "styles.registrationsList" in report
    assert "NOT FOUND" in report
    assert "PROPOSED" not in report


def test_proposed_symbols_do_not_count_toward_the_verdict(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Add a new `stateOptions` list for the dropdown."

    report = verify.verify_claims(answer)

    assert "every checked claim exists" in report or "VERDICT: every checked claim" in report
