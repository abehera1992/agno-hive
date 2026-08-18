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

from swarm.team import _make_read_cache_tool_hook, _build_team, _CACHEABLE_READ_TOOLS


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
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return f"content #{len(calls)}"

    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert first == "content #1"
    assert second == "content #1"  # cached, NOT "content #2" -- the function was not called again
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
# hive-mcp round-trips). Serves 1-2 of an identical (agent, tool, args) triple still
# get the real content; serve 3+ gets a short stub instead.

class _FakeAgent:
    def __init__(self, name):
        self.name = name


@pytest.mark.asyncio
async def test_third_identical_call_from_the_same_agent_gets_a_stub_not_real_content():
    hook = _make_read_cache_tool_hook()
    calls = []

    async def fake_get_file_content(**kwargs):
        calls.append(kwargs)
        return "x" * 500  # stand-in for a real ~500-char file

    researcher = _FakeAgent("Researcher")
    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)
    third = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert first == "x" * 500
    assert second == "x" * 500  # still real content -- serve 2 is within budget
    assert third != "x" * 500  # serve 3 is over budget -- stubbed
    assert "x" * 500 not in third  # the stub must not leak the real content either
    assert len(calls) == 1  # the underlying tool was only ever actually called once


@pytest.mark.asyncio
async def test_duplicate_read_stub_names_the_tool_and_repeat_count():
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "file body"

    researcher = _FakeAgent("Researcher")
    for _ in range(2):
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

    # Serves 1-2 are real content ("file body"); 3-4 are the moderate stub;
    # 5-6 are the escalated stub.
    assert stubs[0] == "file body"
    assert stubs[1] == "file body"
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
    """Serves 1-2 are real content, never stubbed -- activity must not be
    touched until a stub is actually served."""
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(2):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert "max_stub_serve_count" not in activity


@pytest.mark.asyncio
async def test_max_stub_serve_count_records_the_first_stubbed_serve():
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(3):  # serve 3 is the first stub (budget is 2)
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert activity["max_stub_serve_count"] == 3


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

    for _ in range(2):
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert "total_stub_serve_count" not in activity


@pytest.mark.asyncio
async def test_total_stub_serve_count_increments_once_per_stub_serve():
    activity = {}
    hook = _make_read_cache_tool_hook(activity=activity)
    researcher = _FakeAgent("Researcher")

    async def fake_get_file_content(**kwargs):
        return "file body"

    for _ in range(5):  # serves 1-2 real, 3-5 stubbed -- 3 stub serves
        await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, agent=researcher)

    assert activity["total_stub_serve_count"] == 3


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
        for _ in range(4):  # serves 1-2 real, 3-4 stubbed -- 2 stub serves each
            await hook("get_file_content", fake_get_file_content, {"relative_path": path}, agent=researcher)

    # 3 files x 2 stub serves each = 6 total -- no single file's own count (4)
    # comes anywhere near a realistic Tier-2 threshold, but the sum is real.
    assert activity["total_stub_serve_count"] == 6
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
    trigger this path -- only an exact 'File not found:' prefix does."""
    hook = _make_read_cache_tool_hook()

    async def fake_get_file_content(**kwargs):
        return "     1\t# File not found in the usual sense, but this IS real content"

    first = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})
    second = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"})

    assert "STOP calling get_file_content" not in second
    assert second == first  # normal cache behavior, not the not-found stub


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
    on function_name == 'get_file_content'."""
    hook = _make_read_cache_tool_hook()

    async def fake_search_files(**kwargs):
        return "File not found: some/path.py"

    first = await hook("search_files", fake_search_files, {"pattern": "x"})
    second = await hook("search_files", fake_search_files, {"pattern": "x"})

    # Normal identical-args cache behavior (a plain repeat, not the not-found
    # path) -- second call is a cache hit, not the STOP-calling stub.
    assert second == first
    assert "STOP calling get_file_content" not in second
