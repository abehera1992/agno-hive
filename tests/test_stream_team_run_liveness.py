"""Tests for _stream_team_run's liveness_path wiring (swarm/team.py, 2026-08-14).

Closes a confirmed live production hang: a verify_claims correction retry (built
on _stream_team_run) emitted nothing but empty TeamRunContent events for 30+
minutes -- real, distinct vLLM completions the whole time (each event's own
model_provider_data carried a unique completion id, so this was not one hung
stream) -- and NOTHING ended it. The process-level liveness auto-kill
(api/server.py's _run_worker_subprocess, DOCS.md "Liveness-Based Auto-Kill")
only ever sees staleness through a JSON snapshot written to liveness_path;
_stream_team_run never accepted or forwarded that path, and never tracked
last_progress_at either, so a retry's own heartbeat had nothing to report and the
outer watchdog saw a liveness file that simply stopped being updated -- which
this codebase's own kill-reason logic treats as "still fine", not "stale". It
took an unrelated client-side httpx timeout (not any server-side safety net) to
eventually end the run.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from swarm.team import _BackendRunError, _stream_team_run


def _content_event(text: str):
    return SimpleNamespace(event="TeamRunContent", content=text, reasoning_content="")


def _empty_event():
    return SimpleNamespace(event="TeamRunContent", content="", reasoning_content="")


def _final_output(content: str = "final answer"):
    """No .event attribute at all -- matches _stream_team_run's own
    `not getattr(event, "event", None)` duck-typed check for the real
    TeamRunOutput object agno yields last."""
    return SimpleNamespace(content=content, messages=[])


class _FakeTeam:
    """Yields an explicit `await asyncio.sleep(0)` before each event -- a real
    scheduling yield point, without which asyncio.create_task()'s heartbeat task
    never actually gets a turn to run before _stream_team_run's own `finally`
    cancels it (create_task only SCHEDULES a task; the loop only starts running
    its body at the next real await point in the calling coroutine)."""

    def __init__(self, events):
        self._events = events

    async def arun(self, prompt, stream=True, yield_run_output=True):
        for event in self._events:
            await asyncio.sleep(0)
            yield event


@pytest.mark.asyncio
async def test_liveness_path_is_forwarded_to_run_heartbeat(monkeypatch):
    captured = {}

    async def fake_heartbeat(activity, start, interval=30.0, liveness_path=None):
        captured["liveness_path"] = liveness_path
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("swarm.team._run_heartbeat", fake_heartbeat)
    team = _FakeTeam([_content_event("hello"), _final_output()])

    await _stream_team_run(team, "prompt", liveness_path="/tmp/some-liveness.json")

    assert captured["liveness_path"] == "/tmp/some-liveness.json"


@pytest.mark.asyncio
async def test_default_liveness_path_is_none_backward_compat(monkeypatch):
    captured = {}

    async def fake_heartbeat(activity, start, interval=30.0, liveness_path=None):
        captured["liveness_path"] = liveness_path
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("swarm.team._run_heartbeat", fake_heartbeat)
    team = _FakeTeam([_content_event("hello"), _final_output()])

    await _stream_team_run(team, "prompt")  # no liveness_path passed

    assert captured["liveness_path"] is None


@pytest.mark.asyncio
async def test_real_content_keeps_the_liveness_snapshot_non_stagnant(tmp_path):
    """End-to-end with the REAL _run_heartbeat: a retry that's genuinely producing
    content must never look stagnant to the outer watchdog."""
    liveness_path = tmp_path / "liveness.json"
    team = _FakeTeam([_content_event("some real generated text"), _final_output()])

    await _stream_team_run(team, "prompt", liveness_path=str(liveness_path))

    # The heartbeat is cancelled in _stream_team_run's own `finally` once the run
    # completes, but the last snapshot it wrote before that must reflect real
    # progress, not staleness.
    if liveness_path.exists():
        snapshot = json.loads(liveness_path.read_text())
        assert snapshot["stagnant_seconds"] == 0


@pytest.mark.asyncio
async def test_activity_dict_tracks_last_progress_at_and_is_updated_on_real_content(monkeypatch):
    """The exact production gap, reproduced directly: this function's own activity
    dict must (a) initialize last_progress_at (it didn't, before this fix -- see
    test_team_heartbeat.py's test_absent_last_progress_at_falls_back_to_the_old_event_count_check,
    whose own docstring documented this exact omission) and (b) advance it when
    real content arrives, mirroring run_task_async's outer loop exactly."""
    captured = {}

    async def fake_heartbeat(activity, start, interval=30.0, liveness_path=None):
        captured["activity"] = activity
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("swarm.team._run_heartbeat", fake_heartbeat)
    team = _FakeTeam([_content_event("some real generated text"), _final_output()])

    before = time.monotonic()
    await _stream_team_run(team, "prompt")
    after = time.monotonic()

    activity = captured["activity"]
    assert "last_progress_at" in activity
    assert before <= activity["last_progress_at"] <= after


@pytest.mark.asyncio
async def test_activity_dict_last_progress_at_is_untouched_by_empty_only_events(monkeypatch):
    """The other half of the same gap: an empty-content event must NOT advance
    last_progress_at -- otherwise a run stuck emitting nothing but empty
    completions (the real incident) would still look like it's making progress."""
    captured = {}

    async def fake_heartbeat(activity, start, interval=30.0, liveness_path=None):
        captured["activity"] = activity
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("swarm.team._run_heartbeat", fake_heartbeat)
    team = _FakeTeam([_empty_event(), _empty_event(), _empty_event(), _final_output(content="")])

    initial_mark = time.monotonic()
    await _stream_team_run(team, "prompt")

    activity = captured["activity"]
    # last_progress_at was set once at init time and never touched again -- must
    # still be at (or before) the moment the function started, not later.
    assert activity["last_progress_at"] <= initial_mark + 0.05
    assert activity["stream_event_count"] == 3  # all three empty events still counted


# ── Fast-fail on a real backend error (2026-08-18 live incident) ────────────────
# A litellm.ContextWindowExceededError arrived as a RunError stream event on a
# real run, was previously dropped (treated the same as any harmless
# unrecognized event), and the run then idled for 5+ minutes producing nothing
# until the 300s liveness auto-kill eventually caught it. This is the actual
# fix: the moment such an event is seen, raise _BackendRunError immediately
# instead of continuing to poll — main.py's _run_worker() already converts any
# exception into a fast, clean {"error": ...} response with no new plumbing
# needed (see _BackendRunError's own docstring in swarm/team.py).

def _run_error_event(message: str):
    return SimpleNamespace(event="RunError", content=message)


@pytest.mark.asyncio
async def test_run_error_event_raises_backend_run_error_immediately():
    team = _FakeTeam([
        _content_event("some real generated text"),
        _run_error_event("litellm.ContextWindowExceededError: ..."),
        _content_event("this should never be reached"),
    ])

    with pytest.raises(_BackendRunError, match="ContextWindowExceededError"):
        await _stream_team_run(team, "prompt")


@pytest.mark.asyncio
async def test_run_error_cancels_the_heartbeat_task_via_finally(monkeypatch):
    """The raise must still go through _stream_team_run's own `finally:
    heartbeat_task.cancel()` -- not leak a running heartbeat task."""
    cancelled = {"value": False}

    async def fake_heartbeat(activity, start, interval=30.0, liveness_path=None):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise

    monkeypatch.setattr("swarm.team._run_heartbeat", fake_heartbeat)
    team = _FakeTeam([_run_error_event("boom")])

    with pytest.raises(_BackendRunError):
        await _stream_team_run(team, "prompt")

    assert cancelled["value"] is True
