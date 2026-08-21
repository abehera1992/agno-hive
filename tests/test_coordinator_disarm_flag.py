"""Tests: the coordinator disarm is flag-gated and OFF by default.

The disarm (`coordinator_tools: []`) is the intended architecture — the coordinator
delegates and aggregates, members hold the tools, which is where the SQLite
team_role_tools mapping puts them. It was live-confirmed working on 2026-08-21 once
the read_only path stopped discarding the empty list: coordinator surface went from
25 tools to 2, and the run delegated to context-router then researcher as designed.

It ships OFF anyway, because turning it on measurably made ANSWERS worse — and the
regression is not in the disarm. It is in machinery calibrated around a coordinator
that did its own reads. With every read now happening inside a delegated member,
_count_read_calls sees none of them. Same prompts, same hour:

    armed    -> T1 correct 3/3; T3 correct, no disclaimer
    disarmed -> T1 line 127/String(10) (truth: 129/String(8))
                T3 cited "API/parties-service/" — a service that does not exist
                ALL answers carried the UNGROUNDED disclaimer, including a correct one

_count_read_calls' own docstring records why that is dangerous rather than merely
noisy: a false "ungrounded" verdict forces a retry, and "that retry's fresh,
un-grounded re-run then produced a WRONG answer, overwriting the right one."

Flip ON only once a delegated member's reads are visible to _count_read_calls, then
re-run the T1-T13 battery before trusting it.
"""
import pytest

from config.config import config
from swarm.team import _resolve_coordinator_allowlist, _scope_coordinator_tools


def test_the_flag_is_off_by_default():
    """The whole point. If this ever flips silently, answers regress."""
    assert config.enforce_coordinator_disarm is False


def test_off_treats_an_empty_allowlist_as_absent(monkeypatch):
    monkeypatch.setattr(config, "enforce_coordinator_disarm", False)

    assert _resolve_coordinator_allowlist([]) is None


def test_on_preserves_an_empty_allowlist(monkeypatch):
    monkeypatch.setattr(config, "enforce_coordinator_disarm", True)

    assert _resolve_coordinator_allowlist([]) == []


@pytest.mark.parametrize("flag", [True, False])
def test_an_absent_allowlist_is_untouched_either_way(monkeypatch, flag):
    """A team with no coordinator_tools: at all must resolve against the live MCP
    surface regardless — the flag governs [] only."""
    monkeypatch.setattr(config, "enforce_coordinator_disarm", flag)

    assert _resolve_coordinator_allowlist(None) is None


@pytest.mark.parametrize("flag", [True, False])
def test_a_populated_allowlist_is_untouched_either_way(monkeypatch, flag):
    monkeypatch.setattr(config, "enforce_coordinator_disarm", flag)

    assert _resolve_coordinator_allowlist(["get_file_content"]) == ["get_file_content"]


# ── end-to-end through the scoping the run paths actually use ─────────────────

def _mcp():
    from types import SimpleNamespace

    return SimpleNamespace(functions={
        n: SimpleNamespace(name=n)
        for n in ("get_file_content", "db_schema", "list_processes")
    })


def test_off_yields_the_pre_disarm_surface(monkeypatch):
    monkeypatch.setattr(config, "enforce_coordinator_disarm", False)

    scoped = _scope_coordinator_tools(_resolve_coordinator_allowlist([]), [_mcp()], True)

    assert len(scoped) > 0, "OFF must restore the unrestricted surface"


def test_on_yields_a_disarmed_coordinator(monkeypatch):
    monkeypatch.setattr(config, "enforce_coordinator_disarm", True)

    scoped = _scope_coordinator_tools(_resolve_coordinator_allowlist([]), [_mcp()], True)

    assert scoped == []


def test_the_gate_is_applied_before_the_read_only_split():
    """Both run paths must gate on the same value. Until 2026-08-21 only the read_only
    path passed through _strip_mutating, so the disarm was active for normal runs and
    void for read-only ones — a split that hid the bug for a day, since every static
    check on the non-read_only path passed."""
    import inspect

    from swarm import team

    for fn in (team.run_task_async, team.run_task_stream):
        src = inspect.getsource(fn)
        assert "_allowlist = _resolve_coordinator_allowlist(coordinator_tools)" in src, fn.__name__
        assert "_strip_mutating(agent_specs, _allowlist)" in src, fn.__name__
        assert "else (agent_specs, _allowlist))" in src, fn.__name__
        assert "_strip_mutating(agent_specs, coordinator_tools)" not in src, \
            f"{fn.__name__} still bypasses the gate"
