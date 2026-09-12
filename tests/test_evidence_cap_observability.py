"""Phase 14, screw #3 (AGNOHive Reliability Program): evidence-cap
observability. Measurement only -- no behaviour change to what ships in any
answer, guard, or retry. This instruments two existing, already-deployed caps
that Phase 13's forensic trace flagged as real but never actually shown to
cause a specific fabrication:

- team._tool_evidence's _TOOL_EVIDENCE_MAX_ITEMS (12) -- items beyond this
  are dropped from the quotable ledger, but their salient tokens still reach
  team._evidence_tokens (accumulated from every tool result, uncapped), so
  _answer_supported_by_evidence's grounding check is unaffected by this cap.
- the member-volume ceiling (_MEMBER_VOLUME_CEILING, 15,000 chars) -- a
  member's report past this point has its middle elided.

These tests confirm: normal (under-cap) behaviour is unchanged, overflow is
logged with bounded metadata (never the raw giant content), and the two
telemetry stores stay capped themselves.
"""
import swarm.team as team_mod
from swarm.team import (
    _MEMBER_VOLUME_CEILING,
    _TOOL_EVIDENCE_DROPPED_MAX,
    _TOOL_EVIDENCE_MAX_ITEMS,
    _record_stream_artifacts,
)


class _Team:
    pass


def _tool_end_event(name, agent, chars=500, tokens=None):
    return {
        "__tool_event__": "end",
        "name": name,
        "agent_name": agent,
        "result_preview": f"preview of {name}",
        "result_chars": chars,
        "result_tokens": tokens or {f"{name}_token"},
    }


def _member_result_event(agent, content):
    return {"__member_result__": True, "agent_name": agent, "content": content}


# ── tool-evidence cap ────────────────────────────────────────────────────


def test_under_cap_behaviour_is_unchanged():
    team = _Team()
    for i in range(_TOOL_EVIDENCE_MAX_ITEMS - 1):
        _record_stream_artifacts(team, _tool_end_event(f"tool_{i}", "Researcher"))
    assert len(team._tool_evidence) == _TOOL_EVIDENCE_MAX_ITEMS - 1
    assert not getattr(team, "_tool_evidence_dropped", None)
    assert getattr(team, "_tool_evidence_dropped_count", 0) == 0


def test_cap_overflow_is_logged_and_counted(capsys):
    team = _Team()
    for i in range(_TOOL_EVIDENCE_MAX_ITEMS):
        _record_stream_artifacts(team, _tool_end_event(f"tool_{i}", "Researcher"))
    # The 13th call is the first to overflow the 12-item cap.
    _record_stream_artifacts(team, _tool_end_event("get_file_content", "Coder", chars=9_999))

    assert len(team._tool_evidence) == _TOOL_EVIDENCE_MAX_ITEMS  # retained list unaffected
    assert team._tool_evidence_dropped_count == 1
    assert len(team._tool_evidence_dropped) == 1
    dropped = team._tool_evidence_dropped[0]
    assert dropped["name"] == "get_file_content"
    assert dropped["agent"] == "Coder"
    assert dropped["chars"] == 9_999

    out = capsys.readouterr().out
    assert "tool evidence cap" in out
    assert "dropping get_file_content" in out
    assert "1 dropped so far" in out


def test_dropped_item_metadata_is_bounded_no_giant_preview_leakage(capsys):
    """Requirement: bounded metadata/previews only, never raw giant tool
    output. Phase 18 deliberately added a bounded preview to the dropped-item
    record (same ~200-char stream-event preview already used for the
    retained ledger, truncated defensively) so a later TARGETED selection
    (_dropped_evidence_lines_for_missing) can quote a specific dropped item
    verbatim -- this is not new raw-content exposure, since the preview
    field already existed on the tool event and was simply not carried over
    before. The tokens list remains capped, and the preview itself is capped
    to 200 chars regardless of how large the real result was."""
    team = _Team()
    huge_tokens = {f"identifier_{i}" for i in range(500)}
    for i in range(_TOOL_EVIDENCE_MAX_ITEMS):
        _record_stream_artifacts(team, _tool_end_event(f"tool_{i}", "Researcher"))
    _record_stream_artifacts(
        team, _tool_end_event("get_file_content", "Coder", tokens=huge_tokens))

    dropped = team._tool_evidence_dropped[0]
    assert dropped["preview"] == "preview of get_file_content"
    assert len(dropped["preview"]) <= 200
    assert len(dropped["salient_tokens"]) <= 20

    out = capsys.readouterr().out
    # The LOG LINE itself still only names the tool and counts, never a raw
    # content dump -- the preview lives in the bounded telemetry structure,
    # not in unbounded log output.
    assert "preview of get_file_content" not in out


def test_dropped_evidence_store_itself_stays_capped():
    team = _Team()
    for i in range(_TOOL_EVIDENCE_MAX_ITEMS):
        _record_stream_artifacts(team, _tool_end_event(f"tool_{i}", "Researcher"))
    for i in range(_TOOL_EVIDENCE_DROPPED_MAX + 5):
        _record_stream_artifacts(team, _tool_end_event(f"overflow_{i}", "Coder"))

    assert len(team._tool_evidence_dropped) == _TOOL_EVIDENCE_DROPPED_MAX
    assert team._tool_evidence_dropped_count == _TOOL_EVIDENCE_DROPPED_MAX + 5


def test_evidence_tokens_still_accumulate_past_the_tool_evidence_cap():
    """The load-bearing claim behind this whole screw: a dropped item's
    salient tokens must still reach team._evidence_tokens (uncapped), so
    _answer_supported_by_evidence's grounding check is NOT weakened by the
    12-item cap -- only the ability to QUOTE the item verbatim is lost."""
    team = _Team()
    for i in range(_TOOL_EVIDENCE_MAX_ITEMS):
        _record_stream_artifacts(team, _tool_end_event(f"tool_{i}", "Researcher"))
    _record_stream_artifacts(
        team, _tool_end_event("get_file_content", "Coder",
                               tokens={"a_dropped_identifier"}))
    assert "a_dropped_identifier" in team._evidence_tokens


# ── member-volume ceiling ────────────────────────────────────────────────


def test_normal_under_ceiling_member_result_is_unaffected():
    team = _Team()
    team._member_result_chars = 100  # well under _MEMBER_VOLUME_CEILING
    _record_stream_artifacts(team, _member_result_event("Researcher", "a short report"))
    assert not getattr(team, "_member_volume_overflow", None)
    assert getattr(team, "_member_volume_overflow_count", 0) == 0


def test_overflow_past_ceiling_is_logged_with_bounded_metadata(capsys):
    team = _Team()
    team._member_result_chars = _MEMBER_VOLUME_CEILING  # already at the ceiling
    long_report = ("HEAD_MARKER " + ("filler word " * 400)
                   + "MIDDLE_IDENTIFIER_xyz " + ("more filler " * 400)
                   + "TAIL_MARKER")
    _record_stream_artifacts(team, _member_result_event("Researcher", long_report))

    assert team._member_volume_overflow_count == 1
    entry = team._member_volume_overflow[0]
    assert entry["agent"] == "Researcher"
    assert entry["elided_chars"] > 0
    assert len(entry["salient_tokens_elided"]) <= 20
    # The identifier that was genuinely in the ELIDED middle should be
    # capturable -- proving the telemetry looks at the removed span, not the
    # head/tail that survives in the answer anyway.
    assert "middle_identifier_xyz" in entry["salient_tokens_elided"]

    out = capsys.readouterr().out
    assert "member volume ceiling" in out


def test_short_report_past_ceiling_is_not_elided_and_not_logged_as_overflow():
    """_elide_middle no-ops below its own head+tail+200 floor -- this must not
    be miscounted as an overflow event even though the ceiling was reached."""
    team = _Team()
    team._member_result_chars = _MEMBER_VOLUME_CEILING
    short_report = "x" * 2_001  # > 2,000 (site's own gate) but under elide's floor
    _record_stream_artifacts(team, _member_result_event("Researcher", short_report))
    assert getattr(team, "_member_volume_overflow_count", 0) == 0
