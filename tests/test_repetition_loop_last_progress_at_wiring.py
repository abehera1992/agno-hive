"""Wiring-level regression test for the last_progress_at/repetition-detector
compounding bug (swarm/team.py, fixed 2026-08-14), confirmed live during Phase 4
validation of the "AgnoHive - Engineering Team 2.0 Update" plan.

_looks_like_repetition_loop (tests/test_repetition_loop_detector.py) itself was
correct and had been since the day it shipped -- the bug was in how its verdict
got wired into activity["last_progress_at"]. The original wiring updated
last_progress_at UNCONDITIONALLY on every single string chunk, before the
10s-boundary repetition check ever ran, then only rolled it back to
last_logged_at (the start of the CURRENT ~10s window) when that window's content
was flagged as a repeat. Because chunks kept arriving throughout the SAME
repeating block, each one re-stomped last_progress_at forward via the
unconditional per-chunk update -- immediately undoing the rollback the very next
chunk, within the same already-classified-as-repetitive window. Net effect,
confirmed live on task k7afd9wal: 13+ minutes of continuously-detected,
correctly-logged repetition never advanced stagnant_seconds past ~10-20s, so
the 300s Tier-1 liveness auto-kill (api/server.py) never fired and the run had
to be killed manually.

The fix removes the unconditional per-chunk update entirely -- last_progress_at
now only ever advances inside the 10s-boundary check, and only on the non-repeat
branch. These tests reproduce the compounding effect directly (many repeating
chunks spanning many 10s-windows) rather than a single rollback in isolation,
since a single-window test cannot distinguish the fixed code from the buggy one.

Assertions are comparative (same clock schedule up to a shared prefix of
events), not pinned to exact tick counts -- exact counts are an internal
implementation detail (how many time.monotonic() calls happen before the loop
starts) that would make the test brittle to unrelated future changes.
"""
import asyncio
from types import SimpleNamespace

import pytest

from swarm.team import _stream_team_run


def _content_event(text: str):
    return SimpleNamespace(event="TeamRunContent", content=text, reasoning_content="")


def _final_output(content: str = "final answer"):
    return SimpleNamespace(content=content, messages=[])


class _FakeTeam:
    def __init__(self, events):
        self._events = events

    async def arun(self, prompt, stream=True, yield_run_output=True):
        for event in self._events:
            await asyncio.sleep(0)
            yield event


class _SteppingClock:
    """Every call advances by `step` seconds -- deterministic, no real sleeping.
    step=15 (> the production code's 10s window) guarantees EVERY event's own
    `now = time.monotonic()` call crosses a fresh 10s-boundary check on its own,
    so each event maps to exactly one window with no batching to reason about."""

    def __init__(self, step: float = 15.0):
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


REPEATED_SENTENCE = (
    "I'm encountering a persistent tool call limit that's preventing me from "
    "retrieving the utility_ai_client file. Let me try to get the file content "
    "directly from the project structure instead.\n\n"
)

NEW_CONTENT = (
    "Here is the actual grounded answer, citing real_file.py:42 for the claim "
    "about the utility client's retry behavior under rate limiting."
)


async def _run_and_capture_last_progress_at(monkeypatch, events) -> float:
    clock = _SteppingClock(step=15.0)
    captured = {}

    async def capturing_heartbeat(activity, start, interval=30.0, liveness_path=None):
        captured["activity"] = activity
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("swarm.team.time.monotonic", clock)
    monkeypatch.setattr("swarm.team._run_heartbeat", capturing_heartbeat)

    team = _FakeTeam(events)
    await _stream_team_run(team, "prompt")

    return captured["activity"]["last_progress_at"]


@pytest.mark.asyncio
async def test_many_repeat_windows_never_advance_last_progress_at_past_the_seed(monkeypatch):
    """A run that emits ONE genuinely-new seed window followed by FOUR separate
    windows that are all confirmed repeats of it must end with the SAME
    last_progress_at as a run that only ever saw the seed window -- the repeat
    windows must contribute nothing, no matter how many of them there are."""
    seed_only = [_content_event(REPEATED_SENTENCE), _final_output()]
    seed_plus_many_repeats = [_content_event(REPEATED_SENTENCE) for _ in range(5)]
    seed_plus_many_repeats.append(_final_output())

    seed_only_value = await _run_and_capture_last_progress_at(monkeypatch, seed_only)
    with_repeats_value = await _run_and_capture_last_progress_at(monkeypatch, seed_plus_many_repeats)

    assert with_repeats_value == seed_only_value


@pytest.mark.asyncio
async def test_genuinely_new_content_after_a_repeat_streak_still_advances(monkeypatch):
    """The mechanism isn't permanently wedged by a repeat streak -- once real new
    content arrives after several repeat windows, last_progress_at must advance
    past where it sat during the repeat streak."""
    seed_plus_repeats = [_content_event(REPEATED_SENTENCE) for _ in range(3)]
    seed_plus_repeats.append(_final_output())

    seed_plus_repeats_then_new = [_content_event(REPEATED_SENTENCE) for _ in range(3)]
    seed_plus_repeats_then_new.append(_content_event(NEW_CONTENT))
    seed_plus_repeats_then_new.append(_final_output())

    repeats_only_value = await _run_and_capture_last_progress_at(monkeypatch, seed_plus_repeats)
    with_new_content_value = await _run_and_capture_last_progress_at(monkeypatch, seed_plus_repeats_then_new)

    assert with_new_content_value > repeats_only_value
