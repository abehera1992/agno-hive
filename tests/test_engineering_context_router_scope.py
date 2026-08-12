"""Regression test: ContextRouter (teams/engineering.yaml) must not have
get_file_content on its own tool surface.

Confirmed live 2026-08-11/12: the coordinator correctly delegated a structure-only
request -- delegate_task_to_member('ContextRouter', 'list_directory_tree() and
return the full directory structure of the frontend codebase') -- exactly matching
ContextRouter's own documented tier table. Yet 126 get_file_content calls followed,
all attributed to agent_name: 'ContextRouter' (confirmed via raw transcript, not
inferred), sweeping unrelated files (admin/users, admin/email, admin/gst,
business/team, customer/settings/profile, ...) that nobody asked about, never
reaching the actual target area the real task was scoped to.

Root cause: none of ContextRouter's seven instruction tiers (overview/structure,
"how does X work", specific file/symbol, past task/lesson, thematic/cross-module,
URL/GitHub link, external tool/library) ever call for get_file_content -- yet it sat
on the tool list anyway. ContextRouter's own description is explicit: "Pick the
fastest retrieval path and return raw results -- never interpret or answer
yourself." Reading and weighing file content is Researcher's job (its own
description: "Read real files and ground every claim in file content"), not
ContextRouter's. This is the same tool-surface-over-instruction lesson this
codebase has on record elsewhere (_COORDINATOR_DISCOVERY_TOOLS, _strip_mutating)
applied in the reverse direction: an unused-by-instruction tool left reachable, and
the model reached for it anyway.

The fix removes get_file_content from ContextRouter's tools, and with it the one
instruction line that depended on it (a SESSION CONTEXT hive.md pre-fetch,
copy-pasted from Researcher's identical line but inconsistent with ContextRouter's
narrower "route, don't build your own understanding" role).
"""
from pathlib import Path

import yaml

_ENGINEERING_YAML = Path(__file__).parent.parent / "teams" / "engineering.yaml"


def _context_router_spec() -> dict:
    data = yaml.safe_load(_ENGINEERING_YAML.read_text(encoding="utf-8"))
    return next(a for a in data["agents"] if a["name"] == "ContextRouter")


def test_context_router_tools_do_not_include_get_file_content():
    spec = _context_router_spec()
    assert "get_file_content" not in spec["tools"]


def test_context_router_keeps_the_tools_its_own_instruction_tiers_actually_use():
    spec = _context_router_spec()
    for tool in (
        "list_directory_tree", "find_files", "search_files", "list_directory",
        "search_knowledge_graph", "lightrag_query", "web_search", "web_fetch",
        "load_skill",
    ):
        assert tool in spec["tools"], f"{tool} should still be available to ContextRouter"


def test_context_router_instructions_never_reference_get_file_content():
    spec = _context_router_spec()
    text = "\n".join(spec["instructions"])
    assert "get_file_content" not in text


def test_context_router_tool_call_limit_instruction_still_present():
    """The fix removes a tool, not the existing 'stay narrow' guidance -- confirm the
    tool-call-budget instruction survived the edit unchanged."""
    spec = _context_router_spec()
    text = "\n".join(spec["instructions"])
    assert "Tool call limit: 1 for specific lookups" in text
