"""Tests: the zero/low-argument catalog tools are duplicate-guarded.

Live, 2026-08-21, one T1-T13 re-run producing two instances of the same loop:

  T7 ("does API/loyalty-service exist?") — the coordinator called `list_skills({})`
  about twenty times, roughly once a second, byte-identical empty args. That
  exhausted its tool_call_limit in ~22s; past that agno's own run_function_calls
  silently rejects further calls emitting zero stream events, so the model produced
  contentless turns for 300s until the liveness auto-kill fired. It never looked for
  the directory it was asked about.

  T9 — an engineering run cycled `get_project_context({})` → `get_file_content(
  docs/frontend.md, offset 232, limit 10)` → `get_project_context({})` three times
  identically. get_file_content was cached and correctly stubbed; get_project_context
  was not, so the loop kept its footing on the uncached half.

This is the fourth and fifth occurrence of a pattern `_CACHEABLE_READ_TOOLS` already
records three times (lightrag_query 2026-08-15, search_knowledge_graph 2026-08-15,
web_search/web_fetch 2026-08-18). The fix is the same each time: add the tool. These
tests exist so the sixth occurrence is a test failure instead of another 300s stall.
"""
import pytest

from swarm.team import _CACHEABLE_READ_TOOLS


@pytest.mark.parametrize("tool", ["list_skills", "get_project_context"])
def test_the_tools_that_actually_looped_are_guarded(tool):
    """Both live reproductions, named."""
    assert tool in _CACHEABLE_READ_TOOLS


@pytest.mark.parametrize("tool", ["load_skill", "get_context_section", "list_recent_files"])
def test_the_rest_of_the_zero_arg_family_is_guarded(tool):
    """A no-argument read tool is the WORST case for this failure, not an edge case:
    every call is trivially byte-identical, so a model that has lost track of what it
    already holds can re-issue it indefinitely at zero prompt cost. Guarding the two
    that happened to loop and leaving their siblings exposed just relocates the stall."""
    assert tool in _CACHEABLE_READ_TOOLS


@pytest.mark.parametrize("tool", [
    "list_processes",   # process list genuinely changes between calls
    "check_port",       # so does a port's state
    "get_env_info",     # cheap, but a repeat is legitimate after an env change
    "git_status",       # changes the moment any write lands
    "git_diff",
    "db_query",         # a migration mid-run would make a cached answer WRONG
    "db_schema",
])
def test_tools_that_must_not_be_cached_are_absent(tool):
    """The boundary that keeps this safe. Everything cached here must be read-only
    AND fixed for the life of a run. Caching a tool whose answer can legitimately
    change trades a repetition loop for a stale answer — the worse failure, and one
    that would surface as a confident wrong result rather than a visible stall."""
    assert tool not in _CACHEABLE_READ_TOOLS


def test_the_previously_fixed_loops_are_still_guarded():
    """The three prior occurrences — a regression here would reopen a closed bug."""
    for tool in ("lightrag_query", "search_knowledge_graph", "web_search", "web_fetch"):
        assert tool in _CACHEABLE_READ_TOOLS


def test_the_core_read_tools_are_still_guarded():
    for tool in ("get_file_content", "get_files_batch", "search_files",
                 "search_files_batch", "find_files", "list_directory",
                 "list_directory_tree", "count_matches"):
        assert tool in _CACHEABLE_READ_TOOLS


def test_no_write_or_mutating_tool_is_cacheable():
    """Caching a write would be a correctness bug, not a performance one."""
    for tool in ("apply_diff", "write_file", "run_command", "run_shell",
                 "run_docker", "run_migration", "bash_run"):
        assert tool not in _CACHEABLE_READ_TOOLS
