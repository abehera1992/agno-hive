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
    # 2026-08-15: the code example must use the real agno member_id ('context-router'
    # -- a dash inserted at the camelCase boundary, then lowercased, confirmed by
    # reading agno.utils.string.url_safe_string directly), not the display name
    # ('ContextRouter'). Live-confirmed as a real bug: a coordinator that typed the
    # display name verbatim (matching this exact string this test used to assert)
    # failed to delegate and gave up on delegation entirely for the rest of the run.
    assert "delegate_task_to_member('context-router'" in text


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
    # 2026-08-15: 'context-router', not the display name -- see the sibling test
    # above for the real-bug rationale.
    assert "delegate_task_to_member('context-router', 'call list_directory_tree()" in text
    assert "delegate_task_to_member('context-router', 'search_files for" in text
    assert "delegate_task_to_member('context-router', 'find_files for <extension>" in text


def test_clarification_section_no_longer_tells_coordinator_to_research_with_removed_tools():
    text = _joined()
    assert "delegate to ContextRouter to research it, don't ask" in text


# ── Turn-classification only applies with prior context (2026-08-18 live incident) ──
#
# A first-turn, zero-prior-context task got misclassified as REJECT/CANCEL and led
# to a real, unrequested notion_trash_page call against unrelated data (see
# test_niche_team_groundedness_fixes.py's notion_trash_page tests for the
# structural tool-surface half of this fix).

def test_turn_classification_states_it_requires_prior_context():
    text = _joined()
    start = text.index("Conversational turn detection")
    section = text[start:text.index("ACTION APPROVAL —", start)]
    assert "only" in section and "prior turn in THIS session" in section
    assert "first message of a fresh session" in section
    assert "Treat it as a plain TASK" in section


def test_reject_cancel_forbids_any_tool_call_not_just_file_write_tools():
    text = _joined()
    start = text.index("REJECT / CANCEL")
    section = text[start:text.index("CONVERSATIONAL —", start)]
    assert "Do NOT call ANY tool of any kind" in section
    assert "notion_trash_page" in section


def test_outcome_honesty_rule_exists():
    text = _joined()
    assert "OUTCOME HONESTY" in text
    assert "never say 'no changes applied'" in text.lower() or "no changes applied' or 'nothing" in text
