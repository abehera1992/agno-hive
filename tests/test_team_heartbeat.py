"""Unit tests for _run_heartbeat -- a diagnostic background task that prints
periodic status lines while team.arun() is a single opaque blocking await
(swarm/team.py). Added 2026-08-10 after live testing on ZGX reproduced a
"wander-then-go-quiet" pattern twice: the coordinator would read a burst of
unrelated files, then make zero further tool calls for 12+ minutes with no
way to tell from the logs whether it was genuinely generating a long answer
or stalled. _run_heartbeat reads the same `activity` dict the interception
hook (_make_tool_interception_hook) updates on every tool call, so a caller
running it alongside team.arun() gets a visible trail instead of silence.
"""
import asyncio
import time

import pytest

from swarm.team import _run_heartbeat


@pytest.mark.asyncio
async def test_heartbeat_prints_a_line_reflecting_the_last_tool_call():
    activity = {"last_call_name": "some_tool", "last_call_at": time.monotonic()}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_heartbeat_prints_at_least_once_per_interval(capsys):
    activity = {"last_call_name": "some_tool", "last_call_at": time.monotonic()}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    out = capsys.readouterr().out
    assert "heartbeat" in out
    assert "some_tool" in out
    assert "still running" in out


@pytest.mark.asyncio
async def test_heartbeat_reports_none_yet_when_no_tool_called(capsys):
    activity = {"last_call_name": None, "last_call_at": time.monotonic()}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    out = capsys.readouterr().out
    assert "(none yet)" in out


@pytest.mark.asyncio
async def test_heartbeat_never_prints_before_the_first_interval_elapses(capsys):
    activity = {"last_call_name": None, "last_call_at": time.monotonic()}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=10.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    out = capsys.readouterr().out
    assert out == ""


# ── stream_event_count (2026-08-10): distinguishes "no events arriving from ────
# team.arun(stream=True) at all" from "events arriving but the classifier isn't
# recognizing them" -- a live 17-minute run with confirmed ongoing generation
# produced zero content/tool-event log lines, and there was no way to tell
# which of those two, very differently-fixed, problems it was.

@pytest.mark.asyncio
async def test_heartbeat_reports_the_stream_event_count_when_present(capsys):
    activity = {"last_call_name": None, "last_call_at": time.monotonic(), "stream_event_count": 42}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    out = capsys.readouterr().out
    assert "42 stream events received so far" in out


@pytest.mark.asyncio
async def test_heartbeat_reflects_a_live_updated_event_count(capsys):
    """The heartbeat reads activity fresh each tick, so a caller mutating the
    same dict (as run_task_async's stream loop does) sees each new count."""
    activity = {"last_call_name": None, "last_call_at": time.monotonic(), "stream_event_count": 0}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=0.05))
    await asyncio.sleep(0.06)
    activity["stream_event_count"] = 7
    await asyncio.sleep(0.06)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    out = capsys.readouterr().out
    assert "0 stream events received so far" in out
    assert "7 stream events received so far" in out


@pytest.mark.asyncio
async def test_heartbeat_omits_event_count_when_key_absent(capsys):
    """Backward compat: an activity dict without stream_event_count (e.g. an
    older test, or any future caller that doesn't track it) must not crash
    and must not print a stray "None stream events" line."""
    activity = {"last_call_name": None, "last_call_at": time.monotonic()}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    out = capsys.readouterr().out
    assert "stream events" not in out
