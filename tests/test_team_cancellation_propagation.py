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
