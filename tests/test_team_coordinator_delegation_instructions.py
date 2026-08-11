"""Regression test: the coordinator must be steered to delegate FINDING an unfamiliar
file/component to ContextRouter, instead of guessing paths itself.

Confirmed live 2026-08-11: a read-only Phase-1 groundedness retest (find the real party
edit panel component in EkamApp's frontend, describe which files change) produced ZERO
delegate_task_to_member calls across the whole run -- the coordinator did all 35 tool
calls itself (find_files, search_files, get_file_content, list_directory,
list_directory_tree, search_knowledge_graph), guessed a wrong Next.js app-router path,
then read into signoz/ (a vendored, unrelated tool) and the mobile app tree before
finding the real file. Root cause: the coordinator's own top-level instructions
explicitly told it to prefer calling MCP tools directly and to delegate "only for
complex multi-file research" -- a judgment call the model didn't make for this task,
even though ContextRouter/Researcher (teams/engineering.yaml) carry much stricter
grounding discipline (SCAN-FIRST, COVERAGE, an explicit HARD RULE against fabricating
paths) than the coordinator's own leaner instruction set does.

This locks in a concrete, mandatory trigger: FINDING a file/component named only by
feature or description (not an already-known exact path) must be delegated to
ContextRouter first, before the coordinator calls find_files/search_files/
get_file_content itself.
"""
from swarm import team


def _joined():
    return "\n".join(team._COORDINATOR_INSTRUCTIONS)


def test_mandatory_delegation_section_exists():
    text = _joined()
    assert "MANDATORY delegation" in text
    assert "Locating unfamiliar files" in text


def test_mandatory_delegation_names_the_concrete_trigger():
    text = _joined()
    assert "FINDING a file, page, or component" in text
    assert "delegate that lookup to" in text
    assert "ContextRouter FIRST via delegate_task_to_member" in text


def test_mandatory_delegation_carves_out_the_known_path_exception():
    # Must not regress into "always delegate everything" -- a task whose target path
    # is already exact and known should still be handled directly, matching the
    # existing "call MCP tools DIRECTLY" behavior for that case.
    text = _joined()
    assert "already exact and known" in text


def test_tool_restrictions_section_cross_references_the_new_rule():
    text = _joined()
    idx_tool_restrictions = text.index("Tool restrictions")
    idx_mandatory = text.index("MANDATORY delegation")
    idx_scan_first = text.index("Scan-first rule")
    # Ordering: Tool restrictions -> mandatory delegation rule -> Scan-first rule,
    # so the coordinator sees the delegation trigger before the section that used to
    # tell it to run find_files('**/*') itself for exactly this kind of question.
    assert idx_tool_restrictions < idx_mandatory < idx_scan_first
    assert "whenever the task requires FINDING" in text


def test_scan_first_rule_defers_to_mandatory_delegation_for_locating_questions():
    text = _joined()
    scan_first_section = text[text.index("Scan-first rule"):]
    assert "the MANDATORY delegation rule above applies instead" in scan_first_section
