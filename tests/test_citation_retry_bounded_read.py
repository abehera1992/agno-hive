"""Tests: the citation-correction retry asks for a NARROW read and carries the
line numbers verify_claims already found.

Root-caused live, 2026-08-21, on API/inventory-service/models.py (774 lines, 32KB
-- under the skeleton threshold, so the real numbered content IS returned):

    get_file_content(offset=124, limit=12)  -> "129\tsku_prefix = Column(String(8),
                                                nullable=True)" reproduced EXACTLY
    get_file_content(whole file)            -> "line 142 ... String(10)" one run,
                                               "line 142 ... String(20)" the next

Same file, same model, same run config; only the read SHAPE differed. In a
774-line numbered dump the model interpolates a plausible line number instead of
copying one, which is why the wrong value changes every run.

The old retry instruction said "FIRST call get_file_content on the exact file(s)
involved". The logs show the model complied -- and reproduced the identical
unbounded read that caused the error. Not a compliance failure: the instruction
prescribed the wrong remedy. Two hypotheses were ruled out first (a read-cache
stub, and skeleton elision); neither held.

Second fix: verify_claims' symbol-anchored MISMATCH already ends with the real
location ("it actually appears at line(s) 129"), computed by _symbol_line_numbers
reading the file. That was printed and thrown away. The retry now gets it.
"""
import pytest

from swarm.team import _CORRECT_LINE_RE, _MAX_HANDED_OVER_LOCATIONS, _citation_retry_hint

BT = chr(96)

_REPORT_WITH_LINES = (
    "CITATIONS (1 checked):\n"
    f"  MISMATCH   API/inventory-service/models.py:142 <-- {BT}sku_prefix{BT} is not "
    "within 5 lines of 142; it actually appears at line(s) 129\n"
    "VERDICT: 1 claim(s) could NOT be found in the project."
)

_REPORT_QUOTE_ONLY = (
    "CITATIONS (1 checked):\n"
    "  MISMATCH   parties_api.py:194 <-- quoted 'x' not found within 5 lines; "
    "citation and quoted content do not point at the same place\n"
    "VERDICT: 1 claim(s) could NOT be found in the project."
)


# ── The harvest regex, against verify.py's real assembled output ──────────────

def test_the_true_line_is_harvested_from_a_symbol_mismatch():
    assert _CORRECT_LINE_RE.findall(_REPORT_WITH_LINES) == [("sku_prefix", "129")]


def test_multiple_line_hits_are_captured():
    line = (f"  MISMATCH   models.py:142 <-- {BT}ItemCategory{BT} is not within 5 lines "
            "of 142; it actually appears at line(s) 116, 208")
    assert _CORRECT_LINE_RE.findall(line) == [("ItemCategory", "116, 208")]


def test_a_quote_based_mismatch_yields_nothing():
    """It carries no computed location, so there is nothing to hand over. Must not
    match spuriously — a wrong "already located for you" line would be worse than
    none, since the retry is told to trust it."""
    assert _CORRECT_LINE_RE.findall(_REPORT_QUOTE_ONLY) == []


def test_the_regex_matches_verify_pys_real_format_not_an_assumed_one():
    """Built by asking verify.py itself, not by eyeballing. This session already
    shipped one regex that read correctly and matched nothing, so the emitting
    format is pinned here: if verify.py rewords the message, this fails loudly
    instead of the harvest silently returning [] forever.

    Read as TEXT rather than imported — `tools` is hive-mcp's package and is not on
    the root suite's path. The message is assembled from three f-string fragments,
    which is also why grepping for the whole sentence finds nothing."""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "hive-mcp" / "tools" / "verify.py").read_text(
        encoding="utf-8"
    )
    for fragment in ("is not ", "within {_LINE_TOLERANCE} lines of {num}; it actually ",
                     "appears at line(s) {near}"):
        assert fragment in src, f"verify.py no longer emits {fragment!r}"


# ── The assembled retry instruction ───────────────────────────────────────────

def _instruction(report: str) -> str:
    """The REAL production string — _verified_answer calls this exact function with
    exactly this input. Deliberately not a reimplementation: a test that rebuilds the
    wording proves only that the test agrees with itself, and drifts silently the
    first time the real hint is edited."""
    return _citation_retry_hint(_CORRECT_LINE_RE.findall(report))


def test_the_instruction_forbids_a_whole_file_reread():
    """The precise thing the old wording invited."""
    out = _instruction(_REPORT_WITH_LINES)
    assert "Do NOT re-read the whole file" in out


def test_the_instruction_names_both_narrow_routes():
    out = _instruction(_REPORT_WITH_LINES)
    assert "search_files" in out
    assert "offset/limit" in out and "SMALL window" in out


def test_the_true_line_reaches_the_instruction():
    out = _instruction(_REPORT_WITH_LINES)
    assert "sku_prefix is at line(s) 129" in out


def test_no_located_claim_when_there_is_nothing_to_locate():
    """A quote-based MISMATCH must still get the narrow-read guidance, but must not
    be told a grep found something."""
    out = _instruction(_REPORT_QUOTE_ONLY)
    assert "Do NOT re-read the whole file" in out
    assert "ALREADY located" not in out


def test_the_hand_over_is_capped():
    report = "\n".join(
        f"  MISMATCH   m.py:{i} <-- {BT}sym{i}{BT} is not within 5 lines of {i}; "
        f"it actually appears at line(s) {i * 2}"
        for i in range(1, 9)
    )
    out = _instruction(report)
    assert out.count("is at line(s)") == _MAX_HANDED_OVER_LOCATIONS
