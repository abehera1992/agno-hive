"""Regression tests for the read-only tool-call cache hook.

Confirmed live 2026-08-07: get_files_batch was called 21-29 times for the SAME
2 files across one 6-agent coordinate-mode run, because agno's
share_member_interactions only forwards a teammate's final TEXT answer, never
the raw tool result. A companion prompt-level fix (telling agents to forward
and trust citations) did not measurably reduce this in a live re-test, since
it depends on model instruction-following rather than a mechanical guarantee.

Two mechanical facts, verified by reading agno's actual source (not assumed),
that this hook and its wiring depend on:
1. Every MCP-server-backed tool call is async on the client side unconditionally
   (agno.utils.mcp.get_entrypoint_for_tool's call_tool is `async def`, no sync
   variant) -- a sync hook doing `function(**args)` would get an unawaited
   coroutine, not the real result. The hook must be async and must await.
2. In mode="coordinate" the coordinator mostly delegates rather than calling
   tools itself -- a hook registered only on Team(tool_hooks=[...]) never sees
   the member agents' own tool calls, which is where the measured redundant
   reads actually happen. Confirmed live: a coordinator-only hook logged
   nothing across live runs that made dozens of get_files_batch calls.
"""
import asyncio

import pytest

from swarm import team
from swarm.team import (
    _make_read_cache_tool_hook, _build_team, _CACHEABLE_READ_TOOLS,
    _collapse_prior_stub_messages, _COLLAPSED_STUB_MARKER,
    _FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS,
)


# ── _make_read_cache_tool_hook: caching behavior ────────────────────────────────

@pytest.mark.asyncio
async def test_cache_hook_calls_through_on_first_call():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "file content here"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert result == "file content here"
    assert calls == [{"relative_path": "x.py"}]


@pytest.mark.asyncio
async def test_cache_hook_serves_cached_result_on_second_identical_call():
    """Confirms the underlying network fetch happens only once per distinct
    (tool, args) pair, however many times it's asked for -- even once the
    repeat is stubbed by the serve-count budget (see the per-agent stubbing
    tests below), the real function itself is never called a second time."""
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return f"content #{len(calls)}"

    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert first == "content #1"
    assert second != "content #2"  # the underlying function was NOT called a second time
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_hook_treats_different_args_as_different_cache_entries():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return kwargs["relative_path"]

    await hook("get_file_content", fake_get_file_content, {"relative_path": "a.py"})
    await hook("get_file_content", fake_get_file_content, {"relative_path": "b.py"})

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_hook_treats_argument_order_as_the_same_cache_entry():
    """A cache key built from insertion-order dict items would treat
    {"a": 1, "b": 2} and {"b": 2, "a": 1} as different calls -- json.dumps with
    sort_keys=True is what prevents that."""
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_search_files(**kwargs):
        calls.append(kwargs)
        return "match"

    await hook("search_files", fake_search_files, {"pattern": "x", "glob_filter": "**/*.py"})
    await hook("search_files", fake_search_files, {"glob_filter": "**/*.py", "pattern": "x"})

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_hook_does_not_cache_a_non_cacheable_tool():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_apply_diff(**kwargs):
        calls.append(kwargs)
        return "review_pending: x.py"

    await hook("apply_diff", fake_apply_diff, {"relative_path": "x.py", "old_string": "a", "new_string": "b"})
    await hook("apply_diff", fake_apply_diff, {"relative_path": "x.py", "old_string": "a", "new_string": "b"})

    assert len(calls) == 2  # never cached -- a write tool must always call through


@pytest.mark.asyncio
async def test_cache_hook_falls_back_to_call_through_on_unserializable_args():
    hook = _make_read_cache_tool_hook()
    calls = []

    class _NotJsonSerializable:
        pass

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "content"

    bad_args = {"relative_path": "x.py", "weird": _NotJsonSerializable()}
    await hook("get_file_content", fake_get_file_content, bad_args)
    await hook("get_file_content", fake_get_file_content, bad_args)

    assert len(calls) == 2  # can't build a cache key -- always calls through, never crashes


@pytest.mark.asyncio
async def test_cache_hook_two_independent_hooks_have_independent_caches():
    """Confirms the cache is scoped to ONE _make_read_cache_tool_hook() call (one
    run), not a module-level shared cache that would leak across sessions."""
    hook_a = _make_read_cache_tool_hook()
    hook_b = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "content"

    await hook_a("get_file_content", fake_get_file_content, {"relative_path": "x.py"})
    await hook_b("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert len(calls) == 2  # hook_b's cache is empty -- it never saw hook_a's call


def test_cacheable_read_tools_excludes_every_mutating_tool():
    """The cache must never intercept a write/mutating tool -- overlap here would
    mean a second identical write call gets silently skipped instead of actually
    running, which is a correctness bug, not an efficiency win."""
    from swarm.team import _MUTATING_TOOLS
    assert _CACHEABLE_READ_TOOLS.isdisjoint(_MUTATING_TOOLS)


# ── per-agent duplicate-serve stubbing (2026-08-11) ─────────────────────────────
# Confirmed live: the network-only cache above was not enough on its own -- a
# Researcher cycled the SAME 4 files for 4+ minutes, each re-serve appending another
# full copy into its own context (a self-reinforcing bloat spiral, not just wasted
# hive-mcp round-trips). Serve 1 of an identical (agent, delegation generation, tool,
# args) key gets the real content; serve 2+ WITHIN THE SAME delegation instance gets
# a short stub instead (see the "delegation-generation scoping" section further down
# for the fresh-budget-per-delegation half of this).

class _FakeModel:
    """Stand-in for agno's Model instance bound to `agent.model`. Only
    `_tool_choice` matters here -- see _bump_consecutive_stub_and_maybe_force_text_only's
    2026-08-19 correction: this is the attribute Model.aresponse_stream's own
    `while True:` loop actually re-reads on every iteration (`tool_choice or
    self._tool_choice`), unlike `agent.tool_choice` which agno only captures
    once, by value, before that loop starts."""

    def __init__(self):
        self._tool_choice = None


class _FakeAgent:
    def __init__(self, name):
        self.name = name
        self.tool_choice = None  # mirrors the real agno Agent's own default
        self.model = _FakeModel()  # mirrors the real agno Agent's own .model


@pytest.mark.asyncio
async def test_second_identical_call_from_the_same_agent_gets_a_stub_not_real_content():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "x" * 500  # stand-in for a real ~500-char file

    researcher = _FakeAgent("Researcher")
    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert first == "x" * 500
    assert second != "x" * 500  # serve 2 is over budget -- stubbed
    assert "x" * 500 not in second  # the stub must not leak the real content either
    assert len(calls) == 1  # the underlying tool was only ever actually called once


@pytest.mark.asyncio
async def test_duplicate_read_stub_names_the_tool_and_repeat_count():
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "file body"

    researcher = _FakeAgent("Researcher")
    await hook("get_file_content", fake_get_file_content, {"relative_path": "models.py"}, agent=researcher)
    stub = await hook("get_file_content", fake_get_file_content, {"relative_path": "models.py"}, agent=researcher)

    assert "get_file_content" in stub
    assert "models.py" in stub
    assert "Researcher" in stub
    assert "do not call this again" in stub.lower()


@pytest.mark.asyncio
async def test_duplicate_read_stub_escalates_wording_from_the_fifth_serve():
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "file body"

    researcher = _FakeAgent("Researcher")
    stubs = []
    for _ in range(6):
        stubs.append(
            await hook("get_file_content", fake_get_file_content, {"relative_path": "models.py"}, agent=researcher)
        )

    # Serve 1 is real content ("file body"); 2-4 are the moderate stub; 5-6 are
    # the escalated stub (_STUB_ESCALATION_SERVE stays 5, unaffected by the
    # budget-of-1 change).
    assert stubs[0] == "file body"
    assert "STOP calling" not in stubs[1]
    assert "STOP calling" not in stubs[2]
    assert "STOP calling" not in stubs[3]
    assert "STOP calling" in stubs[4]
    assert "STOP calling" in stubs[5]


@pytest.mark.asyncio
async def test_different_agents_each_get_their_own_full_serve_budget():
    """Per-agent counting, not global: the Coder's first read of a file the
    Researcher already read repeatedly must still return real content -- only a
    SINGLE agent repeating the SAME call gets stubbed."""
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "shared file content"

    researcher = _FakeAgent("Researcher")
    coder = _FakeAgent("Coder")

    for _ in range(4):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)
    coder_first_read = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=coder
    )

    assert coder_first_read == "shared file content"  # Coder's own budget is untouched
    assert len(calls) == 1  # still only one real underlying fetch -- the network cache is shared


@pytest.mark.asyncio
async def test_coordinators_own_direct_calls_are_tracked_and_stubbed_too():
    """agent=None (agno's own convention for the coordinator's direct, undelegated
    tool calls) must resolve to a stable agent_key and be subject to the same
    duplicate-serve budget as any named member."""
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(2):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=None)
    stub = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=None)

    assert stub != "file body"
    assert "the coordinator" in stub


@pytest.mark.asyncio
async def test_hook_still_works_when_agent_parameter_is_omitted_entirely():
    """agno only passes `agent` when it's a declared parameter name on the hook
    (confirmed via Function._build_hook_args's signature introspection) -- but a
    caller that omits it (like this hook's own default) must not crash."""
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "file body"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert result == "file body"


# ── delegation-generation scoping of the serve budget (2026-08-19) ─────────────
#
# T6 live incident (task k9732nnic): Reviewer repeated its own entire 8-call
# first read pass verbatim, ALL within a single delegate_task_to_member('reviewer',
# ...) call, landing exactly on config.tool_call_limit's ceiling with no answer
# produced. _MAX_FULL_SERVES_PER_AGENT's old tolerance of 2 full serves existed
# specifically to protect a genuinely fresh, SEPARATE, later delegation to the
# same role -- but a plain (agent, tool, args) key couldn't distinguish that
# case from a repeat WITHIN the same delegation instance, so both got the same
# free pass. The hook now also tracks a per-member delegation "generation",
# bumped once per delegate_task_to_member(s) call, folded into the serve-count
# key -- closing the within-delegation loophole while still protecting the
# cross-delegation case the original tolerance was for.

@pytest.mark.asyncio
async def test_repeat_within_the_same_delegation_is_stubbed_immediately():
    """The exact T6 incident shape: a repeat within ONE delegation instance
    must not get a second full serve."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_delegate(**kwargs):
        return "delegated result"

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "cross-check"})
    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)

    assert first == "file body"
    assert second != "file body"  # repeat within the SAME delegation -- stubbed


@pytest.mark.asyncio
async def test_a_fresh_separate_delegation_to_the_same_member_gets_its_own_full_budget():
    """The legitimate case _MAX_FULL_SERVES_PER_AGENT's own comment describes: a
    second, separate delegate_task_to_member call to the same role may start
    with fresh context and genuinely need the same file again."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_delegate(**kwargs):
        return "delegated result"

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "first sub-task"})
    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "second sub-task"})
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)

    assert first == "file body"
    assert second == "file body"  # a NEW delegation instance -- fresh budget, real content again


@pytest.mark.asyncio
async def test_a_repeat_within_the_new_delegation_is_still_stubbed():
    """Confirms the reset is a genuinely fresh 1-serve budget, not an unlimited
    one -- a second repeat WITHIN the new delegation still gets stubbed."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_delegate(**kwargs):
        return "delegated result"

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "first sub-task"})
    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "second sub-task"})
    first_in_new_generation = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer
    )
    repeat_in_new_generation = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer
    )

    assert first_in_new_generation == "file body"
    assert repeat_in_new_generation != "file body"


@pytest.mark.asyncio
async def test_delegate_task_to_member_call_itself_is_never_cached_or_stubbed():
    """The delegation tool call itself always passes straight through -- only
    the generation counter is bumped as a side effect, never its own result."""
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        return f"result #{len(calls)}"

    first = await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "x"})
    second = await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "x"})

    assert first == "result #1"
    assert second == "result #2"  # called through both times, never cached
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_broadcast_delegate_task_to_members_resets_every_agents_generation():
    """delegate_task_to_members (plural) has no single member_id -- it targets
    the whole team, so it conservatively bumps every agent's effective
    generation via the synthetic __broadcast__ key rather than trying to
    enumerate the roster."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_broadcast(**kwargs):
        return "broadcast result"

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)
    await hook("delegate_task_to_members", fake_broadcast, {"task": "re-verify everything"})
    after_broadcast = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer
    )

    assert after_broadcast == "file body"  # fresh generation after the broadcast -- real content again


@pytest.mark.asyncio
async def test_delegation_to_a_different_member_does_not_reset_an_unrelated_members_generation():
    """A fresh delegation to Coder must not reset Reviewer's own budget -- the
    generation is tracked per member, not globally."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_delegate(**kwargs):
        return "delegated result"

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "cross-check"})
    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "coder", "task": "unrelated"})
    still_stubbed = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer
    )

    assert still_stubbed != "file body"  # Reviewer's own generation is unaffected by Coder's delegation


# ── force text-only after consecutive ignored stubs (T12, 2026-08-19) ──────────
#
# Live incident: Reviewer called the SAME get_file_content(...) 29 times in a
# row, each time getting the escalated FORCED STOP warning and calling again
# immediately anyway -- text alone did not redirect it. These tests confirm the
# mechanical backstop: once an agent's stub streak (reset by any real fetch)
# crosses _FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS, we force it into text-only
# mode.
#
# CORRECTED 2026-08-19 (live T6 re-test, task kcu56j2i3): the original version
# only set `agent.tool_choice = "none"` and a live re-test showed this had NO
# effect -- Researcher's streak crossed the threshold at serve #4, yet calls #5
# and #6 still fired identically. Root cause: `agent.tool_choice` is captured
# ONCE, by value, when Agent._run calls acall_model_with_fallback(...); the
# entire repeated tool-call loop for one delegation happens INSIDE that single
# call, inside Model.aresponse_stream's own `while True:` loop (agno/models/base.py),
# which reuses that same captured value every iteration. Mutating
# `agent.tool_choice` mid-loop was mutating something already read and passed
# by value -- it could only affect a FUTURE, separate delegation, which the
# fresh-delegation reset below immediately undoes anyway. The value actually
# re-read fresh on every loop iteration is `self._tool_choice` on the MODEL
# instance itself (`tool_choice or self._tool_choice`, `self` being `agent.model`)
# -- these tests now assert on `reviewer.model._tool_choice`, the attribute that
# actually reaches the in-flight loop; `reviewer.tool_choice` is still asserted
# too since the fix keeps setting it as a harmless defensive belt-and-suspenders.

@pytest.mark.asyncio
async def test_tool_choice_is_forced_to_none_after_enough_consecutive_stubs():
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)
    assert reviewer.model._tool_choice is None  # untouched after the one real serve

    for _ in range(_FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS - 1):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)
        assert reviewer.model._tool_choice is None  # still under the streak threshold

    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)
    assert reviewer.model._tool_choice == "none"  # streak crossed the threshold -- the real, load-bearing mutation
    assert reviewer.tool_choice == "none"  # defensive belt-and-suspenders, kept alongside


@pytest.mark.asyncio
async def test_stub_streak_resets_on_a_real_fetch_of_a_different_file():
    """Reading three different NEW files, each individually stubbed once on a
    legitimate later re-check, is not the same as 3 stubs IN A ROW for the SAME
    call -- the streak must reset on any real, non-stubbed fetch."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for path in ("a.py", "b.py"):
        await hook("get_file_content", fake_get_file_content, {"relative_path": path}, agent=reviewer)
        await hook("get_file_content", fake_get_file_content, {"relative_path": path}, agent=reviewer)  # 1 stub each

    assert reviewer.model._tool_choice is None  # only 1 consecutive stub at a time, streak never built up


@pytest.mark.asyncio
async def test_tool_choice_force_none_only_applies_to_member_agents_not_the_coordinator():
    """agent=None (the coordinator's own direct calls) has no accessible mutable
    object here -- the streak is still tracked but never mutates anything."""
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(_FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS + 2):
        result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=None)

    assert result != "file body"  # still stubs correctly
    # No object to assert on for the coordinator -- this test's real point is
    # that the call above doesn't raise (agent=None must be handled safely).


@pytest.mark.asyncio
async def test_tool_choice_is_reset_on_a_fresh_delegation_to_the_same_member():
    """A brand-new delegation deserves a clean slate -- same principle the
    generation-scoped serve budget already uses."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_delegate(**kwargs):
        return "delegated result"

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "first"})
    for _ in range(_FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS + 1):  # +1: the 1st call is a real serve, not a stub
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)
    assert reviewer.model._tool_choice == "none"

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "second"})
    assert reviewer.model._tool_choice is None  # restored for the fresh delegation
    assert reviewer.tool_choice is None


@pytest.mark.asyncio
async def test_tool_choice_is_reset_for_everyone_on_a_broadcast_delegation():
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_delegate(**kwargs):
        return "delegated result"

    async def fake_broadcast(**kwargs):
        return "broadcast result"

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("delegate_task_to_member", fake_delegate, {"member_id": "reviewer", "task": "first"})
    for _ in range(_FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS + 1):  # +1: the 1st call is a real serve, not a stub
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)
    assert reviewer.model._tool_choice == "none"

    await hook("delegate_task_to_members", fake_broadcast, {"task": "re-verify everything"})
    assert reviewer.model._tool_choice is None  # broadcast resets every known agent's tool_choice too
    assert reviewer.tool_choice is None


# ── context pruning: collapse prior stub messages (T12, 2026-08-19) ────────────
#
# Each ignored repeat left another near-identical stub message in the agent's
# own conversation history -- by the 10th repeat, recent context was
# increasingly dominated by that same repeated block, plausibly reinforcing the
# exact degenerate-loop tendency the whole safeguard section exists to break.
# _collapse_prior_stub_messages mutates (never removes -- agno's RunContext.messages
# is a shallow-copied list, protected against .clear()/.append(), but the
# Message objects inside it are the real shared references) any EARLIER stub
# message for the SAME (tool_name, args) pair down to a short marker.

class _FakeMessage:
    def __init__(self, role, content, tool_name=None, tool_args=None):
        self.role = role
        self.content = content
        self.tool_name = tool_name
        self.tool_args = tool_args


class _FakeRunContextWithMessages:
    def __init__(self, messages):
        self.messages = messages
        self.session_state = {}


def test_collapse_prior_stub_messages_collapses_a_matching_duplicate_stub():
    prior_stub = _FakeMessage(
        "tool", "Already returned this exact get_file_content(...) result to Reviewer 1 time(s) already this run.",
        tool_name="get_file_content", tool_args={"relative_path": "x.py"},
    )
    run_context = _FakeRunContextWithMessages([prior_stub])

    collapsed = _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}')

    assert collapsed == 1
    assert prior_stub.content == _COLLAPSED_STUB_MARKER


def test_collapse_prior_stub_messages_never_touches_real_content():
    real_result = _FakeMessage(
        "tool", "# API/inventory-service/models.py -- lines 0..50 of 774\n...",
        tool_name="get_file_content", tool_args={"relative_path": "x.py"},
    )
    run_context = _FakeRunContextWithMessages([real_result])

    collapsed = _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}')

    assert collapsed == 0
    assert real_result.content.startswith("# API/inventory-service/models.py")  # untouched


def test_collapse_prior_stub_messages_never_touches_a_different_calls_stub():
    """A stub for a DIFFERENT file must not be collapsed just because the same
    tool name repeats -- only the exact (tool_name, args) pair currently being
    re-stubbed qualifies."""
    other_file_stub = _FakeMessage(
        "tool", "Already returned this exact get_file_content(...) result to Reviewer 1 time(s) already this run.",
        tool_name="get_file_content", tool_args={"relative_path": "y.py"},
    )
    run_context = _FakeRunContextWithMessages([other_file_stub])

    collapsed = _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}')

    assert collapsed == 0
    assert other_file_stub.content != _COLLAPSED_STUB_MARKER


def test_collapse_prior_stub_messages_never_touches_a_different_tools_stub():
    other_tool_stub = _FakeMessage(
        "tool", "STOP calling search_files(...) -- this is repeat #3 of the IDENTICAL call.",
        tool_name="search_files", tool_args={"relative_path": "x.py"},
    )
    run_context = _FakeRunContextWithMessages([other_tool_stub])

    collapsed = _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}')

    assert collapsed == 0


def test_collapse_prior_stub_messages_skips_already_collapsed_entries():
    """An entry already collapsed by an earlier repeat must not be re-counted
    (its content no longer starts with a stub prefix once collapsed)."""
    already_collapsed = _FakeMessage(
        _COLLAPSED_STUB_MARKER, _COLLAPSED_STUB_MARKER,  # role field irrelevant here, content is what matters
        tool_name="get_file_content", tool_args={"relative_path": "x.py"},
    )
    already_collapsed.role = "tool"
    run_context = _FakeRunContextWithMessages([already_collapsed])

    collapsed = _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}')

    assert collapsed == 0


def test_collapse_prior_stub_messages_collapses_multiple_prior_stubs_at_once():
    stubs = [
        _FakeMessage(
            "tool", f"STOP calling get_file_content(...) -- this is repeat #{n} of the IDENTICAL call.",
            tool_name="get_file_content", tool_args={"relative_path": "x.py"},
        )
        for n in range(2, 5)
    ]
    run_context = _FakeRunContextWithMessages(stubs)

    collapsed = _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}')

    assert collapsed == 3
    assert all(msg.content == _COLLAPSED_STUB_MARKER for msg in stubs)


def test_collapse_prior_stub_messages_handles_none_run_context():
    assert _collapse_prior_stub_messages(None, "get_file_content", '{"relative_path": "x.py"}') == 0


def test_collapse_prior_stub_messages_handles_empty_messages():
    run_context = _FakeRunContextWithMessages([])
    assert _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}') == 0


def test_collapse_prior_stub_messages_handles_unserializable_tool_args():
    class _NotJsonSerializable:
        pass

    bad_stub = _FakeMessage(
        "tool", "STOP calling get_file_content(...) -- this is repeat #2 of the IDENTICAL call.",
        tool_name="get_file_content", tool_args={"weird": _NotJsonSerializable()},
    )
    run_context = _FakeRunContextWithMessages([bad_stub])

    # Must not raise -- an unserializable tool_args just never matches.
    collapsed = _collapse_prior_stub_messages(run_context, "get_file_content", '{"relative_path": "x.py"}')
    assert collapsed == 0


@pytest.mark.asyncio
async def test_read_cache_hook_actually_collapses_prior_stubs_via_run_context():
    """End-to-end through the real hook: a repeated call must collapse the
    EARLIER stub message already sitting in run_context.messages."""
    hook = _make_read_cache_tool_hook()
    reviewer = _FakeAgent("Reviewer")

    async def fake_get_file_content(**kwargs):
        return "file body"

    # Real fetch, then two repeats -- each repeat should collapse any prior
    # stub for the SAME call already in messages.
    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=reviewer)

    first_stub_message = _FakeMessage(
        "tool", "placeholder",  # overwritten with the real stub text below
        tool_name="get_file_content", tool_args={"relative_path": "x.py"},
    )
    run_context = _FakeRunContextWithMessages([first_stub_message])
    first_stub_text = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        agent=reviewer, run_context=run_context,
    )
    first_stub_message.content = first_stub_text  # simulate agno appending the real stub to history

    second_stub_text = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        agent=reviewer, run_context=run_context,
    )

    assert first_stub_message.content == _COLLAPSED_STUB_MARKER  # collapsed by the second repeat
    assert second_stub_text != _COLLAPSED_STUB_MARKER  # the NEWEST stub is still the real, current warning


# ── _build_team wiring: shared hook across coordinator AND every member ────────

def test_build_team_registers_a_tool_hook_on_the_coordinator(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    # interception hook (Phase 9a) + search-before-browse gate hook (2026-08-15) +
    # read-cache hook (Detour fix) + decompose-first gate hook (Engineering Team 2.0
    # Phase 2, 2026-08-14) + duplicate-delegation gate hook (2026-08-15, T2c
    # groundedness incident) + delegation-log hook (shared session_state, 2026-08-13)
    # -- all six shared
    assert result.tool_hooks is not None
    assert len(result.tool_hooks) == 6


def test_interception_hook_is_listed_first_so_it_is_outermost(monkeypatch):
    """agno makes the FIRST hook in tool_hooks the OUTERMOST wrapper (hooks are
    reversed and reduced from the innermost outward -- confirmed via
    agno.tools.function.Function's nested-chain builders). interception_hook must be
    first so it always runs and always logs, including when read_cache_hook returns
    early on a cache hit or a duplicate-read stub -- confirmed live 2026-08-11: with
    the old order a heartbeat reported "194s since last tool call" while cached reads
    were visibly still streaming, because interception_hook was skipped entirely on
    every cache hit. search_before_browse_gate_hook (2026-08-15) sits before
    read_cache_hook so a blocked browse call never reaches the cache/serve-count
    bookkeeping either -- it never really happened. decompose_first_gate_hook and
    duplicate_delegation_gate_hook (2026-08-15, T2c groundedness incident) both sit
    before delegation_log_hook so a blocked delegation is never logged as if it had
    happened -- see _build_team's own comment on this ordering."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.tool_hooks[0].__name__ == "_tool_interception_hook"
    assert result.tool_hooks[1].__name__ == "_search_before_browse_gate_hook"
    assert result.tool_hooks[2].__name__ == "_read_cache_tool_hook"
    assert result.tool_hooks[3].__name__ == "_decompose_first_gate_hook"
    assert result.tool_hooks[4].__name__ == "_duplicate_delegation_gate_hook"
    assert result.tool_hooks[5].__name__ == "_delegation_log_hook"


def test_build_team_shares_the_same_hook_instance_between_coordinator_and_fallback_members(monkeypatch):
    """Fallback path (agent_specs=None -> make_coder + make_reviewer). The SAME
    hook object (not just an equivalent one) must reach every member -- that's
    what makes the cache actually shared rather than one-per-agent."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    for member in result.members:
        assert member.tool_hooks is not None
        assert member.tool_hooks == result.tool_hooks  # same objects, not just equal


# ── _record_read: mechanical read_log in shared session_state (2026-08-13) ─────
# The mechanical backstop for _COORDINATOR_INSTRUCTIONS' "Don't make downstream
# agents re-read" section -- that section is prose the coordinator must remember to
# follow every time; this writes the same fact into session_state automatically,
# on every genuinely FRESH fetch (never on a cache hit or a stub -- those aren't
# new information, see _make_read_cache_tool_hook's docstring).


class _FakeRunContext:
    def __init__(self, session_state):
        self.session_state = session_state


@pytest.mark.asyncio
async def test_fresh_fetch_records_one_read_log_entry():
    hook = _make_read_cache_tool_hook()
    run_context = _FakeRunContext({})

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        agent=None, run_context=run_context,
    )

    assert len(run_context.session_state["read_log"]) == 1
    entry = run_context.session_state["read_log"][0]
    assert entry["tool"] == "get_file_content"
    assert entry["args"] == {"relative_path": "x.py"}
    assert entry["read_by"] == "coordinator"
    assert entry["result_chars"] == len("file body")


@pytest.mark.asyncio
async def test_read_log_never_contains_the_actual_file_content():
    """Only the length is recorded -- logging real content would just relocate the
    exact context-bloat problem the duplicate-read stub exists to stop."""
    hook = _make_read_cache_tool_hook()
    run_context = _FakeRunContext({})
    secret_content = "SECRET_MARKER_" + ("x" * 500)

    async def fake_get_file_content(**kwargs):
        return secret_content

    await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        agent=None, run_context=run_context,
    )

    assert "SECRET_MARKER_" not in str(run_context.session_state["read_log"])


@pytest.mark.asyncio
async def test_cache_hit_does_not_add_a_second_read_log_entry():
    """The log tracks DISTINCT facts established, not every serve -- a second
    agent's cache-hit read of the same file is not new information."""
    hook = _make_read_cache_tool_hook()
    run_context = _FakeRunContext({})
    researcher = _FakeAgent("Researcher")
    coder = _FakeAgent("Coder")

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        agent=researcher, run_context=run_context,
    )
    await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        agent=coder, run_context=run_context,
    )

    assert len(run_context.session_state["read_log"]) == 1
    assert run_context.session_state["read_log"][0]["read_by"] == "Researcher"


@pytest.mark.asyncio
async def test_stubbed_repeat_call_does_not_add_a_new_read_log_entry():
    hook = _make_read_cache_tool_hook()
    run_context = _FakeRunContext({})
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(5):  # well past the stub threshold
        await hook(
            "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
            agent=researcher, run_context=run_context,
        )

    assert len(run_context.session_state["read_log"]) == 1  # still just the one fresh fetch


@pytest.mark.asyncio
async def test_missing_run_context_does_not_crash_the_hook():
    """A caller (or an older test) that never passes run_context must not break --
    this is bookkeeping, never load-bearing for the actual tool call."""
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "file body"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert result == "file body"


@pytest.mark.asyncio
async def test_run_context_with_none_session_state_does_not_crash_the_hook():
    """RunContext.session_state defaults to None in agno itself (run/base.py) --
    e.g. a Team/Agent built without an initial session_state=... value."""
    hook = _make_read_cache_tool_hook()
    run_context = _FakeRunContext(None)

    async def fake_get_file_content(**kwargs):
        return "file body"

    result = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        run_context=run_context,
    )

    assert result == "file body"
    assert run_context.session_state is None  # untouched, not silently replaced


# ── activity["max_stub_serve_count"] (2026-08-13, Recommendation #2) ───────────
# The Tier-2 signal for the liveness-based auto-kill (see DOCS.md "Liveness-Based
# Auto-Kill") -- a model still calling the identical read after the escalated
# "STOP calling this" stub is the sharpest "not converging" signal this file
# produces. Reuses the serve_counts bookkeeping that already existed here for an
# unrelated reason; nothing about the caching/stubbing behavior itself changes.

@pytest.mark.asyncio
async def test_max_stub_serve_count_untouched_while_within_the_real_content_budget():
    """Serve 1 is real content, never stubbed -- activity must not be touched
    until a stub is actually served."""
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert "max_stub_serve_count" not in activity


@pytest.mark.asyncio
async def test_max_stub_serve_count_records_the_first_stubbed_serve():
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(2):  # serve 2 is the first stub (budget is 1)
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert activity["max_stub_serve_count"] == 2


@pytest.mark.asyncio
async def test_max_stub_serve_count_tracks_the_highest_count_seen():
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(8):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert activity["max_stub_serve_count"] == 8


@pytest.mark.asyncio
async def test_max_stub_serve_count_is_the_max_across_different_calls_not_the_last():
    """A second, distinct (tool, args) pair stubbed at a LOWER count than an
    earlier one must not overwrite the higher-water mark -- the whole point is
    catching the worst offender across the run, not just the most recent one."""
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(6):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)
    for _ in range(3):  # a different file, stubbed at a lower count
        await hook("get_file_content", fake_get_file_content, {"relative_path": "y.py"}, agent=researcher)

    assert activity["max_stub_serve_count"] == 6


@pytest.mark.asyncio
async def test_max_stub_serve_count_not_touched_when_activity_is_none():
    """Default (activity=None, every other test in this file) -- must not crash,
    same defensive posture as every other optional-activity site in this hook."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(5):
        result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert result != "file body"  # still stubs correctly with no activity dict


# ── activity["total_stub_serve_count"] (2026-08-14) ────────────────────────────
# Closes a real, precisely-measured gap in max_stub_serve_count above: that
# signal is the HIGHEST count seen for any SINGLE (agent, tool, args) key, so a
# model that spreads its non-convergence across several different already-
# stubbed files -- rotating A, B, C, A, B, C... instead of hammering just one --
# can keep every individual key's count just under the Tier-2 threshold even
# though the run is just as stuck. Confirmed live 2026-08-14: a real run
# rotated between 3 files, each served 6-8 times (21 stub serves total), and
# max_stub_serve_count peaked at exactly 8 -- one shy of the >8 trigger -- while
# the run went on to stall completely and was only caught by the much slower
# Tier-1 300s silence backstop. total_stub_serve_count sums EVERY stub serve
# across the whole run, regardless of which key it belongs to, so this same
# pattern trips its own (separate, higher) threshold instead of hiding between
# several individually-under-threshold counters.

@pytest.mark.asyncio
async def test_total_stub_serve_count_untouched_while_within_the_real_content_budget():
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert "total_stub_serve_count" not in activity


@pytest.mark.asyncio
async def test_total_stub_serve_count_increments_once_per_stub_serve():
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(5):  # serve 1 real, 2-5 stubbed -- 4 stub serves (budget is 1)
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert activity["total_stub_serve_count"] == 4


@pytest.mark.asyncio
async def test_total_stub_serve_count_sums_ACROSS_different_keys_unlike_the_max_signal():
    """The exact real-world shape this closes: rotating between 3 DIFFERENT
    files, each individually stubbed a few times, none crossing the per-key
    Tier-2 threshold alone -- but the aggregate must still add up."""
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for path in ("a.py", "b.py", "c.py"):
        for _ in range(4):  # serve 1 real, 2-4 stubbed -- 3 stub serves each (budget is 1)
            await hook("get_file_content", fake_get_file_content, {"relative_path": path}, agent=researcher)

    # 3 files x 3 stub serves each = 9 total -- no single file's own count (4)
    # comes anywhere near a realistic Tier-2 threshold, but the sum is real.
    assert activity["total_stub_serve_count"] == 9
    assert activity["max_stub_serve_count"] == 4  # the per-key signal stays low


@pytest.mark.asyncio
async def test_total_stub_serve_count_not_touched_when_activity_is_none():
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(5):
        result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert result != "file body"  # still stubs correctly with no activity dict


def test_build_team_shares_the_same_hook_instance_with_spec_based_members(monkeypatch):
    """agent_specs path (make_agent_from_spec) -- the primary path used by the
    real engineering.yaml team, not just the make_coder/make_reviewer fallback."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    class _FakeSpec:
        name = "Researcher"
        model = "qwen2.5-coder:32b"
        tools = None
        instructions = ["Research the codebase."]
        role = "Researcher"
        description = "Research specialist."
        skills = None

    result = _build_team(
        agent_specs=[_FakeSpec()],
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert len(result.members) == 1
    assert result.members[0].tool_hooks == result.tool_hooks


# ── "File not found" pagination-retry short-circuit (2026-08-18 live incident) ──
#
# get_file_content on a path that does not exist anywhere in the project got
# retried with a steadily incrementing offset (0 -> 2500+ across 25 calls) as if
# paginating a real file. Each retry's offset differs, so the main identical-args
# cache/serve-count mechanism above never catches it -- a separate counter, keyed
# on relative_path alone (ignoring offset/limit), stubs starting from the 2nd
# same-path not-found result.

@pytest.mark.asyncio
async def test_first_not_found_result_is_returned_unstubbed():
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return f"File not found: {kwargs['relative_path']}"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py"})

    assert result == "File not found: ghost.py"


@pytest.mark.asyncio
async def test_second_not_found_result_for_the_same_path_gets_stubbed():
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return f"File not found: {kwargs['relative_path']}"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py"})
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py"})

    assert "STOP calling get_file_content" in second
    assert "ghost.py" in second
    assert "does not exist" in second.lower() or "not to exist" in second.lower() or "not exist" in second.lower()


@pytest.mark.asyncio
async def test_not_found_retry_is_caught_even_when_offset_differs_each_time():
    """The exact live incident shape: same path, different offset/limit each
    call -- the normal identical-args cache would treat these as unrelated
    entries, but this mechanism keys on relative_path alone."""
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return f"File not found: {kwargs['relative_path']}"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py", "offset": 0, "limit": 100})
    second = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "ghost.py", "offset": 100, "limit": 100},
    )
    third = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "ghost.py", "offset": 200, "limit": 100},
    )

    assert "STOP calling get_file_content" in second
    assert "STOP calling get_file_content" in third


@pytest.mark.asyncio
async def test_not_found_stub_does_not_call_the_real_function_again():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return f"File not found: {kwargs['relative_path']}"

    for offset in (0, 100, 200, 300):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py", "offset": offset})

    # The first call always goes through; subsequent same-path not-found calls
    # are stubbed WITHOUT calling the real function again.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_not_found_tracking_is_scoped_to_relative_path_not_agent():
    """A file's existence is an objective fact, not per-agent context -- unlike
    the main serve-count budget (per-agent), this stubs a SECOND agent's first
    look at an already-confirmed-missing path too."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    coder = _FakeAgent("Coder")

    async def fake_get_file_content(**kwargs):
        return f"File not found: {kwargs['relative_path']}"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py"}, agent=researcher)
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py"}, agent=coder)

    assert "STOP calling get_file_content" in second


@pytest.mark.asyncio
async def test_not_found_for_different_paths_does_not_cross_contaminate():
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return f"File not found: {kwargs['relative_path']}"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "a.py"})
    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "b.py"})

    assert result == "File not found: b.py"  # first look at a DIFFERENT path -- unstubbed


@pytest.mark.asyncio
async def test_real_content_is_never_mistaken_for_a_not_found_result():
    """A file whose real content happens to start with unrelated text must never
    trigger the not-found retry-loop guard -- only an exact 'File not found:'
    prefix does. Checked via a SECOND, different agent's own first read of the
    same path (its own fresh serve budget, see the delegation-generation tests
    further down): if the not-found guard had wrongly fired, that read would
    get the not-found-specific stub regardless of which agent asks (see
    test_not_found_tracking_is_scoped_to_relative_path_not_agent) instead of
    real content."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    coder = _FakeAgent("Coder")

    async def fake_get_file_content(**kwargs):
        return "     1\t# File not found in the usual sense, but this IS real content"

    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=coder)

    assert "STOP calling get_file_content" not in second
    assert second == first  # a different agent's own first read still gets real content


@pytest.mark.asyncio
async def test_not_found_short_circuit_updates_liveness_signals():
    """Reuses the SAME activity signals the main duplicate-serve stub already
    feeds (max_stub_serve_count / total_stub_serve_count), not a separate,
    invisible-to-liveness counter."""
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)

    async def fake_get_file_content(**kwargs):
        return f"File not found: {kwargs['relative_path']}"

    await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py"})
    assert "max_stub_serve_count" not in activity  # first look -- not a repeat yet

    await hook("get_file_content", fake_get_file_content, {"relative_path": "ghost.py"})
    assert activity["max_stub_serve_count"] == 2
    assert activity["total_stub_serve_count"] == 1


@pytest.mark.asyncio
async def test_not_found_stub_is_scoped_to_get_file_content_only():
    """A different cacheable tool returning a string that happens to start with
    'File not found:' must not trigger this -- the check is deliberately gated
    on function_name == 'get_file_content'. Uses two different agents (each its
    own fresh serve budget) so the assertion isolates this from the unrelated
    per-agent duplicate-serve stub."""
    hook = _make_read_cache_tool_hook()
    researcher = _FakeAgent("Researcher")
    coder = _FakeAgent("Coder")

    async def fake_search_files(**kwargs):
        return "File not found: some/path.py"

    first = await hook("search_files", fake_search_files, {"pattern": "x"}, agent=researcher)
    second = await hook("search_files", fake_search_files, {"pattern": "x"}, agent=coder)

    assert second == first
    assert "STOP calling get_file_content" not in second


# ── model-voluntary verify_claims timeout (2026-08-19) ──────────────────────────
# See _MODEL_VERIFY_CLAIMS_TIMEOUT's own comment in swarm/team.py for the live
# incidents this closes: a model-voluntary verify_claims call (the model has it
# as a directly-callable MCP tool, distinct from _verified_answer()'s own
# bespoke automatic check) goes through THIS hook, not _verify_claims()'s
# _BESPOKE_MCP_SESSION_TIMEOUT -- so before this fix it was protected only by
# the much longer _MCP_TIMEOUT (180s) with no graceful degradation, and a
# second stacked call could blow past the 300s liveness ceiling entirely
# unprotected. These tests confirm a slow call is cut off with a clean, handled
# result instead of an uncaught exception, and a normal-speed call passes
# through untouched.

@pytest.mark.asyncio
async def test_model_voluntary_verify_claims_times_out_with_a_handled_result(monkeypatch):
    monkeypatch.setattr(team, "_MODEL_VERIFY_CLAIMS_TIMEOUT", 0.05)
    hook = _make_read_cache_tool_hook()

    async def hanging_verify_claims(**kwargs):
        await asyncio.sleep(10)
        return "should never get here"

    result = await hook("verify_claims", hanging_verify_claims, {"answer": "some answer"})

    assert "VERIFICATION UNAVAILABLE" in result
    assert "Do NOT call verify_claims again" in result


@pytest.mark.asyncio
async def test_model_voluntary_verify_claims_passes_through_when_fast(monkeypatch):
    monkeypatch.setattr(team, "_MODEL_VERIFY_CLAIMS_TIMEOUT", 5)
    hook = _make_read_cache_tool_hook()

    async def fast_verify_claims(**kwargs):
        return "VERDICT: all claims verified"

    result = await hook("verify_claims", fast_verify_claims, {"answer": "some answer"})

    assert result == "VERDICT: all claims verified"


@pytest.mark.asyncio
async def test_model_voluntary_verify_claims_degrades_on_a_real_exception_too(monkeypatch):
    """Not just timeouts -- any failure in the underlying call (e.g. a real
    McpError from the persistent MCPTools connection's own _MCP_TIMEOUT firing
    first) must degrade to the same handled result, never propagate up and
    kill the run."""
    monkeypatch.setattr(team, "_MODEL_VERIFY_CLAIMS_TIMEOUT", 5)
    hook = _make_read_cache_tool_hook()

    async def broken_verify_claims(**kwargs):
        raise RuntimeError("Timed out while waiting for response to ClientRequest. Waited 180.0 seconds.")

    result = await hook("verify_claims", broken_verify_claims, {"answer": "some answer"})

    assert "VERIFICATION UNAVAILABLE" in result
