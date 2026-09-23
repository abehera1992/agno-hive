"""Phase G: liveness observability.

Diagnosis first, per the phase's own instruction ("First determine what is actually
stalled"). The real T12/T13a 300s stalls (2026-09-21 post-deployment battery, commit
49579a9 -- this behaviour predates that patch and is not attributed to it) were
cross-referenced against vLLM's OWN engine metrics (docker logs vllm-coord, captured
separately from this process): continuous non-zero generation throughput and exactly ONE
open HTTP request for the entire ~347s window, while THIS process's own
activity["stream_event_count"] sat completely frozen throughout -- team.arun()'s generator
yielded ZERO events of any kind. From this process's own observable signals, that is
genuinely, honestly WORKFLOW_STALLED (nothing arrived here to see) -- the deeper truth (one
still-generating request on the other side of the HTTP boundary) requires correlating a
SEPARATE service's own logs, which is out of this phase's "smallest targeted change" scope.
What this phase adds is _classify_liveness_state, which reports that WORKFLOW_STALLED
classification (and three others) from signals the runtime already has, instead of the
previous generic "no tool call or new stream content".

No watchdog threshold changed. No serving configuration touched. No new recovery ACTION --
investigated (see the report) and found not applicable to this specific failure shape (a
single already-in-flight generation cannot be un-stuck by changing tool_choice on a NEXT
call that will never happen before the existing kill fires).
"""
from types import SimpleNamespace

import pytest

from api.server import _liveness_kill_reason
from config.config import config
from swarm.team import (
    _LIVENESS_STATES, _classify_liveness_state, _make_tool_interception_hook,
)

INTERVAL = 30.0


def _activity(**overrides):
    base = {
        "last_call_name": None, "last_call_at": 0.0,
        "stream_event_count": 0, "last_progress_at": 0.0,
        "last_token_at": None, "last_model_event_at": None,
        "last_tool_call_at": None, "last_tool_result_at": None,
    }
    base.update(overrides)
    return base


# ── reproduce the T12/T13a shape ─────────────────────────────────────────────────────────

def test_the_real_t12_shape_classifies_as_workflow_stalled():
    """One tool call completed long ago, then 347s of nothing at all -- zero stream
    events, zero tokens, matching the real captured incident exactly."""
    now = 400.0
    activity = _activity(
        last_call_at=53.0, last_tool_call_at=50.0, last_tool_result_at=53.0,
        last_model_event_at=53.0,          # frozen since the last (only) tool result
        last_token_at=None,                # no content chunk ever arrived
    )
    assert _classify_liveness_state(now, activity, INTERVAL) == "WORKFLOW_STALLED"


def test_the_real_t13a_shape_also_classifies_as_workflow_stalled():
    """T13a: two tool calls completed, then the same silent-generator shape."""
    now = 340.0
    activity = _activity(
        last_call_at=42.0, last_tool_call_at=29.0, last_tool_result_at=42.0,
        last_model_event_at=42.0, last_token_at=42.0,
    )
    assert _classify_liveness_state(now, activity, INTERVAL) == "WORKFLOW_STALLED"


# ── each of the four reported states ─────────────────────────────────────────────────────

def test_model_generating_when_a_token_landed_recently():
    activity = _activity(last_token_at=95.0, last_model_event_at=95.0)
    assert _classify_liveness_state(100.0, activity, INTERVAL) == "MODEL_GENERATING"


def test_tool_executing_when_a_call_has_no_later_result():
    activity = _activity(last_tool_call_at=10.0, last_tool_result_at=None)
    assert _classify_liveness_state(500.0, activity, INTERVAL) == "TOOL_EXECUTING"


def test_tool_executing_is_unbounded_by_recent_s_a_slow_tool_still_counts():
    """A tool call started long ago with no result yet is STILL executing -- this must
    not fall through to WORKFLOW_STALLED just because it has run longer than one
    heartbeat interval; a slow db_query/hive-mcp read is legitimately allowed to."""
    activity = _activity(last_tool_call_at=0.0, last_tool_result_at=None)
    assert _classify_liveness_state(600.0, activity, INTERVAL) == "TOOL_EXECUTING"


def test_tool_executing_ends_once_a_later_result_lands():
    activity = _activity(last_tool_call_at=10.0, last_tool_result_at=12.0)
    assert _classify_liveness_state(500.0, activity, INTERVAL) != "TOOL_EXECUTING"


def test_parser_wait_when_an_event_landed_recently_but_nothing_else_explains_it():
    activity = _activity(
        last_model_event_at=95.0, last_token_at=None,
        last_tool_call_at=5.0, last_tool_result_at=6.0,
    )
    assert _classify_liveness_state(100.0, activity, INTERVAL) == "PARSER_WAIT"


def test_all_four_states_are_the_ones_named_in_the_spec():
    assert set(_LIVENESS_STATES) == {
        "MODEL_GENERATING", "TOOL_EXECUTING", "PARSER_WAIT", "WORKFLOW_STALLED"}


def test_never_observed_anything_at_all_is_workflow_stalled():
    activity = _activity()
    assert _classify_liveness_state(1000.0, activity, INTERVAL) == "WORKFLOW_STALLED"


# ── priority ordering ─────────────────────────────────────────────────────────────────────

def test_model_generating_wins_over_a_stale_open_tool_call():
    activity = _activity(
        last_token_at=95.0, last_model_event_at=95.0,
        last_tool_call_at=0.0, last_tool_result_at=None,  # technically still "open"
    )
    assert _classify_liveness_state(100.0, activity, INTERVAL) == "MODEL_GENERATING"


def test_tool_executing_wins_over_parser_wait():
    activity = _activity(
        last_model_event_at=95.0,                          # would say PARSER_WAIT alone
        last_tool_call_at=10.0, last_tool_result_at=None,   # but a call is still open
    )
    assert _classify_liveness_state(100.0, activity, INTERVAL) == "TOOL_EXECUTING"


# ── backward compatibility ────────────────────────────────────────────────────────────────

def test_a_dict_missing_the_new_keys_entirely_is_handled_safely():
    """An older activity dict (or a caller that never sets these) must not raise, and
    must classify as the safe default."""
    old_activity = {"last_call_name": None, "last_call_at": 0.0,
                    "stream_event_count": 0, "last_progress_at": 0.0}
    assert _classify_liveness_state(1000.0, old_activity, INTERVAL) == "WORKFLOW_STALLED"


# ── _liveness_kill_reason: additive messaging, unchanged kill condition ────────────────────

def _snapshot(stagnant, **extra):
    s = {"stagnant_seconds": stagnant, "no_tool_progress_seconds": 0,
        "requests_advancing": False}
    s.update(extra)
    return s


def test_kill_message_now_names_the_observed_state():
    reason = _liveness_kill_reason(_snapshot(
        config.liveness_silence_threshold_s + 1,
        liveness_state="WORKFLOW_STALLED", liveness_state_seconds=347.0))
    assert reason is not None
    assert "no tool call or new stream content" in reason
    assert "WORKFLOW_STALLED" in reason and "347" in reason


def test_kill_message_without_a_liveness_state_is_byte_identical_to_before_this_phase():
    reason = _liveness_kill_reason(_snapshot(config.liveness_silence_threshold_s + 1))
    assert reason == (
        f"no tool call or new stream content for over "
        f"{config.liveness_silence_threshold_s:.0f}s")


def test_the_kill_threshold_itself_is_unchanged_by_this_phase():
    """A snapshot just under the threshold, even with a liveness_state present, must
    still not trigger Tier 1 -- this phase changes WHAT is reported, never WHEN."""
    reason = _liveness_kill_reason(_snapshot(
        config.liveness_silence_threshold_s - 1,
        liveness_state="MODEL_GENERATING", liveness_state_seconds=5.0))
    assert reason is None


def test_other_tiers_are_completely_unaffected_by_liveness_state_presence():
    reason = _liveness_kill_reason(_snapshot(
        0, max_stub_serve_count=999, liveness_state="TOOL_EXECUTING"))
    assert reason == "repeated an identical call 999 times despite being told to stop"


# ── the interception hook actually writes the new timestamps ───────────────────────────────

@pytest.mark.asyncio
async def test_interception_hook_sets_tool_call_and_result_timestamps():
    hook = _make_tool_interception_hook(activity=None)  # signature check only if activity=None short-circuits
    activity = {"last_call_name": None, "last_call_at": 0.0, "stream_event_count": 0,
                "last_progress_at": 0.0, "last_token_at": None, "last_model_event_at": None,
                "last_tool_call_at": None, "last_tool_result_at": None}
    hook = _make_tool_interception_hook(activity=activity)

    async def _real(**kwargs):
        return "ok"

    before = _classify_liveness_state(1000.0, activity, INTERVAL)
    assert before == "WORKFLOW_STALLED"
    result = await hook("some_tool", _real, {"x": 1}, agent=None, team=None)
    assert result == "ok"
    assert activity["last_tool_call_at"] is not None
    assert activity["last_tool_result_at"] is not None
    assert activity["last_tool_result_at"] >= activity["last_tool_call_at"]


@pytest.mark.asyncio
async def test_interception_hook_sets_result_timestamp_even_when_the_call_raises():
    activity = {"last_call_name": None, "last_call_at": 0.0, "stream_event_count": 0,
                "last_progress_at": 0.0, "last_token_at": None, "last_model_event_at": None,
                "last_tool_call_at": None, "last_tool_result_at": None}
    hook = _make_tool_interception_hook(activity=activity)

    async def _failing(**kwargs):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await hook("some_tool", _failing, {}, agent=None, team=None)
    assert activity["last_tool_call_at"] is not None
    assert activity["last_tool_result_at"] is not None


# ── wiring: both stream-consuming loops actually set the new fields ────────────────────────

def test_both_run_functions_write_last_token_at_and_last_model_event_at():
    import inspect
    from swarm import team as team_mod
    for fn in (team_mod.run_task_async, team_mod._stream_team_run):
        src = inspect.getsource(fn)
        assert 'activity["last_model_event_at"] = time.monotonic()' in src
        assert 'activity["last_token_at"] = now' in src


def test_both_activity_dict_inits_include_the_four_new_keys():
    import inspect
    from swarm import team as team_mod
    for fn in (team_mod.run_task_async, team_mod._stream_team_run):
        src = inspect.getsource(fn)
        for key in ("last_token_at", "last_model_event_at",
                   "last_tool_call_at", "last_tool_result_at"):
            assert f'"{key}"' in src, (fn.__name__, key)


def test_run_heartbeat_writes_liveness_state_into_the_snapshot():
    import inspect
    from swarm import team as team_mod
    src = inspect.getsource(team_mod._run_heartbeat)
    assert '"liveness_state": liveness_state' in src
    assert '"liveness_state_seconds"' in src
    assert "_classify_liveness_state(" in src


# ── no serving/threshold/unrelated behavior changed ─────────────────────────────────────────

def test_no_liveness_threshold_constant_was_touched_by_this_phase():
    """Every threshold this module reads from config is untouched -- Phase G only adds a
    NEW, purely additive classification; it reads no new config, defines no new threshold."""
    import inspect
    from swarm import team as team_mod
    src = inspect.getsource(team_mod._classify_liveness_state)
    assert "config." not in src
