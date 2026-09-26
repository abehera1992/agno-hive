"""Phase Y (2026-09-25): context-budget peak-token lifecycle.

Phase X established (empirically, without a live LLM call) that
swarm/tool_fix.py's `_peak_input_tokens`/`_peak_estimated_input_tokens` were plain
module-level globals, initialized once at process import and never reset. Every
in-process caller of run_task_async()/run_task_stream() therefore inherited the
highest peak any PRIOR run in the same process had ever measured -- reproduced live
as Phase W's Workload C (exceptionally heavy) -> D -> E cascade: D and E both hit
"context token budget exhausted" on their very first tool call, despite being small,
independent tasks.

Two of the three interactive endpoints (/run, /run_chunked, /stream) are immune
because api/server.py runs each of THEIR requests in an isolated child subprocess
(_run_worker_subprocess) -- a fresh Python process, fresh module import, fresh state.
/plan is not: it calls run_task_async() directly inside the long-lived FastAPI
process (api/server.py's plan() handler), with no subprocess boundary and no lock
serializing concurrent requests -- confirmed by grep: no asyncio.Lock/Semaphore
guards run_task_async/run_task_stream anywhere in this codebase.

The fix: `_peak_input_tokens`/`_peak_estimated_input_tokens` became
contextvars.ContextVar instances (not plain ints), and a new
`reset_peak_token_state()` is called at the exact same run-start lifecycle boundary
`reset_unavailable_tool_state()` already occupies, in both run_task_async and
run_task_stream (swarm/team.py). ContextVar rather than a plain-global reset
specifically BECAUSE concurrent /plan requests are possible: asyncio.Task copies the
calling context at creation and mutates its own copy from there, so one request's
reset_peak_token_state() call cannot affect a concurrently-running request's own
peak -- no lock needed, verified directly below (Test: concurrent isolation).
"""
import asyncio
import inspect

import swarm.team as team_mod
from swarm.tool_fix import (
    _estimate_prompt_tokens, _record_input_tokens, measured_input_tokens,
    peak_input_tokens, record_prompt_estimate, reset_peak_token_state,
    reset_unavailable_tool_state, _record_unavailable_tool, unavailable_tool_snapshot,
)


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None
        self.role = "user"


class _FakeUsage:
    def __init__(self, input_tokens):
        self.input_tokens = input_tokens


class _FakeModelResponse:
    def __init__(self, input_tokens):
        self.response_usage = _FakeUsage(input_tokens)


def _messages_of_tokens(n_tokens):
    """A single fake message whose estimated token count is ~n_tokens."""
    from swarm.tool_fix import _CHARS_PER_TOKEN
    return [_FakeMessage("x" * int(n_tokens * _CHARS_PER_TOKEN))]


def setup_function(_):
    # Every test starts from a clean slate regardless of execution order or what a
    # prior test in this file (or another file importing tool_fix) left behind.
    reset_peak_token_state()


# ── Test 1: reset after a previous run's high peak ────────────────────────────────

def test_reset_after_previous_peak():
    _record_input_tokens(_FakeModelResponse(150_000))
    assert peak_input_tokens() == 150_000

    reset_peak_token_state()

    assert peak_input_tokens() == 0, (
        "a fresh logical run must start from zero, not inherit the prior run's peak"
    )


# ── Test 2: a fresh session (simulated as a fresh logical run) is isolated ────────

def test_fresh_session_does_not_inherit_prior_session_peak():
    # "session A"
    record_prompt_estimate(_messages_of_tokens(150_000))
    assert peak_input_tokens() >= 150_000

    # "session B" -- the actual lifecycle boundary a new run crosses, not merely a
    # different session_id string (a session_id alone touches none of this module's
    # state -- see swarm/team.py's run_task_async/run_task_stream, which is why the
    # reset must happen there, not be inferred from the session_id argument).
    reset_peak_token_state()

    assert peak_input_tokens() == 0
    assert measured_input_tokens() == 0


# ── Test 3: a fresh Team object is irrelevant to this state ───────────────────────

def test_fresh_team_construction_does_not_reset_peak_state(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    _record_input_tokens(_FakeModelResponse(150_000))
    assert peak_input_tokens() == 150_000

    # Constructing a brand-new Team object touches none of swarm.tool_fix's state --
    # only reset_peak_token_state() (called at the run_task_async/run_task_stream
    # lifecycle boundary, not at Team-construction time) does.
    team_mod._build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None, mode="coordinate", mcp_list=[], instructions=[],
    )

    assert peak_input_tokens() == 150_000, (
        "fresh Team construction must not be required to clear peak-token state -- "
        "only the explicit run-start reset should"
    )


# ── Test 4: peak tracking still works within one run ───────────────────────────────

def test_peak_still_tracks_the_maximum_within_a_run():
    _record_input_tokens(_FakeModelResponse(50_000))
    _record_input_tokens(_FakeModelResponse(100_000))
    _record_input_tokens(_FakeModelResponse(75_000))

    assert peak_input_tokens() == 100_000
    assert measured_input_tokens() == 100_000


def test_peak_estimated_tokens_also_tracks_the_maximum_within_a_run():
    record_prompt_estimate(_messages_of_tokens(50_000))
    record_prompt_estimate(_messages_of_tokens(100_000))
    record_prompt_estimate(_messages_of_tokens(75_000))

    assert peak_input_tokens() >= 100_000


# ── Test 5: a second run can independently exceed or fall short of the first ──────

def test_second_run_accounts_independently_of_the_first():
    # Run A
    _record_input_tokens(_FakeModelResponse(150_000))
    assert peak_input_tokens() == 150_000

    # Run B starts
    reset_peak_token_state()
    _record_input_tokens(_FakeModelResponse(50_000))
    _record_input_tokens(_FakeModelResponse(75_000))

    assert peak_input_tokens() == 75_000, (
        "run B's peak must reflect only run B's own measurements, not run A's 150k"
    )


# ── Test 6: both run_task_async and run_task_stream reset at the same boundary ────

def test_both_run_paths_call_reset_peak_token_state_at_the_existing_boundary():
    async_src = inspect.getsource(team_mod.run_task_async)
    stream_src = inspect.getsource(team_mod.run_task_stream)
    for name, src in (("run_task_async", async_src), ("run_task_stream", stream_src)):
        assert "reset_unavailable_tool_state()" in src, (
            f"{name} must still call the existing reset")
        assert "reset_peak_token_state()" in src, (
            f"{name} must call the new peak-token reset")
        # Same lifecycle boundary, not a separately-timed mechanism: the new reset's
        # own call must appear textually right after the existing one.
        assert src.index("reset_unavailable_tool_state()") < src.index(
            "reset_peak_token_state()")


# ── Test 7: reset_unavailable_tool_state() itself is unchanged ────────────────────

def test_reset_unavailable_tool_state_behaves_exactly_as_before():
    _record_unavailable_tool(owner="researcher", tool="nonexistent_tool", forced=True)
    assert unavailable_tool_snapshot() != {}

    reset_unavailable_tool_state()

    assert unavailable_tool_snapshot() == {}, (
        "unrelated state-reset responsibilities must not have been merged into or "
        "coupled with this existing reset"
    )


# ── Concurrency: two logical runs interleaved in the same process must not race ───

def test_concurrent_runs_do_not_share_or_corrupt_each_others_peak_state():
    """Directly targets the reason ContextVar was chosen over a plain-global reset:
    /plan has no lock serializing requests (confirmed by source inspection -- no
    asyncio.Lock/Semaphore guards run_task_async/run_task_stream anywhere in this
    codebase), so two /plan requests can genuinely interleave as separate asyncio
    Tasks on the one event loop. This simulates that interleaving directly against
    the real reset/record/read functions, with explicit await points forcing the
    two coroutines to interleave rather than run start-to-finish sequentially.
    """
    results = {}

    async def run_a():
        reset_peak_token_state()
        _record_input_tokens(_FakeModelResponse(150_000))
        await asyncio.sleep(0)  # yield -- let run_b's own reset attempt interleave
        await asyncio.sleep(0)
        results["a"] = peak_input_tokens()

    async def run_b():
        await asyncio.sleep(0)  # let run_a set its peak first
        reset_peak_token_state()  # must not affect run_a's already-set 150_000
        _record_input_tokens(_FakeModelResponse(50_000))
        await asyncio.sleep(0)
        results["b"] = peak_input_tokens()

    async def main():
        # each coroutine becomes its own asyncio.Task via gather, exactly how
        # FastAPI/Starlette dispatches concurrent request handlers
        await asyncio.gather(run_a(), run_b())

    asyncio.run(main())

    assert results["a"] == 150_000, (
        "run A's peak must survive run B's concurrent reset -- got "
        f"{results['a']}, expected 150000 (a plain-global reset would corrupt this)"
    )
    assert results["b"] == 50_000, (
        f"run B's peak must reflect only its own measurement -- got {results['b']}"
    )


# ── Regression: the exact Phase W shape (heavy request -> light independent request) ──

def test_phase_w_cascade_regression_heavy_then_light_request():
    """The smallest deterministic equivalent of Phase W's Workload C -> D cascade,
    using the real accounting functions directly -- no live LLM call needed.

    Before this fix: the "light request" section below would see peak_input_tokens()
    already at 150,000 before recording anything of its own, exactly matching
    Workload D's "context token budget exhausted (226,928 of 262,144)" on its very
    first tool call. After this fix: reset_peak_token_state() (now called at
    run_task_async/run_task_stream's start) makes the light request begin at 0.
    """
    # "heavy request" (Workload C equivalent)
    record_prompt_estimate(_messages_of_tokens(157_626))
    heavy_peak = peak_input_tokens()
    assert heavy_peak >= 150_000

    # run-start lifecycle boundary a real run_task_async()/run_task_stream() call
    # now crosses (see Test 6 -- this IS what those functions call at their start)
    reset_peak_token_state()

    # "light request" (Workload D equivalent) -- its own single small tool call
    record_prompt_estimate(_messages_of_tokens(31_087))
    light_peak = peak_input_tokens()

    assert light_peak < heavy_peak
    assert light_peak < 40_000, (
        f"light request's peak ({light_peak}) must reflect only its own small "
        f"prompt, not the heavy request's {heavy_peak}"
    )
