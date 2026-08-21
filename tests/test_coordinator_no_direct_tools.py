"""Tests: an EXPLICITLY empty coordinator_tools means zero MCP tools, not all of them.

`_scope_coordinator_tools` ends every branch with `scoped or mcp_list` -- a fallback that
exists so an allowlist naming only unknown tools (a typo) fails OPEN rather than leaving
the coordinator toolless. That fallback is right for a misconfiguration and exactly wrong
for a deliberate empty list: without an early return, `coordinator_tools: []` would hand
the coordinator every tool from every connected MCP, silently doing the opposite of what
it says. These tests pin the distinction.

Motivation (2026-08-20): _COORDINATOR_DISCOVERY_TOOLS had been growing one entry per live
incident where the coordinator used a tool directly instead of delegating -- find_files,
search_files, ... then lightrag_query/get_context_section, then get_graph_report a commit
later. The next incident was db_schema: asked which line defines a column, the coordinator
called db_schema itself and answered in two turns with ZERO delegations. db_schema returns
no line numbers, so the cited line could only be invented -- it said 102, then 123 on a
re-run; the real line is 129. A denylist needing a new entry per incident is the wrong
shape, so engineering inverts it to an empty allowlist.
"""
from types import SimpleNamespace

import pytest

from swarm.team import _scope_coordinator_tools


def _mcp(**funcs):
    return SimpleNamespace(functions={name: SimpleNamespace(name=name) for name in funcs})


@pytest.fixture
def mcp_list():
    return [_mcp(get_file_content=1, db_query=1, db_schema=1, apply_diff=1, run_command=1)]


def test_explicit_empty_allowlist_yields_no_mcp_tools(mcp_list):
    """The whole point: [] means none."""
    assert _scope_coordinator_tools([], mcp_list) == []


def test_explicit_empty_allowlist_holds_under_read_only(mcp_list):
    assert _scope_coordinator_tools([], mcp_list, True) == []


def test_none_allowlist_is_unchanged(mcp_list):
    """No allowlist configured still means "everything minus the discovery blocklist" --
    every other team relies on this and must not shift."""
    scoped = _scope_coordinator_tools(None, mcp_list)
    names = {f.name for f in scoped}
    assert "get_file_content" in names
    assert "db_schema" in names          # still reachable for teams that don't opt out


def test_a_nonempty_allowlist_of_unknown_tools_still_fails_open(mcp_list):
    """A typo'd allowlist is a misconfiguration, not an intent to disarm the coordinator
    -- that case must keep the pre-existing mcp_list fallback rather than silently
    yielding a toolless coordinator."""
    scoped = _scope_coordinator_tools(["not_a_real_tool"], mcp_list)
    assert scoped == mcp_list


def test_a_nonempty_allowlist_still_scopes_normally(mcp_list):
    scoped = _scope_coordinator_tools(["get_file_content"], mcp_list)
    assert [f.name for f in scoped] == ["get_file_content"]


def test_engineering_yaml_declares_an_empty_coordinator_allowlist():
    """The config half of the change -- asserted here so a later edit that drops the key
    (reverting to 'everything minus the blocklist') fails loudly rather than quietly
    re-arming the coordinator."""
    import yaml
    from pathlib import Path

    data = yaml.safe_load(
        (Path(__file__).parent.parent / "teams" / "engineering.yaml").read_text(encoding="utf-8")
    )
    assert "coordinator_tools" in data, "key must be present, not omitted"
    assert data["coordinator_tools"] == []


def test_researcher_owns_the_db_tools_the_coordinator_gave_up():
    """Both halves must ship together: with the coordinator disarmed and no member
    holding db_query/db_schema, the database would be unreachable by anyone."""
    from swarm.team_config import _load_seed_grants

    tool_grants, _ = _load_seed_grants()
    for tool in ("db_query", "db_schema"):
        assert ("engineering", "Researcher", tool) in tool_grants
