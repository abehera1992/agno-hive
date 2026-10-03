"""Phase C.3 (2026-10-03): bounded member-interaction context.

C.1 proved share_member_interactions=True (swarm/team.py's _build_team) feeds
EVERY prior member interaction this run into every subsequent delegation's
prompt, via agno's own get_team_member_interactions_str, called by agno's own
_determine_team_member_interactions with NO max_interactions bound anywhere in
this codebase's call chain -- confirmed directly from the installed agno
source (agno/team/_tools.py:449-451, agno/utils/team.py:74-119), and that
agno's own fallback (interaction.content or the joined tool-call contents)
can embed raw tool output when a member's own final content is empty. Live
instrumentation (commit 48d6974, temporary) measured the real, bounded shape
of a normal 2-delegation run (1 interaction forwarded, 2,008 serialized
chars, content_present=True, tool_output_chars=0) -- small today, UNBOUNDED
BY CONSTRUCTION as delegation count grows, exactly matching C.1's proven
8-delegation T12 explosion (~110K tokens).

This file proves the fix: agno.team._tools.get_team_member_interactions_str
(the name as THAT module's own namespace holds it -- not agno.utils.team's,
which _tools.py's own `from ... import` already bound at import time) is
patched to call the real, unmodified underlying function with max_interactions
capped at _MAX_FORWARDED_MEMBER_INTERACTIONS (3) -- reusing agno's own
"most recent N" parameter, never a new truncation scheme -- regardless of
how many interactions this run has actually accumulated.
"""
import swarm.team as team_mod
from swarm.team import (
    _MAX_FORWARDED_MEMBER_INTERACTIONS,
    _bounded_get_team_member_interactions_str,
)


class _FakeRunOutput:
    """Minimal stand-in for agno's real RunOutput/TeamRunOutput -- the real
    get_team_member_interactions_str only ever calls .to_dict() on it."""

    def __init__(self, content=None, tools=None):
        self._content = content
        self._tools = tools or []

    def to_dict(self):
        return {"content": self._content, "tools": self._tools}


def _interaction(member_name, task, content=None, tools=None):
    return {
        "run_response": _FakeRunOutput(content=content, tools=tools),
        "member_name": member_name,
        "task": task,
    }


def _team_run_context(interactions):
    return {"member_responses": interactions}


def test_bound_constant_is_small_and_matches_the_patch():
    """The bound is a small, named constant -- not an inline magic number --
    and the installed patch is live against the real agno module namespace."""
    assert 1 <= _MAX_FORWARDED_MEMBER_INTERACTIONS <= 5
    import agno.team._tools as agno_tools
    assert agno_tools.get_team_member_interactions_str is _bounded_get_team_member_interactions_str


def test_fewer_than_bound_forwards_all_of_them():
    interactions = [
        _interaction("Researcher", "task 1", content="finding one"),
        _interaction("Coder", "task 2", content="finding two"),
    ]
    ctx = _team_run_context(interactions)
    result = _bounded_get_team_member_interactions_str(ctx)
    assert "finding one" in result
    assert "finding two" in result


def test_more_than_bound_forwards_only_the_most_recent_N():
    """The exact C.1 mechanism, reproduced generically: 6 accumulated
    interactions (more than T12's own proven explosion point of ~7-8) must
    forward only the most recent _MAX_FORWARDED_MEMBER_INTERACTIONS, not all
    of them -- this is the fix itself, not a hypothetical."""
    interactions = [
        _interaction(f"Researcher", f"task {i}", content=f"finding number {i}")
        for i in range(6)
    ]
    ctx = _team_run_context(interactions)
    result = _bounded_get_team_member_interactions_str(ctx)
    # Only the last _MAX_FORWARDED_MEMBER_INTERACTIONS (3) findings survive.
    for i in range(6 - _MAX_FORWARDED_MEMBER_INTERACTIONS):
        assert f"finding number {i}" not in result, (
            f"interaction {i} should have been dropped by the bound")
    for i in range(6 - _MAX_FORWARDED_MEMBER_INTERACTIONS, 6):
        assert f"finding number {i}" in result, (
            f"interaction {i} should have been kept (most recent {_MAX_FORWARDED_MEMBER_INTERACTIONS})")


def test_empty_content_tool_fallback_is_still_bounded_not_eliminated():
    """Proves the fix bounds COUNT, not content shape -- an interaction whose
    content is empty (triggering agno's own raw-tool-output fallback, the
    proven T12 multiplier) is still subject to the same recency bound as any
    other. This does not change agno's fallback behavior itself (out of
    scope, Hard Rule: no broad orchestration rewrite) -- it only ensures that
    behavior can never compound across more than _MAX_FORWARDED_MEMBER_
    INTERACTIONS interactions regardless of any single interaction's size."""
    huge_tool_output = "X" * 50_000
    interactions = [
        _interaction("Researcher", "task 0", content="small finding 0"),
        _interaction("Researcher", "task 1", content=None,
                     tools=[{"content": huge_tool_output}]),  # empty content -> fallback fires
        _interaction("Researcher", "task 2", content="small finding 2"),
        _interaction("Researcher", "task 3", content="small finding 3"),
    ]
    ctx = _team_run_context(interactions)
    result = _bounded_get_team_member_interactions_str(ctx)
    # task 0 is interaction index 0 of 4 -- older than the most recent 3, dropped.
    assert "small finding 0" not in result
    # The huge-tool-output interaction (index 1) IS within the most recent 3 here,
    # so it is still forwarded -- proving the bound did not silently neuter the
    # fallback case, only cap how many such interactions can ever accumulate.
    assert huge_tool_output in result
    assert "small finding 2" in result
    assert "small finding 3" in result


def test_caller_supplied_max_interactions_is_never_widened():
    """If a future agno version's call site ever passes its own
    max_interactions, this patch must take the SMALLER of the two bounds --
    never silently widen a caller's own narrower request."""
    interactions = [
        _interaction("Researcher", f"task {i}", content=f"finding number {i}")
        for i in range(6)
    ]
    ctx = _team_run_context(interactions)
    result = _bounded_get_team_member_interactions_str(ctx, max_interactions=1)
    assert "finding number 5" in result  # the single most recent
    assert "finding number 4" not in result


def test_zero_interactions_is_a_clean_noop():
    ctx = _team_run_context([])
    result = _bounded_get_team_member_interactions_str(ctx)
    assert result == ""


def test_real_agno_underlying_function_is_genuinely_invoked():
    """Confirms this patch calls through to the REAL, unmodified agno
    function (via _ORIGINAL_AGNO_GET_TEAM_MEMBER_INTERACTIONS_STR) rather
    than reimplementing serialization -- the <member_interaction_context>
    wrapper tags are agno's own real output format, not ours."""
    interactions = [_interaction("Researcher", "t", content="real agno format check")]
    ctx = _team_run_context(interactions)
    result = _bounded_get_team_member_interactions_str(ctx)
    assert "<member_interaction_context>" in result
    assert "Member: Researcher" in result
    assert "real agno format check" in result
