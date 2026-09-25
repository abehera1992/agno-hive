"""Phase E: structured member handoff.

B's mechanical carry guarantee (forwarded content is not silently lost, duplication is
controlled) works correctly -- but T10/T11 (2026-09-21 post-deployment battery) showed it
carrying raw Researcher prose into user-facing answers verbatim: process narration ("I'll
check for X... Let me try a different approach...") and a runtime-injected debugging aid
(the "[DECLARATION INDEX ... extracted from the tool output by code, not written by the
member]" block, addressed to the Coordinator, not a reader).

This does not change B's carry mechanism (_forwarded_gap/_with_forwarded_evidence are
untouched). It changes what forward_member_answer RECORDS into team._forwarded_members:
instead of the raw transcript, a MemberResult built by _build_member_result -- status,
narration/scaffolding-stripped answer, real evidence locators, and any runtime-flagged
unresolved notice -- while team._member_results (the raw transcript) is untouched, so
tracing/debugging is unaffected.
"""
from types import SimpleNamespace

import pytest

from swarm.team import (
    _BUDGET_EXHAUSTED_ANSWER, _DECLARATION_INDEX_MARKER, _RELAY_FILENAME_RE,
    _THIN_REPORT_MARKER, MemberResult, _build_member_result, _forwarded_gap,
    _make_forward_member_answer, _strip_member_narration, _with_forwarded_evidence,
)

# The real Researcher transcript from the T10 live battery run (2026-09-21), reconstructed
# verbatim from the captured journal/response, run-on sentences and all (no space after
# several periods -- the real model output, not a cleaned-up simulation).
T10_RAW = (
    "I'll check for rate-limiting middleware in the authentication service. First, I'll "
    "look for any rate-limiting configuration or middleware in the codebase."
    "I'll try a different approach to search for rate-limiting middleware in the "
    "authentication service. I found a reference to rate limiting in the authentication "
    "service. Let me check the file `API/authentication-service/authUtil/authHelper.py` "
    "to see if it contains rate-limiting middleware."
    "I found a rate-limiting function in the authentication service. The function "
    "`check_login_rate_limit` in `API/authentication-service/authUtil/authHelper.py` at "
    "line 132 implements rate limiting for login attempts. This function checks for "
    "brute-force login attempts and raises a 429 HTTP exception if the threshold is "
    "exceeded.\n\nTo confirm if this is used as middleware, I'll check how it's "
    "integrated in the authentication service. Let me search for its usage."
    "The `check_login_rate_limit` function is imported in "
    "`API/authentication-service/router/auth_service_api.py` at line 68. Let me check "
    "how it's used in that file."
    "I see that `check_login_rate_limit` is imported but I need to check how it's used "
    "in the login endpoint. Let me read the login endpoint implementation."
    "I found that the `check_login_rate_limit` function is called in the login endpoint "
    "at line 513. This means that rate limiting is implemented as part of the login "
    "process in the authentication service.\n\nTo summarize:\n- The authentication "
    "service has rate-limiting functionality implemented in the `check_login_rate_limit` "
    "function in `API/authentication-service/authUtil/authHelper.py` at line 132.\n- "
    "This function is used in the login endpoint in "
    "`API/authentication-service/router/auth_service_api.py` at line 513 to check for "
    "brute-force login attempts.\n\nTherefore, there is rate-limiting middleware in the "
    "authentication service."
)
T10_DECLARATION_TAIL = (
    "\n\n[DECLARATION INDEX — extracted from the tool output by code, not written by the "
    "member. These are every declaration and route the files below actually contain, with "
    "real line numbers. If the report above enumerates fewer than this list does, THIS "
    "list is the complete one; use it.]\n"
    "- API/authentication-service/authUtil/authHelper.py: parse_user_agent:60, "
    "extract_client_info:81, get_geolocation_from_ip:99, check_login_rate_limit:132\n"
    "- API/authentication-service/router/auth_service_api.py: login:492"
)


def _tool(answers, forwarded):
    return _make_forward_member_answer(answers, forwarded)


async def _forward(tool, member_id):
    return await tool.entrypoint(member_id=member_id)


# ── 1. normal member result forwards correctly ───────────────────────────────────────────

def test_normal_short_report_is_carried_through_unchanged():
    raw = "The Party model has 13 fields: party_id, name, email, ... See models.py:42."
    r = _build_member_result(raw)
    assert r.status == "COMPLETE"
    assert r.answer == raw
    assert "models.py" in r.evidence


@pytest.mark.asyncio
async def test_normal_report_forwards_through_the_tool():
    answers, forwarded = {"researcher": "The Party model has 13 fields. models.py:42."}, {}
    receipt = await _forward(_tool(answers, forwarded), "researcher")
    assert forwarded["researcher"] == answers["researcher"]
    assert "FORWARDED" in receipt and answers["researcher"] not in receipt


# ── 2. internal process narration does not become user-facing evidence ──────────────────

def test_t10_real_transcript_narration_is_stripped_findings_survive():
    r = _build_member_result(T10_RAW)
    for finding in ("check_login_rate_limit", "authHelper.py", "line 132", "line 68",
                    "line 513", "429 HTTP exception", "auth_service_api.py"):
        assert finding in r.answer, finding
    for narration in ("I'll check for rate-limiting", "I'll try a different approach",
                      "Let me check the file", "Let me search for its usage",
                      "Let me check how it's used", "Let me read the login endpoint"):
        assert narration not in r.answer, narration
    assert r.status == "COMPLETE"


def test_declaration_index_scaffolding_never_reaches_the_answer():
    r = _build_member_result(T10_RAW + T10_DECLARATION_TAIL)
    assert _DECLARATION_INDEX_MARKER not in r.answer
    assert "extracted from the tool output by code, not written by the member" not in r.answer
    assert r.answer == _build_member_result(T10_RAW).answer  # tail changes nothing else


def test_thin_report_notice_never_reaches_the_answer():
    raw = "Found items_api.py." + f"\n\n{_THIN_REPORT_MARKER} this member read 40,000 chars..."
    r = _build_member_result(raw)
    assert _THIN_REPORT_MARKER not in r.answer
    assert "items_api.py" in r.answer


def test_narration_opener_must_be_at_the_sentence_start_not_merely_present():
    """A finding that happens to CONTAIN one of the narration words is never touched --
    only a sentence that OPENS with it is stripped."""
    raw = "I found that the login handler will let me know if rate limiting is active."
    assert _strip_member_narration(raw) == raw


def test_run_on_sentences_with_no_separating_space_are_still_split_and_stripped():
    raw = "Found it in the codebase.I'll try a different approach now."
    out = _strip_member_narration(raw)
    assert "Found it in the codebase." in out
    assert "I'll try a different approach" not in out


# ── 3. evidence is preserved ─────────────────────────────────────────────────────────────

def test_evidence_lists_every_distinct_file_the_raw_report_names():
    r = _build_member_result(T10_RAW)
    assert r.evidence == sorted(set(_RELAY_FILENAME_RE.findall(T10_RAW)))
    assert "authHelper.py" in r.evidence and "auth_service_api.py" in r.evidence


def test_evidence_survives_even_when_the_naming_sentence_is_stripped():
    """A file named only inside a stripped narration sentence ('Let me check
    authHelper.py...') is still real evidence -- _build_member_result reads it from the
    RAW text, not from what survives narration stripping."""
    raw = "Let me check auth_helper_notes.py for the config."
    r = _build_member_result(raw)
    assert "auth_helper_notes.py" in r.evidence
    assert "Let me check" not in r.answer


def test_no_evidence_never_invents_a_locator():
    r = _build_member_result("There is no rate limiting anywhere in this service.")
    assert r.evidence == []


# ── 4. multiple member results remain distinct ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_members_forward_independently_with_separately_stripped_answers():
    answers = {
        "researcher": "I'll check the routes. Found items_api.py with 9 endpoints.",
        "reviewer": "Let me look at the diff. No issues found in vouchers_api.py.",
    }
    forwarded = {}
    tool = _tool(answers, forwarded)
    await _forward(tool, "researcher")
    await _forward(tool, "reviewer")
    assert forwarded["researcher"] == "Found items_api.py with 9 endpoints."
    assert forwarded["reviewer"] == "No issues found in vouchers_api.py."
    assert forwarded["researcher"] != forwarded["reviewer"]


def test_each_members_evidence_is_its_own():
    r1 = _build_member_result("Found items_api.py.")
    r2 = _build_member_result("Found vouchers_api.py.")
    assert r1.evidence == ["items_api.py"] and r2.evidence == ["vouchers_api.py"]


# ── 5. PARTIAL remains PARTIAL ───────────────────────────────────────────────────────────

def test_thin_report_flag_produces_partial_status():
    raw = "Found parties_api.py." + f"\n\n{_THIN_REPORT_MARKER} this member read a lot..."
    r = _build_member_result(raw)
    assert r.status == "PARTIAL"
    assert r.unresolved and r.unresolved[0].startswith(_THIN_REPORT_MARKER)
    assert "parties_api.py" in r.answer


def test_partial_status_is_not_downgraded_by_declaration_index_alone():
    """The declaration index alone (no thin-report flag) does not itself signal PARTIAL --
    it is grounding metadata, not an incompleteness flag."""
    raw = "Found parties_api.py." + T10_DECLARATION_TAIL
    r = _build_member_result(raw)
    assert r.status == "COMPLETE"


def test_partial_report_is_still_forwardable():
    answers = {"researcher": "Found items_api.py." + f"\n\n{_THIN_REPORT_MARKER} thin..."}
    forwarded = {}
    result = _build_member_result(answers["researcher"])
    assert result.status == "PARTIAL"
    assert result.answer and result.answer != ""


# ── 6. BLOCKED remains BLOCKED ────────────────────────────────────────────────────────────

def test_empty_raw_text_is_blocked():
    for raw in ("", "   ", None):
        r = _build_member_result(raw)
        assert r.status == "BLOCKED" and r.answer == ""


def test_canned_budget_exhausted_text_is_blocked():
    r = _build_member_result(_BUDGET_EXHAUSTED_ANSWER)
    assert r.status == "BLOCKED" and r.answer == ""


def test_only_a_tail_notice_with_no_prose_is_blocked():
    raw = f"{_THIN_REPORT_MARKER} this member read a lot and produced nothing usable.]"
    r = _build_member_result(raw)
    assert r.status == "BLOCKED"
    assert r.answer == ""
    assert r.unresolved  # the notice itself is not lost, just not user-facing


def test_only_narration_no_findings_is_blocked():
    raw = "I'll check for rate-limiting middleware in the authentication service."
    r = _build_member_result(raw)
    assert r.status == "BLOCKED" and r.answer == ""


# ── 7. empty/budget answers remain non-forwardable ───────────────────────────────────────

@pytest.mark.asyncio
async def test_nothing_stored_is_still_nothing_to_forward():
    # PHASE R (2026-09-24): empty-store case now returns an explicit terminal-state
    # message ("NO RESULT FOR ... terminal state") instead of "NOTHING TO FORWARD ...
    # Delegate first, then forward" -- see test_forward_member_answer_handoff.py's
    # test_nothing_stored_records_nothing_and_says_so for the full rationale.
    out = await _forward(_tool({}, {}), "researcher")
    assert out.startswith("NO RESULT FOR")
    assert "terminal state" in out


@pytest.mark.asyncio
async def test_a_stored_but_blocked_answer_is_not_forwarded_and_not_recorded():
    answers = {"researcher": "I'll check the routes. Let me look at the files."}
    forwarded = {}
    out = await _forward(_tool(answers, forwarded), "researcher")
    assert out.startswith("NOTHING TO FORWARD")
    assert "researcher" not in forwarded


@pytest.mark.asyncio
async def test_budget_exhausted_member_text_is_not_forwarded():
    answers = {"researcher": _BUDGET_EXHAUSTED_ANSWER}
    forwarded = {}
    out = await _forward(_tool(answers, forwarded), "researcher")
    assert out.startswith("NOTHING TO FORWARD")
    assert "researcher" not in forwarded


def test_raw_transcript_remains_available_for_tracing_regardless_of_forwarding():
    """team._member_results (the raw transcript) is a separate dict from `forwarded`,
    untouched by this phase -- a BLOCKED/non-forwardable answer is still there for
    debugging even though nothing was recorded into `forwarded`."""
    answers = {"researcher": "I'll check the routes."}
    forwarded = {}
    r = _build_member_result(answers["researcher"])
    assert r.status == "BLOCKED"
    assert r.raw == answers["researcher"]          # nothing lost from the source dict
    assert answers["researcher"] == "I'll check the routes."  # untouched by _build_member_result


# ── 8. existing B duplication/loss tests continue passing (spot-checked here too) ───────

def test_b_carry_mechanism_is_untouched_by_phase_e():
    """_forwarded_gap/_with_forwarded_evidence operate on whatever text they are given --
    Phase E only changes WHAT gets recorded into `forwarded`, not how carry/duplication is
    decided. A stripped MemberResult.answer carried through the Coordinator's own text
    still produces no append; one it drops still gets appended, once."""
    answer = _build_member_result(T10_RAW).answer
    carried = answer  # the Coordinator reproduces the stripped answer exactly
    assert _forwarded_gap(answer, carried) == ("carried", [])
    assert _with_forwarded_evidence(carried, SimpleNamespace(_forwarded_members={"researcher": answer})) == carried

    dropped_content = "Unrelated answer."
    out = _with_forwarded_evidence(dropped_content, SimpleNamespace(_forwarded_members={"researcher": answer}))
    assert out.count(answer.strip()) <= 1          # never duplicated
    assert answer.strip() in out                   # never lost


@pytest.mark.asyncio
async def test_forwarding_a_stripped_answer_twice_does_not_duplicate_in_the_final_text():
    answers = {"researcher": T10_RAW}
    forwarded = {}
    await _forward(_tool(answers, forwarded), "researcher")
    stripped_answer = forwarded["researcher"]
    once = _with_forwarded_evidence("Unrelated.", SimpleNamespace(_forwarded_members=forwarded))
    twice = _with_forwarded_evidence(once, SimpleNamespace(_forwarded_members=forwarded))
    assert once == twice
    assert once.count(stripped_answer.strip()) == 1


# ── MemberResult shape / misc ─────────────────────────────────────────────────────────────

def test_member_result_is_a_plain_dataclass_with_the_five_fields():
    r = MemberResult(status="COMPLETE", answer="x", evidence=[], unresolved=[], raw="x")
    assert (r.status, r.answer, r.evidence, r.unresolved, r.raw) == ("COMPLETE", "x", [], [], "x")


def test_declaration_index_and_thin_report_markers_are_shared_constants_not_retyped():
    import inspect
    from swarm import team as team_mod
    src = inspect.getsource(team_mod)
    # the injection sites use the constants, not a re-typed literal
    assert 'f"\\n\\n{_DECLARATION_INDEX_MARKER}' in src
    assert 'f"\\n\\n{_THIN_REPORT_MARKER}' in src
