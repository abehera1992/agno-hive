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
import json
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


# ── liveness_path (2026-08-13, Recommendation #2): the write side of the ───────
# liveness-based auto-kill -- see DOCS.md "Liveness-Based Auto-Kill". Each tick
# also writes a small JSON snapshot api/server.py's own poll loop reads to decide
# whether to kill the run. Backward compat is the default (liveness_path=None,
# every test above this section) -- these are its opt-in behavior only.

@pytest.mark.asyncio
async def test_no_liveness_file_written_when_path_is_none(tmp_path):
    """Default behaviour (every test above) -- must not write anything anywhere
    when the caller never opts in."""
    activity = {"last_call_name": None, "last_call_at": time.monotonic(), "stream_event_count": 0}
    task = asyncio.create_task(_run_heartbeat(activity, time.monotonic(), interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_liveness_file_is_written_when_path_given(tmp_path):
    liveness_path = tmp_path / "liveness.json"
    activity = {"last_call_name": "some_tool", "last_call_at": time.monotonic(), "stream_event_count": 5}
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(liveness_path))
    )
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert liveness_path.exists()
    snapshot = json.loads(liveness_path.read_text())
    assert "stagnant_seconds" in snapshot
    assert "max_stub_serve_count" in snapshot


@pytest.mark.asyncio
async def test_liveness_snapshot_carries_max_stub_serve_count_from_activity(tmp_path):
    liveness_path = tmp_path / "liveness.json"
    activity = {
        "last_call_name": "get_file_content", "last_call_at": time.monotonic(),
        "stream_event_count": 1, "max_stub_serve_count": 12,
    }
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(liveness_path))
    )
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["max_stub_serve_count"] == 12


@pytest.mark.asyncio
async def test_stagnant_seconds_stays_zero_while_tool_calls_keep_happening(tmp_path):
    """A run that's genuinely active (fresh tool calls arriving) must never look
    stagnant, regardless of how many ticks pass.

    Wide margin deliberately: interval=0.2s with last_call_at refreshed every
    0.03s (~6-7x per tick) so this can't flake under real scheduling jitter --
    an earlier version used a 0.05s/0.06s interval/update-cycle margin that
    passed in isolation but flaked as part of the full suite under load."""
    liveness_path = tmp_path / "liveness.json"
    activity = {"last_call_name": "x", "last_call_at": time.monotonic(), "stream_event_count": 0}
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.2, liveness_path=str(liveness_path))
    )
    for _ in range(8):
        await asyncio.sleep(0.03)
        activity["last_call_at"] = time.monotonic()  # simulates a fresh tool call landing
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["stagnant_seconds"] == 0


@pytest.mark.asyncio
async def test_stagnant_seconds_grows_when_nothing_is_happening(tmp_path):
    """Neither a new tool call NOR new stream content across several ticks --
    stagnant_seconds must climb by one interval's worth per tick."""
    liveness_path = tmp_path / "liveness.json"
    activity = {
        "last_call_name": "x",
        "last_call_at": time.monotonic() - 100,  # already old -- no calls during the test either
        "stream_event_count": 3,  # never changes across ticks below
    }
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(liveness_path))
    )
    await asyncio.sleep(0.17)  # ~3 ticks
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["stagnant_seconds"] >= 0.05 * 2  # at least 2 consecutive stagnant ticks


@pytest.mark.asyncio
async def test_new_stream_content_resets_stagnant_seconds_even_without_a_tool_call(tmp_path):
    """The whole point of tracking stream_event_count alongside last_call_at:
    a coordinator generating a long answer with zero tool calls is still making
    progress, not stalled -- must not be misjudged as stagnant."""
    liveness_path = tmp_path / "liveness.json"
    activity = {
        "last_call_name": "x",
        "last_call_at": time.monotonic() - 100,
        "stream_event_count": 0,
    }
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(liveness_path))
    )
    await asyncio.sleep(0.06)
    activity["stream_event_count"] = 1  # new content arrives between ticks
    await asyncio.sleep(0.06)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["stagnant_seconds"] == 0


@pytest.mark.asyncio
async def test_liveness_write_failure_does_not_crash_the_heartbeat(tmp_path, capsys):
    """A bookkeeping side effect must never take down the run it's watching --
    an unwritable path (a directory that doesn't exist) logs a warning and the
    heartbeat keeps running, exactly like every other defensive check in this
    mechanism (_record_read, _make_delegation_log_hook)."""
    bad_path = tmp_path / "does" / "not" / "exist" / "liveness.json"
    activity = {"last_call_name": None, "last_call_at": time.monotonic(), "stream_event_count": 0}
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(bad_path))
    )
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    out = capsys.readouterr().out
    assert "liveness write warning" in out
    assert "heartbeat" in out  # the normal status line still printed too
