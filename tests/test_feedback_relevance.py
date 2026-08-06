"""Regression tests: the self-improvement loop's failure-context injection must
be scoped by relevance, not just recency.

Confirmed live 2026-08-06: load_failure_context used to return the N most recent
failures for the project with NO relevance filtering at all. A day spent
correcting one specific SCSS namespace bug on the parties module posted several
/feedback corrections mentioning "statusBadge" and "parties.module.scss", and
those corrections were then injected VERBATIM into a completely unrelated
vouchers-module research task's coordinator instructions (labeled "read every
point before writing code"). The coordinator, dutifully following that
instruction, searched for "statusBadge" -- a term the vouchers task never
mentioned -- found an unrelated real occurrence in a different module's
stylesheet, and misattributed it to vouchers in its final answer.
"""
from swarm.feedback import _significant_tokens, _filter_relevant_failures


# ── _significant_tokens ───────────────────────────────────────────────────────

def test_significant_tokens_keeps_filenames():
    tokens = _significant_tokens("Fix the namespace mismatch in parties.module.scss")
    assert "parties.module.scss" in tokens


def test_significant_tokens_keeps_underscored_identifiers():
    tokens = _significant_tokens("Read vouchers_api.py and check the endpoints")
    assert "vouchers_api.py" in tokens


def test_significant_tokens_keeps_long_identifiers():
    tokens = _significant_tokens("The statusBadge class needs a prefix")
    assert "statusbadge" in tokens  # lowercased, >=6 chars


def test_significant_tokens_drops_generic_dev_words():
    tokens = _significant_tokens("Add the new class and fix the file")
    assert not tokens & {"add", "new", "class", "fix", "the", "and", "file"}


def test_significant_tokens_full_path_and_bare_filename_overlap():
    """Confirmed live 2026-08-06: a full repo-relative path in one text and a
    bare filename in another clearly name the same file, but the greedy token
    regex swallows a full path into ONE token distinct from the bare filename
    string -- without extracting the basename too, they'd never overlap."""
    full_path_tokens = _significant_tokens(
        "Read API/inventory-service/router/vouchers_api.py to research the module"
    )
    bare_filename_tokens = _significant_tokens("cited the wrong file for vouchers_api.py badges")

    assert full_path_tokens & bare_filename_tokens
    assert "vouchers_api.py" in full_path_tokens
    assert "vouchers_api.py" in bare_filename_tokens


# ── _filter_relevant_failures ─────────────────────────────────────────────────

def _failure(task, err_msg, err_type="correction"):
    return (task, err_type, err_msg)


def test_irrelevant_failure_is_excluded():
    """The exact live incident: a parties/statusBadge correction must not surface
    for an unrelated vouchers research task."""
    failures = [
        _failure(
            "Add a new CSS class named statusBadge to an existing .module.scss file",
            "bare $success used in parties.module.scss, but this file already "
            "references it as index.$success elsewhere",
        ),
    ]
    current_task = (
        "Read API/inventory-service/router/vouchers_api.py and "
        "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx"
    )

    out = _filter_relevant_failures(current_task, failures, limit=10)

    assert out == []


def test_relevant_failure_by_shared_filename_is_included():
    failures = [
        _failure(
            "Add a new CSS class named statusBadge to parties.module.scss",
            "namespace mismatch in parties.module.scss",
        ),
    ]
    current_task = "Read parties.module.scss and add a totalCount badge"

    out = _filter_relevant_failures(current_task, failures, limit=10)

    assert len(out) == 1


def test_mixed_pool_keeps_only_the_relevant_entries_ranked_first():
    failures = [
        _failure("Fix statusBadge namespace in parties.module.scss", "index.$success needed"),  # irrelevant to vouchers
        _failure("Read vouchers_api.py and list voucher types", "cited wrong file for vouchers_api.py badges"),  # relevant
        _failure("Add a totalCount to items_api.py response", "field name mismatch"),  # irrelevant
    ]
    current_task = "Read API/inventory-service/router/vouchers_api.py to research the Vouchers module"

    out = _filter_relevant_failures(current_task, failures, limit=10)

    assert len(out) == 1
    assert "vouchers_api.py" in out[0][0]


def test_no_current_task_falls_back_to_recency_only():
    """When there's no task text to score against, keep the old recency-only
    behaviour rather than silently injecting nothing."""
    failures = [_failure(f"task {i}", f"correction {i}") for i in range(3)]

    out = _filter_relevant_failures("", failures, limit=2)

    assert out == failures[:2]


def test_ties_are_broken_by_recency():
    failures = [
        _failure("Read parties.module.scss for badge A", "correction A"),  # index 0, most recent
        _failure("Read parties.module.scss for badge B", "correction B"),  # index 1, older
    ]
    current_task = "Read parties.module.scss again"

    out = _filter_relevant_failures(current_task, failures, limit=1)

    assert out == [failures[0]]  # both tie on overlap; more recent wins
