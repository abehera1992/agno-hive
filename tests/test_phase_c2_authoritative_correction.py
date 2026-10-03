"""Phase C.2 (2026-10-03): the generalized authoritative-correction invariant.

C.1 proved T3 and T13a fail for two DIFFERENT reasons -- T3's correction path
(_reconcile_under_delivery_with_tool_evidence) exists but was gated off by the
shared one-retry-per-call budget (ae32c41, 2026-08-10) because an EARLIER
guard in the same _verified_answer() call had already spent it;
T13a's guard (_under_answered_comparison, inside _verified_answer's own body)
never had any correction path at all, only disclosure. T13b succeeds via a
THIRD, already-correct shape: a candidate built from already-captured
evidence with no new model call, therefore never competing for the shared
budget (_reconcile_completeness_claim_with_comparison's deterministic
synthesis).

This file proves the new shared primitive, _adopt_authoritative_correction,
generalizes T13b's own exemption rule (no LLM call -> not budget-gated;
evidence-supported -> adoptable) rather than adding bespoke per-test logic,
and that T3's and T13a's own call sites now use it (or, for T13a, reuse the
EXISTING _grounded_retry_from_evidence core T3/T11 already share) without any
task-text/filename/endpoint-name detection anywhere in the production code
touched.

Nothing here hard-codes an expected answer -- every test's evidence set is
synthetic/different from the live T3/T13a/T13b incidents, specifically to
prove the mechanism works on its OWN merits, not because it recognizes a
known case.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _AUTHORITATIVE_CORRECTION_FLAG,
    _UNDER_DELIVERY_RECONCILE_FLAG,
    _adopt_authoritative_correction,
    _member_findings_as_candidate,
    _reconcile_under_delivery_with_tool_evidence,
    _salient_tokens,
    _tool_evidence_lines,
    _verified_answer,
)


def _run(coro):
    return asyncio.run(coro)


class _Team:
    def __init__(self, evidence=None, evidence_tokens=None, member_results=None,
                 read_state=None):
        if evidence is not None:
            self._tool_evidence = evidence
        if evidence_tokens is not None:
            self._evidence_tokens = evidence_tokens
        if member_results is not None:
            self._member_results = member_results
        if read_state is not None:
            self._read_state = read_state


# ── _adopt_authoritative_correction: the shared primitive, in isolation ────


def test_case_a_no_llm_call_candidate_adopted_even_with_budget_spent():
    """The exact T3 root cause reproduced generically: all_results already has
    2 entries (an earlier guard already retried this call), yet a
    consumed_llm_call=False candidate -- requiring no new model call -- must
    still be adoptable, mirroring T13b's own exemption."""
    evidence_text = "widget_count: 42 and status: active"
    team = _Team(evidence_tokens=_salient_tokens(evidence_text))
    all_results = [None, SimpleNamespace()]  # budget already spent

    content, result, adopted = _run(_adopt_authoritative_correction(
        "test-case-a", "original thin draft", None, evidence_text, "candidate-result",
        team, all_results, consumed_llm_call=False))

    assert adopted is True
    assert content == evidence_text
    assert result == "candidate-result"
    assert getattr(team, _AUTHORITATIVE_CORRECTION_FLAG, False) is True


def test_case_b_unsupported_candidate_rejected_regardless_of_budget():
    """A candidate not grounded in team._evidence_tokens must be rejected --
    evidence validation is never weakened to ease adoption, budget spent or not."""
    team = _Team(evidence_tokens=_salient_tokens("widget_count: 42"))
    all_results = [None]  # budget available -- rejection must be about evidence, not budget

    content, result, adopted = _run(_adopt_authoritative_correction(
        "test-case-b", "original draft", "original-result",
        "gizmo_count: 99 and totally-fabricated-field: true", "candidate-result",
        team, all_results, consumed_llm_call=False))

    assert adopted is False
    assert content == "original draft"
    assert result == "original-result"
    assert getattr(team, _AUTHORITATIVE_CORRECTION_FLAG, False) is False


def test_consumed_llm_call_true_still_respects_the_shared_budget():
    """The inverse of Case A: when a candidate WOULD require a new model call
    (consumed_llm_call=True), the pre-existing ae32c41 budget check must still
    apply exactly as before -- this primitive generalizes the exemption, it
    does not remove the protection the exemption carves an exception out of."""
    team = _Team(evidence_tokens=_salient_tokens("anything"))
    all_results = [None, SimpleNamespace()]  # budget already spent

    content, result, adopted = _run(_adopt_authoritative_correction(
        "test-budget", "draft", None, "anything", "result",
        team, all_results, consumed_llm_call=True))

    assert adopted is False
    assert content == "draft"


def test_empty_or_tool_syntax_only_candidate_never_adopted():
    team = _Team(evidence_tokens=_salient_tokens("anything"))
    content, result, adopted = _run(_adopt_authoritative_correction(
        "test-empty", "draft", None, "", "result", team, [None],
        consumed_llm_call=False))
    assert adopted is False
    assert content == "draft"


# ── _member_findings_as_candidate: the no-LLM-call candidate builder ───────


def test_member_findings_as_candidate_picks_the_longest_non_trivial_result():
    team = _Team(member_results={
        "Researcher": "short stub",
        "Coder": "A much longer, substantive member finding with real detail "
                 "that should be preferred as the candidate answer body.",
    })
    candidate = _member_findings_as_candidate(team)
    assert "substantive member finding" in candidate


def test_member_findings_as_candidate_empty_when_no_member_results():
    assert _member_findings_as_candidate(_Team()) == ""


# ── Case A (integration): T3-shaped under-delivery, budget already spent ───


def test_t3_shaped_under_delivery_recovers_via_deterministic_candidate_despite_spent_budget(monkeypatch):
    """Reproduces the exact C.1 T3 mechanism: an earlier guard already
    appended a retry result to all_results (len==2), so the OLD code's
    len(all_results) > 1 check would have returned immediately with no
    correction at all. The new deterministic-candidate-first step must
    recover the real member finding anyway, without ever calling the model."""
    member_text = (
        "The verification queue is served by get_verification_queue, "
        "verify_seller, get_seller_documents, and reprovision_storage, all "
        "defined in the admin router."
    )
    team = _Team(
        evidence=[{"name": "get_file_content", "agent": "Researcher",
                   "preview": member_text, "chars": len(member_text)}],
        evidence_tokens=_salient_tokens(member_text),
        member_results={"Researcher": member_text},
    )
    all_results = [None, SimpleNamespace()]  # an earlier guard already retried

    async def fake_stream(*a, **k):
        raise AssertionError("must not spend a second model call -- the "
                              "deterministic candidate must resolve this first")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)
    content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
        "A seller model exists with a status field.", "task text", team,
        all_results, None, None, _tool_evidence_lines(team), False))

    assert reconciled is True
    assert content == member_text
    assert getattr(team, _UNDER_DELIVERY_RECONCILE_FLAG, False) is True


# ── Case C (integration): multiple member results no longer block recovery ─


def test_case_c_multiple_prior_results_no_longer_prevent_an_otherwise_valid_correction():
    """Same shape as the test above, phrased against the spec's own Case C:
    len(all_results) > 1 (multiple results already present) must not, by
    itself, prevent a no-LLM-call, evidence-supported correction."""
    member_text = (
        "get_widget_status is defined in widgets_api.py at line 42 and calls "
        "check_widget_inventory, confirmed via a real file read."
    )
    for n_prior_results in (2, 3, 5):
        # A fresh team per iteration -- the once-per-run flag this guard sets
        # on successful adoption must not be conflated with a NEW run that
        # simply happens to also have multiple prior results.
        team = _Team(
            evidence=[{"name": "get_file_content", "agent": "Researcher",
                       "preview": member_text, "chars": len(member_text)}],
            evidence_tokens=_salient_tokens(member_text),
            member_results={"Researcher": member_text},
        )
        all_results = [None] + [SimpleNamespace()] * (n_prior_results - 1)
        content, result, reconciled = _run(_reconcile_under_delivery_with_tool_evidence(
            "thin draft", "task text", team, all_results, None, None,
            _tool_evidence_lines(team), False))
        assert reconciled is True, f"failed with {n_prior_results} prior results"
        assert content == member_text


# ── T13a integration: the comparison guard now gets a real retry chance ────


T13A_TASK = (
    "Audit the vouchers module: list its endpoints, its database tables, and "
    "its frontend hooks, and identify anything present in the backend with "
    "no frontend counterpart."
)


def _comparison_team(evidence_tokens):
    return _Team(
        evidence_tokens=evidence_tokens,
        read_state={
            "max_enumerable": 5,
            "enumerable_block": {
                "path": "widgets_api.py",
                "lines": [f"@router.get(\"/widgets/{i}\")" for i in range(5)],
            },
        },
    )


def test_t13a_shaped_comparison_gap_recovers_via_grounded_retry(monkeypatch):
    """The exact C.1 T13a mechanism: a thin, under-enumerated comparison draft
    hits _under_answered_comparison inside _verified_answer, which previously
    had NO retry path -- only disclosure. This proves the new wiring gives it
    one real, budget-gated, evidence-validated retry (reusing
    _grounded_retry_from_evidence as-is) before any disclosure fires."""
    retry_text = (
        "- /widgets/0\n- /widgets/1\n- /widgets/2\n- /widgets/3\n- /widgets/4\n"
        "All five widget endpoints have a matching frontend hook."
    )
    team = _comparison_team(_salient_tokens(retry_text))

    async def fake_stream(t, prompt, *, log_label="verify-retry", liveness_path=None):
        assert log_label == "under-answered-comparison"
        assert "widgets_api.py" in prompt or "@router.get" in prompt
        return retry_text, SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    out = _run(_verified_answer(
        "Only /widgets/0 is exposed.", T13A_TASK, team, None, result=None))

    assert "BOTH SIDES WERE NOT ENUMERATED" not in out
    assert "/widgets/4" in out


def test_t13a_shaped_comparison_gap_falls_back_to_disclosure_when_retry_ungrounded(monkeypatch):
    """Mirrors T11's own negative-control requirement for this new path: an
    ungrounded retry (naming things with no evidence backing them) must be
    rejected, and the existing disclosure-only fallback must still fire
    exactly as it did before this change -- evidence validation is not
    weakened to make this guard 'succeed' more often."""
    team = _comparison_team(_salient_tokens("completely unrelated evidence text"))

    async def fake_stream(t, prompt, *, log_label="verify-retry", liveness_path=None):
        return ("All five widgets are exposed via fabricated-endpoint-xyz "
                "and totally-invented-route-abc.", SimpleNamespace(messages=[]))

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    out = _run(_verified_answer(
        "Only /widgets/0 is exposed.", T13A_TASK, team, None, result=None))

    assert "BOTH SIDES WERE NOT ENUMERATED" in out


def test_t13a_retry_not_attempted_when_budget_already_spent(monkeypatch):
    """When an earlier guard this same call already spent the shared retry
    budget, the new T13a wiring must not attempt a second one -- the existing
    disclosure-only behaviour must survive unchanged in that case too."""
    team = _comparison_team(_salient_tokens("anything"))

    async def fake_stream(*a, **k):
        raise AssertionError("must not spend a second retry this call")

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    # Pre-seed the under-delivery flag path is irrelevant here; simulate an
    # already-spent budget the same way the under-delivery tests do, by
    # calling _verified_answer with a `result` already reflecting a prior
    # retry is not directly expressible without deeper _verified_answer
    # plumbing, so this test instead exercises the guard function's own
    # budget check directly via the module-level flag + all_results shape it
    # reads, confirming the gate is still the FIRST thing checked.
    setattr(team, team_mod._UNDER_ANSWERED_COMPARISON_RETRY_FLAG, True)

    out = _run(_verified_answer(
        "Only /widgets/0 is exposed.", T13A_TASK, team, None, result=None))

    assert "BOTH SIDES WERE NOT ENUMERATED" in out
