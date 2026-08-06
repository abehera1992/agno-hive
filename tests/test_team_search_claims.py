"""Regression tests: the orchestrator must not accept a "NOT FOUND, I searched for
X" claim unless a search_files()/find_files() call for X actually happened.

Confirmed live 2026-08-06: a Coder answered "expiry handling: NOT FOUND (searched
for expiry_date, valid_until, expires_at ... in all files)" -- but a real field
named ewb_valid_until exists directly on the vouchers table. "valid_until" as a
literal ripgrep pattern would have matched it trivially as a substring, meaning the
claimed search either never ran, or ran scoped to the wrong file/glob and the
negative result was over-generalized to "doesn't exist anywhere". Neither
_verify_claims (the claim names no fabricated symbol to grep for) nor
_count_read_calls (some reading DID happen, just not of the claimed term) could
catch this.
"""
import json
from types import SimpleNamespace

import pytest

from swarm import team


def _msgs(*items):
    return SimpleNamespace(messages=list(items))


def _tool_msg(name: str, content: str):
    return SimpleNamespace(role="tool", tool_name=name, content=content)


def _search_call(name: str, **kwargs):
    """An assistant message requesting a search_files()/find_files() call --
    tool_calls live on the REQUEST side (agno's own message shape), not the
    tool's response text, which only has the result."""
    return SimpleNamespace(
        role="assistant",
        tool_calls=[{"function": {"name": name, "arguments": json.dumps(kwargs)}}],
    )


class _FakeTeam:
    def __init__(self, retry_result):
        self._retry_result = retry_result
        self.prompts = []

    async def arun(self, prompt):
        self.prompts.append(prompt)
        return self._retry_result


# ── _claimed_search_terms ─────────────────────────────────────────────────────

def test_claimed_search_terms_extracts_comma_separated_list():
    content = "expiry handling — NOT FOUND (searched for expiry_date, valid_until, expires_at in all files)"
    terms = team._claimed_search_terms(content)
    assert terms == ["expiry_date", "valid_until", "expires_at"]


def test_claimed_search_terms_dedupes():
    content = "NOT FOUND (searched for bulk, batch, bulk in all files)"
    terms = team._claimed_search_terms(content)
    assert terms == ["bulk", "batch"]


def test_claimed_search_terms_empty_when_no_claim_present():
    assert team._claimed_search_terms("The Vouchers module supports CRUD operations.") == []


# ── _extract_searched_patterns ────────────────────────────────────────────────

def test_extract_searched_patterns_reads_the_pattern_argument():
    result = _msgs(_search_call("search_files", pattern="valid_until", glob_filter="**/*.py"))
    patterns = team._extract_searched_patterns(result)
    assert "valid_until" in patterns


def test_extract_searched_patterns_reads_find_files_glob_pattern():
    result = _msgs(_search_call("find_files", glob_pattern="**/vouchers_api.py"))
    patterns = team._extract_searched_patterns(result)
    assert "**/vouchers_api.py" in patterns


def test_extract_searched_patterns_ignores_unrelated_tools():
    result = _msgs(_search_call("get_file_content", relative_path="x.py"))
    patterns = team._extract_searched_patterns(result)
    assert patterns == set()


def test_extract_searched_patterns_unions_across_multiple_results():
    r1 = _msgs(_search_call("search_files", pattern="bulk"))
    r2 = _msgs(_search_call("search_files", pattern="batch"))
    patterns = team._extract_searched_patterns(r1, r2)
    assert patterns == {"bulk", "batch"}


# ── _unverified_claimed_searches ──────────────────────────────────────────────

def test_unverified_claimed_searches_flags_a_search_that_never_ran():
    content = "expiry handling — NOT FOUND (searched for valid_until in all files)"
    result = _msgs(_tool_msg("get_file_content", "some file content"))  # no search at all

    unverified = team._unverified_claimed_searches(content, result)

    assert unverified == ["valid_until"]


def test_unverified_claimed_searches_accepts_a_substring_match():
    """Confirmed live 2026-08-06: the claimed term ('valid_until') and the real
    field name ('ewb_valid_until') are substrings of each other -- a search that
    genuinely used either string as its pattern counts as covering the claim."""
    content = "expiry handling — NOT FOUND (searched for valid_until in all files)"
    result = _msgs(_search_call("search_files", pattern="ewb_valid_until"))

    unverified = team._unverified_claimed_searches(content, result)

    assert unverified == []


def test_unverified_claimed_searches_accepts_an_exact_match():
    content = "NOT FOUND (searched for bulk, batch in all files)"
    result = _msgs(
        _search_call("search_files", pattern="bulk"),
        _search_call("search_files", pattern="batch"),
    )

    unverified = team._unverified_claimed_searches(content, result)

    assert unverified == []


def test_unverified_claimed_searches_flags_only_the_missing_terms():
    content = "NOT FOUND (searched for bulk, batch, generate_multiple in all files)"
    result = _msgs(_search_call("search_files", pattern="bulk"))  # only one of three ran

    unverified = team._unverified_claimed_searches(content, result)

    assert unverified == ["batch", "generate_multiple"]


def test_unverified_claimed_searches_empty_when_no_claim_present():
    content = "The Vouchers module supports CRUD operations."
    result = _msgs(_tool_msg("get_file_content", "some file content"))

    assert team._unverified_claimed_searches(content, result) == []


# ── _verified_answer integration ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verified_answer_retries_when_a_claimed_search_never_ran():
    original_result = _msgs(_tool_msg("get_file_content", "models.py content"))  # no search call
    content = "expiry handling — NOT FOUND (searched for valid_until in all files)."

    retry_result = SimpleNamespace(
        content="expiry handling — FOUND: ewb_valid_until at models.py:386.",
        messages=[_search_call("search_files", pattern="valid_until")],
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "research vouchers", fake_team, None, result=original_result)

    assert len(fake_team.prompts) == 1  # it actually retried
    assert "valid_until" in fake_team.prompts[0]
    assert out.startswith("expiry handling — FOUND")


@pytest.mark.asyncio
async def test_verified_answer_appends_disclaimer_when_retry_still_has_no_search():
    original_result = _msgs(_tool_msg("get_file_content", "models.py content"))
    content = "expiry handling — NOT FOUND (searched for valid_until in all files)."

    retry_result = SimpleNamespace(
        content="expiry handling — still NOT FOUND (searched for valid_until in all files).",
        messages=[_tool_msg("get_file_content", "models.py content")],  # still no search
    )
    fake_team = _FakeTeam(retry_result)

    out = await team._verified_answer(content, "research vouchers", fake_team, None, result=original_result)

    assert len(fake_team.prompts) == 1
    assert "UNVERIFIED" in out


@pytest.mark.asyncio
async def test_verified_answer_does_not_retry_when_the_claimed_search_actually_ran():
    original_result = _msgs(
        _search_call("search_files", pattern="valid_until"),
        _tool_msg("get_file_content", "models.py content"),
    )
    content = "expiry handling — NOT FOUND (searched for valid_until in all files)."
    fake_team = _FakeTeam(retry_result=None)

    out = await team._verified_answer(content, "research vouchers", fake_team, None, result=original_result)

    assert fake_team.prompts == []  # no retry needed
    assert out == content
