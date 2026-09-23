"""Phase H: guard-coupled answer repair.

Closes the gap the 2026-09-22 post-Phase-G battery found (docs/runbooks/
groundedness-battery.md): verify_claims and the term-citation check correctly
DETECT bad answers, but a failed check does not constrain what actually
ships.

  * T13b -- verify_claims flagged the exact 4 fabricated hooks
    (createGrnFromPo/createCreditNote/createStockAdjustment/
    createStockTransfer) up front, but an earlier guard ("BOTH SIDES WERE
    NOT ENUMERATED") returned first in _verified_answer's first-match-wins
    chain, so the correction-retry-and-reverify mechanism near that
    function's own bottom never ran. The fabricated hooks shipped with
    verify_claims's own report riding along as inert disclosure.
  * T12 -- the correction-retry DID run and failed (a real
    ContextWindowExceededError); the existing disposition shipped the
    ORIGINAL, still-fabricated draft anyway, flagged but not withheld.
  * T10 -- _affirmed_term_absent_from_citations is "a disclosure, not a
    correction" by explicit design: no repair path exists for it at all.

These tests exercise the new outer choke point (_enforce_verification_
invariant, wired in at _verified_answer's single call site, same placement
as the existing _hoist_denied_premise) directly, using the same
monkeypatch-a-module-function style already established in
test_draft_repair_verify_claims.py and test_comparison_reconciliation.py.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _RepairFinding,
    _REPAIR_ATTEMPTED_FLAG,
    _enforce_verification_invariant,
    _not_found_claims,
    _repair_prompt,
    _repair_verification_failure,
    _verification_failed_answer,
)

# The real T13b verify_claims report, reconstructed verbatim from the live
# 2026-09-22 run (docs/runbooks/groundedness-battery.md's own dated entry).
T13B_REAL_REPORT = (
    "verify_claims — deterministic grep of the claims in this answer\n\n"
    "SYMBOLS (21 checked):\n"
    "  DECLARED   list_vouchers                        <-- function in "
    "API/inventory-service/router/vouchers_api.py:213\n"
    "  DECLARED   post_voucher                         <-- function in "
    "API/inventory-service/router/vouchers_api.py:399\n"
    "  FOUND      getVouchers                            Client/EcommClient-Web/"
    "ekamweb/src/lib/api/services/inventory/inventoryApi.ts:581:    getV\n"
    "  NOT FOUND  createGrnFromPo                        <-- does not exist in "
    "the project\n"
    "  NOT FOUND  createCreditNote                       <-- does not exist in "
    "the project\n"
    "  NOT FOUND  createStockAdjustment                  <-- does not exist in "
    "the project\n"
    "  NOT FOUND  createStockTransfer                    <-- does not exist in "
    "the project\n\n"
    "PATHS (3 checked):\n"
    "  EXISTS     API/inventory-service/router/vouchers_api.py (file)\n\n"
    "VERDICT: 4 claim(s) could NOT be found in the project. Fix the answer "
    "before returning it — a NOT FOUND symbol or a BAD citation is "
    "fabrication, not a near miss."
)
T13B_FABRICATED_CONCLUSION = (
    "#### Backend-Frontend Coverage\nAll backend endpoints have corresponding "
    "frontend hooks (createGrnFromPo, createCreditNote, createStockAdjustment, "
    "createStockTransfer all present), so there are no backend-only endpoints "
    "without frontend counterparts."
)
T13B_REPAIRED_CONCLUSION = (
    "#### Backend-Frontend Coverage\nFour backend endpoints have no frontend "
    "counterpart: grn, credit-note, stock-adjustment, stock-transfer."
)

# The real T12 verify_claims report shape (a genuine grep miss on a fabricated
# compound route, real prefix + invented suffix).
T12_REAL_REPORT = (
    "verify_claims — deterministic grep of the claims in this answer\n\n"
    "  NOT FOUND  /parties/{party_id}/locations/{location_id}  <-- no trace of "
    "segment 'parties/{party_id}/locations/{location_id}'\n\n"
    "VERDICT: 3 claim(s) could NOT be found in the project."
)
T12_FABRICATED_ROUTE = (
    "@router.delete(\"/parties/{party_id}/locations/{location_id}\") removes a "
    "party location."
)

# _flagged_draft_note's own exact text -- what _verified_answer's internal
# correction retry already stamps on a draft when IT tried and failed.
T12_ALREADY_FLAGGED_CONTENT = (
    T12_FABRICATED_ROUTE + "\n\n---\n**UNVERIFIED CLAIMS, AND THE CORRECTION "
    "ATTEMPT DID NOT COMPLETE — the checks below flagged this answer and the "
    "one retry this run allows produced nothing to replace it with, so what "
    "you are reading is the flagged draft:**\n```\n" + T12_REAL_REPORT + "\n```"
)

T10_NOTE = (
    "\n\n---\n**ASKED ABOUT A MIDDLEWARE, AND NOTHING CITED IS ONE — the "
    "question asked whether a middleware exists and the answer above says it "
    "does, but the word does not appear in any of the 3 file(s) it cites "
    "(checked with count_matches).**"
)
T10_DRAFT = (
    "Yes, there is rate-limiting middleware in the authentication service — "
    "see authHelper.py:129."
)
T10_REPAIRED = (
    "There is a rate-limiting function (check_login_rate_limit, "
    "authHelper.py:132), called inline from the login route — it is not "
    "registered as ASGI middleware."
)


def _run(coro):
    return asyncio.run(coro)


class _Team:
    """Bare stand-in, matching test_comparison_reconciliation.py's own -- plain
    attribute get/set (the repair flag) is all this module's new code needs."""


# ── _not_found_claims: pure extraction ───────────────────────────────────────

def test_not_found_claims_extracts_exactly_the_four_real_hook_names():
    claims = _not_found_claims(T13B_REAL_REPORT)
    assert claims == (
        "createGrnFromPo", "createCreditNote", "createStockAdjustment",
        "createStockTransfer")


def test_not_found_claims_ignores_declared_and_found_lines():
    claims = _not_found_claims(T13B_REAL_REPORT)
    assert "list_vouchers" not in claims
    assert "post_voucher" not in claims
    assert "getVouchers" not in claims


def test_not_found_claims_empty_report_yields_nothing():
    assert _not_found_claims("") == ()
    assert _not_found_claims("verify_claims: no checkable claims found") == ()


def test_not_found_claims_deduplicates():
    report = "  NOT FOUND  foo  <-- x\n  NOT FOUND  foo  <-- x again\n"
    assert _not_found_claims(report) == ("foo",)


# ── _repair_prompt: concrete failures, not a generic instruction ────────────

def test_repair_prompt_names_the_exact_flagged_claims():
    finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                              claims=("createGrnFromPo", "createCreditNote"))
    prompt = _repair_prompt("audit the vouchers module", finding)
    assert "createGrnFromPo" in prompt
    assert "createCreditNote" in prompt
    assert "audit the vouchers module" in prompt
    # Not a bare "fix the answer" -- the concrete names ARE the instruction.
    assert "fix the answer" not in prompt.lower()


def test_repair_prompt_falls_back_to_the_report_when_no_claim_list():
    finding = _RepairFinding(guard="term-citation", report=T10_NOTE)
    prompt = _repair_prompt("is there a middleware?", finding)
    assert "MIDDLEWARE" in prompt or "middleware" in prompt


# ── _verification_failed_answer: the bounded, explicit result ───────────────

def test_verification_failed_answer_never_asserts_the_flagged_claim_as_fact():
    finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                              claims=("createGrnFromPo",))
    out = _verification_failed_answer(T13B_FABRICATED_CONCLUSION, finding)
    assert "VERIFICATION FAILED" in out
    assert "not being returned as fact" in out
    # Preserved for tracing (Phase H's own explicit requirement), but clearly
    # marked as not confirmed rather than silently dropped.
    assert "createGrnFromPo" in out
    assert "kept below for tracing only" in out


def test_verification_failed_answer_includes_the_repair_recheck_report_when_given():
    finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                              claims=("createGrnFromPo",))
    out = _verification_failed_answer(
        T13B_FABRICATED_CONCLUSION, finding, repaired_report="still NOT FOUND: x")
    assert "still NOT FOUND: x" in out


# ── _repair_verification_failure: the full ANSWER->VERIFY->FAIL->REPAIR->VERIFY AGAIN loop ──

@pytest.mark.asyncio
async def test_successful_repair_returns_the_repaired_answer(monkeypatch):
    """PASS branch: recheck comes back clean -> repaired text ships."""
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        assert "createGrnFromPo" in prompt
        return T13B_REPAIRED_CONCLUSION, SimpleNamespace(messages=[])
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def recheck(text):
        assert text == T13B_REPAIRED_CONCLUSION
        return "verify_claims: no checkable claims found", False, False

    finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                              claims=_not_found_claims(T13B_REAL_REPORT))
    team = _Team()
    out = await _repair_verification_failure(
        T13B_FABRICATED_CONCLUSION, "audit the vouchers module", team,
        "http://fake-hive-mcp", None, None, finding, recheck)

    assert out == T13B_REPAIRED_CONCLUSION
    assert getattr(team, _REPAIR_ATTEMPTED_FLAG) is True


@pytest.mark.asyncio
async def test_failed_repair_never_returns_the_invalid_answer(monkeypatch):
    """FAIL branch: recheck still flags it -> the ORIGINAL fabricated
    conclusion (and the corrected-but-still-bad retry text) must both be
    absent from the shipped answer's assertive text."""
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        # A "repair" that only rephrases the same fabrication.
        return T13B_FABRICATED_CONCLUSION, SimpleNamespace(messages=[])
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def recheck(text):
        return T13B_REAL_REPORT, True, False  # still bad

    finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                              claims=_not_found_claims(T13B_REAL_REPORT))
    out = await _repair_verification_failure(
        T13B_FABRICATED_CONCLUSION, "audit the vouchers module", _Team(),
        "http://fake-hive-mcp", None, None, finding, recheck)

    assert "VERIFICATION FAILED" in out
    assert "not being returned as fact" in out
    # The claim itself is quoted in the tracing block (Phase H's own "preserve
    # raw transcripts" requirement), but not asserted as fact in the primary,
    # assertive text ahead of it.
    assertive_part = out.split("tracing only")[0]
    assert "so there are no backend-only endpoints without frontend" not in assertive_part


@pytest.mark.asyncio
async def test_repair_that_raises_does_not_return_the_invalid_answer(monkeypatch):
    """T12's real shape: the retry itself crashes (ContextWindowExceededError).
    The known-bad original must not ship unmodified."""
    async def raising_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        raise RuntimeError(
            "litellm.ContextWindowExceededError: maximum context length exceeded")
    monkeypatch.setattr(team_mod, "_stream_team_run", raising_stream)

    async def recheck(text):
        raise AssertionError("recheck must not run when the retry itself raised")

    finding = _RepairFinding(guard="verify_claims", report=T12_REAL_REPORT,
                              claims=_not_found_claims(T12_REAL_REPORT))
    out = await _repair_verification_failure(
        T12_FABRICATED_ROUTE, "architectural overview", _Team(),
        "http://fake-hive-mcp", None, None, finding, recheck)

    assert "VERIFICATION FAILED" in out
    assert "delete(\"/parties/{party_id}/locations/{location_id}\") removes a party " \
           "location." not in out.split("tracing only")[0]


@pytest.mark.asyncio
async def test_repair_that_returns_nothing_usable_does_not_return_the_invalid_answer(
        monkeypatch):
    async def empty_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return "", SimpleNamespace(messages=[])
    monkeypatch.setattr(team_mod, "_stream_team_run", empty_stream)

    async def recheck(text):
        raise AssertionError("recheck must not run when the retry produced nothing")

    finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                              claims=_not_found_claims(T13B_REAL_REPORT))
    out = await _repair_verification_failure(
        T13B_FABRICATED_CONCLUSION, "audit the vouchers module", _Team(),
        "http://fake-hive-mcp", None, None, finding, recheck)

    assert "VERIFICATION FAILED" in out


@pytest.mark.asyncio
async def test_repair_budget_already_spent_this_run_skips_a_second_live_attempt():
    """Repair-budget exhaustion -> bounded failure, no second _stream_team_run call."""
    calls = {"n": 0}

    async def counting_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        calls["n"] += 1
        return "should never be reached", SimpleNamespace(messages=[])

    team_mod_stream_backup = team_mod._stream_team_run
    team_mod._stream_team_run = counting_stream
    try:
        team = _Team()
        setattr(team, _REPAIR_ATTEMPTED_FLAG, True)  # budget already spent

        async def recheck(text):
            raise AssertionError("recheck must not run — budget already spent")

        finding = _RepairFinding(guard="term-citation", report=T10_NOTE)
        out = await _repair_verification_failure(
            T10_DRAFT, "is there a middleware?", team,
            "http://fake-hive-mcp", None, None, finding, recheck)

        assert calls["n"] == 0
        assert "VERIFICATION FAILED" in out
    finally:
        team_mod._stream_team_run = team_mod_stream_backup


@pytest.mark.asyncio
async def test_internal_repair_already_attempted_marker_skips_a_second_live_retry():
    """T12's exact real shape: _verified_answer's OWN internal correction retry
    already ran against this text (its _flagged_draft_note marker is already
    baked in) -- do not spend a THIRD full-pipeline attempt on an
    already-crashing task; go straight to bounded failure."""
    calls = {"n": 0}

    async def counting_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        calls["n"] += 1
        return "should never be reached", SimpleNamespace(messages=[])

    backup = team_mod._stream_team_run
    team_mod._stream_team_run = counting_stream
    try:
        async def recheck(text):
            raise AssertionError("recheck must not run — internal retry already failed")

        finding = _RepairFinding(guard="verify_claims", report=T12_REAL_REPORT,
                                  claims=_not_found_claims(T12_REAL_REPORT))
        team = _Team()
        out = await _repair_verification_failure(
            T12_ALREADY_FLAGGED_CONTENT, "architectural overview", team,
            "http://fake-hive-mcp", None, None, finding, recheck)

        assert calls["n"] == 0
        assert "VERIFICATION FAILED" in out
        assert getattr(team, _REPAIR_ATTEMPTED_FLAG) is True
    finally:
        team_mod._stream_team_run = backup


@pytest.mark.asyncio
async def test_unavailable_recheck_is_treated_as_repair_not_confirmed():
    """The checker itself being unreachable on the recheck cannot be read as
    success -- an unconfirmed repair is not a confirmed one."""
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return T13B_REPAIRED_CONCLUSION, SimpleNamespace(messages=[])
    backup = team_mod._stream_team_run
    team_mod._stream_team_run = fake_stream
    try:
        async def recheck(text):
            return "", False, True  # unavailable=True

        finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                                  claims=_not_found_claims(T13B_REAL_REPORT))
        out = await _repair_verification_failure(
            T13B_FABRICATED_CONCLUSION, "audit the vouchers module", _Team(),
            "http://fake-hive-mcp", None, None, finding, recheck)
        assert "VERIFICATION FAILED" in out
    finally:
        team_mod._stream_team_run = backup


# ── T10: term-citation finding through the same repair primitive ────────────

@pytest.mark.asyncio
async def test_t10_imprecise_citation_successful_repair_ships_the_correction():
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        assert "middleware" in prompt.lower()
        return T10_REPAIRED, SimpleNamespace(messages=[])
    backup = team_mod._stream_team_run
    team_mod._stream_team_run = fake_stream
    try:
        async def recheck(text):
            assert text == T10_REPAIRED
            return "", False, False  # T10_REPAIRED no longer affirms "middleware"

        finding = _RepairFinding(guard="term-citation", report=T10_NOTE)
        out = await _repair_verification_failure(
            T10_DRAFT, "is there a rate-limiting middleware?", _Team(),
            "http://fake-hive-mcp", None, None, finding, recheck)
        assert out == T10_REPAIRED
    finally:
        team_mod._stream_team_run = backup


@pytest.mark.asyncio
async def test_t10_imprecise_citation_failed_repair_withholds_the_affirmation():
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return T10_DRAFT, SimpleNamespace(messages=[])  # unchanged, still wrong
    backup = team_mod._stream_team_run
    team_mod._stream_team_run = fake_stream
    try:
        async def recheck(text):
            return T10_NOTE, True, False  # still flagged

        finding = _RepairFinding(guard="term-citation", report=T10_NOTE)
        out = await _repair_verification_failure(
            T10_DRAFT, "is there a rate-limiting middleware?", _Team(),
            "http://fake-hive-mcp", None, None, finding, recheck)
        assert "VERIFICATION FAILED" in out
        assert "Yes, there is rate-limiting middleware in the authentication " \
               "service — see authHelper.py:129." not in out.split("tracing only")[0]
    finally:
        team_mod._stream_team_run = backup


# ── _enforce_verification_invariant: the wired choke point ──────────────────

@pytest.mark.asyncio
async def test_enforce_invariant_clean_content_is_returned_unchanged(monkeypatch):
    async def clean_verify(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", clean_verify)

    async def no_term_issue(task, content, hive_mcp_url, hive_mcp_tools):
        return ""
    monkeypatch.setattr(team_mod, "_affirmed_term_absent_from_citations", no_term_issue)

    out = await _enforce_verification_invariant(
        "A perfectly grounded answer.", "some task", _Team(),
        "http://fake-hive-mcp", None, None)
    assert out == "A perfectly grounded answer."


@pytest.mark.asyncio
async def test_enforce_invariant_no_hive_mcp_url_is_a_no_op():
    out = await _enforce_verification_invariant(
        "some content", "some task", _Team(), None, None, None)
    assert out == "some content"


@pytest.mark.asyncio
async def test_enforce_invariant_routes_not_found_through_repair(monkeypatch):
    async def bad_verify(content, hive_mcp_url, hive_mcp_tools):
        return T13B_REAL_REPORT, True, False
    monkeypatch.setattr(team_mod, "_verify_claims", bad_verify)

    async def fake_repair(content, task, team, hive_mcp_url, hive_mcp_tools,
                           liveness_path, finding, recheck):
        assert finding.guard == "verify_claims"
        assert "createGrnFromPo" in finding.claims
        return "REPAIRED"
    monkeypatch.setattr(team_mod, "_repair_verification_failure", fake_repair)

    out = await _enforce_verification_invariant(
        T13B_FABRICATED_CONCLUSION, "audit the vouchers module", _Team(),
        "http://fake-hive-mcp", None, None)
    assert out == "REPAIRED"


@pytest.mark.asyncio
async def test_enforce_invariant_ambiguous_only_report_does_not_trigger_repair(monkeypatch):
    """T3's real shape: AMBIGUOUS (a genuine 2-real-files-share-this-name
    collision) plus one unclear BAD line, zero NOT FOUND, over an otherwise
    well-grounded answer -- forcing this into a bounded failure would throw
    away a mostly-correct answer to enforce an invariant written for
    fabricated claims specifically. Must NOT invoke the repair primitive."""
    ambiguous_report = (
        "  AMBIGUOUS  business_service_client.py:1 <-- 2 files share that "
        "name; cite a repo-relative path\n"
        "  BAD        BusinessProfile.status:1442 <-- no such file in the "
        "project\n"
        "VERDICT: 2 claim(s) could NOT be verified."
    )

    async def ambiguous_verify(content, hive_mcp_url, hive_mcp_tools):
        return ambiguous_report, True, False
    monkeypatch.setattr(team_mod, "_verify_claims", ambiguous_verify)

    async def no_term_issue(task, content, hive_mcp_url, hive_mcp_tools):
        return ""
    monkeypatch.setattr(team_mod, "_affirmed_term_absent_from_citations", no_term_issue)

    repair_calls = {"n": 0}

    async def fail_if_called(*a, **k):
        repair_calls["n"] += 1
        return "should not be reached"
    monkeypatch.setattr(team_mod, "_repair_verification_failure", fail_if_called)

    draft = "Seller verification flows through business_admin_api.py:84."
    out = await _enforce_verification_invariant(
        draft, "how does seller verification work?", _Team(),
        "http://fake-hive-mcp", None, None)

    assert repair_calls["n"] == 0
    assert out == draft


@pytest.mark.asyncio
async def test_enforce_invariant_falls_through_to_term_citation_when_verify_claims_clean(
        monkeypatch):
    async def clean_verify(content, hive_mcp_url, hive_mcp_tools):
        return "", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", clean_verify)

    async def term_issue(task, content, hive_mcp_url, hive_mcp_tools):
        return T10_NOTE
    monkeypatch.setattr(team_mod, "_affirmed_term_absent_from_citations", term_issue)

    async def fake_repair(content, task, team, hive_mcp_url, hive_mcp_tools,
                           liveness_path, finding, recheck):
        assert finding.guard == "term-citation"
        return "REPAIRED-T10"
    monkeypatch.setattr(team_mod, "_repair_verification_failure", fake_repair)

    out = await _enforce_verification_invariant(
        T10_DRAFT, "is there a rate-limiting middleware?", _Team(),
        "http://fake-hive-mcp", None, None)
    assert out == "REPAIRED-T10"


# ── Core invariant, driven as a table across every scenario above ───────────

@pytest.mark.asyncio
@pytest.mark.parametrize("flagged_claim,draft,recheck_result", [
    ("createGrnFromPo", T13B_FABRICATED_CONCLUSION, (T13B_REAL_REPORT, True, False)),
    ("/parties/{party_id}/locations/{location_id}", T12_FABRICATED_ROUTE,
     (T12_REAL_REPORT, True, False)),
])
async def test_core_invariant_flagged_claim_x_never_survives_an_unresolved_repair(
        monkeypatch, flagged_claim, draft, recheck_result):
    """"If verification identifies claim X as invalid, the final returned answer
    must never contain X unless a subsequent verification pass explicitly
    clears X" -- driven end to end through _repair_verification_failure with
    a repair that does NOT clear X (recheck still flags it)."""
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return draft, SimpleNamespace(messages=[])  # "repair" that changes nothing
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def recheck(text):
        return recheck_result

    report, _, _ = recheck_result
    finding = _RepairFinding(guard="verify_claims", report=report,
                              claims=_not_found_claims(report))
    out = await _repair_verification_failure(
        draft, "some task", _Team(), "http://fake-hive-mcp", None, None,
        finding, recheck)

    # X survives only inside the clearly-marked, non-assertive tracing block.
    assertive_part = out.split("tracing only")[0] if "tracing only" in out else out
    assert flagged_claim not in assertive_part or "VERIFICATION FAILED" in assertive_part[:40]


@pytest.mark.asyncio
async def test_core_invariant_claim_x_may_ship_once_a_later_pass_explicitly_clears_it():
    """The other half of the same invariant: a claim that verification
    initially flagged CAN ship, once a subsequent verification pass
    (the recheck) explicitly clears it."""
    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return T13B_REPAIRED_CONCLUSION, SimpleNamespace(messages=[])
    backup = team_mod._stream_team_run
    team_mod._stream_team_run = fake_stream
    try:
        async def recheck(text):
            return "verify_claims: no checkable claims found", False, False  # cleared

        finding = _RepairFinding(guard="verify_claims", report=T13B_REAL_REPORT,
                                  claims=_not_found_claims(T13B_REAL_REPORT))
        out = await _repair_verification_failure(
            T13B_FABRICATED_CONCLUSION, "audit the vouchers module", _Team(),
            "http://fake-hive-mcp", None, None, finding, recheck)
        assert out == T13B_REPAIRED_CONCLUSION
    finally:
        team_mod._stream_team_run = backup
