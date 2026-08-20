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


def test_retry_pause_is_short_enough_to_be_irrelevant():
    """Worst case must stay well under the liveness watchdog -- the same
    do-not-race-the-watchdog invariant that drove _MCP_TIMEOUT down to 180."""
    from swarm.team import _BESPOKE_MCP_SESSION_TIMEOUT, _VERIFY_RETRY_PAUSE_S
    from config.config import config

    worst_case = _BESPOKE_MCP_SESSION_TIMEOUT + _BESPOKE_MCP_SESSION_TIMEOUT // 2 + _VERIFY_RETRY_PAUSE_S
    assert worst_case < config.liveness_silence_threshold_s
