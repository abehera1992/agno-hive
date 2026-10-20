"""T39 Priority A/B -- the primary defect this phase selected for implementation.

Root cause, proven by direct ZGX journalctl recovery of session fd5bf842c52c
(T13a-T37-LIVE1, the delegation that made 77 search_files calls): every single
call used a BARE LITERAL identifier (no regex metacharacter at all -- not even
an alternation), scoped correctly the whole time (the real hooks ARE inside
that exact glob_filter, confirmed directly against the live EkamApp repo:
`grep -rnE 'use[A-Za-z]*(Query|Mutation)' .../vouchers/` finds
useGetVouchersQuery and useCreateVoucherMutation via page.tsx's own import
line, in one call). search_files's own docstring (hive-mcp/tools/context.py)
explicitly advertises regex support with a wildcard example
("search_files('import.*React', '**/*.tsx')") -- ruling out Hypothesis A
(capability unavailable) and Hypothesis B (undiscoverable). An EXISTING
advisory mechanism (_BATCH_ALTERNATIVE_HINTS, swarm/team.py) already recommends
exactly the right fix ("search_files takes a REGEX: one call with an
alternation...") and fired 9 TIMES across this exact 77-call run (every 8th
call) -- and was ignored every time. This is Hypothesis D (useful advice
ignored) as the earliest-proven failure boundary: the capability, its
discoverability, and a correctly-targeted nudge were all present; only the
model's own strategy never changed.

Per Decision Gate A ("If useful results are ignored, investigate the
result-consumption or progress-control boundary... prefer [a control
intervention] over a broad architecture change... If prompt-only behavior is
unreliable under the controlled comparison, investigate a minimal
deterministic control"), this phase escalates the SAME existing mechanism
(same injection channel the advisory hint already used, same _force_text_only
actuator the budget guard hook already uses and that THIS incident's own
journal confirms genuinely stops further tool calls -- `[budget] Researcher:
call 79/80` was immediately followed by zero further tool calls) for the one
case the advisory form measurably failed on: a long, uninterrupted run of
literal-pattern, zero-match search_files calls.

Driven through the REAL hook (not a reimplementation of its logic), same
discipline this file's sibling read-cache tests already establish.
"""
import pytest

from swarm.team import (
    _make_read_cache_tool_hook, _LITERAL_SEARCH_PROBE_LIMIT,
)
from tests.test_team_read_cache_hook import _FakeAgent


def _no_match(pattern: str) -> str:
    return f"No matches for: {pattern}"


@pytest.mark.asyncio
async def test_the_exact_incident_shape_forces_text_only_before_the_real_streak_length():
    """Reproduces the real incident's own 77-call shape with distinct literal
    patterns (same as the real run -- zero repeats). Asserts the agent is
    forced text-only strictly BEFORE 77 calls -- the whole point of this fix."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/**/*"

    guessed_names = [f"useGuess{i}" for i in range(77)]

    async def fake_search_files(**kwargs):
        return _no_match(kwargs["pattern"])

    calls_before_forced = 0
    for name in guessed_names:
        await hook("search_files", fake_search_files,
                   {"pattern": name, "glob_filter": glob}, agent=researcher)
        calls_before_forced += 1
        if researcher.tool_choice == "none":
            break

    assert researcher.tool_choice == "none", (
        "the agent must be forced text-only at some point during 77 consecutive "
        "zero-match literal guesses -- it never was, in the real incident"
    )
    assert calls_before_forced <= _LITERAL_SEARCH_PROBE_LIMIT, (
        f"forced at call {calls_before_forced}, expected at or before "
        f"{_LITERAL_SEARCH_PROBE_LIMIT} -- the real incident took 77"
    )


@pytest.mark.asyncio
async def test_a_match_resets_the_streak_and_never_forces():
    """Negative control: if the agent eventually finds something, the streak
    that led up to it must not itself trigger a force -- only a SUSTAINED,
    UNBROKEN run of zero-match literal guesses should.

    Each cycle's hit is a DISTINCT pattern (realistic: a model does not
    re-search for the exact identifier it already found) -- a REPEATED
    identical call would be served from the read-cache and correctly skip
    this hook's whole fresh-fetch branch, which is a distinct, already-tested
    cache behavior, not this mechanism's own reset path."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "**/*"
    real_hooks = ["useGetVouchersQuery", "useGetVoucherQuery", "useCreateVoucherMutation"]

    async def fake_search_files(**kwargs):
        pattern = kwargs["pattern"]
        if pattern in real_hooks:
            return f"page.tsx:33: {pattern}(...)"
        return _no_match(pattern)

    # (_LITERAL_SEARCH_PROBE_LIMIT - 1) misses, then a hit, repeated -- the
    # streak must never accumulate past the reset point.
    for cycle, hit in enumerate(real_hooks):
        for i in range(_LITERAL_SEARCH_PROBE_LIMIT - 1):
            await hook("search_files", fake_search_files,
                       {"pattern": f"useMiss{cycle}_{i}", "glob_filter": glob},
                       agent=researcher)
        await hook("search_files", fake_search_files,
                   {"pattern": hit, "glob_filter": glob},
                   agent=researcher)

    assert researcher.tool_choice is None, (
        "a real match must reset the streak -- repeated near-threshold runs "
        "that each end in a hit must never force text-only"
    )


@pytest.mark.asyncio
async def test_a_regex_alternation_attempt_resets_the_streak_even_if_it_also_misses():
    """A model that DOES try a regex/alternation must never be penalized for
    the attempt, even if that specific pattern happens to find nothing --
    punishing a correct strategy choice would teach the opposite lesson."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "**/*"

    async def fake_search_files(**kwargs):
        return _no_match(kwargs["pattern"])

    for i in range(_LITERAL_SEARCH_PROBE_LIMIT - 1):
        await hook("search_files", fake_search_files,
                   {"pattern": f"useMiss{i}", "glob_filter": glob}, agent=researcher)
    # A regex alternation -- still misses, but is NOT a bare literal.
    await hook("search_files", fake_search_files,
               {"pattern": "useFoo|useBar|useBaz", "glob_filter": glob}, agent=researcher)
    # One more literal miss -- streak must have reset, so this alone can't force.
    await hook("search_files", fake_search_files,
               {"pattern": "useOneMore", "glob_filter": glob}, agent=researcher)

    assert researcher.tool_choice is None, (
        "the regex attempt must reset the streak even though it also found nothing"
    )


@pytest.mark.asyncio
async def test_a_legitimate_small_enumeration_never_fires():
    """Positive control: the 9-real-endpoint shape _BATCH_HINT_EVERY was already
    tuned around must stay completely silent under this new mechanism too."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "**/*"

    async def fake_search_files(**kwargs):
        return f"router.py:{10 + len(kwargs['pattern'])}: @router.{kwargs['pattern']}(...)"

    for name in ["get", "post", "put", "delete", "patch", "options", "head", "list", "create"]:
        await hook("search_files", fake_search_files,
                   {"pattern": name, "glob_filter": glob}, agent=researcher)

    assert researcher.tool_choice is None


@pytest.mark.asyncio
async def test_forcing_appends_a_note_explaining_what_happened_and_what_to_do():
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "**/*"

    async def fake_search_files(**kwargs):
        return _no_match(kwargs["pattern"])

    result = ""
    for i in range(_LITERAL_SEARCH_PROBE_LIMIT):
        result = await hook("search_files", fake_search_files,
                            {"pattern": f"useGuess{i}", "glob_filter": glob}, agent=researcher)

    assert "NOTE FROM THE RUN" in result
    assert "REGEX" in result
    assert "UNRESOLVED" in result
    assert "cannot make another tool call" in result


@pytest.mark.asyncio
async def test_different_tools_do_not_interfere_with_the_streak():
    """An interleaved get_file_content call (a different tool) must not reset
    the search_files streak -- only a match or a regex attempt on search_files
    itself should."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    glob = "**/*"

    async def fake_search_files(**kwargs):
        return _no_match(kwargs["pattern"])

    async def fake_get_file_content(**kwargs):
        return "some file content"

    for i in range(_LITERAL_SEARCH_PROBE_LIMIT):
        await hook("search_files", fake_search_files,
                   {"pattern": f"useGuess{i}", "glob_filter": glob}, agent=researcher)
        await hook("get_file_content", fake_get_file_content,
                   {"relative_path": f"file{i}.py"}, agent=researcher)

    assert researcher.tool_choice == "none", (
        "interleaved calls to a DIFFERENT tool must not reset the search_files "
        "literal-probe streak"
    )


@pytest.mark.asyncio
async def test_each_member_has_its_own_independent_streak():
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    reviewer = _FakeAgent("Reviewer")
    glob = "**/*"

    async def fake_search_files(**kwargs):
        return _no_match(kwargs["pattern"])

    for i in range(_LITERAL_SEARCH_PROBE_LIMIT - 1):
        await hook("search_files", fake_search_files,
                   {"pattern": f"useGuess{i}", "glob_filter": glob}, agent=researcher)

    await hook("search_files", fake_search_files,
               {"pattern": "useOther1", "glob_filter": glob}, agent=reviewer)

    assert researcher.tool_choice is None
    assert reviewer.tool_choice is None
