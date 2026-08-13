"""Tests for the shared session_state mechanism (AGNOHive architecture review,
2026-08-13) -- agno's Team AND Agent classes already support session_state/
enable_agentic_state (confirmed by direct source read of the installed agno
2.5.17: team/team.py, agent/agent.py, team/_task_tools.py), a real structured
dict copied to each delegated member at dispatch and merged back after, that
this codebase never used before now.

Two mechanical consumers:
1. _record_read (tested in test_team_read_cache_hook.py) -- read_log.
2. _make_delegation_log_hook (tested here) -- delegations_made.

Both exist as the mechanical backstop for two existing PROSE-only coordinator
instructions ("Don't make downstream agents re-read", "Do NOT repeat the same
delegation again") that depend entirely on the coordinator remembering to
follow them -- see swarm/team.py's _COORDINATOR_INSTRUCTIONS "Shared state
across the whole run" section for the model-facing side of this.

_build_team()'s own session_state=/enable_agentic_state=/
add_session_state_to_context= wiring is also tested here (not in
test_team_read_cache_hook.py, which predates this feature and is scoped to the
tool_hooks list itself).
"""
import pytest

from swarm.team import _make_delegation_log_hook, _build_team


class _FakeRunContext:
    def __init__(self, session_state):
        self.session_state = session_state


# ── _make_delegation_log_hook ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delegation_call_is_logged_and_passed_through_unchanged():
    hook = _make_delegation_log_hook()
    run_context = _FakeRunContext({})

    async def fake_delegate(**kwargs):
        return "Researcher's answer"

    result = await hook(
        "delegate_task_to_member", fake_delegate,
        {"member_id": "Researcher", "task": "find the auth flow"},
        run_context=run_context,
    )

    assert result == "Researcher's answer"  # never short-circuited -- not idempotent like a read
    assert len(run_context.session_state["delegations_made"]) == 1
    entry = run_context.session_state["delegations_made"][0]
    assert entry["tool"] == "delegate_task_to_member"
    assert entry["args"] == {"member_id": "Researcher", "task": "find the auth flow"}


@pytest.mark.asyncio
async def test_delegate_task_to_members_plural_is_also_logged():
    """The broadcast-mode plural tool -- same logging, same shape."""
    hook = _make_delegation_log_hook()
    run_context = _FakeRunContext({})

    async def fake_delegate_all(**kwargs):
        return ["a", "b", "c"]

    await hook(
        "delegate_task_to_members", fake_delegate_all,
        {"task": "review this for security issues"},
        run_context=run_context,
    )

    assert run_context.session_state["delegations_made"][0]["tool"] == "delegate_task_to_members"


@pytest.mark.asyncio
async def test_repeated_delegation_calls_all_appear_so_the_coordinator_can_see_the_pattern():
    """Unlike the read-cache hook, this never dedupes or stubs -- a repeated-looking
    delegation is a real event that happened and must be visible to catch the
    exact re-delegation pattern this exists to make catchable in the first place."""
    hook = _make_delegation_log_hook()
    run_context = _FakeRunContext({})

    async def fake_delegate(**kwargs):
        return "answer"

    for _ in range(3):
        await hook(
            "delegate_task_to_member", fake_delegate,
            {"member_id": "Researcher", "task": "find the auth flow"},
            run_context=run_context,
        )

    assert len(run_context.session_state["delegations_made"]) == 3


@pytest.mark.asyncio
async def test_non_delegation_tool_call_passes_through_and_is_not_logged():
    hook = _make_delegation_log_hook()
    run_context = _FakeRunContext({})

    async def fake_get_file_content(**kwargs):
        return "file body"

    result = await hook(
        "get_file_content", fake_get_file_content, {"relative_path": "x.py"},
        run_context=run_context,
    )

    assert result == "file body"
    assert run_context.session_state.get("delegations_made", []) == []


@pytest.mark.asyncio
async def test_long_task_text_is_truncated_before_logging():
    hook = _make_delegation_log_hook()
    run_context = _FakeRunContext({})
    long_task = "x" * 5000

    async def fake_delegate(**kwargs):
        return "answer"

    await hook(
        "delegate_task_to_member", fake_delegate,
        {"member_id": "Researcher", "task": long_task},
        run_context=run_context,
    )

    logged_task = run_context.session_state["delegations_made"][0]["args"]["task"]
    assert len(logged_task) < len(long_task)
    assert logged_task.endswith("...(truncated)")


@pytest.mark.asyncio
async def test_missing_run_context_does_not_crash_the_hook():
    hook = _make_delegation_log_hook()

    async def fake_delegate(**kwargs):
        return "answer"

    result = await hook(
        "delegate_task_to_member", fake_delegate,
        {"member_id": "Researcher", "task": "x"},
    )

    assert result == "answer"


@pytest.mark.asyncio
async def test_none_session_state_does_not_crash_the_hook():
    hook = _make_delegation_log_hook()
    run_context = _FakeRunContext(None)

    async def fake_delegate(**kwargs):
        return "answer"

    result = await hook(
        "delegate_task_to_member", fake_delegate,
        {"member_id": "Researcher", "task": "x"},
        run_context=run_context,
    )

    assert result == "answer"
    assert run_context.session_state is None


@pytest.mark.asyncio
async def test_log_entries_are_bounded_on_a_very_long_run():
    from swarm.team import _MAX_DELEGATION_LOG_ENTRIES

    hook = _make_delegation_log_hook()
    run_context = _FakeRunContext({})

    async def fake_delegate(**kwargs):
        return "answer"

    for i in range(_MAX_DELEGATION_LOG_ENTRIES + 10):
        await hook(
            "delegate_task_to_member", fake_delegate,
            {"member_id": "Researcher", "task": f"task {i}"},
            run_context=run_context,
        )

    assert len(run_context.session_state["delegations_made"]) == _MAX_DELEGATION_LOG_ENTRIES
    # oldest entries dropped first -- the most recent ones survive
    assert run_context.session_state["delegations_made"][-1]["args"]["task"] == "task 209"


# ── _build_team: Team-level session_state wiring ────────────────────────────────

def test_build_team_seeds_session_state_with_read_log_and_delegations_made(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.session_state == {"read_log": [], "delegations_made": []}


def test_build_team_enables_agentic_state_and_context_injection(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.enable_agentic_state is True
    assert result.add_session_state_to_context is True


def test_build_team_fallback_members_also_have_agentic_state_enabled(monkeypatch):
    """make_coder/make_reviewer (the agent_specs=None fallback path) -- members
    need this too, not just the coordinator's own Team object, since agno hands
    each delegated member its own session_state copy at dispatch regardless."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = _build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    for member in result.members:
        assert member.enable_agentic_state is True
        assert member.add_session_state_to_context is True


def test_build_team_spec_based_members_also_have_agentic_state_enabled(monkeypatch):
    """agent_specs path (make_agent_from_spec) -- the primary path used by the
    real engineering.yaml team, not just the make_coder/make_reviewer fallback."""
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    class _FakeSpec:
        name = "Researcher"
        model = "qwen2.5-coder:32b"
        tools = None
        instructions = ["Research the codebase."]
        role = "Researcher"
        description = "Research specialist."
        skills = None

    result = _build_team(
        agent_specs=[_FakeSpec()],
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert result.members[0].enable_agentic_state is True
    assert result.members[0].add_session_state_to_context is True
