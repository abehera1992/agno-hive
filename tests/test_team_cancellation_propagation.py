"""Regression test: a run cancelled mid-stream must actually raise CancelledError,
not fall through as if it had completed normally.

Confirmed live 2026-08-11: agno's own _arun_tasks_stream (agno/team/_run.py, the
function backing team.arun(stream=True, ...)) catches (KeyboardInterrupt,
asyncio.CancelledError) internally and yields a TeamRunCancelled/RunCancelled event
instead of re-raising. Direct evidence: agno-api's own process held an ESTABLISHED
connection to LiteLLM for several minutes after its HTTP client had fully
disconnected -- api/server.py's _run_cancel_on_disconnect calls task.cancel() on
disconnect and awaits the task expecting CancelledError, but since agno swallowed it
internally, the "cancelled" run just looked like a normal completed one to every
`except Exception` guard along the way (none of which catch CancelledError in
Python 3.8+ -- that was never the bug; the bug is the exception never got raised in
the first place). This meant a cancelled run could fall through into
_verified_answer's retry guards and kick off a genuine, uncancelled retry cycle,
explaining sustained GPU activity well past the original cancel.

_stream_team_run now detects the swallowed-cancellation event via _is_cancelled_event
and raises a real asyncio.CancelledError itself, restoring the propagation contract
_run_cancel_on_disconnect's own docstring assumes. The identical check is duplicated
at run_task_async's and run_task_stream's own team.arun() consumption loops (not
exercised here -- see test_team_stream_events.py's pure _is_cancelled_event unit
tests, which cover the classifier those two sites also call).
"""
import asyncio

import pytest

from swarm import team


class _FakeTeamThatGetsCancelledMidStream:
    """Mirrors agno's real calling convention: team.arun(prompt, stream=True,
    yield_run_output=True) returns an async generator directly (no await before the
    call). Yields one real content event, then the cancelled-run event agno's own
    internals produce when it swallows a CancelledError -- exactly the shape observed
    live."""

    def __init__(self):
        self.prompts = []

    def arun(self, prompt, stream=False, yield_run_output=False):
        self.prompts.append(prompt)
        return self._stream()

    async def _stream(self):
        from types import SimpleNamespace
        yield SimpleNamespace(event="TeamRunContent", content="partial answer so far")
        yield SimpleNamespace(event="TeamRunCancelled", content=None)


@pytest.mark.asyncio
async def test_stream_team_run_raises_cancelled_error_on_a_swallowed_cancellation():
    fake_team = _FakeTeamThatGetsCancelledMidStream()

    with pytest.raises(asyncio.CancelledError):
        await team._stream_team_run(fake_team, "some retry prompt")


@pytest.mark.asyncio
async def test_stream_team_run_cleans_up_its_heartbeat_task_even_when_cancelled():
    """The heartbeat task started at the top of _stream_team_run must still be
    cancelled and awaited via the function's own finally block -- a raised
    CancelledError from inside the loop must not skip that cleanup."""
    fake_team = _FakeTeamThatGetsCancelledMidStream()

    with pytest.raises(asyncio.CancelledError):
        await team._stream_team_run(fake_team, "some retry prompt")

    # If the heartbeat task leaked, this would show up as a warning/lingering task in
    # a real event loop; asserting no exception was raised getting here is the
    # practical signal available without reaching into _stream_team_run's locals.
    await asyncio.sleep(0)  # let any leaked task's next step run, if one exists


@pytest.mark.asyncio
async def test_stream_team_run_does_not_raise_cancelled_when_no_cancellation_event_occurs():
    """Sanity check the fix is narrowly scoped -- a normal completion must not be
    misdetected as a cancellation."""
    class _NormalFakeTeam:
        def arun(self, prompt, stream=False, yield_run_output=False):
            return self._stream()

        async def _stream(self):
            from types import SimpleNamespace
            yield SimpleNamespace(event="TeamRunContent", content="the real answer")
            yield SimpleNamespace(content="the real answer", messages=[], tools=[])  # final TeamRunOutput, no .event

    content, run_output = await team._stream_team_run(_NormalFakeTeam(), "prompt")

    assert content == "the real answer"
    assert run_output is not None


# ── _make_disconnect_checker ────────────────────────────────────────────────────
# The more targeted fix: agno's own native cancellation API (agno.run.cancel.
# acancel_run) is checked via araise_if_cancelled(run_id) on EVERY event during
# model/tool-call streaming (confirmed by direct source reading of agno/team/_run.py's
# _arun_tasks_stream) -- far more reliable than hoping generic asyncio task
# cancellation lands at the right internal await point. Confirmed live 2026-08-11 a
# SECOND time: a run cancelled while an MCP tool call (web_search) was in flight kept
# running -- new tool calls, a brand-new LiteLLM connection -- 11+ seconds after
# task.cancel() was called and awaited, with no TeamRunCancelled event ever logged at
# all (unlike the first, LLM-streaming-cancelled case _is_cancelled_event fixes).

from types import SimpleNamespace


def _event(run_id=None, **kwargs):
    return SimpleNamespace(run_id=run_id, **kwargs)


@pytest.mark.asyncio
async def test_disconnect_checker_does_nothing_when_is_disconnected_is_none():
    check = team._make_disconnect_checker(None)
    # Must not raise, must not require is_disconnected to be callable at all.
    await check(_event(run_id="abc-123", event="TeamRunContent", content="x"))


@pytest.mark.asyncio
async def test_disconnect_checker_cancels_via_agno_native_api(monkeypatch):
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)  # no throttle wait in the test
    cancelled_run_ids = []

    async def fake_acancel_run(run_id):
        cancelled_run_ids.append(run_id)
        return True

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    async def always_disconnected():
        return True

    check = team._make_disconnect_checker(always_disconnected)
    # run_id capture happens before the disconnect check within the same call (no
    # throttle window here), so a single call both captures it and triggers cancel.
    with pytest.raises(asyncio.CancelledError):
        await check(_event(run_id="run-xyz", event="TeamRunContent", content="x"))

    assert cancelled_run_ids == ["run-xyz"]


@pytest.mark.asyncio
async def test_disconnect_checker_throttles_is_disconnected_calls(monkeypatch):
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 999)  # never elapses in this test
    call_count = 0

    async def counting_is_disconnected():
        nonlocal call_count
        call_count += 1
        return True

    check = team._make_disconnect_checker(counting_is_disconnected)
    for _ in range(5):
        await check(_event(run_id="run-1", event="TeamRunContent", content="x"))

    # First call sets last_check to "now" without checking (throttle window not yet
    # elapsed relative to itself) -- none of the 5 calls should have triggered
    # is_disconnected given the huge poll interval.
    assert call_count == 0


@pytest.mark.asyncio
async def test_disconnect_checker_does_not_call_acancel_run_when_run_id_never_captured(monkeypatch):
    """Edge case: disconnect detected before any event carried a run_id (e.g. the
    very first event itself has none, which shouldn't happen in practice but must not
    crash if it does)."""
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)
    acancel_calls = []

    async def fake_acancel_run(run_id):
        acancel_calls.append(run_id)

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    async def always_disconnected():
        return True

    check = team._make_disconnect_checker(always_disconnected)

    with pytest.raises(asyncio.CancelledError):
        await check(_event(run_id=None, event="TeamRunContent", content="x"))

    assert acancel_calls == []


@pytest.mark.asyncio
async def test_stream_team_run_cancels_via_disconnect_checker_mid_stream(monkeypatch):
    """End-to-end: a team that never yields a cancelled-run event on its own (unlike
    _FakeTeamThatGetsCancelledMidStream above) still gets stopped, because
    is_disconnected() reports True partway through -- this is the case a swallowed
    CancelledError from a mid-tool-call disconnect could never trigger via
    _is_cancelled_event alone, since no such event is ever yielded in that failure
    mode."""
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)
    cancelled_run_ids = []

    async def fake_acancel_run(run_id):
        cancelled_run_ids.append(run_id)

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    class _LongRunningFakeTeam:
        def arun(self, prompt, stream=False, yield_run_output=False):
            return self._stream()

        async def _stream(self):
            yield SimpleNamespace(run_id="run-abc", event="TeamRunContent", content="first chunk")
            yield SimpleNamespace(run_id="run-abc", event="TeamRunContent", content="second chunk")
            yield SimpleNamespace(run_id="run-abc", event="TeamRunContent", content="third chunk")

    calls = {"n": 0}

    async def disconnected_after_first_event():
        calls["n"] += 1
        return calls["n"] > 1  # connected for the first check, disconnected after

    with pytest.raises(asyncio.CancelledError):
        await team._stream_team_run(
            _LongRunningFakeTeam(), "prompt", is_disconnected=disconnected_after_first_event
        )

    assert cancelled_run_ids == ["run-abc"]


# ── _make_disconnect_checker's `.claimed` coordination (THIRD incident) ──────────
# Confirmed live 2026-08-11, a third time: acancel_run (tested above) DOES fire
# reliably, but api/server.py's OUTER _run_cancel_on_disconnect polling loop and
# THIS inner checker both independently poll the same is_disconnected() on their
# own ~2s timers. Whichever fires first starts cancelling/unwinding; if the other
# ALSO fires while that unwind is mid-flight through nested anyio task-group
# teardown (agno's MCP connection cleanup), the second cancellation delivery
# corrupts anyio's per-task cancel-scope bookkeeping (confirmed via anyio's own
# source: CancelScope.__exit__ raises "Attempted to exit a cancel scope that isn't
# the current tasks's current cancel scope"), leaving the run in a state that never
# resolves on its own. Fix: api.server.DisconnectSignal's shared `.claimed`
# asyncio.Event, duck-typed here via getattr so a bare callable (every test above)
# keeps working unchanged.


class _FakeDisconnectSignal:
    """Minimal stand-in for api.server.DisconnectSignal -- just the two things this
    checker actually uses: an async __call__ and a shared `claimed` asyncio.Event."""

    def __init__(self, disconnected: bool = True):
        self.claimed = asyncio.Event()
        self._disconnected = disconnected

    async def __call__(self) -> bool:
        return self._disconnected


@pytest.mark.asyncio
async def test_disconnect_checker_claims_the_signal_before_raising_when_unclaimed(monkeypatch):
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)

    async def fake_acancel_run(run_id):
        pass

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    signal = _FakeDisconnectSignal(disconnected=True)
    assert not signal.claimed.is_set()

    check = team._make_disconnect_checker(signal)
    with pytest.raises(asyncio.CancelledError):
        await check(_event(run_id="run-first-claim", event="TeamRunContent", content="x"))

    # This checker is the one that detected the disconnect first -- it must have
    # claimed the signal before raising, so api/server.py's outer loop stands down.
    assert signal.claimed.is_set()


@pytest.mark.asyncio
async def test_disconnect_checker_stands_down_without_raising_when_already_claimed(monkeypatch):
    """The regression case: the OUTER _run_cancel_on_disconnect loop claimed this
    disconnect first (e.g. its own poll happened to land a moment earlier). This
    checker must not ALSO raise CancelledError -- that second, uncoordinated
    cancellation is exactly what corrupted anyio's cancel-scope bookkeeping live."""
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)
    cancelled_run_ids = []

    async def fake_acancel_run(run_id):
        cancelled_run_ids.append(run_id)

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    signal = _FakeDisconnectSignal(disconnected=True)
    signal.claimed.set()  # the outer loop already claimed this disconnect

    check = team._make_disconnect_checker(signal)
    # Must return quietly -- no CancelledError, even though is_disconnected() is True.
    await check(_event(run_id="run-already-claimed", event="TeamRunContent", content="x"))

    # acancel_run is still called: it's idempotent, and agno's own cancellation
    # registry should know about the run regardless of which side "claimed" it --
    # only the second raise (the dangerous part) is skipped.
    assert cancelled_run_ids == ["run-already-claimed"]


@pytest.mark.asyncio
async def test_disconnect_checker_still_raises_for_a_bare_callable_without_claimed(monkeypatch):
    """Backward compatibility: is_disconnected without a `.claimed` attribute (a bare
    async callable -- what every test above this section passes, and what a caller
    with no outer coordinator to worry about would still pass) is treated as "nobody
    else is coordinating" and this still raises normally, unchanged from before this
    fix."""
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)

    async def fake_acancel_run(run_id):
        pass

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    async def always_disconnected():
        return True

    check = team._make_disconnect_checker(always_disconnected)
    with pytest.raises(asyncio.CancelledError):
        await check(_event(run_id="run-bare-callable", event="TeamRunContent", content="x"))


# ── Multi-run_id tracking (FOURTH incident) ───────────────────────────────────────
# Confirmed live 2026-08-12, root-caused via direct agno source reading: this checker
# used to capture run_id from only the FIRST event of the whole run and never update
# it again. The first event is always the team's own -- but every
# delegate_task_to_member call runs the member agent under a BRAND NEW run_id (agno's
# `run_id = run_id or str(uuid4())`, never inherited from the team). So acancel_run
# was always cancelling the team's run, a no-op for whichever member was actually
# mid-delegation -- reproduced live twice as a redundant-read loop, then 100+ empty
# model-request cycles, surviving a detected disconnect for 30-60+ seconds of real
# vLLM throughput. Fix: track every DISTINCT run_id seen across the whole event
# stream (team's + every member's) and cancel all of them, not just the first.


def _disconnect_after(n):
    """is_disconnected() stand-in that reports "connected" for the first n-1 calls,
    then "disconnected" from call n onward -- lets a test seed run_ids across several
    check() calls (each of which invokes is_disconnected() once, since
    _DISCONNECT_POLL_S=0 removes throttling) before the one that actually raises."""
    calls = {"n": 0}

    async def is_disconnected():
        calls["n"] += 1
        return calls["n"] >= n

    return is_disconnected


@pytest.mark.asyncio
async def test_disconnect_checker_cancels_every_distinct_run_id_seen_not_just_the_first(monkeypatch):
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)
    cancelled_run_ids = []

    async def fake_acancel_run(run_id):
        cancelled_run_ids.append(run_id)

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    check = team._make_disconnect_checker(_disconnect_after(2))

    # Simulates: team's own first event (still "connected"), then a delegated
    # member's event arriving under a completely different run_id (exactly what
    # delegate_task_to_member produces -- member events flow through this same
    # check(event) call) on the check that actually detects the disconnect.
    await check(_event(run_id="team-run-1", event="TeamRunContent", content="x"))
    with pytest.raises(asyncio.CancelledError):
        await check(_event(run_id="member-run-2", event="RunContent", content="y"))

    assert set(cancelled_run_ids) == {"team-run-1", "member-run-2"}


@pytest.mark.asyncio
async def test_disconnect_checker_tracks_run_ids_across_calls_before_any_disconnect(monkeypatch):
    """The set must accumulate across multiple check() calls while still connected --
    not just within a single call -- since real events arrive one at a time over the
    life of the run."""
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)
    cancelled_run_ids = []

    async def fake_acancel_run(run_id):
        cancelled_run_ids.append(run_id)

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    check = team._make_disconnect_checker(_disconnect_after(3))

    await check(_event(run_id="run-a", event="TeamRunContent", content="1"))
    await check(_event(run_id="run-b", event="RunContent", content="2"))
    with pytest.raises(asyncio.CancelledError):
        await check(_event(run_id="run-c", event="RunContent", content="3"))

    assert set(cancelled_run_ids) == {"run-a", "run-b", "run-c"}


@pytest.mark.asyncio
async def test_disconnect_checker_calls_acancel_run_once_per_distinct_id_not_per_event(monkeypatch):
    """Many events from the same member (the normal case -- a delegated agent yields
    dozens of stream events under one run_id) must not turn into dozens of redundant
    acancel_run calls for the identical id."""
    monkeypatch.setattr(team, "_DISCONNECT_POLL_S", 0)
    cancelled_run_ids = []

    async def fake_acancel_run(run_id):
        cancelled_run_ids.append(run_id)

    monkeypatch.setattr(team, "acancel_run", fake_acancel_run)

    check = team._make_disconnect_checker(_disconnect_after(4))

    await check(_event(run_id="team-run", event="TeamRunContent", content="x"))
    await check(_event(run_id="member-run", event="RunContent", content="a"))
    await check(_event(run_id="member-run", event="RunContent", content="b"))
    with pytest.raises(asyncio.CancelledError):
        await check(_event(run_id="member-run", event="RunContent", content="c"))

    assert cancelled_run_ids.count("member-run") == 1
    assert cancelled_run_ids.count("team-run") == 1


@pytest.mark.asyncio
async def test_disconnect_checker_ignores_events_with_no_run_id_when_tracking():
    check = team._make_disconnect_checker(None)  # is_disconnected=None never raises
    # Must not crash on an event with no run_id at all -- same edge case the original
    # single-id capture already handled, now exercised through the accumulating set.
    await check(_event(run_id=None, event="TeamRunContent", content="x"))
    await check(_event(event="SomeEventWithNoRunIdField"))
