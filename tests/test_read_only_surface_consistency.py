"""A1 / A2: what a read_only run TELLS an agent must match what it can CALL.

read_only strips mutating tools from the tool surface (_strip_mutating), but the
Coordinator's roster preamble was composed from the un-stripped specs, and member
instruction lines that told an agent to call a stripped tool were left in place. An
agent was then instructed to use tools that no longer existed for it.

A1: the roster is composed from stripped specs (early rebind in both run_task_*).
A2: (a) member lines that name ONLY removed tools go with them; (b) the Coordinator's
    get_file_content ownership lines are made true for the surface it actually holds.
"""
import inspect

import pytest

from swarm import team as team_mod
from swarm.team import (
    _COORDINATOR_FILE_READ_REWRITES, _COORDINATOR_INSTRUCTIONS,
    _coordinator_instructions_for_surface, _instructions_without_removed_tool_lines,
    _strip_mutating, _team_roster_preamble,
)
from api.models import AgentSpec


def _spec(name="Coder", tools=None, instructions=None):
    return AgentSpec(name=name, role="r", model="m", tools=tools,
                     instructions=instructions or [])


# ── A2 (a): member instruction lines ─────────────────────────────────────────

def test_line_naming_only_a_removed_tool_is_dropped():
    out = _instructions_without_removed_tool_lines(
        ["Use apply_diff() for existing files.", "Read carefully."],
        removed={"apply_diff"}, kept={"get_file_content"})
    assert out == ["Read carefully."]


def test_mixed_line_naming_a_removed_and_a_kept_tool_survives():
    line = "Read with get_file_content, then apply_diff the change."
    out = _instructions_without_removed_tool_lines(
        [line], removed={"apply_diff"}, kept={"get_file_content"})
    assert out == [line]


def test_line_naming_no_tool_is_untouched_and_names_match_whole_words_only():
    lines = ["Be concise.", "apply_diff_helper is a variable, not the tool."]
    out = _instructions_without_removed_tool_lines(
        lines, removed={"apply_diff"}, kept=set())
    assert out == lines


def test_only_this_members_own_removed_set_is_consulted():
    """A tool this member never held (so nothing removed from it) is never filtered,
    even though it is mutating for other members."""
    spec = _spec(tools=["get_file_content"],
                 instructions=["Use apply_diff() for edits.", "Read files."])
    (out,), _ = _strip_mutating([spec], None)
    assert out.instructions == ["Use apply_diff() for edits.", "Read files."]


def test_strip_mutating_filters_member_lines_from_the_actual_removed_set():
    spec = _spec(
        tools=["get_file_content", "apply_diff", "write_file"],
        instructions=[
            "Use apply_diff() for existing files, write_file() only for new ones.",
            "Read with get_file_content, then apply_diff.",
            "Answer plainly.",
        ])
    (out,), _ = _strip_mutating([spec], None)
    assert out.tools == ["get_file_content"]
    assert out.instructions == ["Read with get_file_content, then apply_diff.",
                                "Answer plainly."]
    # the caller's own spec is not mutated
    assert len(spec.instructions) == 3 and "apply_diff" in spec.tools


def test_strip_mutating_is_idempotent():
    spec = _spec(tools=["get_file_content", "apply_diff"],
                 instructions=["Use apply_diff().", "Mixed get_file_content apply_diff."])
    once, _ = _strip_mutating([spec], ["get_file_content", "run_shell"])
    twice, ctools = _strip_mutating(once, ["get_file_content", "run_shell"])
    assert twice[0].tools == once[0].tools
    assert twice[0].instructions == once[0].instructions
    assert ctools == ["get_file_content"]


def test_spec_with_no_tool_list_is_not_filtered():
    spec = _spec(tools=None, instructions=["Use apply_diff()."])
    (out,), _ = _strip_mutating([spec], None)
    assert out.instructions == ["Use apply_diff()."]


# ── A1: the roster reflects the stripped surface ─────────────────────────────

def test_roster_from_stripped_specs_does_not_advertise_removed_tools():
    spec = _spec(tools=["get_file_content", "apply_diff", "run_shell"])
    raw = "\n".join(_team_roster_preamble([spec]))
    assert "apply_diff" in raw
    stripped, _ = _strip_mutating([spec], None)
    text = "\n".join(_team_roster_preamble(stripped))
    assert "apply_diff" not in text and "run_shell" not in text
    assert "get_file_content" in text


@pytest.mark.parametrize("fn_name", ["run_task_stream", "run_task_async"])
def test_both_run_functions_rebind_specs_before_the_roster_is_composed(fn_name):
    src = inspect.getsource(getattr(team_mod, fn_name))
    rebind = src.index("agent_specs, _ = _strip_mutating(agent_specs, None)")
    roster = src.index("_team_roster_preamble(agent_specs)")
    assert rebind < roster
    # non-read_only runs are unchanged: the rebind is gated on read_only
    assert "if read_only and agent_specs:" in src[rebind - 80:rebind]


@pytest.mark.asyncio
async def test_run_task_stream_composes_the_roster_from_stripped_specs(monkeypatch):
    seen = {}

    async def _none(*a, **k):
        return None

    async def _empty(*a, **k):
        return ""

    def _capture(specs):
        seen["specs"] = specs
        raise RuntimeError("stop after roster")

    monkeypatch.setattr(team_mod, "load_failure_context", _empty)
    monkeypatch.setattr(team_mod, "load_success_context", _empty)
    monkeypatch.setattr(team_mod, "load_project_memory_context", _empty)
    monkeypatch.setattr(team_mod, "_fetch_skill_catalog", _none)
    monkeypatch.setattr(team_mod, "_team_roster_preamble", _capture)

    original = _spec(tools=["get_file_content", "apply_diff"])
    with pytest.raises(RuntimeError, match="stop after roster"):
        async for _ in team_mod.run_task_stream(
                "t", agent_specs=[original], mcp_url="http://x", read_only=True):
            pass
    assert seen["specs"][0].tools == ["get_file_content"]
    assert original.tools == ["get_file_content", "apply_diff"]

    seen.clear()
    with pytest.raises(RuntimeError, match="stop after roster"):
        async for _ in team_mod.run_task_stream(
                "t", agent_specs=[original], mcp_url="http://x", read_only=False):
            pass
    assert seen["specs"][0].tools == ["get_file_content", "apply_diff"]


# ── A2 (b): Coordinator get_file_content ownership lines ─────────────────────

def _joined(lines):
    return "\n".join(lines)


def test_every_rewrite_key_is_a_real_line_of_the_coordinator_instructions():
    missing = [k for k in _COORDINATOR_FILE_READ_REWRITES if k not in _COORDINATOR_INSTRUCTIONS]
    assert missing == []


def test_owner_of_get_file_content_gets_identical_text():
    out = _coordinator_instructions_for_surface(
        list(_COORDINATOR_INSTRUCTIONS), {"get_file_content", "request_clarification"})
    assert out == list(_COORDINATOR_INSTRUCTIONS)


def test_disarmed_coordinator_is_no_longer_told_to_call_get_file_content_itself():
    out = _coordinator_instructions_for_surface(
        list(_COORDINATOR_INSTRUCTIONS), {"request_clarification", "update_session_state"})
    assert len(out) == len(_COORDINATOR_INSTRUCTIONS)
    text = _joined(out)
    for phrase in ("that tool IS still yours", "yourself with get_file_content",
                   "get_file_content() yourself", "get_file_content(path) yourself",
                   "IS still yours to call directly",
                   "(get_file_content, apply_diff, write_file, etc.)"):
        assert phrase not in text, phrase


def test_apply_diff_instructions_are_left_untouched():
    out = _coordinator_instructions_for_surface(list(_COORDINATOR_INSTRUCTIONS), set())
    keep = [ln for ln in _COORDINATOR_INSTRUCTIONS if "apply_diff" in ln
            and ln not in _COORDINATOR_FILE_READ_REWRITES]
    assert keep and all(ln in out for ln in keep)


def test_unrelated_lines_keep_their_position():
    out = _coordinator_instructions_for_surface(list(_COORDINATOR_INSTRUCTIONS), set())
    for i, (a, b) in enumerate(zip(_COORDINATOR_INSTRUCTIONS, out)):
        if a not in _COORDINATOR_FILE_READ_REWRITES:
            assert a == b, i


# ── through the real _build_team ─────────────────────────────────────────────

def _build(monkeypatch, coordinator_tools, instructions=None):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")
    return team_mod._build_team(
        agent_specs=None, coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=coordinator_tools, mode="coordinate", mcp_list=[],
        instructions=list(instructions if instructions is not None else _COORDINATOR_INSTRUCTIONS),
        read_only=True)


def test_build_team_disarmed_coordinator_gets_delegate_the_read_text(monkeypatch):
    t = _build(monkeypatch, [])       # engineering's shape: coordinator_tools: []
    text = _joined(t.instructions)
    assert "that tool IS still yours" not in text
    assert "you do not hold get_file_content" in text


def test_build_team_coordinator_that_owns_get_file_content_is_unchanged(monkeypatch):
    class _T:                          # stands in for an MCP-derived tool on its surface
        name = "get_file_content"

    monkeypatch.setattr(team_mod, "_scope_coordinator_tools", lambda *a, **k: [_T()])
    t = _build(monkeypatch, None)
    assert list(t.instructions) == list(_COORDINATOR_INSTRUCTIONS)
