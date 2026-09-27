"""Phase Z16 (2026-09-26) -- in-process early stop on the Coordinator synthesis
repetition loop, swarm/team.py.

Root cause (see the Z16 report): _looks_like_repetition_loop/_looks_like_repetition_decay
have always correctly DETECTED a runaway generation, but the only response was to
withhold progress credit from activity["last_progress_at"] -- nothing inside either
streaming-consumption loop (_stream_team_run, run_task_async's own inline copy) ever
stopped CONSUMING the stream. The only actual stop was an EXTERNAL SIGKILL of the whole
worker process once api/server.py's Tier 5 liveness auto-kill saw
repetition_count >= config.liveness_repetition_threshold on the next heartbeat tick --
which discards every in-memory guard/verification pass and forces a lossy draft-plus-
repair salvage from disk, even when the generation had already produced a complete,
correct answer before it started looping.

This phase adds an IN-PROCESS check at the exact point activity["repetition_count"] is
incremented, reusing the SAME already-tuned threshold Tier 5 uses (no new constant): once
reached, the streaming loop breaks itself, and the post-loop answer-extraction is trimmed
back to last_good_len -- the end of the last CONFIRMED-non-repeat window -- so the
repeating tail never reaches the answer, but the grounded content generated before it did
survives and flows through the SAME guard chain (verify_claims, evidence-integrity, etc.)
any normal completion does.

A note on fixture design: _looks_like_repetition_loop can only ever flag a segment
against text that ALREADY PRECEDES it -- the very first occurrence of any block is
therefore always classified as genuine ("nothing to repeat against yet"), and only its
SECOND and later occurrences are flagged. Every fixture below accounts for this: a block
appearing once is "the seed", and repetition_count only starts climbing from its second
occurrence onward.

These tests exercise _stream_team_run directly (the shared, testable half of the two
byte-identical loops -- run_task_async's own copy is kept in sync by inspection, the same
way test_repetition_loop_last_progress_at_wiring.py tests only _stream_team_run for the
last_progress_at wiring both loops share).
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
    """Same fixture as test_repetition_loop_last_progress_at_wiring.py -- step=15
    (> the 10s window) guarantees every event crosses its own fresh boundary check."""

    def __init__(self, step: float = 15.0):
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


GROUNDED_ANSWER = (
    "The inventory-service exposes three routers: parties, vouchers, and "
    "categories. Each is mounted in main.py via include_router."
)

BOILERPLATE_REPEAT = (
    "## Verified Findings\n\nAll claims above are grounded in file content read "
    "during this session, and nothing here contradicts it.\n\n"
)

GENUINELY_NEW_CONTENT = (
    "Additionally, the categories router exposes a bulk-import endpoint at "
    "POST /categories/import, added in a later commit and not part of the "
    "original three-router set."
)


async def _run(monkeypatch, events, threshold=None):
    clock = _SteppingClock(step=15.0)

    async def noop_heartbeat(activity, start, interval=30.0, liveness_path=None):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("swarm.team.time.monotonic", clock)
    monkeypatch.setattr("swarm.team._run_heartbeat", noop_heartbeat)
    if threshold is not None:
        monkeypatch.setattr("swarm.team.config.liveness_repetition_threshold", threshold)

    team = _FakeTeam(events)
    return await _stream_team_run(team, "prompt")


# Test 1 -- valid synthesis terminates normally, never engaging the new code path.
@pytest.mark.asyncio
async def test_valid_synthesis_terminates_normally(monkeypatch):
    events = [_content_event(GROUNDED_ANSWER), _final_output(GROUNDED_ANSWER)]

    content, final_run_output = await _run(monkeypatch, events, threshold=4)

    assert content == GROUNDED_ANSWER
    assert final_run_output is not None
    assert final_run_output.content == GROUNDED_ANSWER


# Test 2 -- a short repeat streak that never reaches threshold: existing behavior
# (withhold progress credit, no early stop, run to natural completion) is unchanged.
@pytest.mark.asyncio
async def test_repeats_below_threshold_do_not_trigger_early_stop(monkeypatch):
    events = (
        [_content_event(BOILERPLATE_REPEAT)]                          # seed (genuine)
        + [_content_event(BOILERPLATE_REPEAT) for _ in range(2)]      # repeats #1, #2
        + [_final_output(BOILERPLATE_REPEAT)]
    )

    content, final_run_output = await _run(monkeypatch, events, threshold=4)

    assert final_run_output is not None
    assert content == BOILERPLATE_REPEAT.strip()


# Test 5 -- repeated boilerplate AT threshold terminates safely in-process instead of
# running away, and the previously-grounded answer survives with exactly the ONE
# legitimate occurrence of the repeated block (its seed), not the many that followed.
@pytest.mark.asyncio
async def test_repeats_at_threshold_stop_in_process_and_keep_the_grounded_prefix(monkeypatch):
    events = (
        [_content_event(GROUNDED_ANSWER)]                              # genuine
        + [_content_event(BOILERPLATE_REPEAT)]                         # seed of the repeat (genuine)
        + [_content_event(BOILERPLATE_REPEAT) for _ in range(10)]      # would run away forever
    )

    content, final_run_output = await _run(monkeypatch, events, threshold=2)

    # Broke before natural exhaustion -- a runaway loop never reaches
    # final_run_output in reality either.
    assert final_run_output is None
    # Test 7 -- the previously grounded answer survives, WITH its one legitimate
    # occurrence of the repeated block; the nine further repeats do not.
    assert content == GROUNDED_ANSWER + BOILERPLATE_REPEAT.strip()
    assert content.count("Verified Findings") == 1


# Test 7 (explicit) -- confirms the surviving content is a clean, complete prefix, not
# truncated mid-sentence and not padded with any of the trimmed repeated material.
@pytest.mark.asyncio
async def test_surviving_answer_is_not_truncated_mid_sentence(monkeypatch):
    events = (
        [_content_event(GROUNDED_ANSWER)]
        + [_content_event(BOILERPLATE_REPEAT)]
        + [_content_event(BOILERPLATE_REPEAT) for _ in range(6)]
    )

    content, _ = await _run(monkeypatch, events, threshold=3)

    assert content == GROUNDED_ANSWER + BOILERPLATE_REPEAT.strip()
    assert content.endswith(".")


# Edge case -- the ONLY thing ever generated is a block that immediately starts
# repeating itself (no separate "good answer" precedes it). last_good_len still
# correctly captures the ONE legitimate occurrence -- must not return the accumulated
# repeats, and must not crash.
@pytest.mark.asyncio
async def test_content_that_repeats_from_its_own_second_occurrence_keeps_one_copy(monkeypatch):
    events = [_content_event(BOILERPLATE_REPEAT) for _ in range(6)]

    content, final_run_output = await _run(monkeypatch, events, threshold=2)

    assert final_run_output is None
    assert content == BOILERPLATE_REPEAT.strip()


# Test 6 -- no false early termination: genuinely new content arriving after a short
# repeat streak (still below threshold) must survive intact, not be discarded, and
# must not itself be misclassified as a reason to stop.
@pytest.mark.asyncio
async def test_new_evidence_after_a_short_repeat_streak_is_not_discarded(monkeypatch):
    events = (
        [_content_event(GROUNDED_ANSWER)]
        + [_content_event(BOILERPLATE_REPEAT)]                    # seed (genuine)
        + [_content_event(BOILERPLATE_REPEAT)]                    # repeat #1 (below threshold=4)
        + [_content_event(GENUINELY_NEW_CONTENT)]
        + [_final_output(GROUNDED_ANSWER + BOILERPLATE_REPEAT + GENUINELY_NEW_CONTENT)]
    )

    content, final_run_output = await _run(monkeypatch, events, threshold=4)

    assert final_run_output is not None
    assert content == GROUNDED_ANSWER + BOILERPLATE_REPEAT + GENUINELY_NEW_CONTENT


# Regression guard -- repetition_count is a run-wide cumulative counter (matches Tier
# 5's own existing semantics, deliberately unchanged by this phase): separate short
# repeat streaks divided by genuine content still add up and can still trigger the
# in-process stop, exactly as they already can trigger the external Tier 5 kill.
@pytest.mark.asyncio
async def test_repetition_count_accumulates_across_separated_streaks(monkeypatch):
    events = (
        [_content_event(GROUNDED_ANSWER)]                          # genuine
        + [_content_event(BOILERPLATE_REPEAT)]                     # seed (genuine)
        + [_content_event(BOILERPLATE_REPEAT)]                     # repeat #1
        + [_content_event(GENUINELY_NEW_CONTENT)]                  # genuine -- does not reset the count
        + [_content_event(BOILERPLATE_REPEAT)]                     # repeat #2 -- reaches threshold=2
        + [_content_event(BOILERPLATE_REPEAT) for _ in range(10)]  # would run away
    )

    content, final_run_output = await _run(monkeypatch, events, threshold=2)

    assert final_run_output is None
    # last_good_len was last advanced after GENUINELY_NEW_CONTENT (the last confirmed
    # non-repeat window), so the surviving answer includes EVERYTHING generated up to
    # there -- including the repeat#1 window, which was withheld from progress credit
    # but still legitimately part of the accumulated text once real progress after it
    # confirmed the run wasn't stuck. Only the final, never-resolved repeat#2-onward
    # tail is discarded.
    assert content == GROUNDED_ANSWER + BOILERPLATE_REPEAT + BOILERPLATE_REPEAT + GENUINELY_NEW_CONTENT
