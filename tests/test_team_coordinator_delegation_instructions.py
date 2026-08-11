"""Regression test: the coordinator's own instructions must accurately describe its
ACTUAL tool surface -- discovery tools are structurally removed (see
test_team_tool_interception_hook.py's _COORDINATOR_DISCOVERY_TOOLS tests for the
enforcement itself), not just discouraged by prose.

Confirmed live 2026-08-11, in two stages:

Stage 1 (root cause): a read-only Phase-1 groundedness retest (find the real party
edit panel component in EkamApp's frontend, describe which files change) produced ZERO
delegate_task_to_member calls across the whole run -- the coordinator did all 35 tool
calls itself (find_files, search_files, get_file_content, list_directory,
list_directory_tree, search_knowledge_graph), guessed a wrong Next.js app-router path,
then read into signoz/ (a vendored, unrelated tool) and the mobile app tree before
finding the real file.

Stage 2 (the fix that didn't work): added a PROSE-ONLY instruction telling the
coordinator to prefer delegate_task_to_member('ContextRouter', ...) for "find this
unfamiliar file" tasks, while leaving find_files/search_files/etc. in its own direct
tool list. A second live retest with that instruction live showed the IDENTICAL
direct-tool-call pattern -- 0 delegate_task_to_member calls, same glob-guessing.
This repo's own established lesson (_strip_mutating's docstring, 2026-07-31) already
covered this exact failure shape: "Instructions shape what a model says; only the
tool surface constrains what it does."

The actual fix (this file tests the resulting instruction text; the tool-surface
removal itself is _COORDINATOR_DISCOVERY_TOOLS in swarm/team.py, tested in
test_team_tool_interception_hook.py): find_files, search_files, list_directory,
list_directory_tree, and search_knowledge_graph are removed from the coordinator's
own tool list entirely. The instructions were rewritten to match reality -- explaining
WHY those tools are missing and routing every "discover the structure" step through
delegate_task_to_member('ContextRouter', ...) instead of telling the coordinator to
call tools it no longer has.
"""
from swarm import team


def _joined():
    return "\n".join(team._COORDINATOR_INSTRUCTIONS)


def test_locating_unfamiliar_files_section_exists():
    text = _joined()
    assert "Locating unfamiliar files" in text
    assert "you do not have find_files/search_files/list_directory" in text


def test_section_names_every_removed_discovery_tool():
    text = _joined()
    for tool in team._COORDINATOR_DISCOVERY_TOOLS:
        assert tool in text, f"{tool} not mentioned in coordinator instructions"


def test_section_directs_to_delegate_task_to_member_contextrouter():
    text = _joined()
    assert "delegate_task_to_member('ContextRouter'" in text


def test_get_file_content_is_explicitly_kept_as_still_directly_callable():
    # Must not regress into "always delegate everything" -- reading a path you
    # already have should still be a direct get_file_content() call, no wasted
    # round-trip through delegation.
    text = _joined()
    assert "that tool IS still" in text


def test_tool_restrictions_section_precedes_the_discovery_tools_explanation():
    text = _joined()
    idx_tool_restrictions = text.index("Tool restrictions")
    idx_discovery_section = text.index("Locating unfamiliar files")
    idx_scan_first = text.index("Scan-first rule")
    assert idx_tool_restrictions < idx_discovery_section < idx_scan_first


def test_scan_first_rule_reflects_the_removed_tools_not_the_old_direct_steps():
    text = _joined()
    scan_first_section = text[text.index("Scan-first rule"):]
    assert "You do NOT have find_files/search_files/list_directory" in scan_first_section
    # The old direct-call steps ("find_files('**/*') -- get the full file tree") must
    # be gone from this section -- regression guard against silently reverting to the
    # prose-only version.
    assert "get the full file tree" not in scan_first_section


def test_query_routing_tables_delegate_instead_of_calling_tools_directly():
    text = _joined()
    assert "delegate_task_to_member('ContextRouter', 'call list_directory_tree()" in text
    assert "delegate_task_to_member('ContextRouter', 'search_files for" in text
    assert "delegate_task_to_member('ContextRouter', 'find_files for <extension>" in text


def test_clarification_section_no_longer_tells_coordinator_to_research_with_removed_tools():
    text = _joined()
    assert "delegate to ContextRouter to research it, don't ask" in text
