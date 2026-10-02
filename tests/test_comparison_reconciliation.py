"""Phase 2B (AGNOHive Reliability Program): make the deterministic comparison
decision-relevant BEFORE the coordinator's answer is finalised, instead of
only appending it as a trailing footnote after an already-wrong conclusion.

Phase 2's forensic trace found the exact failure this closes, live on R4/R6
T2: the coordinator's own text-generation completed and committed "There are
no missing or mismatched items." BEFORE _computed_comparison ever ran; the
deterministic "LEFT ONLY (6)" result was then merely concatenated onto the
end of the same message. The evidence never participated in the decision.

These tests exercise _reconcile_completeness_claim_with_comparison directly
(the new mechanism) and, for the central claim of this phase, drive the real
_verified_answer() end-to-end so the FINAL returned content -- not just the
presence of a comparison string somewhere in it -- is asserted against.
"""
import asyncio
from types import SimpleNamespace

import pytest

import dataclasses

import swarm.team as team_mod
from swarm.execution_context import RunContext
from swarm.team import (
    Claim,
    _comparison_body,
    _comparison_gap_counts,
    _COMPARISON_RECONCILE_FLAG,
    _create_comparison_claim,
    _get_claim_store,
    _get_evidence_ledger,
    _make_claim,
    _record_comparison_evidence,
    _reconcile_completeness_claim_with_comparison,
    _reconcile_completeness_claims,
    _validate_claim,
    _validate_claim_evidence,
    _verified_answer,
)

NO_GAP_CMP_NOTE = (
    "\n\n---\n**THE COMPARISON, COMPUTED — the answer above states a "
    "relationship between two files; this is that same relationship worked "
    "out by string match over both, not by reading and comparing. Where the "
    "two disagree, trust this one.**\n```\n"
    "compare_enumerations — a.py  vs  b.ts\n"
    "TOTALS: left 2, right 2, matched 2, left-only 0, right-only 0.\n"
    "```"
)
GAP_CMP_NOTE = (
    "\n\n---\n**THE COMPARISON, COMPUTED — the answer above states a "
    "relationship between two files; this is that same relationship worked "
    "out by string match over both, not by reading and comparing. Where the "
    "two disagree, trust this one.**\n```\n"
    "compare_enumerations — API/business-service/router/business_api.py  vs  "
    "Client/.../business/businessApi.ts\n"
    "join: HTTP method + path-boundary suffix match (exact string, no inference)\n"
    "\n"
    "LEFT ONLY — defined on the left with no match on the right (6):\n"
    "  POST /register\n"
    "  GET /status/{user_id}\n"
    "  POST /register-additional\n"
    "  GET /internal/ondc-domains\n"
    "  GET /internal/tenants/names\n"
    "  POST /{business_id}/documents\n"
    "\n"
    "TOTALS: left 13, right 16, matched 7, left-only 6, right-only 9.\n"
    "```"
)
NO_GAPS_CLAIM_CONTENT = (
    "### Endpoints:\n- POST /register\n- GET /status\n\n"
    "### Hooks:\n- useGetBusinessStatusQuery\n\n"
    "### Gap Analysis:\nThere are no missing or mismatched items."
)
NAMED_GAPS_CONTENT = (
    "### Endpoints:\n- POST /register\n\n### Gap Analysis:\n"
    "6 endpoints have no corresponding hook: /register, /status/{user_id}, "
    "/register-additional, /internal/ondc-domains, /internal/tenants/names, "
    "/{business_id}/documents."
)


def _run(coro):
    return asyncio.run(coro)


class _Team:
    """Bare stand-in -- _run_read_count/_member_reads_delta both degrade to
    -1/0 for an object with no _read_state, which _more_grounded then treats
    as 'could not tell' -> always adopt. Nothing else in the reconciliation
    path touches the team object directly."""


# ── unit tests: _reconcile_completeness_claim_with_comparison directly ──────


def test_1_real_gap_makes_comparison_available_and_triggers_reconciliation(monkeypatch):
    """Deterministic comparison finds real differences -> the reconciliation
    step actually calls back into the pipeline with that evidence, rather
    than silently skipping."""
    captured = {}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        captured["prompt"] = prompt
        return NAMED_GAPS_CONTENT, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None,
        None, GAP_CMP_NOTE, False))
    assert reconciled is True
    assert "prompt" in captured
    # The evidence itself -- not a paraphrase -- must reach the retry prompt.
    assert "LEFT ONLY" in captured["prompt"]
    assert "6 item(s) on the left" in captured["prompt"]


def test_2_no_gaps_claim_contradicted_by_six_gaps_final_output_is_not_unreconciled(monkeypatch):
    """The central Phase 2B assertion: the FINAL content must not remain the
    unreconciled 'no gaps' claim once a real, adopted retry has run."""
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return NAMED_GAPS_CONTENT, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None,
        None, GAP_CMP_NOTE, False))
    assert reconciled is True
    assert content == NAMED_GAPS_CONTENT
    assert "no missing or mismatched items" not in content
    assert "6 endpoints have no corresponding hook" in content


def test_3_no_differences_normal_positive_conclusion_is_left_alone(monkeypatch):
    """Deterministic comparison finds NO differences -> a genuinely positive
    conclusion must not be disturbed or retried."""
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not retry when the comparison found no gap")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "All endpoints have a corresponding hook. There are no missing items.",
        "task text", _Team(), all_results, None, None, NO_GAP_CMP_NOTE, False))
    assert reconciled is False
    assert called["n"] == 0
    assert content == "All endpoints have a corresponding hook. There are no missing items."


def test_4_comparison_skipped_existing_behaviour_unchanged(monkeypatch):
    """No comparison ran at all (cmp_note == "") -- e.g. Phase 2A's own
    'no safe pair' skip, or a one-sided task. Must be a pure no-op."""
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not retry when no comparison ran")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None, None, "", False))
    assert reconciled is False
    assert called["n"] == 0
    assert content == NO_GAPS_CLAIM_CONTENT


def test_5_ambiguous_no_safe_pair_is_also_a_pure_skip(monkeypatch):
    """Phase 2A's 'no safe pair' outcome IS an empty cmp_note (the function
    returns "" via _skip) -- same path as test 4, named for the Phase 2A
    scenario explicitly, so a future change to either phase's skip shape is
    caught by the right-named test."""
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not retry on an ambiguous/no-safe-pair skip")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None, None,
        "", False))
    assert reconciled is False
    assert called["n"] == 0


def test_6_t13_style_comparison_participates_in_synthesis(monkeypatch):
    """A T13-shaped completeness claim ('no gaps exist' for a vouchers audit)
    contradicted by a real comparison gap goes through the SAME reconciliation
    path as T2 -- the mechanism is task-shape-agnostic, only reacting to the
    completeness-claim + real-gap combination."""
    t13_content = (
        "### Endpoints, tables, hooks enumerated.\n\n"
        "These are backend-only operations... this is expected and intentional. "
        "No gaps exist."
    )
    t13_gap_note = GAP_CMP_NOTE  # same shape; the mechanism does not read task wording

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        assert "No gaps exist" in prompt
        return "Corrected: 4 backend-only endpoints have no frontend counterpart.", \
            SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        t13_content, "Audit the vouchers module...", _Team(), all_results, None,
        None, t13_gap_note, False))
    assert reconciled is True
    assert "no frontend counterpart" in content


def test_7_non_comparison_task_no_regression():
    """An ordinary answer with no completeness claim and no comparison at all
    must pass through completely untouched -- no retry, no flag set, no
    change to content."""
    all_results = [None]
    team = _Team()
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "The register_seller function validates the GSTIN and creates a row.",
        "What does register_seller do?", team, all_results, None, None, "", False))
    assert reconciled is False
    assert content == "The register_seller function validates the GSTIN and creates a row."
    assert not getattr(team, _COMPARISON_RECONCILE_FLAG, False)


def test_budget_already_spent_by_an_earlier_guard_is_respected(monkeypatch):
    called = {"n": 0}

    async def fake_stream(*a, **k):
        called["n"] += 1
        raise AssertionError("must not spend a second retry")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None, SimpleNamespace()]  # a prior guard already retried once
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None, None,
        GAP_CMP_NOTE, False))
    assert reconciled is False
    assert called["n"] == 0
    assert content == NO_GAPS_CLAIM_CONTENT


def test_synthesis_run_never_reconciles():
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None, None,
        GAP_CMP_NOTE, True))
    assert reconciled is False
    assert content == NO_GAPS_CLAIM_CONTENT


def test_retry_flag_prevents_a_second_fire_on_the_same_team(monkeypatch):
    async def fake_stream(*a, **k):
        raise AssertionError("must not retry once the flag is already set")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _Team()
    setattr(team, _COMPARISON_RECONCILE_FLAG, True)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", team, all_results, None, None,
        GAP_CMP_NOTE, False))
    assert reconciled is False


def test_retry_returning_nothing_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        return "", SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None, None,
        GAP_CMP_NOTE, False))
    assert reconciled is False
    assert content == NO_GAPS_CLAIM_CONTENT


T13B_FULL_GAP_CMP_NOTE = (
    "\n\n---\n**THE COMPARISON, COMPUTED — the answer above states a "
    "relationship between two files; this is that same relationship worked "
    "out by string match over both, not by reading and comparing. Where the "
    "two disagree, trust this one.**\n```\n"
    "compare_enumerations — API/inventory-service/router/vouchers_api.py  vs  "
    "Client/.../inventory/inventoryApi.ts\n"
    "join: HTTP method + path-boundary suffix match (exact string, no inference)\n"
    "\n"
    "MATCHED (3):\n"
    "  GET /vouchers   <->   /api/inventoryservice/vouchers?${qs}\n"
    "  GET /vouchers/{voucher_id}   <->   /api/inventoryservice/vouchers/${id}\n"
    "  POST /vouchers   <->   /api/inventoryservice/vouchers\n"
    "\n"
    "PARTIAL MATCHES (2):\n"
    "  /vouchers/{}/post   PUT /vouchers/{voucher_id}/post  <->  "
    "POST /api/inventoryservice/vouchers/${id}/post   (http_method: PUT != POST)\n"
    "  /vouchers/{}/cancel   PUT /vouchers/{voucher_id}/cancel  <->  "
    "POST /api/inventoryservice/vouchers/${id}/cancel   (http_method: PUT != POST)\n"
    "\n"
    "AMBIGUOUS (0):\n"
    "  (none)\n"
    "\n"
    "LEFT ONLY — defined on the left with no match on the right (4):\n"
    "  POST /vouchers/grn/{po_id}\n"
    "  POST /vouchers/credit-note/{invoice_id}\n"
    "  POST /vouchers/stock-adjustment\n"
    "  POST /vouchers/stock-transfer\n"
    "\n"
    "RIGHT ONLY — present on the right with no match on the left (43):\n"
    "  GET /api/inventoryservice/categories\n"
    "\n"
    "TOTALS: left 9, right 48, matched 3, left-only 4, right-only 43.\n"
    "```"
)


def test_t13b_full_comparison_note_synthesizes_deterministically_no_model_call(monkeypatch):
    """Test A (always-on gate, 2026-10-02): when cmp_note carries all five real
    compare_enumerations category headers AND the model made a (false)
    completeness claim, the reconciliation must release Phase L's templated
    answer directly -- _stream_team_run must NEVER be called, so the result
    cannot be corrupted by a second generative pass (the exact mechanism that
    truncated the real T13b answer)."""
    async def fake_stream(*a, **k):
        raise AssertionError(
            "must not re-invoke the model when the comparison note is "
            "fully synthesis-eligible -- the deterministic template must "
            "be used instead")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "### Vouchers Module Audit\n\nNo gaps exist.",
        "Audit the vouchers module...", _Team(), all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    assert "No gaps exist" not in content
    # The four real gaps, copied verbatim from the tool's own output.
    assert "POST /vouchers/grn/{po_id}" in content
    assert "POST /vouchers/credit-note/{invoice_id}" in content
    assert "POST /vouchers/stock-adjustment" in content
    assert "POST /vouchers/stock-transfer" in content
    # The PARTIAL_MATCH items must appear in their own section, never folded
    # into the LEFT_ONLY list (the historical PUT/POST conflation defect).
    assert "PARTIAL_MATCH" in content
    assert "LEFT_ONLY" in content


def test_b_no_completeness_claim_honest_disclosure_still_fires_deterministic_gate(monkeypatch):
    """Test B (the live-reproduced gap this second pass closes): the model
    does NOT claim completeness -- it honestly reports it could not retrieve
    the frontend hooks, exactly the real 3/3 live T13b trials after 701eb03.
    `_reconcile_completeness_claims` finds nothing to reconcile in this text,
    so the OLD gate (`if not claims: return` sitting ahead of synthesis)
    would return the draft unchanged here. The always-on gate must still
    fire: deterministic evidence exists and is actionable regardless of
    whether the model asserted anything about it."""
    honest_disclosure = (
        "Here are the 9 endpoints and 31 tables.\n\n"
        "I was unable to retrieve the frontend hooks and cannot identify "
        "any gaps."
    )
    assert not _reconcile_completeness_claims(honest_disclosure), (
        "test setup invalid: this text must NOT match the completeness-"
        "claim lexicon, or Test B is not exercising the new code path")

    async def fake_stream(*a, **k):
        raise AssertionError(
            "must not re-invoke the model -- the deterministic gate must "
            "fire even though the model made no completeness claim")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        honest_disclosure, "Audit the vouchers module...", _Team(),
        all_results, None, None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    assert "unable to retrieve" not in content
    assert "POST /vouchers/grn/{po_id}" in content
    assert "POST /vouchers/credit-note/{invoice_id}" in content
    assert "POST /vouchers/stock-adjustment" in content
    assert "POST /vouchers/stock-transfer" in content


def test_c_truncated_model_response_cannot_suppress_deterministic_evidence(monkeypatch):
    """Test C: a response cut off mid-sentence (the real shape repetition-
    decay produced in the original T13b incident) makes no completeness
    claim at all -- the always-on gate must still fire."""
    truncated = "The vouchers module has the following API endpoints in `API/invent"
    assert not _reconcile_completeness_claims(truncated)

    async def fake_stream(*a, **k):
        raise AssertionError("must not re-invoke the model on a truncated draft")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        truncated, "Audit the vouchers module...", _Team(), all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    assert "POST /vouchers/stock-transfer" in content


def test_d_empty_model_response_cannot_suppress_deterministic_evidence(monkeypatch):
    """Test D: an empty/unusable draft -- the extreme case of 'no claim'.
    Deterministic evidence must still reach the final answer."""
    async def fake_stream(*a, **k):
        raise AssertionError("must not re-invoke the model on an empty draft")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "", "Audit the vouchers module...", _Team(), all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    assert "POST /vouchers/stock-transfer" in content


def test_e_no_actionable_comparison_does_not_fabricate_a_discrepancy(monkeypatch):
    """Test E: a full five-header note where NOTHING is left/right-only or
    ambiguous must not fire the deterministic gate (nor the generative one)
    -- 'always evaluate' is not 'always fire'. Reuses test_3's no-op
    assertion style against a fully-eligible (not partially-shaped) note."""
    clean_full_note = (
        "\n\n---\n**THE COMPARISON, COMPUTED**\n```\n"
        "compare_enumerations — a.py  vs  b.ts\n"
        "\n"
        "MATCHED (2):\n"
        "  GET /x   <->   /api/x\n"
        "  POST /y   <->   /api/y\n"
        "\n"
        "PARTIAL MATCHES (0):\n"
        "  (none)\n"
        "\n"
        "AMBIGUOUS (0):\n"
        "  (none)\n"
        "\n"
        "LEFT ONLY — defined on the left with no match on the right (0):\n"
        "  (none)\n"
        "\n"
        "RIGHT ONLY — present on the right with no match on the left (0):\n"
        "  (none)\n"
        "\n"
        "TOTALS: left 2, right 2, matched 2, left-only 0, right-only 0.\n"
        "```"
    )

    async def fake_stream(*a, **k):
        raise AssertionError("must not retry/synthesize when nothing is actionable")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "All endpoints have a corresponding hook. There are no missing items.",
        "task text", _Team(), all_results, None, None, clean_full_note, False))
    assert reconciled is False
    assert content == "All endpoints have a corresponding hook. There are no missing items."


# ── Phase 3 (2026-10-02): deterministic path independent of the shared ─────
# generative retry budget. Root cause of live Trial 3 (second-pass POC,
# 4f1fb91): the `len(all_results) > 1` check used to sit ABOVE summary/
# existence_contradicted/claims and above the deterministic synthesis
# attempt -- so it blocked BOTH "permission to spend the shared LLM retry"
# and "permission to process already-complete, zero-LLM evidence" behind
# one guard, even though compare_enumerations had already produced a
# complete, actionable result in that trial. The check now sits only in
# front of the generative fallback (_stream_team_run) -- these tests
# construct the exact budget-exhausted state (`all_results` already length
# 2, i.e. a prior guard already spent the one retry) and assert the
# deterministic path still fires.


def test_budget_phase3_a_retry_available_and_actionable_comparison(monkeypatch):
    """Test A: the ordinary case -- retry budget untouched (all_results has
    only its initial entry), full comparison evidence, model made a
    (false) completeness claim. Deterministic path fires, no model call."""
    async def fake_stream(*a, **k):
        raise AssertionError("deterministic path must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "No gaps exist.", "Audit the vouchers module...", _Team(),
        all_results, None, None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    assert len(all_results) == 1, "deterministic path must not append a retry"
    assert "POST /vouchers/stock-transfer" in content


def test_budget_phase3_b_retry_exhausted_actionable_comparison_still_fires(monkeypatch):
    """Test B -- THE CRITICAL TEST (Trial 3's exact shape): the shared
    retry budget is ALREADY exhausted (all_results already has 2 entries --
    an earlier guard, e.g. _complete_repetition_truncated_answer, already
    spent the one retry this call allows) AND a complete, actionable
    comparison already exists. The deterministic path must still fire:
    permission to process already-computed evidence must not depend on
    permission to make another LLM call."""
    async def fake_stream(*a, **k):
        raise AssertionError(
            "must not call the model -- the retry budget is already spent, "
            "and the deterministic path does not need it anyway")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results_before = [None, SimpleNamespace(messages=[])]  # budget spent
    retry_count_before = len(all_results_before)
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "There is no apparent gap between the backend endpoints and "
        "frontend hooks for the vouchers module.",
        "Audit the vouchers module...", _Team(), all_results_before, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    assert "POST /vouchers/grn/{po_id}" in content
    assert "POST /vouchers/credit-note/{invoice_id}" in content
    assert "POST /vouchers/stock-adjustment" in content
    assert "POST /vouchers/stock-transfer" in content
    # Retry count preservation (Phase 2.2): the deterministic path must not
    # itself consume or grow the shared budget.
    assert len(all_results_before) == retry_count_before == 2


def test_budget_phase3_c_repetition_recovery_then_actionable_comparison(monkeypatch):
    """Test C: the full live shape -- a truncated draft (the real text
    repetition-decay produces mid-cutoff) arrives alongside an already-
    exhausted budget (simulating _complete_repetition_truncated_answer
    having already run and appended its own attempt) and a complete
    comparison. The correct deterministic result must survive."""
    truncated_mid_sentence = (
        "Here is the completed audit of the vouchers module:\n\n"
        "### Endpoints (from `API/inventory-service/router/vouchers_api.py`)\n\n"
        "1. **GET** `/vouchers`\n   - Lists vouchers wi"
    )

    async def fake_stream(*a, **k):
        raise AssertionError("must not call the model when budget is exhausted")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    budget_exhausted_results = [None, SimpleNamespace(messages=[])]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        truncated_mid_sentence, "Audit the vouchers module...", _Team(),
        budget_exhausted_results, None, None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    assert "POST /vouchers/stock-transfer" in content


def test_budget_phase3_d_no_comparison_evidence_unchanged_with_budget_exhausted(monkeypatch):
    """Test D: no comparison ran at all (cmp_note == "") -- existing no-op
    behavior must be unchanged regardless of budget state."""
    async def fake_stream(*a, **k):
        raise AssertionError("must not retry when no comparison ran")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    budget_exhausted_results = [None, SimpleNamespace(messages=[])]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "No gaps exist.", "task text", _Team(), budget_exhausted_results,
        None, None, "", False))
    assert reconciled is False
    assert content == "No gaps exist."


def test_budget_phase3_e_deterministic_template_never_echoes_fabricated_content(monkeypatch):
    """Test E: even when the model's own draft contains fabricated-looking
    symbol names, the deterministic template is built ENTIRELY from
    compare_enumerations' own parsed output (_synthesize_comparison_answer
    takes only `cmp_note`, never `content`) -- it cannot certify or echo
    anything the model invented, fabricated or not."""
    fabricated_draft = (
        "The vouchers API exposes get_vouchers, update_voucher, "
        "delete_voucher, get_voucher_by_id, and uses a voucher_redemptions "
        "table. All endpoints have frontend counterparts."
    )

    async def fake_stream(*a, **k):
        raise AssertionError("must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        fabricated_draft, "Audit the vouchers module...", _Team(),
        all_results, None, None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    # None of the model's fabricated names appear -- the template is built
    # solely from the tool's own real output.
    for fabricated in ("get_vouchers", "update_voucher", "delete_voucher",
                       "get_voucher_by_id", "voucher_redemptions"):
        assert fabricated not in content
    assert "POST /vouchers/stock-transfer" in content


def test_retry_exception_keeps_the_draft(monkeypatch):
    async def fake_stream(*a, **k):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    all_results = [None]
    content, result, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, "task text", _Team(), all_results, None, None,
        GAP_CMP_NOTE, False))
    assert reconciled is False
    assert content == NO_GAPS_CLAIM_CONTENT


# ── helpers ──────────────────────────────────────────────────────────────────


def test_comparison_gap_counts_parses_totals_line():
    assert _comparison_gap_counts(GAP_CMP_NOTE) == (6, 9)
    assert _comparison_gap_counts(NO_GAP_CMP_NOTE) == (0, 0)
    assert _comparison_gap_counts("") is None
    assert _comparison_gap_counts("no totals line here") is None


def test_comparison_body_strips_the_wrapper_prose():
    body = _comparison_body(GAP_CMP_NOTE)
    assert "THE COMPARISON, COMPUTED" not in body
    assert "LEFT ONLY" in body
    assert "TOTALS: left 13, right 16, matched 7, left-only 6, right-only 9." in body


def test_reconcile_completeness_claims_catches_the_real_r4_t2_sentence():
    """Pins the exact gap this phase found: the real R4 T2 answer's own
    conclusion sentence matches NONE of _COMPLETENESS_CLAIM_RE's five
    alternatives (no digit after 'all', no 'are/is covered|listed|...', and
    'items' is not in its other/additional noun list) -- reusing that
    existing lexicon unchanged would have made reconciliation a no-op on the
    very case that motivated it."""
    assert _reconcile_completeness_claims(
        "There are no missing or mismatched items.")
    assert not team_mod._completeness_claims(
        "There are no missing or mismatched items.")


def test_reconcile_completeness_claims_catches_the_real_t13b_sentence():
    """Same gap, T13's shape: 'No gaps exist.' fails 'there are no gaps'
    (which requires the literal 'there are' prefix)."""
    assert _reconcile_completeness_claims("No gaps exist.")
    assert not team_mod._completeness_claims("No gaps exist.")


# ── end-to-end: the real _verified_answer(), not just the unit function ─────


class _FakeSession:
    """Answers compare_enumerations with a real gap; any other tool call
    (the many other guards _verified_answer also runs, all gated only on
    hive_mcp_tools being truthy) fails, exactly as a genuinely unreachable
    tool would -- every one of those guards already treats a session.call_tool
    exception as 'unavailable' and degrades to an empty note, the same
    established pattern _computed_comparison itself uses."""

    def __init__(self, text):
        self.text = text
        self.compare_calls: list[tuple[str, str]] = []

    async def call_tool(self, name, args):
        if name == "compare_enumerations":
            self.compare_calls.append((args["left_path"], args["right_path"]))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])
        raise RuntimeError(f"tool {name!r} not available in this fake")


class _FakeMCPTools:
    def __init__(self, session):
        self.session = session

    async def get_session_for_run(self, **kwargs):
        return self.session


GAP_TOOL_TEXT = (
    "compare_enumerations — API/business-service/router/business_api.py  vs  "
    "Client/.../business/businessApi.ts\n"
    "LEFT ONLY — defined on the left with no match on the right (6):\n"
    "  POST /register\n"
    "  GET /status/{user_id}\n"
    "  POST /register-additional\n"
    "  GET /internal/ondc-domains\n"
    "  GET /internal/tenants/names\n"
    "  POST /{business_id}/documents\n"
    "TOTALS: left 13, right 16, matched 7, left-only 6, right-only 9."
)


class _E2ETeam:
    def __init__(self):
        self._read_state = {
            "enumerations": {
                "a": {"path": "API/business-service/router/business_api.py",
                      "tool": "get_file_content", "lines": [], "count": 13},
                "b": {"path": "Client/.../business/businessApi.ts",
                      "tool": "get_file_content", "lines": [], "count": 16},
            }
        }


T2_TASK = (
    "List every endpoint defined in API/business-service/router/business_api.py, "
    "then list every RTK Query hook exported by the frontend's business API "
    "slice, and state which endpoints have no corresponding hook. Enumerate "
    "both sides in full before comparing."
)


def test_e2e_contradiction_case_final_conclusion_is_reconciled_not_just_appended(monkeypatch):
    """The exact assertion Step 5 requires: NOT 'both strings appear
    somewhere', but 'the final conclusion agrees with the deterministic
    result'. A pre-Phase-2B _verified_answer would return the ORIGINAL
    "no missing or mismatched items" text with the comparison merely appended
    after it -- this must no longer be the case for the returned final text's
    own leading conclusion."""
    session = _FakeSession(GAP_TOOL_TEXT)
    tools = _FakeMCPTools(session)
    team = _E2ETeam()

    async def fake_stream(t, prompt, *, log_label="verify-retry", liveness_path=None):
        assert "no missing or mismatched items" in prompt
        assert "LEFT ONLY" in prompt
        return NAMED_GAPS_CONTENT, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    out = _run(_verified_answer(
        NO_GAPS_CLAIM_CONTENT, T2_TASK, team, "http://x/mcp",
        result=None, hive_mcp_tools=tools))

    # The comparison ran with the correct pair -- twice, by design: once to
    # decide whether to reconcile, once more after the retry was adopted, so
    # the footnote describes the content that actually ships (see
    # _reconcile_completeness_claim_with_comparison's return contract).
    expected_pair = (
        "API/business-service/router/business_api.py", "Client/.../business/businessApi.ts")
    assert session.compare_calls == [expected_pair, expected_pair]
    # The FINAL text's own conclusion is the reconciled one, not the original
    # false claim -- this is the assertion that distinguishes "fixed" from
    # "both strings appear in the output".
    assert "no missing or mismatched items" not in out
    assert "6 endpoints have no corresponding hook" in out


def test_e2e_no_gap_normal_positive_conclusion_ships_unchanged(monkeypatch):
    session = _FakeSession(
        "compare_enumerations — a.py  vs  b.ts\n"
        "TOTALS: left 2, right 2, matched 2, left-only 0, right-only 0."
    )
    tools = _FakeMCPTools(session)
    team = _E2ETeam()

    async def fake_stream(*a, **k):
        raise AssertionError("must not retry when the comparison found no gap")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    positive = "All endpoints have a corresponding hook. There are no missing items."
    out = _run(_verified_answer(
        positive, T2_TASK, team, "http://x/mcp", result=None, hive_mcp_tools=tools))
    assert "There are no missing items." in out


def test_e2e_non_comparison_task_no_regression(monkeypatch):
    """A completely ordinary, one-sided task must behave exactly as before
    Phase 2B -- no comparison attempted, no retry, content passes through."""
    async def fake_stream(*a, **k):
        raise AssertionError("must not retry on a non-comparison task")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _E2ETeam()
    out = _run(_verified_answer(
        "The register_seller function validates the GSTIN and creates a row.",
        "What does register_seller do?", team, None, result=None))
    assert "The register_seller function validates the GSTIN and creates a row." in out


# ── EvidenceLedger foundation (Phase 4, 2026-10-02) ─────────────────────────
#
# These exercise the minimum-viable, execution-local ledger added to
# _computed_comparison / _reconcile_completeness_claim_with_comparison:
# ComparisonEvidence, _EvidenceLedger, _get_evidence_ledger,
# _record_comparison_evidence. No new LLM call is introduced anywhere in
# this phase -- every test below either asserts that directly (Test 6) or
# monkeypatches _stream_team_run to raise if the model is ever invoked.


def _team_with_run_context() -> _Team:
    team = _Team()
    team._run_context = RunContext(session_id=None, run_id="run-test")
    return team


def _record_t13b_evidence(
        team,
        left: str = "API/inventory-service/router/vouchers_api.py",
        right: str = "Client/.../inventory/inventoryApi.ts"):
    """Mirrors _computed_comparison's own real call sequence: start a tool
    call on the run's RunContext, finish it with the tool's raw result text,
    then record that result into the ledger -- the exact order
    _computed_comparison uses, just without the real MCP round trip."""
    tool_call = team._run_context.start_tool_call(
        "compare_enumerations", {"left_path": left, "right_path": right})
    team._run_context.finish_tool_call(
        tool_call, content=T13B_FULL_GAP_CMP_NOTE, success=True, error=None)
    return _record_comparison_evidence(team, tool_call, left, right,
                                        T13B_FULL_GAP_CMP_NOTE)


def test_ledger_1_record_and_retrieve_exact_equality():
    """Test 1: what _record_comparison_evidence stores is exactly what
    _get_evidence_ledger(...).get(...) returns back -- the same object, not
    a copy or a re-derived approximation."""
    team = _team_with_run_context()
    record = _record_t13b_evidence(team)
    assert record is not None
    fetched = _get_evidence_ledger(team).get(record.evidence_id)
    assert fetched is record
    assert fetched == record


def test_ledger_2_execution_isolation_between_separate_runs():
    """Test 2: two independent team/RunContext instances (two separate runs)
    never share ledger state -- recording on one must be invisible to the
    other, by construction (team._evidence_ledger is lazily created per
    team, never a module-level global)."""
    team_a = _team_with_run_context()
    team_b = _team_with_run_context()
    record_a = _record_t13b_evidence(team_a)

    assert _get_evidence_ledger(team_b).list() == []
    assert _get_evidence_ledger(team_b).get(record_a.evidence_id) is None
    assert _get_evidence_ledger(team_a).get(record_a.evidence_id) is record_a


def test_ledger_3_consumption_independent_of_model_prose(monkeypatch, capsys):
    """Test 3: the SAME ledger evidence is consumed (same evidence_id logged
    via EVIDENCE_LEDGER_CONSUMED) regardless of what the model's own draft
    said -- a false completeness claim and an honest disclosure of failure
    both resolve to the identical recorded evidence."""
    async def fake_stream(*a, **k):
        raise AssertionError("deterministic path must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()
    record = _record_t13b_evidence(team)

    honest_disclosure = (
        "I was unable to retrieve the frontend hooks and cannot identify "
        "any gaps."
    )
    for content in ("No gaps exist.", honest_disclosure):
        setattr(team, _COMPARISON_RECONCILE_FLAG, False)
        all_results = [None]
        _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
            content, "Audit the vouchers module...", team, all_results, None,
            None, T13B_FULL_GAP_CMP_NOTE, False))
        assert reconciled is True
        out = capsys.readouterr().out
        assert f"EVIDENCE_LEDGER_CONSUMED: evidence_id={record.evidence_id} " in out


def test_ledger_4_consumption_survives_retry_budget_exhaustion(monkeypatch, capsys):
    """Test 4: the ledger lookup/consumption happens in the deterministic
    synthesis branch, which fires before the generative-retry budget check
    -- so a budget already spent by an earlier guard (all_results length > 1)
    must not prevent the recorded evidence from being consulted and
    consumed."""
    async def fake_stream(*a, **k):
        raise AssertionError("deterministic path must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()
    record = _record_t13b_evidence(team)

    all_results = [None, SimpleNamespace()]  # a prior guard already retried once
    _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "No gaps exist.", "Audit the vouchers module...", team, all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    out = capsys.readouterr().out
    assert f"EVIDENCE_LEDGER_CONSUMED: evidence_id={record.evidence_id} " in out


def test_ledger_5_consumption_survives_truncated_repetition_decay_draft(monkeypatch, capsys):
    """Test 5: a draft truncated mid-sentence -- the real shape repetition
    decay produced in the original T13b incident -- must not prevent the
    recorded evidence from being consulted and consumed either."""
    async def fake_stream(*a, **k):
        raise AssertionError("must not call the model on a truncated draft")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()
    record = _record_t13b_evidence(team)

    truncated = "The vouchers module has the following API endpoints in path API/invent"
    all_results = [None]
    _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        truncated, "Audit the vouchers module...", team, all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    out = capsys.readouterr().out
    assert f"EVIDENCE_LEDGER_CONSUMED: evidence_id={record.evidence_id} " in out


def test_ledger_6_recording_and_retrieval_are_synchronous_zero_llm_calls():
    """Test 6: the ledger's own write/read path makes zero LLM calls -- not
    just "doesn't happen to call the model in this test", but structurally
    incapable of it: none of record/get/list/latest/_record_comparison_evidence
    is a coroutine, and the whole record-then-fetch round trip succeeds with
    no asyncio event loop running at all."""
    import inspect

    assert not inspect.iscoroutinefunction(_record_comparison_evidence)
    assert not inspect.iscoroutinefunction(team_mod._EvidenceLedger.record)
    assert not inspect.iscoroutinefunction(team_mod._EvidenceLedger.get)
    assert not inspect.iscoroutinefunction(team_mod._EvidenceLedger.list)
    assert not inspect.iscoroutinefunction(team_mod._EvidenceLedger.latest)

    team = _team_with_run_context()
    record = _record_t13b_evidence(team)  # no asyncio.run anywhere above this line
    assert record is not None
    assert _get_evidence_ledger(team).list() == [record]
    assert _get_evidence_ledger(team).latest("compare_enumerations") is record


def test_ledger_7_fabricated_reconciliation_never_populates_ledger(monkeypatch):
    """Test 7: the deterministic synthesis/reconciliation path can fire (and
    does, here) purely from cmp_note TEXT, with no tool call ever recorded on
    this team's RunContext. That must never fabricate a ledger entry --
    team._evidence_ledger must not even come into existence unless a real
    compare_enumerations call (_record_comparison_evidence) created it."""
    async def fake_stream(*a, **k):
        raise AssertionError("must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()  # has a RunContext, but no tool call was ever made on it

    all_results = [None]
    _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "No gaps exist.", "Audit the vouchers module...", team, all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True  # the deterministic template still fires from cmp_note alone
    assert not hasattr(team, "_evidence_ledger"), (
        "reconciliation/synthesis must never create or populate the ledger "
        "-- only a real compare_enumerations call may")


def test_ledger_8_exact_t13b_values_via_deterministic_parse_not_hardcoded():
    """Test 8: the ledger's own fields must equal exactly what the SAME
    deterministic parsers (_comparison_summary/_comparison_entries) produce
    from the identical note -- the ledger is built FROM that parse, never a
    second, independently-hardcoded set of numbers. The literal numbers
    asserted here are the real T13b trial's own values (left=9, right=48,
    matched=3, partial_match=2, 4 left-only gaps, 43 right-only), kept as a
    fixed fixture (T13B_FULL_GAP_CMP_NOTE) rather than encoded into
    production logic anywhere. T13B_FULL_GAP_CMP_NOTE's own TOTALS line
    carries no separate "PARTIAL-MATCH TOTAL:"/"AMBIGUOUS TOTAL:" line (an
    older-shaped fixture, pre-dating Phase J-A/J-B's additive totals), so
    _comparison_summary correctly defaults partial_match/ambiguous to 0 for
    this fixture even though the PARTIAL MATCHES section itself lists 2
    entries -- the ledger must match that same (fixture-accurate) 0, not an
    assumption read off the section header alone."""
    team = _team_with_run_context()
    record = _record_t13b_evidence(team)

    summary = team_mod._comparison_summary(T13B_FULL_GAP_CMP_NOTE)
    entries = team_mod._comparison_entries(T13B_FULL_GAP_CMP_NOTE)

    assert record.left_count == summary["left"] == 9
    assert record.right_count == summary["right"] == 48
    assert record.match_count == summary["matched"] == 3
    assert record.partial_match_count == summary["partial_match"] == 0
    assert len(entries["partial_match"]) == 2
    assert summary["left_only"] == 4
    assert summary["right_only"] == 43
    assert summary["ambiguous"] == 0

    assert record.left_only == tuple(entries["left_only"]) == (
        "POST /vouchers/grn/{po_id}",
        "POST /vouchers/credit-note/{invoice_id}",
        "POST /vouchers/stock-adjustment",
        "POST /vouchers/stock-transfer",
    )
    assert record.right_only == tuple(entries["right_only"])
    assert record.ambiguous == tuple(entries["ambiguous"]) == ()
    assert record.authoritative is True
    assert record.producer == "compare_enumerations"
    assert record.evidence_type == "compare_enumerations"


# ── Claim layer foundation (Phase 4, 2026-10-02) ────────────────────────────
#
# Execution -> EvidenceLedger -> Claim -> (future) Verification/Decision.
# These exercise Claim/_ClaimStore/_get_claim_store/_make_claim/
# _validate_claim/_create_comparison_claim. No new LLM call anywhere in this
# phase -- every test below either asserts that directly or monkeypatches
# _stream_team_run to raise if the model is ever invoked.


def test_claim_1_valid_claim_is_authoritative():
    """Test 1: a Claim built from a real, just-recorded EvidenceLedger
    record is authoritative, and its evidence_ids is the explicit C1->E1
    link -- inspectable without parsing any model text."""
    team = _team_with_run_context()
    evidence = _record_t13b_evidence(team)
    claim = _create_comparison_claim(team, evidence)
    assert claim.authoritative is True
    assert claim.evidence_ids == (evidence.evidence_id,)
    assert _get_claim_store(team).get(claim.claim_id) is claim


def test_claim_2_missing_evidence_is_not_authoritative():
    """Test 2 (Phase 0.4's chosen contract): a Claim referencing an
    evidence_id that does not exist in this team's ledger is still
    recorded (creation never raises/refuses), but is explicitly, visibly
    NOT authoritative -- never a silent fallback, never an exception."""
    team = _team_with_run_context()
    claim = _make_claim(
        team, claim_type="comparison_summary",
        statement="fabricated reference to evidence that was never recorded",
        evidence_ids=["nonexistent-evidence-id"],
        provenance="test")
    assert claim.authoritative is False
    assert _get_claim_store(team).get(claim.claim_id) is claim


def test_claim_3_multiple_evidence_references_all_must_exist():
    """Test 3: a Claim referencing two evidence ids is authoritative only
    when BOTH resolve in the ledger -- one missing reference is enough to
    make the whole claim non-authoritative."""
    team = _team_with_run_context()
    e1 = _record_t13b_evidence(team, left="a.py", right="a.ts")
    e2 = _record_t13b_evidence(team, left="b.py", right="b.ts")
    assert e1.evidence_id != e2.evidence_id

    both_exist = _make_claim(
        team, claim_type="comparison_summary", statement="both real",
        evidence_ids=[e1.evidence_id, e2.evidence_id], provenance="test")
    assert both_exist.authoritative is True

    one_missing = _make_claim(
        team, claim_type="comparison_summary", statement="one fabricated",
        evidence_ids=[e1.evidence_id, "nonexistent-evidence-id"],
        provenance="test")
    assert one_missing.authoritative is False


def test_claim_4_cross_run_isolation():
    """Test 4: a Claim/evidence pair recorded on one team/RunContext cannot
    be validated against, or even seen from, a separate team/RunContext --
    same isolation guarantee as the EvidenceLedger itself (test_ledger_2),
    now proven for the Claim layer sitting on top of it."""
    team_a = _team_with_run_context()
    team_b = _team_with_run_context()
    evidence_a = _record_t13b_evidence(team_a)
    claim_a = _create_comparison_claim(team_a, evidence_a)

    assert _get_claim_store(team_b).list() == []
    assert _get_claim_store(team_b).get(claim_a.claim_id) is None
    # Team B's ledger has never seen team A's evidence_id, so revalidating
    # team A's claim against team B's ledger must fail.
    assert _validate_claim_evidence(team_b, claim_a.evidence_ids) is False


def test_claim_5_model_prose_independence(monkeypatch, capsys):
    """Test 5: the SAME claim_id is looked up, revalidated, and logged via
    CLAIM_VALIDATED regardless of what the model's own draft said -- a
    false completeness claim and an honest disclosure both resolve to the
    identical Claim/Evidence lineage."""
    async def fake_stream(*a, **k):
        raise AssertionError("deterministic path must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()
    evidence = _record_t13b_evidence(team)
    claim = _create_comparison_claim(team, evidence)

    for content in (
        "No gaps exist.",
        "I was unable to retrieve the frontend hooks and cannot identify "
        "any gaps.",
    ):
        setattr(team, _COMPARISON_RECONCILE_FLAG, False)
        all_results = [None]
        _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
            content, "Audit the vouchers module...", team, all_results, None,
            None, T13B_FULL_GAP_CMP_NOTE, False))
        assert reconciled is True
        out = capsys.readouterr().out
        assert f"CLAIM_VALIDATED: claim_id={claim.claim_id} " in out
        assert "validation_result=True" in out
        assert f"claim_id={claim.claim_id} claim_authoritative_consumed=True" in out


def test_claim_6_retry_independence(monkeypatch, capsys):
    """Test 6: Claim validation/consumption happens in the deterministic
    synthesis branch, before the generative-retry budget check -- an
    already-exhausted budget (all_results length > 1) must not prevent the
    claim from being looked up, revalidated, and consumed."""
    async def fake_stream(*a, **k):
        raise AssertionError("deterministic path must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()
    evidence = _record_t13b_evidence(team)
    claim = _create_comparison_claim(team, evidence)

    all_results = [None, SimpleNamespace()]  # a prior guard already retried once
    _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "No gaps exist.", "Audit the vouchers module...", team, all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    out = capsys.readouterr().out
    assert f"CLAIM_VALIDATED: claim_id={claim.claim_id} " in out
    assert f"claim_id={claim.claim_id} claim_authoritative_consumed=True" in out


def test_claim_7_repetition_independence(monkeypatch, capsys):
    """Test 7: claim creation/consumption must work after the real shape
    repetition-decay produced in the original T13b incident -- a draft
    truncated mid-sentence."""
    async def fake_stream(*a, **k):
        raise AssertionError("must not call the model on a truncated draft")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()
    evidence = _record_t13b_evidence(team)
    claim = _create_comparison_claim(team, evidence)

    truncated = "The vouchers module has the following API endpoints in path API/invent"
    all_results = [None]
    _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        truncated, "Audit the vouchers module...", team, all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True
    out = capsys.readouterr().out
    assert f"CLAIM_VALIDATED: claim_id={claim.claim_id} " in out


def test_claim_8_fabrication_independence(monkeypatch):
    """Test 8: the deterministic synthesis/reconciliation path can fire (and
    does, here) purely from cmp_note TEXT, with no evidence or claim ever
    recorded on this team. Fabricated model text must never be able to
    create an authoritative Claim out of nothing -- the claim store must
    not even come into existence."""
    async def fake_stream(*a, **k):
        raise AssertionError("must not call the model")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    team = _team_with_run_context()  # has a RunContext, but no evidence/claim ever recorded

    all_results = [None]
    _, _, reconciled = _run(_reconcile_completeness_claim_with_comparison(
        "No gaps exist.", "Audit the vouchers module...", team, all_results, None,
        None, T13B_FULL_GAP_CMP_NOTE, False))
    assert reconciled is True  # the deterministic template still fires from cmp_note alone
    assert not hasattr(team, "_claim_store"), (
        "reconciliation/synthesis must never fabricate a Claim -- only a "
        "real compare_enumerations call (_create_comparison_claim) may")


def test_claim_9_actual_t13b_lineage_not_hardcoded():
    """Test 9: Evidence E -> Claim C -> C.evidence_ids == [E.evidence_id],
    and E.left_only is exactly the real T13b trial's 4 gaps -- read from the
    actual deterministic evidence object this run produced, never
    hard-coded into production logic (same discipline as test_ledger_8)."""
    team = _team_with_run_context()
    evidence = _record_t13b_evidence(team)
    claim = _create_comparison_claim(team, evidence)

    assert claim.evidence_ids == (evidence.evidence_id,)
    assert claim.authoritative is True
    assert _get_evidence_ledger(team).get(claim.evidence_ids[0]) is evidence
    assert evidence.left_only == (
        "POST /vouchers/grn/{po_id}",
        "POST /vouchers/credit-note/{invoice_id}",
        "POST /vouchers/stock-adjustment",
        "POST /vouchers/stock-transfer",
    )


def test_claim_4_1_immutable_and_cannot_be_recorded_twice():
    """Phase 4.1: a Claim's evidence_ids cannot be silently changed after
    creation (frozen dataclass -> FrozenInstanceError on any attempted
    mutation), and the store refuses a second record() for the same
    claim_id -- the same immutability contract _EvidenceLedger already
    enforces for ComparisonEvidence."""
    team = _team_with_run_context()
    evidence = _record_t13b_evidence(team)
    claim = _create_comparison_claim(team, evidence)

    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.evidence_ids = ("tampered",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.authoritative = False

    with pytest.raises(ValueError):
        _get_claim_store(team).record(claim)


def test_claim_4_2_claim_statement_never_overrides_evidence():
    """Phase 4.2: a Claim's own (possibly wrong/stale) prose statement must
    never be treated as authoritative over the EvidenceLedger record it
    references. Here the claim's statement deliberately underclaims ("one
    gap exists") against evidence that actually shows 4 left-only gaps --
    the real evidence object, reached via claim.evidence_ids, must still
    show all 4, proving nothing in the Claim machinery reads or trusts
    `statement` as data."""
    team = _team_with_run_context()
    evidence = _record_t13b_evidence(team)
    misleading_claim = _make_claim(
        team, claim_type="comparison_summary",
        statement="one gap exists",  # deliberately wrong/underclaiming prose
        evidence_ids=[evidence.evidence_id],
        provenance="test")

    assert misleading_claim.authoritative is True  # the reference itself is valid
    real_evidence = _get_evidence_ledger(team).get(misleading_claim.evidence_ids[0])
    assert len(real_evidence.left_only) == 4
    assert real_evidence.left_only == (
        "POST /vouchers/grn/{po_id}",
        "POST /vouchers/credit-note/{invoice_id}",
        "POST /vouchers/stock-adjustment",
        "POST /vouchers/stock-transfer",
    )
    assert real_evidence is evidence
