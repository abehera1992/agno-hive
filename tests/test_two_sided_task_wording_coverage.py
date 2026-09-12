"""Phase 14, screw #2 (AGNOHive Reliability Program): completeness/comparison
task-shape recognition, requirement F wording coverage.

_TWO_SIDED_TASK_RE and its reconciliation path (_reconcile_completeness_
claim_with_comparison) already exist locally (Phase 2A/2B of this program,
commits a7f14f4/875b630) and already carry 17+19 focused tests including
T2-shaped, T13a-shaped, and T13b-shaped wording, one-sided enumeration
(negative), and an unrelated task (negative) -- see
tests/test_computed_comparison.py and tests/test_comparison_reconciliation.py.

This file fills the one gap in that existing coverage against Phase 14's
explicit requirement-F wording list ("counterpart", "corresponding",
"matching", "missing"): the regex's third alternative ("no matching X") had
no dedicated test, and "missing" is not one of the regex's alternatives at
all. Rather than widen the regex to add "missing" -- an unvalidated change
outside this phase's evidence -- this pins the CURRENT, documented
behaviour: "matching" wording triggers, generic "missing" wording alone does
not. No project nouns, no filenames, no battery prompt text.
"""
from swarm.team import _TWO_SIDED_TASK_RE


def test_no_matching_wording_triggers_the_regex():
    """The regex's third alternative, "no matching \\w+", has no existing
    dedicated test -- only "no corresponding" (T2) and "no ... counterpart"
    (T13a/b) are pinned elsewhere."""
    task = ("List every service defined in the config file and every consumer "
            "registered in the worker file, and name any service with no "
            "matching consumer.")
    assert _TWO_SIDED_TASK_RE.search(task)


def test_generic_missing_wording_alone_does_not_trigger():
    """Documented gap, not silently widened: "missing" is not one of this
    regex's alternatives. A task that only says "identify anything missing"
    (no "corresponding"/"matching"/"counterpart") does not reach the
    deterministic comparison path today. Recorded here so a future change
    that intends to add "missing" support does so deliberately, against a
    failing test it turns green, rather than silently."""
    task = "List the deployed modules and identify anything missing from the config."
    assert _TWO_SIDED_TASK_RE.search(task) is None


def test_genuine_two_sided_comparison_unrelated_to_the_battery_triggers():
    """A comparison task in a wholly different domain (no endpoints, no
    hooks, no vouchers) still triggers -- confirms the mechanism is
    task-shape-general, not keyed to any project noun."""
    task = ("Enumerate both sides: every environment variable read in the "
            "deploy script, and every one defined in the .env template, then "
            "state which reads have no corresponding definition.")
    assert _TWO_SIDED_TASK_RE.search(task)


def test_one_sided_summary_request_does_not_trigger():
    """A plain one-sided descriptive question -- no enumeration of two sets,
    no completeness-gap language -- must not false-trigger."""
    task = "Summarize what the deploy script does."
    assert _TWO_SIDED_TASK_RE.search(task) is None


def test_completeness_conclusion_without_enumeration_language_does_not_trigger():
    """A task that only asks a yes/no question about coverage, without
    enumerating or comparing two named sets, should not be treated as a
    two-sided comparison task -- this regex targets the ENUMERATE-then-
    COMPARE shape, not any question that could theoretically be answered
    completely."""
    task = "Is every environment variable used somewhere in the codebase?"
    assert _TWO_SIDED_TASK_RE.search(task) is None
