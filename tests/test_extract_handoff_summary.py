"""Tests for _extract_handoff_summary (swarm/team.py) -- the chain-boundary digest
saved between chained agno_run calls. No prior tests existed for this function, so
this file locks down BOTH the original behavior (task/status/file-path/bullet
extraction from the final answer text) and the 2026-08-14 widening (session/context-
overflow pipeline, part #1): real excerpts from the run's own read-tool results,
when final_run_output is passed, instead of only ever regexing the rendered final
answer.

Root incident this widening closes: a chained call working from the original,
final-answer-only digest had no memory of the actual file content or schema field
names a prior turn had already read -- only a short list of file PATHS and up to 5
truncated bullets survived the handoff. The next call had to re-search and re-read
everything from scratch before it could even start the actual requested work, then
stalled before producing an answer.
"""
from types import SimpleNamespace

from swarm.team import _extract_handoff_summary


def _tool_msg(name: str, content: str):
    """Minimal fake matching the getattr-based access _extract_handoff_summary (and
    _count_read_calls/_count_successful_write_calls before it) already use -- role,
    tool_name, content. Deliberately not a real agno Message, matching this
    codebase's own stated preference for lightweight fakes over constructing real
    framework objects in tests."""
    return SimpleNamespace(role="tool", tool_name=name, content=content)


def _assistant_msg():
    return SimpleNamespace(role="assistant", tool_calls=[], content="")


# ── original behavior, backward compat (final_run_output=None or omitted) ───────

def test_backward_compat_no_final_run_output_still_works():
    result = _extract_handoff_summary("Compare X against Y", "Some answer text.")

    assert "Chain handoff" in result
    assert "Task: Compare X against Y" in result


def test_task_text_is_truncated_to_200_chars():
    long_task = "x" * 500
    result = _extract_handoff_summary(long_task, "answer")

    assert "x" * 200 in result
    assert "x" * 201 not in result


def test_status_is_complete_when_no_review_pending_present():
    result = _extract_handoff_summary("task", "The gap is X. No files were written.")

    assert "Status: COMPLETE" in result


def test_status_is_pending_review_when_review_pending_present():
    result = _extract_handoff_summary("task", "review_pending: src/foo.tsx\nStaged for review.")

    assert "Status: PENDING_REVIEW" in result


def test_file_paths_are_extracted_from_backticks():
    content = "The gap is in `src/app/parties/page.tsx` and `API/models.py`."
    result = _extract_handoff_summary("task", content)

    assert "src/app/parties/page.tsx" in result
    assert "API/models.py" in result


def test_key_outcomes_bullets_are_extracted():
    content = "Summary:\n- This is a real finding worth keeping\n- short\n- Another substantial finding here"
    result = _extract_handoff_summary("task", content)

    assert "This is a real finding worth keeping" in result
    assert "Another substantial finding here" in result


def test_short_bullets_are_filtered_out():
    content = "- tiny\n- ok\n- This one is long enough to survive the 15-char filter"
    result = _extract_handoff_summary("task", content)

    assert "This one is long enough to survive the 15-char filter" in result
    assert "\n  - tiny\n" not in result


def test_no_tool_excerpts_section_when_final_run_output_omitted():
    result = _extract_handoff_summary("task", "answer text")

    assert "Recent tool results" not in result


# ── widened behavior: real excerpts from read-tool results ──────────────────────

def test_read_tool_result_content_is_included_when_final_run_output_passed():
    messages = [
        _assistant_msg(),
        _tool_msg("get_file_content", "class Party(Base):\n    party_id = Column(UUID, primary_key=True)\n"),
    ]
    final_run_output = SimpleNamespace(messages=messages)

    result = _extract_handoff_summary("task", "answer", final_run_output)

    assert "Recent tool results" in result
    assert "class Party(Base):" in result
    assert "party_id = Column" in result
    assert "get_file_content" in result


def test_non_read_tool_results_are_excluded():
    """A write tool's result (apply_diff/write_file) is not evidence to carry
    forward the same way a read result is -- excluded same as _count_read_calls
    already excludes it from its own tally via the same _READ_TOOLS set."""
    messages = [_tool_msg("apply_diff", "applied: src/foo.tsx")]
    final_run_output = SimpleNamespace(messages=messages)

    result = _extract_handoff_summary("task", "answer", final_run_output)

    assert "applied: src/foo.tsx" not in result


def test_empty_tool_result_content_is_skipped():
    messages = [_tool_msg("get_file_content", ""), _tool_msg("search_files", "No matches for: xyz")]
    final_run_output = SimpleNamespace(messages=messages)

    result = _extract_handoff_summary("task", "answer", final_run_output)

    assert "No matches for: xyz" in result  # non-empty result still included
    # empty one contributes nothing to break on -- absence of a crash is the real assertion


def test_excerpts_are_capped_to_the_most_recent_five():
    messages = [_tool_msg("get_file_content", f"file number {i} content") for i in range(8)]
    final_run_output = SimpleNamespace(messages=messages)

    result = _extract_handoff_summary("task", "answer", final_run_output)

    assert "file number 7 content" in result  # most recent kept
    assert "file number 3 content" in result
    assert "file number 0 content" not in result  # oldest, past the cap of 5, dropped
    assert "file number 1 content" not in result
    assert "file number 2 content" not in result


def test_each_excerpt_is_truncated_to_800_chars():
    huge = "y" * 5000
    messages = [_tool_msg("get_file_content", huge)]
    final_run_output = SimpleNamespace(messages=messages)

    result = _extract_handoff_summary("task", "answer", final_run_output)

    assert ("y" * 800) in result
    assert ("y" * 801) not in result


def test_final_run_output_with_no_messages_attribute_does_not_crash():
    final_run_output = SimpleNamespace()  # no .messages at all

    result = _extract_handoff_summary("task", "answer", final_run_output)

    assert "Chain handoff" in result
    assert "Recent tool results" not in result


def test_final_run_output_with_none_messages_does_not_crash():
    final_run_output = SimpleNamespace(messages=None)

    result = _extract_handoff_summary("task", "answer", final_run_output)

    assert "Chain handoff" in result
