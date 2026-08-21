"""Tests: the model-directed imperative never reaches a human reader.

A verify_claims report has two audiences. During the correction retry the MODEL
reads it, and "Fix the answer before returning it — a NOT FOUND symbol or a BAD
citation is fabrication, not a near miss" is exactly right there. When the retry
budget is already spent the SAME report is appended to the final answer — on
purpose ("Surface rather than hide: the reader needs to know which claims are
unsupported", swarm/team.py). There it reads as an instruction the pipeline was
handed and visibly ignored.

Seen twice in one T1-T13 re-run, 2026-08-21. The findings must stay; only that one
sentence goes.
"""
from swarm.team import _reader_facing_report


_REPORT = (
    "verify_claims — deterministic grep of the claims in this answer\n"
    "\n"
    "SYMBOLS (1 checked):\n"
    "  FOUND      delete_party                  parties_api.py:194:async def delete_party(\n"
    "\n"
    "CITATIONS (1 checked):\n"
    "  MISMATCH   parties_api.py:194 <-- quoted 'x' not found within 5 lines\n"
    "\n"
    "VERDICT: 1 claim(s) could NOT be found in the project. Fix the answer before "
    "returning it — a NOT FOUND symbol or a BAD citation is fabrication, not a near miss."
)


def test_the_model_directed_imperative_is_removed():
    assert "Fix the answer before returning it" not in _reader_facing_report(_REPORT)


def test_the_factual_half_of_the_verdict_survives():
    """The reader still needs the count and the conclusion — only the instruction
    aimed at the model is dropped."""
    out = _reader_facing_report(_REPORT)
    assert "VERDICT: 1 claim(s) could NOT be found in the project." in out


def test_every_finding_line_survives():
    """These are the whole reason the report is surfaced at all."""
    out = _reader_facing_report(_REPORT)
    for line in ("SYMBOLS (1 checked):", "CITATIONS (1 checked):",
                 "FOUND      delete_party", "MISMATCH   parties_api.py:194"):
        assert line in out, line


def test_a_clean_report_is_unchanged():
    clean = (
        "verify_claims — deterministic grep\n\n"
        "VERDICT: every checked claim exists in the project."
    )
    assert _reader_facing_report(clean) == clean


def test_an_empty_report_is_handled():
    assert _reader_facing_report("") == ""


def test_it_does_not_eat_the_following_lines():
    """The imperative is stripped to end-of-line only — a report that continues
    after the VERDICT keeps everything below it."""
    report = _REPORT + "\nNOTE: 2 claims were doc-only."
    out = _reader_facing_report(report)
    assert "NOTE: 2 claims were doc-only." in out
    assert "Fix the answer before returning it" not in out
