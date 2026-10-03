"""Phase C.3 (2026-10-03): evidence-adequacy fix for _make_search_before_browse_gate_hook.

C.3-PREFLIGHT (the investigation immediately preceding this phase) proved, live,
that the gate conflated "a search tool was called" with "evidence was found":
Researcher searched for the literal word "verification", search_files correctly
reported zero matches (the real term is "KYC" -- confirmed in this project's own
CLAUDE.md), and the pre-existing gate treated that zero-hit call exactly like a
real hit, clearing itself for the rest of the run. The run's own later guard
("THE ANSWER IS LONGER THAN ITS EVIDENCE") caught the resulting bad answer, but
only after synthesis -- too late to change the outcome.

This file tests the narrow fix: a LEXICAL search tool (search_files/
search_files_batch) only satisfies the gate if its result is non-empty;
lightrag_query still satisfies it unconditionally (it is the designated
escalation target, already rate-limited elsewhere by _LIGHTRAG_QUERY_CAP). A
zero-hit lexical search no longer silently passes -- it blocks the next browse
call ONCE, with a redirect that specifically names the vocabulary-gap failure
mode and suggests lightrag_query or a different term, then stands down on the
second blocked browse call (same "one redirect, then stand down" convention
already used elsewhere in this module for the target-resolution gate), so a
genuinely absent concept cannot lock Researcher out of ever reading anything.

Explicitly NOT in scope (see the C.3 phase report's root-cause section): a
non-empty but irrelevant lexical hit. No live evidence in either C.3-PREFLIGHT's
battery or this phase's own probes showed that failure mode actually occurring
for this gate, so no new machinery was built for it here -- see _result_text's
own existing round-trip, already exercised by test_search_before_browse_gate_
and_forced_answer.py, for why a real non-empty result (however good or bad) is
left exactly as it was.
"""
import pytest

from swarm.team import (
    _is_empty_lexical_search_result,
    _make_search_before_browse_gate_hook,
)

_MULTI_PART_TASK = (
    "Compare Phase 1 requirements against the actual implementation. "
    "What's already covered vs what's still missing?"
)


class _FakeAgent:
    def __init__(self, name):
        self.name = name


async def _fake_browse(**kwargs):
    return f"browsed: {kwargs}"


def _fake_empty_search(text="No matches for: verification"):
    async def _search(**kwargs):
        return text
    return _search


def _fake_real_search(text="API/business-service/kyc_api.py:42: def check_kyc_status"):
    async def _search(**kwargs):
        return text
    return _search


# ── _is_empty_lexical_search_result: the classifier itself ─────────────────────


def test_empty_string_is_empty():
    assert _is_empty_lexical_search_result("")


def test_whitespace_only_is_empty():
    assert _is_empty_lexical_search_result("   \n  ")


@pytest.mark.parametrize("prefix", ["No matches", "No files", "Error", "Invalid"])
def test_known_zero_hit_prefixes_are_empty(prefix):
    assert _is_empty_lexical_search_result(f"{prefix} for: whatever")


def test_real_content_is_not_empty():
    assert not _is_empty_lexical_search_result(
        "API/business-service/kyc_api.py:42: def check_kyc_status"
    )


def test_wrapper_object_is_unwrapped_via_result_text():
    """Mirrors the real MCP wrapper shape _result_text already handles -- the
    gate must not be fooled by an object whose str() isn't the actual text."""
    class _Wrapper:
        content = "No matches for: verification"
    assert _is_empty_lexical_search_result(_Wrapper())


# ── Lexical success: no unnecessary escalation ──────────────────────────────


@pytest.mark.asyncio
async def test_non_empty_search_files_result_satisfies_the_gate():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("search_files", _fake_real_search(), {"pattern": "kyc"},
               agent=_FakeAgent("Researcher"))

    result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_non_empty_search_files_batch_result_satisfies_the_gate():
    """search_files_batch was entirely invisible to the old set -- confirm it now
    both participates in, and can satisfy, the gate."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("search_files_batch", _fake_real_search(), {"pattern": "kyc"},
               agent=_FakeAgent("Researcher"))

    result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_lightrag_query_satisfies_the_gate_regardless_of_result_content():
    """lightrag_query stays the unconditional escalation target -- even a thin
    result counts, since looping it further is _LIGHTRAG_QUERY_CAP's job, not
    this gate's."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("lightrag_query", _fake_empty_search("no relevant chunks found"),
               {"query": "seller verification"}, agent=_FakeAgent("Researcher"))

    result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert result.startswith("browsed:")


# ── Zero-result vocabulary gap: bounded escalation ──────────────────────────


@pytest.mark.asyncio
async def test_empty_search_files_result_does_not_satisfy_the_gate():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("search_files", _fake_empty_search(), {"pattern": "verification"},
               agent=_FakeAgent("Researcher"))

    blocked = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert "REDIRECTED" in blocked


@pytest.mark.asyncio
async def test_redirect_after_empty_search_names_the_vocabulary_gap_and_lightrag():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("search_files", _fake_empty_search(), {"pattern": "verification"},
               agent=_FakeAgent("Researcher"))
    blocked = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))

    assert "NOTHING" in blocked
    assert "lightrag_query" in blocked
    assert "vocabulary" in blocked.lower()


@pytest.mark.asyncio
async def test_escalating_to_a_real_search_after_an_empty_one_unblocks_normally():
    """The intended recovery path: empty lexical search, blocked once, model
    retries with a different term (or lightrag_query) and gets a real hit --
    every later browse call then passes freely, same as the pre-C.3 behavior."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("search_files", _fake_empty_search(), {"pattern": "verification"},
               agent=_FakeAgent("Researcher"))
    blocked = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert "REDIRECTED" in blocked

    await hook("search_files", _fake_real_search(), {"pattern": "kyc"},
               agent=_FakeAgent("Researcher"))

    for _ in range(3):
        result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
        assert result.startswith("browsed:")


@pytest.mark.asyncio
async def test_two_consecutive_empty_searches_still_block_only_once_more():
    """Two separate empty searches in a row must not compound into a longer
    block -- the stand-down is keyed on redirect_count (blocked browse calls),
    not on how many empty searches preceded it."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("search_files", _fake_empty_search("No matches for: verification"),
               {"pattern": "verification"}, agent=_FakeAgent("Researcher"))
    first_block = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    assert "REDIRECTED" in first_block

    await hook("search_files", _fake_empty_search("No matches for: kyc"),
               {"pattern": "kyc"}, agent=_FakeAgent("Researcher"))
    second_attempt = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
    # Stand-down: the second blocked browse call lets the run make progress
    # rather than looping a third time on a concept that may genuinely be absent.
    assert second_attempt.startswith("browsed:")


@pytest.mark.asyncio
async def test_stand_down_is_bounded_to_exactly_one_redirect_after_an_empty_search():
    """Mirrors the pre-existing target-resolution gate's own documented
    reasoning word-for-word: one redirect, then stand down -- never a third
    block, regardless of what happens in between. Scoped to AFTER a genuine
    (empty) search attempt -- see the next test for the complementary case."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    await hook("search_files", _fake_empty_search(), {"pattern": "verification"},
               agent=_FakeAgent("Researcher"))

    redirected = 0
    for _ in range(5):
        result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
        if "REDIRECTED" in result:
            redirected += 1

    assert redirected == 1


@pytest.mark.asyncio
async def test_never_searching_at_all_blocks_every_single_browse_call():
    """The pre-existing, unchanged guarantee this fix must not regress (locked
    in by test_search_before_browse_gate_and_forced_answer.py's own
    test_all_three_browse_tool_names_are_blocked): with ZERO search attempts of
    any kind, the stand-down never applies -- Step 3a's whole point is that
    Researcher cannot escape by simply browsing instead of searching."""
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    redirected = 0
    for _ in range(5):
        result = await hook("get_file_content", _fake_browse, {}, agent=_FakeAgent("Researcher"))
        if "REDIRECTED" in result:
            redirected += 1

    assert redirected == 5


# ── Regression: pre-existing behavior for a first-ever blocked call (no search
# attempted at all yet) must be byte-for-byte the original generic message ──


@pytest.mark.asyncio
async def test_first_block_with_no_prior_search_keeps_the_original_generic_message():
    hook = _make_search_before_browse_gate_hook(task=_MULTI_PART_TASK)

    result = await hook("find_files", _fake_browse, {}, agent=_FakeAgent("Researcher"))

    assert "REDIRECTED" in result
    assert "NOTHING" not in result
    assert "search_files(<the checklist" in result
