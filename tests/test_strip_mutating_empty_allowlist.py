"""Regression tests: read_only must not un-disarm a deliberately toolless coordinator.

Live root cause, 2026-08-21. `_strip_mutating` tested `if tool_names:` — falsy for an
EXPLICITLY EMPTY allowlist — and returned None, which `_scope_coordinator_tools`
reads as "no allowlist, use the live MCP surface minus the discovery blocklist".

So engineering's coordinator disarm (`coordinator_tools: []`, shipped 2026-08-20 to
stop the coordinator answering db_schema questions itself instead of delegating) was
silently voided on EVERY read_only run — i.e. every question-answering call, since
read_only=True is the documented default for those.

Measured by logging the coordinator's resolved surface on a live run:

    [team] coordinator surface (25): ['get_project_context', 'get_file_content',
     'get_files_batch', ..., 'check_port', 'list_processes', ..., 'db_query',
     'db_schema', ...]

25 tools, including the exact ones the disarm removed. Every static check passed:
the YAML said [], _load_team returned [], the worker payload carried [], and
_build_team produced [] when called by hand. Only the read_only hop in between
turned [] into None, which is why it took a runtime log to find.

Fourth instance of empty-vs-absent in this codebase — the same conflation was fixed
in _scope_coordinator_tools (early return for []) and twice in _load_team.
"""
import pytest

from swarm.team import _strip_mutating


class _Spec:
    def __init__(self, name, tools=None):
        self.name = name
        self.tools = tools


def test_an_empty_allowlist_survives_read_only_stripping():
    """The whole incident in one assertion."""
    _, ctools = _strip_mutating([_Spec("Researcher")], [])

    assert ctools == [], "[] must stay [] — None means UNRESTRICTED downstream"
    assert ctools is not None


def test_an_absent_allowlist_still_becomes_none():
    """The behaviour that must NOT change: a team with no coordinator_tools: at all is
    resolved against the live MCP surface. Conflating this with [] in the other
    direction would disarm every team that never opted in."""
    _, ctools = _strip_mutating([_Spec("Researcher")], None)

    assert ctools is None


def test_a_populated_allowlist_still_drops_mutating_tools():
    _, ctools = _strip_mutating(
        [_Spec("Researcher")], ["get_file_content", "apply_diff", "run_command"]
    )

    assert "get_file_content" in ctools
    assert "apply_diff" not in ctools
    assert "run_command" not in ctools


def test_an_allowlist_of_only_mutating_tools_becomes_empty_not_none():
    """The subtle case: stripping can PRODUCE an empty list. That result means "this
    coordinator may call nothing", not "give it everything" — the same distinction,
    one layer down."""
    _, ctools = _strip_mutating([_Spec("Coder")], ["apply_diff", "write_file"])

    assert ctools == []
    assert ctools is not None


def test_member_specs_are_still_stripped():
    specs, _ = _strip_mutating(
        [_Spec("Coder", ["get_file_content", "apply_diff"])], []
    )

    assert specs[0].tools == ["get_file_content"]


def test_the_end_to_end_shape_engineering_actually_runs(monkeypatch):
    """_strip_mutating feeds _scope_coordinator_tools directly (swarm/team.py's
    run_task_async/run_task_stream). Pinning the pair together, because each is
    individually correct and it was the HANDOFF that broke."""
    from types import SimpleNamespace

    from swarm.team import _scope_coordinator_tools

    mcp = SimpleNamespace(functions={
        n: SimpleNamespace(name=n)
        for n in ("get_file_content", "db_schema", "list_processes", "check_port")
    })

    _, ctools = _strip_mutating([_Spec("Researcher")], [])
    scoped = _scope_coordinator_tools(ctools, [mcp], True)

    assert scoped == [], "a disarmed coordinator must stay disarmed under read_only"


def test_the_pre_fix_behaviour_would_have_failed_this(monkeypatch):
    """Guards the guard: proves the assertion above is not vacuous by reproducing the
    old truthy check and showing it yields the 4-tool unrestricted surface."""
    from types import SimpleNamespace

    from swarm.team import _scope_coordinator_tools

    mcp = SimpleNamespace(functions={
        n: SimpleNamespace(name=n)
        for n in ("get_file_content", "db_schema", "list_processes", "check_port")
    })

    old_style_ctools = [] or None          # exactly what `if tool_names:` produced
    scoped = _scope_coordinator_tools(old_style_ctools, [mcp], True)

    assert len(scoped) > 0, "pre-fix, the coordinator got a full surface"
