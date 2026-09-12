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

import swarm.team as team_mod
from swarm.team import (
    _comparison_body,
    _comparison_gap_counts,
    _COMPARISON_RECONCILE_FLAG,
    _reconcile_completeness_claim_with_comparison,
    _reconcile_completeness_claims,
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
