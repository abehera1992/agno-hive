"""T40 Track B -- precise threshold-boundary tests for T39's literal-probe
hard-stop (_LITERAL_SEARCH_PROBE_LIMIT, swarm/team.py), driven through the real
production hook (_make_read_cache_tool_hook), not a reimplementation.

T39's own test suite (tests/test_t39_literal_search_probe_escalation.py)
proved the mechanism's shape (resets on match/regex-attempt, fires somewhere
at-or-before the limit on a sustained run, never fires on a small legitimate
enumeration). This file adds the exact boundary T40's mission calls out
specifically: 19 consecutive unproductive literal probes must never fire, and
the 20th must fire exactly then -- not "eventually," precisely at the
documented threshold.
"""
import pytest

from swarm.team import _make_read_cache_tool_hook, _LITERAL_SEARCH_PROBE_LIMIT
from tests.test_team_read_cache_hook import _FakeAgent


def _no_match(pattern: str) -> str:
    return f"No matches for: {pattern}"


@pytest.mark.asyncio
async def test_exactly_one_below_the_limit_never_fires():
    assert _LITERAL_SEARCH_PROBE_LIMIT == 20, (
        "this test's own precision depends on the documented constant; if it "
        "changes, this assertion should be updated deliberately, not silently"
    )
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "**/*"

    async def fake_search_files(**kwargs):
        return _no_match(kwargs["pattern"])

    for i in range(_LITERAL_SEARCH_PROBE_LIMIT - 1):  # 19 calls
        await hook("search_files", fake_search_files,
                   {"pattern": f"useGuess{i}", "glob_filter": glob}, agent=researcher)

    assert researcher.tool_choice is None, (
        f"{_LITERAL_SEARCH_PROBE_LIMIT - 1} consecutive unproductive literal "
        f"probes must not reach the {_LITERAL_SEARCH_PROBE_LIMIT}-probe threshold"
    )


@pytest.mark.asyncio
async def test_exactly_the_limit_fires_on_that_call():
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "**/*"

    async def fake_search_files(**kwargs):
        return _no_match(kwargs["pattern"])

    for i in range(_LITERAL_SEARCH_PROBE_LIMIT - 1):  # 19 calls, no fire yet
        await hook("search_files", fake_search_files,
                   {"pattern": f"useGuess{i}", "glob_filter": glob}, agent=researcher)
    assert researcher.tool_choice is None

    # The 20th consecutive unproductive literal probe.
    result = await hook("search_files", fake_search_files,
                        {"pattern": "useGuessFinal", "glob_filter": glob}, agent=researcher)

    assert researcher.tool_choice == "none", (
        f"the {_LITERAL_SEARCH_PROBE_LIMIT}th consecutive unproductive literal "
        f"probe must trigger _force_text_only exactly at this call"
    )
    assert "NOTE FROM THE RUN" in result
