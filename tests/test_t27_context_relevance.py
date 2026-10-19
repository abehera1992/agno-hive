"""Phase T27 -- relevance gate for T26's inherited-context block.

T26 (commit 18e97de5) attached team._session_summary to every
delegate_structured_task call whenever one existed, unconditionally. Live
validation on ZGX found this correctly rescued a dependent follow-up (the
real T23 incident shape) but also bled an unsolicited, redundant voucher
comparison into an UNRELATED delegation (a hardcoded-credentials check on
cleanup.py) that had nothing to do with the prior turn.

T27 adds _inherited_context_is_relevant as a gate in front of the existing
T26 attachment, reusing two already-available, deterministic signals rather
than a new relevance classifier (T19 already established no reliable
programmatic "is this content relevant to this new objective" signal
exists): continuity language in the TURN'S OWN top-level task text
(team._task, new in T27), and/or the delegation naming one of the session's
own known file basenames (already extracted by _extract_handoff_summary's
"Files referenced:" line). See _inherited_context_is_relevant's own
docstring for the full rationale and why the signal has to be checked
against team._task rather than the per-delegation target/objective (a vague,
pathless objective is exactly the T23 failure shape this must still match).

Tests 1-7 exercise the pure gate function directly, covering the mission's
required dependent / partially-dependent / unrelated / ambiguous cases.
Tests 8-9 exercise the REAL delegate_structured_task production call shape
(same harness as test_phase_s_structural_delegation.py) to prove the gate
is wired at the actual boundary -- the member's real received task text,
not just the gate function in isolation.
"""
from unittest.mock import patch

import agno.team._default_tools as agno_default_tools
import swarm.team as team_mod
from swarm.team import (
    _build_team, _inherited_context_is_relevant, _session_summary_file_basenames,
)

from tests.test_phase_s_structural_delegation import (
    _call_production_path, _run_entrypoint,
)

_SUMMARY = (
    "── Chain handoff (2026-10-19T12:00:00Z) ──\n"
    "Task: Compare vouchers_api.py vs inventoryApi.ts\n"
    "Status: COMPLETE\n"
    "Files referenced: API/inventory-service/router/vouchers_api.py, "
    "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts\n"
    "Key outcomes:\n"
    "  - 4 gaps: grn, credit-note, stock-adjustment, stock-transfer\n"
)


# ── 1-7. The pure gate function ─────────────────────────────────────────────

def test_dependent_followup_with_reflective_language_is_relevant():
    """The real T23/T26 Test A shape: a vague, pathless per-delegation
    objective, but the TURN'S OWN task plainly reflects on prior work."""
    assert _inherited_context_is_relevant(
        _SUMMARY,
        "Reconsider the gap analysis you just produced. Based only on the "
        "evidence already gathered in this run, does it establish anything?",
        "evidence gathered in this run", "Review the evidence gathered in this run",
        "x", "y",
    ) is True


def test_partially_dependent_followup_naming_known_files_is_relevant():
    """No reflective language in the per-delegation objective, but it names
    one of the session's own known files directly."""
    assert _inherited_context_is_relevant(
        _SUMMARY, "What does vouchers_api.py's test coverage look like?",
        "API/inventory-service/router/vouchers_api.py", "check test coverage",
        "x", "y",
    ) is True


def test_unrelated_task_naming_a_different_file_is_not_relevant():
    """The live T26 incident this phase fixes: an unrelated task, explicitly
    marked as separate, naming its own different target file."""
    assert _inherited_context_is_relevant(
        _SUMMARY,
        "Separately, check whether API/storage-service/cleanup.py has any "
        "hardcoded credentials or secrets.",
        "API/storage-service/cleanup.py", "check for hardcoded credentials",
        "x", "y",
    ) is False


def test_ambiguous_task_with_neither_signal_defaults_to_not_relevant():
    """Mission requirement D: ambiguity is never proof of dependency --
    the conservative default is exclusion, not inclusion."""
    assert _inherited_context_is_relevant(
        _SUMMARY, "What does the storage service do in general?",
        "storage-service", "describe its responsibilities", "x", "y",
    ) is False


def test_fresh_call_with_no_session_summary_is_never_relevant():
    """A first-turn (non-chained) call has nothing to attach regardless of
    what the task text says -- matches _build_canonical_researcher_task's
    own no-op-on-empty-inherited_context behavior."""
    assert _inherited_context_is_relevant(
        None, "Reconsider the evidence already gathered.",
        "x", "y", "z", "w",
    ) is False
    assert _inherited_context_is_relevant(
        "", "Reconsider the evidence already gathered.", "x", "y", "z", "w",
    ) is False


def test_relevance_signal_also_checked_against_delegation_fields_not_only_task():
    """A known file named in the delegation's OWN objective (not the
    top-level task) still counts -- the file-overlap signal is OR'd across
    both sources, not task-text-only."""
    assert _inherited_context_is_relevant(
        _SUMMARY, "Unrelated top-level phrasing with no reflective language",
        "target", "check inventoryApi.ts for something new", "x", "y",
    ) is True


def test_session_summary_file_basenames_extracts_and_lowercases():
    names = _session_summary_file_basenames(_SUMMARY)
    assert names == {"vouchers_api.py", "inventoryapi.ts"}
    assert _session_summary_file_basenames(None) == set()
    assert _session_summary_file_basenames("no files line at all") == set()


# ── 8-9. The real delegate_structured_task production call shape ───────────

def _structured_team_with_context(session_summary, top_level_task):
    team = _build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate", mcp_list=[], instructions=[],
    )
    team._session_summary = session_summary
    team._task = top_level_task
    return team


async def _delegate_and_capture_task(team, objective_task_text):
    """Drives the REAL delegate_structured_task entrypoint (through the real
    production call shape) with a dependent-looking per-delegation objective,
    and returns the exact `task` string the (faked) underlying agno engine
    received -- i.e. the real member input, not an inference from it."""
    calls = []
    new_function, _ = _call_production_path(team, calls)
    await _run_entrypoint(
        new_function, member_id="researcher",
        target="x", objective=objective_task_text,
        evidence_required="y", completion_criteria="z",
    )
    assert len(calls) == 1
    return calls[0]["task"]


def test_dependent_turn_attaches_inherited_context_at_the_real_boundary():
    import asyncio
    team = _structured_team_with_context(
        _SUMMARY,
        "Reconsider the gap analysis you just produced. Based only on the "
        "evidence already gathered in this run, does it establish anything?",
    )
    task_text = asyncio.run(_delegate_and_capture_task(team, "review prior findings"))
    assert "INHERITED CONTEXT" in task_text
    assert "vouchers_api.py" in task_text


def test_unrelated_turn_does_not_attach_inherited_context_at_the_real_boundary():
    import asyncio
    team = _structured_team_with_context(
        _SUMMARY,
        "Separately, check whether API/storage-service/cleanup.py has any "
        "hardcoded credentials or secrets.",
    )
    task_text = asyncio.run(
        _delegate_and_capture_task(team, "check for hardcoded credentials"))
    assert "INHERITED CONTEXT" not in task_text
    assert "vouchers_api.py" not in task_text
