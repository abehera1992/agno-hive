"""Phase R -- Coordinator evidence integrity.

Forensics (Phase P/Q battery): T13a/T13b's dominant failure was never "no
evidence" -- it was a draft contradicting structured evidence the run already
held (compare_enumerations' own output, a db_schema listing, a failed glob).
Several existing checks already DETECT this shape correctly
(_table_claimed_missing_but_present, _contradicted_by_failed_lookup,
_miscounted_against_tool, _reconcile_completeness_claim_with_comparison's own
gap check) but most are disclosure-only footnotes threaded through _tail():
the wrong claim still ships as the answer's own primary text.

This phase adds exactly one new, narrow, LAST-RESORT guard --
swarm.team._evidence_integrity_check -- applied at _verified_answer's single
call site (the same "one choke point" placement _hoist_denied_premise already
uses immediately below it), never inside _verified_answer's own ~20-guard
chain. It reuses existing detectors unchanged (_contradicted_by_failed_lookup,
_miscounted_against_tool, _comparison_gap_counts/_reconcile_completeness_claims,
_computed_comparison) plus two new narrow structural checks (DB row count,
environment/branch facts) across the five categories this phase scopes. On a
genuine contradiction it makes ONE targeted reconciliation attempt, rechecks
deterministically, and forces explicit uncertainty if the retry does not
resolve it -- never a generic LLM judge, never touching compare_enumerations,
hive-mcp, the shared internal retry budget, or memory infrastructure.

Every test here is SIMULATED (constructed content/team state and a
monkeypatched _stream_team_run standing in for a live model) -- this project
has no live model available in this environment.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _EVIDENCE_INTEGRITY_FLAG,
    _evidence_integrity_check,
    _evidence_integrity_findings,
    _force_uncertainty_answer,
    _integrity_branch_contradiction,
    _integrity_db_count_contradiction,
    _integrity_env_fact_contradiction,
    _persist_evidence_integrity_trace,
)


def _run(coro):
    return asyncio.run(coro)


# ── 1-3. _integrity_db_count_contradiction (DB row counts) ------------------

def test_db_count_contradiction_detected():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "0 rows", "chars": 6},
    ])
    found = _integrity_db_count_contradiction(
        "The parties table contains 12,473 rows.", team)
    assert found == ("12473", "0")


def test_db_count_agreement_is_silent():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "0 rows", "chars": 6},
    ])
    assert _integrity_db_count_contradiction(
        "The parties table contains 0 rows.", team) is None


def test_db_count_no_db_tool_evidence_is_silent():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "get_file_content", "agent": "Researcher", "preview": "x", "chars": 1},
    ])
    assert _integrity_db_count_contradiction(
        "The parties table contains 12,473 rows.", team) is None


# ── 4-6. _integrity_env_fact_contradiction (environment facts) -------------

def test_env_python_version_contradiction_detected():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "get_env_info", "agent": "Executor",
         "preview": "OS: Linux 6.6.87.2-microsoft-standard-WSL2\nPython: 3.12.14",
         "chars": 60},
    ])
    found = _integrity_env_fact_contradiction(
        "This runs on Python 3.11.6 on Linux.", team)
    assert found == ("Python version", "3.11.6", "3.12.14")


def test_env_os_contradiction_detected():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "get_env_info", "agent": "Executor",
         "preview": "OS: Linux 6.6.87.2-microsoft-standard-WSL2", "chars": 40},
    ])
    found = _integrity_env_fact_contradiction("OS: Ubuntu 22.04.4 LTS", team)
    assert found == ("operating system", "Ubuntu 22.04.4 LTS",
                      "Linux 6.6.87.2-microsoft-standard-WSL2")


def test_env_agreement_is_silent():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "get_env_info", "agent": "Executor",
         "preview": "OS: Linux 6.6.87.2\nPython: 3.12.14", "chars": 40},
    ])
    assert _integrity_env_fact_contradiction("OS: Linux, Python 3.12.14", team) is None


# ── 7-8. _integrity_branch_contradiction ------------------------------------

def test_branch_contradiction_detected():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "git_status", "agent": "Executor",
         "preview": "On branch abehera/Ekam-webApp, clean", "chars": 30},
    ])
    found = _integrity_branch_contradiction(
        "The current branch is `main`.", team)
    assert found == ("main", "abehera/Ekam-webApp")


def test_branch_agreement_is_silent():
    team = SimpleNamespace(_tool_evidence=[
        {"name": "git_status", "agent": "Executor",
         "preview": "On branch main, clean", "chars": 20},
    ])
    assert _integrity_branch_contradiction("The current branch is `main`.", team) is None


# ── 9-13. _evidence_integrity_findings (all five categories combined) -------

def _team_with_globs(missed_pattern="API/business-service/clients/*.py"):
    return SimpleNamespace(
        _read_state={"globs": [{"pattern": missed_pattern, "missed": True}]},
        _tool_evidence=[],
    )


@pytest.mark.asyncio
async def test_findings_file_path_existence_category():
    team = _team_with_globs()
    content = "The `API/business-service/clients/kafka_client.py` file handles this."
    findings = await _evidence_integrity_findings(
        content, "task", team, None, None, "")
    assert any(f["category"] == "file/path existence" for f in findings)


@pytest.mark.asyncio
async def test_findings_enumeration_count_category(monkeypatch):
    team = SimpleNamespace(_read_state={}, _tool_evidence=[])

    async def fake_miscounted(content, url, tools):
        return "vouchers_api.py has 9 endpoints, not 6"
    monkeypatch.setattr(team_mod, "_miscounted_against_tool", fake_miscounted)

    findings = await _evidence_integrity_findings(
        "There are 6 endpoints.", "task", team, "url", None, "")
    assert any(f["category"] == "file enumeration/count" for f in findings)


@pytest.mark.asyncio
async def test_findings_comparison_gap_category():
    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    cmp_note = (
        "\n\n---\n**THE COMPARISON, COMPUTED**\n```\nTOTALS: left 9, right 5, "
        "matched 5, left-only 4, right-only 0.\n```")
    findings = await _evidence_integrity_findings(
        "There are no gaps between the backend and frontend.",
        "task", team, None, None, cmp_note)
    assert any(f["category"] == "comparison completeness/gap" for f in findings)


@pytest.mark.asyncio
async def test_findings_db_row_count_category():
    team = SimpleNamespace(_read_state={}, _tool_evidence=[
        {"name": "db_query", "agent": "Researcher", "preview": "0 rows", "chars": 6}])
    findings = await _evidence_integrity_findings(
        "The table has 12,473 rows.", "task", team, None, None, "")
    assert any(f["category"] == "DB row count" for f in findings)


@pytest.mark.asyncio
async def test_findings_environment_category():
    team = SimpleNamespace(_read_state={}, _tool_evidence=[
        {"name": "get_env_info", "agent": "Executor",
         "preview": "OS: Linux\nPython: 3.12.14", "chars": 30}])
    findings = await _evidence_integrity_findings(
        "This runs on Python 3.11.6.", "task", team, None, None, "")
    assert any(f["category"] == "environment fact (Python version)" for f in findings)


@pytest.mark.asyncio
async def test_findings_empty_when_nothing_contradicts():
    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    findings = await _evidence_integrity_findings(
        "A perfectly ordinary, uncontroversial answer.", "task", team, None, None, "")
    assert findings == []


# ── 14. _force_uncertainty_answer --------------------------------------------

def test_force_uncertainty_preserves_original_content_and_names_findings():
    findings = [{"category": "DB row count", "claimed": "12473 rows", "real": "0 rows"}]
    forced = _force_uncertainty_answer("The table has 12,473 rows.", findings)
    assert "UNRESOLVED" in forced
    assert "12473 rows" in forced
    assert "0 rows" in forced
    assert "The table has 12,473 rows." in forced  # original text preserved verbatim


# ── 15-20. _evidence_integrity_check (the orchestrating guard) --------------

def _team_for_check(**kw):
    kw.setdefault("_read_state", {})
    kw.setdefault("_tool_evidence", [])
    return SimpleNamespace(**kw)


@pytest.mark.asyncio
async def test_no_contradiction_is_a_pure_passthrough(monkeypatch):
    calls = {"n": 0}

    async def fake_stream(*a, **k):
        calls["n"] += 1
        return "should never be called", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check()
    content = "A perfectly ordinary, uncontroversial answer."
    result = await _evidence_integrity_check(content, "task", team, None, None)
    assert result == content
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_synthesis_run_never_checked(monkeypatch):
    async def fake_stream(*a, **k):
        raise AssertionError("must not run on a synthesis run")
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(
        content, "task", team, None, None, synthesis_run=True)
    assert result == content


@pytest.mark.asyncio
async def test_runs_at_most_once_per_team(monkeypatch):
    calls = {"n": 0}

    async def fake_stream(*a, **k):
        calls["n"] += 1
        return "0 rows now, corrected.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    first = await _evidence_integrity_check(content, "task", team, None, None)
    second = await _evidence_integrity_check(content, "task", team, None, None)
    assert calls["n"] == 1          # second call is a no-op, flag already set
    assert second == content        # unchanged -- the flag short-circuits it
    assert getattr(team, _EVIDENCE_INTEGRITY_FLAG) is True


@pytest.mark.asyncio
async def test_retry_that_resolves_the_contradiction_is_adopted(monkeypatch):
    async def fake_stream(*a, **k):
        return "The table has 0 rows.", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(content, "task", team, None, None)
    assert result == "The table has 0 rows."


@pytest.mark.asyncio
async def test_retry_that_still_contradicts_forces_uncertainty(monkeypatch):
    async def fake_stream(*a, **k):
        return "The table has 500 rows.", object()   # still wrong
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(content, "task", team, None, None)
    assert "UNRESOLVED" in result
    assert "500" in result
    assert "0 rows" in result


@pytest.mark.asyncio
async def test_retry_exception_forces_uncertainty_not_a_crash(monkeypatch):
    async def fake_stream(*a, **k):
        raise RuntimeError("backend unavailable")
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(content, "task", team, None, None)
    assert "UNRESOLVED" in result


@pytest.mark.asyncio
async def test_retry_returning_nothing_forces_uncertainty(monkeypatch):
    async def fake_stream(*a, **k):
        return "", object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(content, "task", team, None, None)
    assert "UNRESOLVED" in result


@pytest.mark.asyncio
async def test_retry_returning_the_canned_budget_exhausted_answer_forces_uncertainty(monkeypatch):
    async def fake_stream(*a, **k):
        return team_mod._BUDGET_EXHAUSTED_ANSWER, object()
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check(_tool_evidence=[
        {"name": "db_query", "agent": "R", "preview": "0 rows", "chars": 6}])
    content = "The table has 12,473 rows."
    result = await _evidence_integrity_check(content, "task", team, None, None)
    assert "UNRESOLVED" in result


@pytest.mark.asyncio
async def test_empty_content_is_a_no_op(monkeypatch):
    async def fake_stream(*a, **k):
        raise AssertionError("must not be called for empty content")
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    team = _team_for_check()
    result = await _evidence_integrity_check("", "task", team, None, None)
    assert result == ""


# ── 21-23. _persist_evidence_integrity_trace (fail-open, Phase E reuse) -----

@pytest.mark.asyncio
async def test_no_run_context_is_a_silent_no_op(monkeypatch):
    called = {"n": 0}

    async def fake_persist_claim(*a, **k):
        called["n"] += 1
    monkeypatch.setattr(team_mod.execution_store, "persist_claim", fake_persist_claim)

    team = SimpleNamespace()   # no _run_context attribute at all
    await _persist_evidence_integrity_trace(
        team, [{"category": "DB row count", "claimed": "x", "real": "y"}],
        resolved=False, retried=True)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_no_findings_is_a_silent_no_op(monkeypatch):
    called = {"n": 0}

    async def fake_persist_claim(*a, **k):
        called["n"] += 1
    monkeypatch.setattr(team_mod.execution_store, "persist_claim", fake_persist_claim)

    team = SimpleNamespace(_run_context=object())
    await _persist_evidence_integrity_trace(team, [], resolved=True, retried=False)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_findings_with_a_run_context_persist_one_claim_each(monkeypatch):
    persisted = []

    async def fake_persist_claim(run_context, statement, status, **kw):
        persisted.append((statement, status))
    monkeypatch.setattr(team_mod.execution_store, "persist_claim", fake_persist_claim)

    team = SimpleNamespace(_run_context=object())
    findings = [
        {"category": "DB row count", "claimed": "12473 rows", "real": "0 rows"},
        {"category": "environment fact (Python version)", "claimed": "3.11.6", "real": "3.12.14"},
    ]
    await _persist_evidence_integrity_trace(team, findings, resolved=False, retried=True)
    assert len(persisted) == 2
    assert all(status == "contradicted" for _, status in persisted)
    assert persisted[0][0] == "DB row count: claimed '12473 rows'"


@pytest.mark.asyncio
async def test_resolved_findings_persist_as_supported(monkeypatch):
    persisted = []

    async def fake_persist_claim(run_context, statement, status, **kw):
        persisted.append(status)
    monkeypatch.setattr(team_mod.execution_store, "persist_claim", fake_persist_claim)

    team = SimpleNamespace(_run_context=object())
    findings = [{"category": "DB row count", "claimed": "12473 rows", "real": "0 rows"}]
    await _persist_evidence_integrity_trace(team, findings, resolved=True, retried=True)
    assert persisted == ["supported"]


@pytest.mark.asyncio
async def test_persistence_failure_never_raises(monkeypatch):
    async def failing_persist_claim(*a, **k):
        raise RuntimeError("db unreachable")
    monkeypatch.setattr(team_mod.execution_store, "persist_claim", failing_persist_claim)

    team = SimpleNamespace(_run_context=object())
    findings = [{"category": "DB row count", "claimed": "12473 rows", "real": "0 rows"}]
    # execution_store.guard() is what makes this fail-open -- not mocked here,
    # so this exercises the REAL guard() wrapping the REAL persist_claim call.
    await _persist_evidence_integrity_trace(team, findings, resolved=False, retried=True)
    # No exception reaching here IS the assertion.


# ── 24. Read-only with respect to the durable Claim/comparison machinery ----

def test_source_never_writes_directly_only_via_persist_claim():
    import inspect
    src = inspect.getsource(team_mod._persist_evidence_integrity_trace)
    assert "execution_store.persist_claim" in src
    assert "execution_store.guard" in src
    for forbidden in (".insert(", ".update(", ".delete(", "execution_store.promote"):
        assert forbidden not in src


def test_evidence_integrity_check_never_calls_compare_enumerations_directly():
    """Rule 6 in this phase's own prompt: keep compare_enumerations' behavior
    untouched. This module never calls the hive-mcp tool itself -- only
    _computed_comparison, the same pre-existing wrapper
    _reconcile_completeness_claim_with_comparison already uses. (The function's
    own comments mention compare_enumerations by name to explain that choice --
    checked for an actual call, "compare_enumerations(", not the bare word.)"""
    import inspect
    src = inspect.getsource(team_mod._evidence_integrity_check)
    assert "compare_enumerations(" not in src
    assert "_computed_comparison(" in src


# ── 25-31. Named-item and zero-claim variants (added after a live Phase R  --
# rerun found the ORIGINAL category-3 check, alone, caught 0 of 4 textbook
# contradictions in a 10-run T13a/T13b battery -- see
# _integrity_named_item_falsely_gapped's own docstring for the exact battery
# evidence these tests reproduce.) -----------------------------------------

from swarm.team import (  # noqa: E402
    _integrity_comparison_zero_claim_contradiction,
    _integrity_named_item_falsely_gapped,
)

_REAL_CMP_BODY = """compare_enumerations — API/inventory-service/router/vouchers_api.py  vs  Client/.../inventoryApi.ts
join: HTTP method + path-boundary suffix match (exact string, no inference)

LEFT — @router routes in API/inventory-service/router/vouchers_api.py (9):
  GET /vouchers
  GET /vouchers/{voucher_id}
  POST /vouchers

RIGHT — RTK Query endpoints in Client/.../inventoryApi.ts (5):
  GET /api/inventoryservice/vouchers
  GET /api/inventoryservice/vouchers/{voucher_id}
  POST /api/inventoryservice/vouchers

MATCHED (3):
  GET /vouchers   <->   useGetVouchersQuery
  GET /vouchers/{voucher_id}   <->   useGetVoucherQuery
  POST /vouchers   <->   useCreateVoucherMutation

LEFT ONLY — defined on the left with no match on the right (6):
  PUT /vouchers/{voucher_id}/post
  PUT /vouchers/{voucher_id}/cancel

RIGHT ONLY — present on the right with no match on the left (0):
  (none)

HOOKS EXPORTED BY Client/.../inventoryApi.ts (5):
  useCancelVoucherMutation
  useCreateVoucherMutation
  useGetVoucherQuery
  useGetVouchersQuery
  usePostVoucherMutation

TOTALS: left 9, right 5, matched 3, left-only 6, right-only 0."""


def _cmp_note(body: str = _REAL_CMP_BODY) -> str:
    return f"\n\n---\n**THE COMPARISON, COMPUTED**\n```\n{body}\n```"


def test_named_matched_endpoint_falsely_called_a_gap():
    """T13b live: `/vouchers/{voucher_id}` (GET) named a gap two paragraphs
    above the tool's own MATCHED(3) list that pairs it."""
    content = (
        "Gaps identified: GET /vouchers/{voucher_id} has no corresponding "
        "frontend hook."
    )
    found = _integrity_named_item_falsely_gapped(content, _cmp_note())
    assert found is not None
    name, real = found
    assert name == "/vouchers/{voucher_id}"
    assert "MATCHED" in real


def test_named_matched_hook_falsely_called_a_gap():
    """T13b live: claimed only one hook is defined, against the tool's own
    HOOKS EXPORTED list."""
    content = "useCreateVoucherMutation is missing from the frontend."
    found = _integrity_named_item_falsely_gapped(content, _cmp_note())
    assert found == ("useCreateVoucherMutation",
                      "compare_enumerations' own MATCHED/HOOKS EXPORTED output "
                      "lists this as present")


def test_named_item_check_silent_when_gap_claim_names_a_genuine_left_only_item():
    """The known, EXPECTED non-fix (Phase R's own explicit constraint): PUT
    /vouchers/{id}/post is genuinely LEFT ONLY (never in MATCHED or HOOKS
    EXPORTED), so calling it a gap is not a contradiction of this tool's own
    output and must stay silent."""
    content = "PUT /vouchers/{voucher_id}/post is a gap with no frontend hook."
    assert _integrity_named_item_falsely_gapped(content, _cmp_note()) is None


def test_named_item_check_silent_with_no_gap_claiming_language():
    content = "GET /vouchers/{voucher_id} maps to useGetVoucherQuery."
    assert _integrity_named_item_falsely_gapped(content, _cmp_note()) is None


def test_named_item_check_silent_with_no_comparison_note():
    content = "GET /vouchers/{voucher_id} is missing a hook."
    assert _integrity_named_item_falsely_gapped(content, "") is None


def test_zero_claim_contradicted_by_totals():
    """T13a live: 'no endpoints found, no hooks found' against TOTALS left=9,
    matched=2 (represented here via the same real body's left=9, matched=3)."""
    content = "No endpoints found, no hooks found in this module."
    found = _integrity_comparison_zero_claim_contradiction(content, _cmp_note())
    assert found is not None
    claimed, real = found
    assert "left=9" in real


def test_zero_claim_silent_when_totals_are_genuinely_zero():
    body = _REAL_CMP_BODY.rsplit("TOTALS:", 1)[0] + "TOTALS: left 0, right 0, matched 0, left-only 0, right-only 0."
    content = "No endpoints found in this module."
    assert _integrity_comparison_zero_claim_contradiction(content, _cmp_note(body)) is None


@pytest.mark.asyncio
async def test_findings_catches_named_item_variant_end_to_end():
    """The exact live-battery failure shape (T13b), run through the full
    _evidence_integrity_findings entry point, not just the isolated helper."""
    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    content = "Gaps identified: GET /vouchers/{voucher_id} has no corresponding hook."
    findings = await _evidence_integrity_findings(
        content, "task", team, None, None, _cmp_note())
    assert any(f["category"] == "comparison completeness/gap" for f in findings)


# ── 32-33. Two more wordings, found in a SECOND live battery rerun ----------
# (Phase R follow-up 2, same day): the first widening (above) still missed
# "no frontend list function" (a qualifier between "no" and the noun) and "no
# other frontend hooks ... were found" (same shape, in the zero-claim check).

def test_named_item_check_catches_no_qualified_noun_function_phrasing():
    content = "GET /vouchers has no frontend list function."
    found = _integrity_named_item_falsely_gapped(content, _cmp_note())
    assert found == ("/vouchers",
                      "compare_enumerations' own MATCHED/HOOKS EXPORTED output "
                      "lists this as present")


def test_zero_claim_catches_qualified_noun_phrasing():
    content = "No other frontend hooks or functions were found for this module."
    found = _integrity_comparison_zero_claim_contradiction(content, _cmp_note())
    assert found is not None
    assert "left=9" in found[1]


# ── Phase Z2 (2026-09-26) — list-item lookahead widening, Tests A-F ─────────
# _integrity_named_item_falsely_gapped's docstring documents the T13b live
# shape that motivated this widening: a gap-declaring header, a colon-
# terminated sub-header, a blank line, THEN a bulleted list -- none of which
# alone satisfied the original same-line check. These tests use a synthetic,
# deliberately non-vouchers fixture (widgets/activate/deactivate) to prove
# the widening is general-purpose, not keyed to T13b's own endpoint/hook
# names, per Phase Z2's explicit constraint against task-specific hard-coding.
#
# Two of the LEFT-only endpoints below (activate/deactivate) have their hook
# names listed in HOOKS EXPORTED anyway -- this mirrors the real T13b shape,
# where compare_enumerations' HOOKS EXPORTED block is derived independently
# of its endpoint-side LEFT ONLY/MATCHED/PARTIAL MATCH classification, so a
# hook can be confirmed present even when its paired endpoint's own
# comparison bucket lags or disagrees.

_GENERIC_CMP_BODY = """compare_enumerations — a/left_module.py  vs  b/right_module.ts
join: HTTP method + path-boundary suffix match (exact string, no inference)

LEFT — @router routes in a/left_module.py (5):
  GET /widgets
  POST /widgets
  PUT /widgets/{widget_id}/activate
  PUT /widgets/{widget_id}/deactivate
  DELETE /widgets/{widget_id}

RIGHT — RTK Query endpoints in b/right_module.ts (3):
  GET /api/widgets
  POST /api/widgets
  DELETE /api/widgets/{widget_id}

MATCHED (2):
  GET /widgets   <->   useGetWidgetsQuery
  POST /widgets   <->   useCreateWidgetMutation

LEFT ONLY — defined on the left with no match on the right (3):
  PUT /widgets/{widget_id}/activate
  PUT /widgets/{widget_id}/deactivate
  DELETE /widgets/{widget_id}

RIGHT ONLY — present on the right with no match on the left (0):
  (none)

HOOKS EXPORTED BY b/right_module.ts (4):
  useActivateWidgetMutation
  useCreateWidgetMutation
  useDeactivateWidgetMutation
  useGetWidgetsQuery

TOTALS: left 5, right 3, matched 2, left-only 3, right-only 0."""


def _generic_cmp_note(body: str = _GENERIC_CMP_BODY) -> str:
    return f"\n\n---\n**THE COMPARISON, COMPUTED**\n```\n{body}\n```"


def test_a_header_colon_intro_blank_then_list_is_scanned():
    """The exact widened shape: gap header, colon-terminated sub-header,
    blank line, then a bulleted list naming hooks HOOKS EXPORTED confirms
    exist. Must be caught even though no single line has both the gap
    keyword and the confirmed name."""
    content = (
        "#### Backend-Frontend Gap Analysis\n"
        "**Backend endpoints with no corresponding frontend hook:**\n"
        "\n"
        "- `useActivateWidgetMutation`\n"
        "- `useDeactivateWidgetMutation`\n"
    )
    found = _integrity_named_item_falsely_gapped(content, _generic_cmp_note())
    assert found is not None
    name, real = found
    assert name == "useActivateWidgetMutation"
    assert "HOOKS EXPORTED" in real or "MATCHED" in real


def test_b_lookahead_is_bounded_and_does_not_wander_past_the_cap():
    """More than _GAP_LIST_LOOKAHEAD blank lines between the gap-declaring
    line and the list must NOT be bridged -- proves the skip is capped, not
    an unbounded scan forward through the rest of the answer. The bullet
    itself deliberately carries no gap-claim language of its own (a bare
    name mention), so the only way this could be found is via the header's
    own lookahead reaching it -- isolating exactly what this test targets."""
    padding = "\n" * 10  # well past _GAP_LIST_LOOKAHEAD (5)
    content = (
        "The following endpoints are missing frontend coverage:" + padding +
        "- `useActivateWidgetMutation`\n"
    )
    assert _integrity_named_item_falsely_gapped(content, _generic_cmp_note()) is None


def test_c_a_real_content_line_stops_the_lookahead():
    """A non-blank, non-colon-terminated, non-list-item line between the
    gap-declaring line and the list must halt the skip immediately -- the
    widening must never read through unrelated prose to reach a later list,
    only through blank lines and colon-terminated intro lines. As in Test B,
    the bullet carries no gap-claim language of its own."""
    content = (
        "The following endpoints are missing frontend coverage:\n"
        "This paragraph is unrelated explanatory prose, not a list intro.\n"
        "- `useActivateWidgetMutation`\n"
    )
    assert _integrity_named_item_falsely_gapped(content, _generic_cmp_note()) is None


def test_d_mixed_list_a_genuine_gap_does_not_mask_a_falsely_gapped_sibling():
    """A list with one genuinely-absent item followed by one falsely-gapped
    item must still catch the second -- the scan checks every list-item
    line, it does not stop or get satisfied at the first non-match."""
    content = (
        "The following endpoints are missing frontend coverage:\n"
        "\n"
        "- `DELETE /widgets/{widget_id}` -- genuinely no frontend hook\n"
        "- `useActivateWidgetMutation`\n"
    )
    found = _integrity_named_item_falsely_gapped(content, _generic_cmp_note())
    assert found is not None
    assert found[0] == "useActivateWidgetMutation"


@pytest.mark.asyncio
async def test_e_generic_t13b_shaped_case_end_to_end_proves_generality():
    """The full _evidence_integrity_findings entry point, not just the
    isolated helper, on a synthetic fixture that shares T13b's *shape*
    (header + colon intro + blank + bulleted list; some items partial-
    overlap, not full 1:1 matches) but none of its literal names -- proof
    this is a general reconciliation fix, not one keyed to voucher/post/
    cancel specifically."""
    team = SimpleNamespace(_read_state={}, _tool_evidence=[])
    content = (
        "#### Backend-Frontend Gap Analysis\n"
        "**Backend endpoints with no corresponding frontend hook:**\n"
        "\n"
        "- `useDeactivateWidgetMutation`\n"
    )
    findings = await _evidence_integrity_findings(
        content, "task", team, None, None, _generic_cmp_note())
    assert any(f["category"] == "comparison completeness/gap" for f in findings)


def test_f_no_false_positive_on_a_correct_answer_with_an_unrelated_bulleted_list():
    """A bulleted list that follows a line which merely CONTAINS the
    substring 'gap' in an unrelated, non-gap-claiming sense (or a list of
    genuinely-confirmed items with no gap language at all) must not trigger
    -- the widening only activates on an actual _GAP_CLAIM_LINE_RE match,
    never merely because a list follows some line."""
    content = (
        "Matched endpoints:\n"
        "\n"
        "- `useActivateWidgetMutation`\n"
        "- `useGetWidgetsQuery`\n"
    )
    assert _integrity_named_item_falsely_gapped(content, _generic_cmp_note()) is None
