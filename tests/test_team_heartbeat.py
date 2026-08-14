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


# ── last_progress_at (2026-08-14): closes a gap the two tests above don't cover ─
# -- stream_event_count climbing does NOT always mean real progress. Live
# investigation 2026-08-13/14 found a Researcher stuck 700+s with vLLM generating
# real tokens every ~2s (event_count climbing continuously) while every single
# RunContent event carried content='' -- agno's own tool_call_limit enforcement
# (agno/models/base.py:run_function_calls) silently rejects a tool call once the
# per-agent cumulative count is exceeded (appends an error Message, `continue`s --
# yields NO stream event for the rejected call), and nothing forces the model to
# stop retrying. The OLD stagnation check (event_count == last_event_count) never
# fired here because raw event_count kept growing from the empty RunContent
# events themselves. last_progress_at is a NEW, separate signal the caller updates
# only when _stream_event_to_chunk returns real content or a tool event (never on
# an empty/unrecognized one) -- when present, _run_heartbeat prefers it over the
# raw event-count check; when absent (every test above, and any older caller),
# behavior is byte-for-byte unchanged.

@pytest.mark.asyncio
async def test_stagnant_seconds_grows_when_events_keep_arriving_but_carry_no_progress(tmp_path):
    """The exact bug: stream_event_count climbs every tick (as if raw events keep
    arriving) but last_progress_at never advances (as if every event is an empty,
    silently-rejected tool-call turn) -- must still be judged stagnant."""
    liveness_path = tmp_path / "liveness.json"
    activity = {
        "last_call_name": "search_files",
        "last_call_at": time.monotonic() - 100,
        "stream_event_count": 0,
        "last_progress_at": time.monotonic() - 100,  # no real progress in 100s
    }
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(liveness_path))
    )
    for _ in range(3):
        await asyncio.sleep(0.05)
        activity["stream_event_count"] += 40  # raw events keep pouring in...
        # ...but last_progress_at is deliberately NOT touched
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["stagnant_seconds"] >= 0.05 * 2


@pytest.mark.asyncio
async def test_last_progress_at_being_refreshed_keeps_stagnant_seconds_at_zero(tmp_path):
    """Sanity check on the other side: when last_progress_at DOES keep advancing
    (real content or a real tool call landing), stagnant_seconds must stay zero --
    same guarantee test_new_stream_content_resets_stagnant_seconds_even_without_a_tool_call
    gives the old signal, now proven for the new one."""
    liveness_path = tmp_path / "liveness.json"
    activity = {
        "last_call_name": "x",
        "last_call_at": time.monotonic() - 100,
        "stream_event_count": 0,
        "last_progress_at": time.monotonic(),
    }
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.03, liveness_path=str(liveness_path))
    )
    for _ in range(8):
        await asyncio.sleep(0.01)
        activity["last_progress_at"] = time.monotonic()  # real progress landing repeatedly
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["stagnant_seconds"] == 0


@pytest.mark.asyncio
async def test_absent_last_progress_at_falls_back_to_the_old_event_count_check(tmp_path):
    """Backward compat, explicit: a caller that never sets last_progress_at (every
    test above this section) must behave exactly as before -- judged by raw
    stream_event_count staying unchanged, not by the new field's absence.
    _stream_team_run's own retry-loop activity dict USED to be such a caller
    (documented here until 2026-08-14) -- it now tracks last_progress_at itself,
    see test_stream_team_run_liveness.py, closing a real production hang this
    gap directly caused. This test still matters as the general contract for any
    other/future bare caller, just no longer describes _stream_team_run."""
    liveness_path = tmp_path / "liveness.json"
    activity = {
        "last_call_name": "x",
        "last_call_at": time.monotonic() - 100,
        "stream_event_count": 3,  # never changes across ticks below, no last_progress_at key at all
    }
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(liveness_path))
    )
    await asyncio.sleep(0.17)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["stagnant_seconds"] >= 0.05 * 2


@pytest.mark.asyncio
async def test_liveness_snapshot_carries_total_stub_serve_count_from_activity(tmp_path):
    """total_stub_serve_count (2026-08-14) -- the aggregate Tier-2 signal
    alongside max_stub_serve_count's own per-key one, see
    test_team_read_cache_hook.py's own section for the live incident this
    closes (a model rotating between several already-stubbed files, each
    individually staying under the per-key threshold)."""
    liveness_path = tmp_path / "liveness.json"
    activity = {
        "last_call_name": "get_file_content", "last_call_at": time.monotonic(),
        "stream_event_count": 1, "max_stub_serve_count": 4, "total_stub_serve_count": 11,
    }
    task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), interval=0.05, liveness_path=str(liveness_path))
    )
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = json.loads(liveness_path.read_text())
    assert snapshot["total_stub_serve_count"] == 11


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
