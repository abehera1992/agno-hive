"""Tests for _log_unclassified_stream_event (swarm/team.py) -- extracted 2026-08-14
from two near-identical inline copies (run_task_stream's _stream_team_run and
run_task_async's own streaming block) so a diagnostic addition lands in one place,
plus the model_provider_data addition itself: the one field on these events most
likely to carry a raw finish_reason/provider hint that .content and
.reasoning_content alone don't, added after a live incident where the coordinator
emitted nothing but empty TeamRunContent events for 5 minutes straight and neither
of those two fields explained why.
"""
from types import SimpleNamespace

from swarm.team import _log_unclassified_stream_event


def _event(event_type="TeamRunContent", content="", reasoning_content="", **extra):
    return SimpleNamespace(event=event_type, content=content, reasoning_content=reasoning_content, **extra)


def test_first_occurrence_is_logged(capsys):
    _log_unclassified_stream_event("team", _event(), {})

    out = capsys.readouterr().out
    assert "unrecognized stream event #1 of type 'TeamRunContent'" in out


def test_second_occurrence_is_logged(capsys):
    counts = {"TeamRunContent": 1}
    _log_unclassified_stream_event("team", _event(), counts)

    out = capsys.readouterr().out
    assert "#2" in out


def test_third_through_nineteenth_occurrences_are_not_logged(capsys):
    counts = {"TeamRunContent": 2}
    for _ in range(17):  # brings count from 2 up to 19
        _log_unclassified_stream_event("team", _event(), counts)

    out = capsys.readouterr().out
    assert out == ""
    assert counts["TeamRunContent"] == 19


def test_twentieth_occurrence_is_logged(capsys):
    counts = {"TeamRunContent": 19}
    _log_unclassified_stream_event("team", _event(), counts)

    out = capsys.readouterr().out
    assert "#20" in out


def test_model_provider_data_is_included_in_the_log_line(capsys):
    _log_unclassified_stream_event(
        "team", _event(model_provider_data={"finish_reason": "tool_calls", "id": "chatcmpl-abc"}), {}
    )

    out = capsys.readouterr().out
    assert "model_provider_data=" in out
    assert "finish_reason" in out
    assert "tool_calls" in out


def test_missing_model_provider_data_attr_does_not_crash(capsys):
    """A fake/older event object with no .model_provider_data at all must still log
    cleanly, same fail-safe posture as the pre-existing content/reasoning_content
    getattr defaults."""
    event = SimpleNamespace(event="SomeOtherEventType", content="", reasoning_content="")

    _log_unclassified_stream_event("team", event, {})

    out = capsys.readouterr().out
    assert "no .model_provider_data attr" in out


def test_counts_are_tracked_independently_per_event_type(capsys):
    counts: dict[str, int] = {}
    _log_unclassified_stream_event("team", _event(event_type="TypeA"), counts)
    _log_unclassified_stream_event("team", _event(event_type="TypeB"), counts)

    assert counts == {"TypeA": 1, "TypeB": 1}
    out = capsys.readouterr().out
    assert "TypeA" in out
    assert "TypeB" in out


def test_log_label_is_used_in_the_bracket_prefix(capsys):
    _log_unclassified_stream_event("chain-retry", _event(), {})

    out = capsys.readouterr().out
    assert "[chain-retry]" in out


def test_content_and_reasoning_content_values_are_still_shown(capsys):
    _log_unclassified_stream_event("team", _event(content="", reasoning_content=""), {})

    out = capsys.readouterr().out
    assert "content=''" in out
    assert "reasoning_content=''" in out
