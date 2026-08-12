"""Regression tests for api.server.DisconnectSignal and _run_cancel_on_disconnect's
`disconnect_signal` coordination.

Confirmed live 2026-08-11, a third incident in this area: this OUTER polling loop
and swarm/team.py's INNER _make_disconnect_checker both independently poll the same
http_request.is_disconnected() on their own ~2s timers. Whichever fires first starts
cancelling/unwinding the run; if the other ALSO fires while that unwind is mid-flight
through nested anyio task-group teardown (agno's MCP connection cleanup), the second
cancellation delivery corrupts anyio's per-task cancel-scope bookkeeping (confirmed
via anyio's own source: CancelScope.__exit__ raises "Attempted to exit a cancel scope
that isn't the current tasks's current cancel scope"), leaving the run in a state
that never resolves on its own -- confirmed live via a lingering ESTABLISHED
connection to LiteLLM with real ongoing vLLM generation, 10+ minutes after the client
had disconnected, ended only by restarting agno-api.

DisconnectSignal's shared `claimed` asyncio.Event, checked-and-set synchronously (no
await between check and act) on both sides, fixes this: whichever side observes the
disconnect first commits to handling it alone. See test_team_cancellation_propagation.py
for the matching tests on the inner checker's (swarm/team.py) side of this contract.

Note on technique: asyncio.Task is a C-implemented, immutable type in this
interpreter (monkeypatch.setattr(asyncio.Task, "cancel", ...) raises TypeError), so
these tests can't spy on Task.cancel directly. Instead they prove whether
task.cancel() fired *behaviorally*: a coroutine blocked on asyncio.sleep(N) only gets
interrupted mid-sleep if something external cancelled it; if it completes the full
sleep and only then raises on its own, no external cancel() reached it.
"""
import asyncio

import pytest
from fastapi import HTTPException

from api.server import DisconnectSignal, _run_cancel_on_disconnect


class _FakeHTTPRequest:
    """Minimal stand-in for a Starlette Request -- only is_disconnected() is used."""

    def __init__(self, disconnected_after: int = 0):
        self.calls = 0
        self._disconnected_after = disconnected_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self._disconnected_after


async def _never_finishes():
    await asyncio.Event().wait()  # blocks forever unless the wrapping task is cancelled


# ── DisconnectSignal ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_signal_call_delegates_to_the_wrapped_request():
    http_request = _FakeHTTPRequest(disconnected_after=0)
    signal = DisconnectSignal(http_request)

    assert await signal() is True
    assert http_request.calls == 1


@pytest.mark.asyncio
async def test_disconnect_signal_call_returns_false_while_still_connected():
    http_request = _FakeHTTPRequest(disconnected_after=999)
    signal = DisconnectSignal(http_request)

    assert await signal() is False


def test_disconnect_signal_starts_unclaimed():
    signal = DisconnectSignal(_FakeHTTPRequest())
    assert isinstance(signal.claimed, asyncio.Event)
    assert not signal.claimed.is_set()


# ── _run_cancel_on_disconnect ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_cancel_on_disconnect_returns_the_result_when_client_stays_connected():
    http_request = _FakeHTTPRequest(disconnected_after=999)

    async def _quick_coro():
        return "ok"

    result = await _run_cancel_on_disconnect(http_request, _quick_coro())
    assert result == "ok"


@pytest.mark.asyncio
async def test_run_cancel_on_disconnect_without_a_signal_preserves_original_behavior(monkeypatch):
    """Backward compatibility: disconnect_signal=None (the default -- every caller
    before this fix) must keep the original always-cancel-on-disconnect behavior.
    _never_finishes() blocks forever on an Event that nothing ever sets, so the only
    way this test can complete at all (rather than hang until the suite times out)
    is if task.cancel() actually fired."""
    # If task.cancel() regressed and never fired, _never_finishes() would hang
    # forever (nothing else ever sets its Event) -- no pytest-timeout is configured
    # in this suite, so wait_for turns that failure mode into a clean assertion
    # instead of hanging the whole test run.
    monkeypatch.setattr("api.server._DISCONNECT_POLL_S", 0.01)
    http_request = _FakeHTTPRequest(disconnected_after=0)

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(_run_cancel_on_disconnect(http_request, _never_finishes()), timeout=2.0)

    assert exc_info.value.status_code == 499


@pytest.mark.asyncio
async def test_run_cancel_on_disconnect_claims_and_cancels_when_not_already_claimed(monkeypatch):
    """The normal case: this outer loop detects the disconnect first (the inner
    checker in swarm/team.py hasn't fired yet). It must claim the signal itself and
    actually cancel the task -- proven the same way as the backward-compat test
    above: _never_finishes() can only end via an external cancel(), and wait_for
    bounds the failure mode if it doesn't."""
    monkeypatch.setattr("api.server._DISCONNECT_POLL_S", 0.01)
    http_request = _FakeHTTPRequest(disconnected_after=0)
    signal = DisconnectSignal(http_request)

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(
            _run_cancel_on_disconnect(http_request, _never_finishes(), disconnect_signal=signal),
            timeout=2.0,
        )

    assert exc_info.value.status_code == 499
    assert signal.claimed.is_set()


@pytest.mark.asyncio
async def test_run_cancel_on_disconnect_stands_down_when_already_claimed(monkeypatch):
    """The regression case: the INNER checker (swarm/team.py's
    _make_disconnect_checker) claimed this disconnect first and is unwinding the run
    itself via its own raised CancelledError. This outer loop must not ALSO call
    task.cancel() -- that second, uncoordinated cancellation is exactly what
    corrupted anyio's cancel-scope bookkeeping live.

    Proof technique: the wrapped coroutine sleeps for 0.2s, well past the 0.01s poll
    interval, then raises CancelledError itself (standing in for the inner
    mechanism's own raise reaching the top of the run). If the outer loop's guard
    failed and called task.cancel() during one of its polls, asyncio.sleep would be
    interrupted immediately and the coroutine would record "cancelled_during_sleep"
    instead of completing the sleep normally -- so the recorded marker distinguishes
    an untouched task from a wrongly double-cancelled one.
    """
    monkeypatch.setattr("api.server._DISCONNECT_POLL_S", 0.01)
    http_request = _FakeHTTPRequest(disconnected_after=0)
    signal = DisconnectSignal(http_request)
    signal.claimed.set()  # the inner checker already claimed this disconnect

    record = []

    async def _slow_then_self_cancels():
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            record.append("cancelled_during_sleep")  # outer loop wrongly cancelled it
            raise
        record.append("slept_fully")
        raise asyncio.CancelledError()  # simulates the inner mechanism's own raise

    with pytest.raises(HTTPException) as exc_info:
        await _run_cancel_on_disconnect(
            http_request, _slow_then_self_cancels(), disconnect_signal=signal
        )

    assert exc_info.value.status_code == 499
    assert record == ["slept_fully"]
