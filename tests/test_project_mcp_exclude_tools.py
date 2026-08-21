"""Regression tests: excluding a tool the server does not have kills the SERVER.

Live incident, 2026-08-21. `memory_search`/`memory_store` were added to
`_PROJECT_MCP_EXCLUDE_TOOLS` on 2026-08-20 — the same day those two tools were
DELETED from EkamApp MCP. Either change alone is fine. Together they took project
MCP from 4 usable tools to 0, because agno fails the entire toolkit when one
excluded name does not resolve. Measured directly against the live server:

    exclude=None                                 -> 6 tools
    exclude=[agno_run, agno_list_teams]          -> 4 tools
    exclude=[..., memory_search, memory_store]   -> 0 tools + "Failed to initialize"

For a full day every run logged `MCP connected: ...:9000/mcp (0 tools)` and
silently lost get_context_section / get_graph_report / list_recent_files /
search_knowledge_graph. Nothing failed; answers just got less grounded.

Two guards, because the list will be edited again:
  1. Every name in the exclude list must be a tool project MCP actually serves.
  2. A server that connects and serves nothing must say so loudly, whatever the
     cause — the general case, not just this one.
"""
import pytest

from swarm import team


# The tools EkamApp MCP actually serves, verified against a live `list_tools()`
# on 2026-08-21. Kept here as the contract the exclude list is checked against;
# update it when project MCP genuinely gains or loses a tool.
_PROJECT_MCP_TOOLS = {
    "agno_run", "agno_list_teams",
    "get_context_section", "get_graph_report",
    "list_recent_files", "search_knowledge_graph",
}


def test_every_excluded_name_is_a_tool_the_server_actually_serves():
    """The whole incident in one assertion. A name here that project MCP does not
    serve does not merely fail to exclude anything — it drops the entire server."""
    unknown = set(team._PROJECT_MCP_EXCLUDE_TOOLS) - _PROJECT_MCP_TOOLS
    assert not unknown, (
        f"{sorted(unknown)} are excluded but not served by project MCP — agno will "
        f"return 0 tools for the whole server. A tool that no longer exists needs no "
        f"exclusion."
    )


def test_the_deleted_memory_tools_are_not_excluded():
    """Pinned by name: these were removed from EkamApp MCP on 2026-08-20, so
    re-adding them here would reproduce the outage exactly."""
    assert "memory_search" not in team._PROJECT_MCP_EXCLUDE_TOOLS
    assert "memory_store" not in team._PROJECT_MCP_EXCLUDE_TOOLS


def test_the_recursion_guards_are_still_excluded():
    """The list's actual job — agno_run would recurse into this same swarm."""
    assert "agno_run" in team._PROJECT_MCP_EXCLUDE_TOOLS
    assert "agno_list_teams" in team._PROJECT_MCP_EXCLUDE_TOOLS


def test_excluding_everything_would_be_caught():
    """The guard is only useful if it can fail — proves it is not vacuously true."""
    unknown = {"a_tool_that_does_not_exist"} - _PROJECT_MCP_TOOLS
    assert unknown, "the check would pass for any name, which would make it useless"


@pytest.mark.parametrize("source", ["run_task_async", "run_task_stream"])
def test_both_connect_paths_warn_on_a_zero_tool_server(source):
    """Guard 2, checked structurally: both run paths must branch on
    `mcp.functions` rather than printing a bare count. A count of 0 scrolls past;
    a sentence naming the likely cause does not.

    Asserted against the source because the connect loop lives inside a long
    `async with AsyncExitStack()` body that cannot be called in isolation without
    standing up real MCP servers."""
    import inspect

    src = inspect.getsource(getattr(team, source))

    assert "if mcp.functions:" in src, f"{source} lost the zero-tool branch"
    assert "served 0 TOOLS" in src, f"{source} lost the zero-tool warning"
    assert "exclude_tools entry" in src, f"{source}'s warning lost the likely cause"
