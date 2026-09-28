"""Phase Z19 (2026-09-28) -- T7 regression: the task text can name a specific
tool the Coordinator itself does not hold (its own surface is
`coordinator_tools`, e.g. just ["project_map"] for the engineering team).

Root cause, proven live: told "Call list_directory on <path>...", the
Coordinator never delegated at all -- it called its own project_map 60 times
(its entire tool budget) on a rotating handful of targets, never got anything
resembling a directory listing, and only once forced to stop fabricated "the
directory does not exist" with zero real reads (the run's own phase0 summary:
delegations=0, read_chars_total=0). Two existing guards correctly caught the
fabrication and forced an honest "NOT VERIFIED" disclosure instead of shipping
it -- but the task still went unanswered after ~425s and the Coordinator's
full 60-call budget.

_tool_naming_hint_lines (swarm/team.py) closes this by injecting one
instruction block, only when the task names a known member-only tool the
Coordinator's own surface does not include, telling it to delegate instead of
substituting a different tool it does hold. Deliberately narrow and silent
whenever coordinator_tools is None (unrestricted) -- same "not ours to judge"
rule _make_capability_routing_gate_hook already applies to the analogous
delegation-side mistake.
"""
from swarm.team import _tool_naming_hint_lines, _NAMEABLE_MEMBER_ONLY_TOOLS


# Test A shape -- exact T7 reproduction: task names list_directory, Coordinator
# only holds project_map.
def test_named_tool_not_held_produces_a_delegate_hint():
    task = ("Call list_directory on API/business-service/router/ and report "
            "the EXACT number of .py files the tool returns, then list them "
            "all. Use the tool's own output, not recall.")

    lines = _tool_naming_hint_lines(task, ["project_map"])

    joined = "\n".join(lines)
    assert "list_directory" in joined
    assert "you do not hold" in joined
    assert "delegate" in joined.lower()
    assert "project_map" in joined  # named as the thing NOT to substitute with


# Test B shape -- normal neighboring task (names no tool at all): no hint, no
# regression to the ordinary instruction composition.
def test_task_naming_no_tool_produces_no_hint():
    task = "How does seller verification work in this codebase?"

    lines = _tool_naming_hint_lines(task, ["project_map"])

    assert lines == []


def test_task_naming_a_tool_the_coordinator_already_holds_produces_no_hint():
    task = "Call project_map on the inventory service and summarise it."

    lines = _tool_naming_hint_lines(task, ["project_map"])

    assert lines == []


# Unrestricted coordinator (coordinator_tools=None): "does not hold" cannot be
# determined, so this must stay completely silent -- same rule the delegation-
# side gate already follows.
def test_unrestricted_coordinator_never_fires():
    task = "Call list_directory on API/business-service/router/ and list every file."

    lines = _tool_naming_hint_lines(task, None)

    assert lines == []


def test_empty_coordinator_tools_list_still_fires():
    """[] (a real, concrete allowlist that grants nothing) is not None -- the
    Coordinator demonstrably holds nothing, so the hint must still fire."""
    task = "Call db_query('select 1') and report the result."

    lines = _tool_naming_hint_lines(task, [])

    assert any("db_query" in l for l in lines)


# Test C shape -- a closely neighboring wording variant must behave the same
# way, confirming this is not sensitive to exact phrasing.
def test_neighboring_wording_variant_still_fires():
    task = "Use the find_files tool to locate every .tsx file under Client/."

    lines = _tool_naming_hint_lines(task, ["project_map"])

    assert any("find_files" in l for l in lines)


def test_partial_word_match_does_not_false_positive():
    """A substring match inside an unrelated longer identifier must not fire --
    e.g. 'db_query_helper' mentioning 'db_query' as a strict prefix only."""
    task = "Read the db_query_helper module and summarise what it does."

    lines = _tool_naming_hint_lines(task, ["project_map"])

    assert lines == []


def test_every_nameable_tool_is_a_real_registered_tool_name_shape():
    """Sanity: the hand-maintained list stays non-empty and lowercase/snake_case,
    matching real MCP tool naming -- catches an accidental typo landing silently
    unused."""
    assert len(_NAMEABLE_MEMBER_ONLY_TOOLS) >= 5
    for name in _NAMEABLE_MEMBER_ONLY_TOOLS:
        assert name == name.lower()
        assert " " not in name


def test_only_the_first_matching_tool_produces_one_hint_block():
    """Two named tools in the same task -> one hint, not a growing pile -- keeps
    the injected block small and readable rather than stacking."""
    task = "Call list_directory then get_file_content on each file found."

    lines = _tool_naming_hint_lines(task, ["project_map"])

    header_lines = [l for l in lines if l.startswith("── This task names")]
    assert len(header_lines) == 1
