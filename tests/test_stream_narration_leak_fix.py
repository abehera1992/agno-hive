"""Tests for _stream_team_run's narration-leak fix (swarm/team.py, 2026-08-15).

Live incident (T1e/T2e/T3e, engineering-team groundedness retest): when agno's
final_run_output.content comes back empty (confirmed to happen on normal,
successfully-completed multi-agent coordination runs, not just mid-stream
cancellations), the fallback used to be the FULL accumulated transcript of every
text chunk streamed across the whole run, from every agent -- including each
agent's own mid-process narration emitted BEFORE a tool call ("I'll investigate
the pattern...", "I apologize for the error, let me correct that...", "I'll check
the Notion page..."). That narration ended up prefixed onto the real final answer
in every user-facing response that hit this fallback.

The fix: track the index into full_content where the CURRENT text segment started
(reset on every tool event, start or end, from any agent), and prefer
"".join(full_content[last_segment_start:]) over the full accumulated transcript --
this isolates just the text generated SINCE the last tool call, which is reliably
the coordinator's final synthesis (no further tool calls happen once synthesis
begins). Still falls back to the full transcript if that last segment is empty,
so this function never returns truly nothing.

The identical fix was applied at the same time to run_task_async's and
run_task_stream's own separate copies of this same accumulation pattern (not
independently unit-tested here -- both are large, DB/tracing-dependent functions;
_stream_team_run is the cleanly-isolated shared logic, covered directly).
"""
import asyncio
from types import SimpleNamespace

import pytest

from swarm.team import _stream_team_run


def _content_event(text: str):
    return SimpleNamespace(event="TeamRunContent", content=text, reasoning_content="")


def _tool_start_event(name: str = "get_file_content", agent_name: str = ""):
    tool = SimpleNamespace(tool_name=name, tool_args={})
    return SimpleNamespace(event="ToolCallStarted", tool=tool, agent_name=agent_name)


def _tool_end_event(name: str = "get_file_content", agent_name: str = "", result: str = "ok"):
    tool = SimpleNamespace(tool_name=name, result=result)
    return SimpleNamespace(event="ToolCallCompleted", tool=tool, agent_name=agent_name)


def _final_output_empty_content():
    """No .event attribute -- matches _stream_team_run's own final-yield check.
    content="" is the live-confirmed failure mode: agno completed the run
    successfully but never populated the Team-level .content field."""
    return SimpleNamespace(content="", messages=[])


class _FakeTeam:
    def __init__(self, events):
        self._events = events

    async def arun(self, prompt, stream=True, yield_run_output=True):
        for event in self._events:
            await asyncio.sleep(0)
            yield event


@pytest.mark.asyncio
async def test_narration_before_a_tool_call_is_excluded_from_the_fallback():
    """The exact live-incident shape: narration, then a tool call, then the real
    final answer. When final_run_output.content is empty, the fallback must be
    ONLY the text after the last tool call."""
    team = _FakeTeam([
        _content_event("I'll investigate the pattern by reading the docs.\n\n"),
        _tool_start_event(),
        _tool_end_event(),
        _content_event("Based on the documentation, the pattern is X."),
        _final_output_empty_content(),
    ])

    content, _ = await _stream_team_run(team, "prompt")

    assert content == "Based on the documentation, the pattern is X."
    assert "I'll investigate" not in content


@pytest.mark.asyncio
async def test_multiple_narration_tool_cycles_only_the_last_segment_survives():
    """T2e's real shape: narration -> tool -> apology/narration -> tool -> more
    narration -> tool -> the real answer. Only the segment after the LAST tool
    event must survive."""
    team = _FakeTeam([
        _content_event("I'll help you discover the tables.\n\n"),
        _tool_start_event(name="delegate_task_to_member"),
        _tool_end_event(name="delegate_task_to_member"),
        _content_event("I apologize for the error. Let me correct that.\n\n"),
        _tool_start_event(name="find_files"),
        _tool_end_event(name="find_files"),
        _content_event("Now that I have the files, let me read the schema.\n\n"),
        _tool_start_event(name="get_file_content"),
        _tool_end_event(name="get_file_content"),
        _content_event("Based on the actual code, here is the schema: party_id, name."),
        _final_output_empty_content(),
    ])

    content, _ = await _stream_team_run(team, "prompt")

    assert content == "Based on the actual code, here is the schema: party_id, name."
    assert "I apologize" not in content
    assert "I'll help you" not in content
    assert "Now that I have" not in content


@pytest.mark.asyncio
async def test_real_final_run_output_content_is_still_always_preferred():
    """When agno DOES populate final_run_output.content, that must win outright --
    the segment-isolation fallback only ever applies when it's empty."""
    team = _FakeTeam([
        _content_event("I'll investigate.\n\n"),
        _tool_start_event(),
        _tool_end_event(),
        _content_event("some intermediate streamed text"),
        _final_output_empty_content_with("The real clean final answer."),
    ])

    content, _ = await _stream_team_run(team, "prompt")

    assert content == "The real clean final answer."


def _final_output_empty_content_with(text: str):
    return SimpleNamespace(content=text, messages=[])


@pytest.mark.asyncio
async def test_no_tool_calls_at_all_falls_back_to_the_whole_accumulated_text():
    """A run with zero tool calls has no segment boundary to isolate -- the whole
    accumulated text IS the one and only segment, so it must not be dropped."""
    team = _FakeTeam([
        _content_event("The answer is simply X, no tools were needed."),
        _final_output_empty_content(),
    ])

    content, _ = await _stream_team_run(team, "prompt")

    assert content == "The answer is simply X, no tools were needed."


@pytest.mark.asyncio
async def test_trailing_tool_call_with_no_text_after_it_falls_back_to_full_transcript():
    """Edge case explicitly called out in the docstring: if the LAST event is a
    tool call with no trailing text, the last segment is empty -- this must never
    return an empty string, it must fall back to the full accumulated transcript
    rather than return nothing."""
    team = _FakeTeam([
        _content_event("Here is the answer: X."),
        _tool_start_event(name="update_session_state"),
        _tool_end_event(name="update_session_state"),
        _final_output_empty_content(),
    ])

    content, _ = await _stream_team_run(team, "prompt")

    assert content == "Here is the answer: X."


@pytest.mark.asyncio
async def test_segment_isolation_works_regardless_of_which_agent_emitted_the_text():
    """A delegated member's own narration (agent_name != coordinator) must be
    excluded from the fallback exactly the same as the coordinator's own --
    T2e's leaked "I apologize..." narration came from a delegated member, not
    the coordinator itself."""
    team = _FakeTeam([
        SimpleNamespace(event="RunContent", content="Member narration before its own tool call.\n\n", reasoning_content=""),
        _tool_start_event(name="find_files", agent_name="Researcher"),
        _tool_end_event(name="find_files", agent_name="Researcher"),
        SimpleNamespace(event="TeamRunContent", content="Coordinator's final synthesized answer.", reasoning_content=""),
        _final_output_empty_content(),
    ])

    content, _ = await _stream_team_run(team, "prompt")

    assert content == "Coordinator's final synthesized answer."
    assert "Member narration" not in content
