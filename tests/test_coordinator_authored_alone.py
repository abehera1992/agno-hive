"""Tests for P1 (coordinator-authored-alone detection) and P2 (verify_claims retry).

P1 -- the mechanical gates this codebase depends on (decompose-first,
search-before-browse) are wired onto the RESEARCHER's tool calls. An answer the
coordinator writes itself, having never delegated, is subject to none of them. That is
the still-open finding recorded 2026-08-15: a coordinator-authored retry "repeated the
exact wrong-service mistake Phase 6 fixed ... producing a confidently WRONG answer that
overwrote Researcher's correct one as the final result."

The counter is closure-local ON PURPOSE. session_state["delegations_made"] cannot answer
"did this run delegate", because agno builds a new RunContext -- and a new, empty
session_state -- per delegate_task_to_members call (root-caused with instrumentation
2026-08-16). These tests pin the closure behaviour so a future refactor back onto
session_state fails here rather than silently in production.

P2 -- verify_claims was disabling itself precisely on the runs most likely to fabricate:
of three live probes on 2026-08-20, both LONG runs (128s/138s) reported the check
unavailable and both shipped a real fabrication uncaught; the one short run (79s)
verified cleanly. One retry, because the check is a deterministic grep with no model in
it, so a failure is transient rather than a verdict.
"""
import asyncio
from types import SimpleNamespace

import pytest

from swarm.team import (
    _count_delegations,
    _make_delegation_log_hook,
    _verify_claims,
)


# ── P1: closure-local delegation counter ──────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


async def _noop_tool(**kwargs):
    return "member finished the subtask"


def test_delegation_hook_counts_real_delegations_in_its_closure():
    hook = _make_delegation_log_hook()
    state = hook.state
    assert state["count"] == 0

    _run(hook("delegate_task_to_member", _noop_tool, {"member_id": "researcher",
                                                      "task": "go look"}))
    assert state["count"] == 1

    _run(hook("delegate_task_to_member", _noop_tool, {"member_id": "reviewer",
                                                      "task": "cross-check"}))
    assert state["count"] == 2


def test_delegation_counter_survives_a_null_run_context():
    """The exact condition that makes session_state useless here: agno hands the hook a
    fresh/absent context per delegate call. The closure counter must not depend on it."""
    hook = _make_delegation_log_hook()
    state = hook.state
    _run(hook("delegate_task_to_member", _noop_tool, {"task": "x"}, None))
    assert state["count"] == 1


def test_non_delegation_calls_are_not_counted():
    hook = _make_delegation_log_hook()
    state = hook.state
    _run(hook("get_file_content", _noop_tool, {"relative_path": "a.py"}))
    _run(hook("search_files", _noop_tool, {"pattern": "x"}))
    assert state["count"] == 0


def test_delegation_hook_still_returns_the_real_tool_result():
    """Pure observer: counting must never change what the delegation returns."""
    hook = _make_delegation_log_hook()
    out = _run(hook("delegate_task_to_member", _noop_tool, {"task": "x"}))
    assert out == "member finished the subtask"


def test_count_delegations_reads_the_state_off_the_team():
    team = SimpleNamespace(_delegation_state={"count": 3})
    assert _count_delegations(team) == 3


def test_count_delegations_is_undeterminable_without_the_attribute():
    """A team built by another path or a test double must read as "unknown" (-1), never
    as "delegated nothing" (0) -- the same rule _count_read_calls follows, and the
    difference between staying quiet and slapping a warning on a fine answer."""
    assert _count_delegations(SimpleNamespace()) == -1
    assert _count_delegations(SimpleNamespace(_delegation_state=None)) == -1
    assert _count_delegations(SimpleNamespace(_delegation_state={})) == -1


# ── P2: verify_claims retry ───────────────────────────────────────────────────


def _patch_client(monkeypatch, behaviour):
    """Patch the mcp streamable client _verify_claims imports at call time.

    _verify_claims does `from mcp.client.streamable_http import streamablehttp_client`
    INSIDE the function, so the name must be patched on the source module, not on
    swarm.team -- patching the latter silently does nothing and the test would pass
    for the wrong reason.
    """
    import mcp.client.streamable_http as sh

    monkeypatch.setattr(sh, "streamablehttp_client", behaviour)


def test_verify_claims_makes_a_second_attempt_after_a_transient_failure(monkeypatch):
    """The core P2 contract: one transient failure must not disable the check for the
    whole run. Asserts the client was actually called TWICE, which is the only thing
    that distinguishes a real retry from the old single-shot give-up."""
    import swarm.team as team_mod

    monkeypatch.setattr(team_mod, "_VERIFY_RETRY_PAUSE_S", 0)
    attempts = {"n": 0}

    def flaky(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("hive-mcp busy")

    _patch_client(monkeypatch, flaky)

    report, bad, unavailable = _run(_verify_claims("some `symbol` claim", "http://x/mcp"))

    assert attempts["n"] == 2, "verify_claims gave up without retrying"
    assert unavailable is True
    assert bad is False
    assert report == ""


def test_verify_claims_skips_silently_when_not_configured():
    """No hive_mcp_url is a deliberate config choice, not a degradation: it must stay
    unavailable=False so no disclaimer is attached."""
    report, bad, unavailable = _run(_verify_claims("claim", None))
    assert (report, bad, unavailable) == ("", False, False)

    report, bad, unavailable = _run(_verify_claims("", "http://x/mcp"))
    assert (report, bad, unavailable) == ("", False, False)


class _FakeSession:
    """Minimal stand-in for an mcp ClientSession already held open by the run."""

    def __init__(self, text="verify_claims: every checked claim exists", boom=None):
        self.text, self.boom, self.calls = text, boom, 0

    async def call_tool(self, name, args):
        self.calls += 1
        if self.boom:
            raise self.boom
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class _FakeMCPTools:
    def __init__(self, session):
        self.session, self.get_session_calls = session, 0

    async def get_session_for_run(self, **kwargs):
        self.get_session_calls += 1
        return self.session


def test_verify_claims_uses_the_runs_live_session_and_never_opens_a_connection(monkeypatch):
    """The whole point of the reuse: opening a NEW streamablehttp_client while the run's
    own connections to the same server are open is the step that was hanging. If the live
    session works, the fresh-connection path must never be touched."""
    opened = {"n": 0}

    def _must_not_be_called(*a, **k):
        opened["n"] += 1
        raise AssertionError("opened a fresh connection despite a live session")

    _patch_client(monkeypatch, _must_not_be_called)
    tools = _FakeMCPTools(_FakeSession())

    report, bad, unavailable = _run(
        _verify_claims("a `symbol` claim", "http://x/mcp", tools)
    )

    assert tools.session.calls == 1
    assert opened["n"] == 0
    assert unavailable is False
    assert bad is False
    assert "every checked claim exists" in report


def test_verify_claims_falls_back_to_a_fresh_connection_when_the_live_session_fails(monkeypatch):
    """A dead/stale live session must degrade to the old behaviour, not to no check."""
    import swarm.team as team_mod

    monkeypatch.setattr(team_mod, "_VERIFY_RETRY_PAUSE_S", 0)
    tried = {"n": 0}

    def fresh(*a, **k):
        tried["n"] += 1
        raise RuntimeError("fresh connection also down")

    _patch_client(monkeypatch, fresh)
    tools = _FakeMCPTools(_FakeSession(boom=RuntimeError("session closed")))

    report, bad, unavailable = _run(
        _verify_claims("a `symbol` claim", "http://x/mcp", tools)
    )

    assert tools.session.calls == 1, "live session should have been tried first"
    assert tried["n"] == 1, "should have fallen back to a fresh connection exactly once"
    assert unavailable is True


def test_verify_claims_still_works_with_no_tools_passed(monkeypatch):
    """Backward compatibility: every existing caller passes only (content, url)."""
    import swarm.team as team_mod

    monkeypatch.setattr(team_mod, "_VERIFY_RETRY_PAUSE_S", 0)
    attempts = {"n": 0}

    def flaky(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("down")

    _patch_client(monkeypatch, flaky)
    _, _, unavailable = _run(_verify_claims("claim `x`", "http://x/mcp"))
    assert attempts["n"] == 2, "url-only callers keep the two fresh-connection attempts"
    assert unavailable is True


def test_verify_claims_runs_with_tools_but_no_url():
    """A live session alone is sufficient -- no url means no fallback, not no check."""
    tools = _FakeMCPTools(_FakeSession())
    report, _, unavailable = _run(_verify_claims("claim `x`", None, tools))
    assert unavailable is False
    assert tools.session.calls == 1


class _CountSession:
    """Session stand-in for count_matches. `boom=True` simulates a dead live session."""

    def __init__(self, total=7, boom=False):
        self.total, self.boom, self.calls = total, boom, 0

    async def call_tool(self, name, args):
        self.calls += 1
        if self.boom:
            raise RuntimeError("session closed")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"TOTAL: {self.total}")]
        )


def test_count_markers_resolved_on_the_live_session_without_a_new_connection(monkeypatch):
    """_fill_count_markers runs post-run, in the same place and for the same reason as
    verify_claims -- so it must reuse the run's session rather than open a new one."""
    from swarm.team import _fill_count_markers

    def _must_not_be_called(*a, **k):
        raise AssertionError("opened a fresh connection despite a live session")

    _patch_client(monkeypatch, _must_not_be_called)
    tools = _FakeMCPTools(_CountSession(total=7))

    out = _run(_fill_count_markers(
        "There are [[COUNT pattern=`foo` glob=`**/*.py`]] matches.",
        "http://x/mcp", tools))

    assert "7" in out and "[[COUNT" not in out
    assert tools.session.calls == 1


def test_count_markers_fall_back_to_a_fresh_connection_when_the_live_session_is_dead(monkeypatch):
    """The subtle case: the per-marker try/except swallows a dead-session error into
    '[count unavailable]' instead of raising, so a session-level failure can never reach
    the outer handler. Without the resolved_any signal the fallback would never fire and
    every count would silently degrade."""
    from swarm.team import _fill_count_markers

    fresh_used = {"n": 0}

    def fresh(*a, **k):
        fresh_used["n"] += 1
        raise RuntimeError("fresh also down")

    _patch_client(monkeypatch, fresh)
    tools = _FakeMCPTools(_CountSession(boom=True))

    out = _run(_fill_count_markers(
        "There are [[COUNT pattern=`foo` glob=`**/*.py`]] matches.",
        "http://x/mcp", tools))

    assert tools.session.calls == 1, "live session should have been tried first"
    assert fresh_used["n"] == 1, "a dead live session must trigger the fresh-connection fallback"
    assert "[count unavailable]" in out


def test_count_markers_are_a_noop_with_no_markers_present():
    """Unchanged behaviour: no markers means no connection of either kind is opened."""
    from swarm.team import _fill_count_markers

    text = "A perfectly ordinary answer with no markers."
    assert _run(_fill_count_markers(text, "http://x/mcp", _FakeMCPTools(_CountSession()))) == text


def test_retry_pause_is_short_enough_to_be_irrelevant():
    """Worst case must stay well under the liveness watchdog -- the same
    do-not-race-the-watchdog invariant that drove _MCP_TIMEOUT down to 180."""
    from swarm.team import _BESPOKE_MCP_SESSION_TIMEOUT, _VERIFY_RETRY_PAUSE_S
    from config.config import config

    worst_case = _BESPOKE_MCP_SESSION_TIMEOUT + _BESPOKE_MCP_SESSION_TIMEOUT // 2 + _VERIFY_RETRY_PAUSE_S
    assert worst_case < config.liveness_silence_threshold_s
