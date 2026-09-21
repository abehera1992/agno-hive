"""A3: a call to a tool the caller does not hold gets a structured reply, and a second
CONSECUTIVE such round flips the caller to text-only.

agno answers an unknown tool name with one generic sentence and nothing else, so a model
whose instructions or roster still name a stripped tool just asks again until its budget
is gone -- and the parent then reports "tool budget is spent". The runtime knows the real
surface. These tests drive the REAL agno dispatcher (Model.get_function_calls_to_run)
through the mixin, not a stand-in for it.
"""
import inspect

import pytest
from agno.models.message import Message
from agno.tools.function import Function

from swarm.tool_fix import (
    VLLMToolFix, OllamaToolFix, reset_unavailable_tool_state, unavailable_tool_snapshot,
)


def _known(name):
    def _f() -> str:
        return "ok"
    _f.__name__ = name
    return Function(name=name, entrypoint=_f)


def _call(cid, name):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _model(cls=VLLMToolFix):
    m = cls(id="m")
    m.forced = 0

    def _force():
        m.forced += 1
    m._on_repeated_unavailable_tool = _force
    m._unavailable_owner = "Tester"
    return m


def _round(model, messages, calls, functions):
    """One tool-call round: append the assistant message, then dispatch it."""
    assistant = Message(role="assistant", tool_calls=calls)
    messages.append(assistant)
    return model.get_function_calls_to_run(assistant, messages, functions)


@pytest.fixture(autouse=True)
def _fresh():
    reset_unavailable_tool_state()
    yield
    reset_unavailable_tool_state()


def test_first_unavailable_call_gets_a_structured_reply_with_the_real_surface():
    m, msgs = _model(), []
    fns = {"get_file_content": _known("get_file_content"),
           "list_directory": _known("list_directory")}
    run = _round(m, msgs, [_call("c1", "apply_diff")], fns)

    assert run == []
    (reply,) = [x for x in msgs if x.role == "tool"]
    assert reply.tool_call_id == "c1"                 # still matches its tool call
    assert reply.content.startswith("TOOL_UNAVAILABLE")
    assert "'apply_diff'" in reply.content
    assert "get_file_content" in reply.content and "list_directory" in reply.content
    assert m.forced == 0                              # first attempt never forces


def test_reply_never_exposes_a_stripped_tool_schema():
    m, msgs = _model(), []
    _round(m, msgs, [_call("c1", "write_file")], {"get_file_content": _known("get_file_content")})
    reply = [x for x in msgs if x.role == "tool"][0].content
    assert "write_file" in reply                      # only the name the model itself used
    assert "old_string" not in reply and "parameters" not in reply


def test_second_consecutive_unavailable_round_forces_text_only_and_still_replies():
    m, msgs = _model(), []
    fns = {"get_file_content": _known("get_file_content")}
    _round(m, msgs, [_call("c1", "apply_diff")], fns)
    _round(m, msgs, [_call("c2", "apply_diff")], fns)

    replies = {x.tool_call_id: x.content for x in msgs if x.role == "tool"}
    assert set(replies) == {"c1", "c2"}
    assert all(v.startswith("TOOL_UNAVAILABLE") for v in replies.values())
    assert m.forced == 1


def test_a_known_call_between_resets_the_streak():
    m, msgs = _model(), []
    fns = {"get_file_content": _known("get_file_content")}
    _round(m, msgs, [_call("c1", "apply_diff")], fns)
    run = _round(m, msgs, [_call("c2", "get_file_content")], fns)
    assert len(run) == 1                              # the known call is dispatched
    msgs.append(Message(role="tool", tool_call_id="c2", content="file text"))
    _round(m, msgs, [_call("c3", "apply_diff")], fns)
    assert m.forced == 0                              # streak restarted: a first attempt


def test_a_round_mixing_known_and_unavailable_calls_is_not_a_repeat():
    m, msgs = _model(), []
    fns = {"get_file_content": _known("get_file_content")}
    _round(m, msgs, [_call("c1", "apply_diff")], fns)
    run = _round(m, msgs, [_call("c2", "apply_diff"), _call("c3", "get_file_content")], fns)
    assert len(run) == 1
    assert m.forced == 0


def test_streak_comes_from_history_not_from_a_counter_on_the_model():
    fns = {"get_file_content": _known("get_file_content")}
    m1, msgs = _model(), []
    _round(m1, msgs, [_call("c1", "apply_diff")], fns)
    m2 = _model()                                     # a rebuilt agent sees the same history
    _round(m2, msgs, [_call("c2", "apply_diff")], fns)
    assert m2.forced == 1
    _round(m2, [], [_call("c9", "apply_diff")], fns)  # fresh history: starts over
    assert m2.forced == 1


@pytest.mark.parametrize("cls", [VLLMToolFix, OllamaToolFix])
def test_both_provider_classes_carry_the_mixin(cls):
    m, msgs = _model(cls), []
    _round(m, msgs, [_call("c1", "nope")], {"a": _known("a")})
    assert [x for x in msgs if x.role == "tool"][0].content.startswith("TOOL_UNAVAILABLE")


def test_no_callback_installed_is_harmless():
    m = VLLMToolFix(id="m")
    msgs = []
    fns = {"a": _known("a")}
    _round(m, msgs, [_call("c1", "nope")], fns)
    _round(m, msgs, [_call("c2", "nope")], fns)
    assert all(x.content.startswith("TOOL_UNAVAILABLE") for x in msgs if x.role == "tool")


# ── telemetry ────────────────────────────────────────────────────────────────

def test_recorder_counts_and_reset_clears_it():
    assert unavailable_tool_snapshot() == {}
    m, msgs = _model(), []
    fns = {"a": _known("a")}
    _round(m, msgs, [_call("c1", "gone")], fns)
    _round(m, msgs, [_call("c2", "gone")], fns)
    snap = unavailable_tool_snapshot()
    assert snap["count"] == 2 and snap["last_tool"] == "gone"
    assert snap["last_owner"] == "Tester" and snap["forced_text_only"] is True
    reset_unavailable_tool_state()
    assert unavailable_tool_snapshot() == {}


def test_no_unavailable_call_leaves_the_record_empty():
    m, msgs = _model(), []
    _round(m, msgs, [_call("c1", "a")], {"a": _known("a")})
    assert unavailable_tool_snapshot() == {}


def test_run_entry_points_reset_the_record():
    from swarm import team as team_mod
    for fn in (team_mod.run_task_stream, team_mod.run_task_async):
        assert "reset_unavailable_tool_state()" in inspect.getsource(fn)


def test_heartbeat_snapshot_carries_the_record():
    from swarm import team as team_mod
    assert '"unavailable_tool_calls": unavailable_tool_snapshot()' in inspect.getsource(
        team_mod._run_heartbeat)


# ── Tier 4 reason ────────────────────────────────────────────────────────────

def _tier4_snapshot(**extra):
    snap = {"stagnant_seconds": 200, "no_tool_progress_seconds": 200,
            "requests_advancing": True}
    snap.update(extra)
    return snap


def test_tier4_reports_the_unavailable_tool_instead_of_a_spent_budget(monkeypatch):
    from api import server
    monkeypatch.setattr(server.config, "liveness_silence_threshold_s", 10_000)
    monkeypatch.setattr(server.config, "liveness_refused_call_threshold_s", 60)
    reason = server._liveness_kill_reason(_tier4_snapshot(
        unavailable_tool_calls={"count": 4, "last_tool": "apply_diff"}))
    assert "apply_diff" in reason and "does not have" in reason
    assert "budget is spent" not in reason


def test_tier4_without_the_record_keeps_the_original_reason(monkeypatch):
    from api import server
    monkeypatch.setattr(server.config, "liveness_silence_threshold_s", 10_000)
    monkeypatch.setattr(server.config, "liveness_refused_call_threshold_s", 60)
    for extra in ({}, {"unavailable_tool_calls": {}}):
        reason = server._liveness_kill_reason(_tier4_snapshot(**extra))
        assert "tool budget is spent" in reason


# ── wiring through _build_team ───────────────────────────────────────────────

def _built(monkeypatch):
    from swarm import team as team_mod
    from api.models import AgentSpec
    monkeypatch.setattr("swarm.team.config.inference_backend", "vllm")
    spec = AgentSpec(name="Researcher", role="r", model="m", instructions=[], tools=[])
    return team_mod._build_team(
        agent_specs=[spec], coordinator_model="m", coordinator_tools=[], mode="coordinate",
        mcp_list=[], instructions=[])


def test_build_team_installs_the_callback_on_the_coordinator_and_every_member(monkeypatch):
    t = _built(monkeypatch)
    assert callable(t.model._on_repeated_unavailable_tool)
    assert t.model._unavailable_owner == "Coordinator"
    for member in t.members:
        assert callable(member.model._on_repeated_unavailable_tool)
        assert member.model._unavailable_owner == member.name


def test_callback_flips_the_owning_agent_to_text_only(monkeypatch):
    t = _built(monkeypatch)
    t.model._on_repeated_unavailable_tool()
    assert t.tool_choice == "none"
    t.members[0].model._on_repeated_unavailable_tool()
    assert t.members[0].tool_choice == "none"
