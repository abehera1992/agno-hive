"""Tests: force an answer BEFORE tool_call_limit, and honour per-role budgets.

Two fixes, one root problem — recorded on the Notion hardening page as "agno's own
tool_call_limit rejection bypasses every one of this file's reinforcement hooks
entirely", open since 2026-08-19 and measured 2026-08-21.

Measurement: two stalled runs each made exactly 25 tool calls
(config.tool_call_limit), after which agno silently refused every further call and
the model re-emitted the same one at ~2/s for 300s until the liveness watchdog
killed it — no answer, no diagnostic, and a 97.9% prefix-cache hit rate confirming
the prompt never advanced.

Both REACTIVE routes are provably closed, each confirmed against source:
  * a tool hook never fires for a refused call (agno appends
    create_tool_call_limit_error_result and continues — no tool event);
  * a stream-loop watcher never sees it either (streaming yields only Event objects
    with no .messages; TeamRunOutput arrives once, at the end).

So the guard is PRE-EMPTIVE: count the calls that succeed — which hooks see
perfectly — and flip tool_choice one call early. There is then no refusal to
detect, because the model has no tool call left to make.
"""
import pytest

from config.config import config
from swarm.team import (
    _TOOL_BUDGET_RESERVE,
    _make_tool_budget_guard_hook,
    _resolve_tool_call_limit,
)


class _Agent:
    def __init__(self, name):
        self.name = name
        self.tool_choice = "auto"
        self.model = type("M", (), {"_tool_choice": "auto"})()


async def _fn(**kwargs):
    return "REAL RESULT"


# ── _resolve_tool_call_limit (fix #4) ─────────────────────────────────────────

def test_a_db_override_is_honoured(monkeypatch):
    """The whole point of fix #4: engineering's Coordinator=60 row never reached the
    Team, so raising it via /admin/model-routes silently did nothing."""
    from swarm import model_routing

    policy = type("P", (), {"tool_call_limit": 60})()
    monkeypatch.setattr(model_routing, "get_role_policy", lambda t, r: policy)

    assert _resolve_tool_call_limit("engineering", "Coordinator") == 60


def test_no_row_falls_back_to_config(monkeypatch):
    from swarm import model_routing

    monkeypatch.setattr(model_routing, "get_role_policy", lambda t, r: None)

    assert _resolve_tool_call_limit("engineering", "Coordinator") == config.tool_call_limit


def test_a_row_with_a_null_column_falls_back_to_config(monkeypatch):
    """The second, independent None: RolePolicy.tool_call_limit is itself int|None,
    and NULL means "no override" — not "limit of zero"."""
    from swarm import model_routing

    policy = type("P", (), {"tool_call_limit": None})()
    monkeypatch.setattr(model_routing, "get_role_policy", lambda t, r: policy)

    assert _resolve_tool_call_limit("engineering", "Coordinator") == config.tool_call_limit


def test_a_teamless_build_never_looks_up_a_policy(monkeypatch):
    """_build_team's team_name is str|None — the request.agents path passes no team.
    Looking up get_role_policy(None, ...) would be a silent miss at best."""
    from swarm import model_routing

    def _boom(t, r):
        raise AssertionError("must not be called without a team name")

    monkeypatch.setattr(model_routing, "get_role_policy", _boom)

    assert _resolve_tool_call_limit(None, "Coordinator") == config.tool_call_limit


# ── the pre-emptive guard (fix #2) ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_it_forces_text_only_before_the_limit(monkeypatch):
    """The core behaviour: tool_choice flips while the agent still has budget left,
    so agno never has to refuse anything."""
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit", lambda t, r: 5)
    hook = _make_tool_budget_guard_hook("engineering")
    agent = _Agent("Researcher")

    for _ in range(5 - _TOOL_BUDGET_RESERVE - 1):
        await hook("get_file_content", _fn, {}, agent=agent)
    assert agent.tool_choice == "auto", "fired too early"

    await hook("get_file_content", _fn, {}, agent=agent)
    assert agent.tool_choice == "none"
    assert agent.model._tool_choice == "none"


@pytest.mark.asyncio
async def test_the_real_result_is_returned_not_replaced(monkeypatch):
    """This call was WITHIN budget — its content is legitimately needed for the answer
    the agent must now write. Replacing it would throw away a good fetch."""
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit", lambda t, r: 2)
    hook = _make_tool_budget_guard_hook("engineering")

    out = await hook("get_file_content", _fn, {}, agent=_Agent("Researcher"))

    assert "REAL RESULT" in out
    assert "TOOL BUDGET REACHED" in out


@pytest.mark.asyncio
async def test_the_notice_says_not_to_retry(monkeypatch):
    """Naming the useless next action is what stops the loop — the same reason the
    "File not found" and empty-file branches carry anti-retry wording."""
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit", lambda t, r: 2)
    hook = _make_tool_budget_guard_hook("engineering")

    out = await hook("get_file_content", _fn, {}, agent=_Agent("Researcher"))

    assert "Do NOT attempt" in out
    assert "refused silently" in out


@pytest.mark.asyncio
async def test_it_fires_once_per_role(monkeypatch):
    """Past the threshold every later call would otherwise re-append the notice,
    reproducing the context-bloat problem _collapse_prior_stub_messages exists for."""
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit", lambda t, r: 2)
    hook = _make_tool_budget_guard_hook("engineering")
    agent = _Agent("Researcher")

    first = await hook("get_file_content", _fn, {}, agent=agent)
    second = await hook("get_file_content", _fn, {}, agent=agent)

    assert "TOOL BUDGET REACHED" in first
    assert second == "REAL RESULT"


@pytest.mark.asyncio
async def test_budgets_are_tracked_per_role(monkeypatch):
    """Roles have genuinely different limits (Coordinator 60, Researcher 50,
    Reviewer 45, others 25). One shared counter would starve whoever called first."""
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit",
                        lambda t, r: 2 if r == "Researcher" else 50)
    hook = _make_tool_budget_guard_hook("engineering")
    researcher, coder = _Agent("Researcher"), _Agent("Coder")

    await hook("get_file_content", _fn, {}, agent=researcher)
    await hook("get_file_content", _fn, {}, agent=coder)

    assert researcher.tool_choice == "none"
    assert coder.tool_choice == "auto", "Coder's own budget is untouched"


@pytest.mark.asyncio
async def test_the_coordinators_own_calls_are_counted(monkeypatch):
    """The coordinator's calls arrive with agent=None — it has a budget too, and it
    was engineering's Coordinator=60 row that fix #4 restored."""
    seen = []
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit",
                        lambda t, r: seen.append(r) or 2)
    hook = _make_tool_budget_guard_hook("engineering")

    out = await hook("get_file_content", _fn, {}, agent=None)

    assert seen == ["Coordinator"]
    assert "TOOL BUDGET REACHED" in out   # counted, even with no agent object to flip


@pytest.mark.asyncio
async def test_every_tool_is_counted_not_just_cacheable_reads(monkeypatch):
    """agno's budget covers EVERY tool call. Counting only the cacheable reads the
    read-cache hook filters to would undercount and let the real wall arrive early."""
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit", lambda t, r: 3)
    hook = _make_tool_budget_guard_hook("engineering")
    agent = _Agent("Researcher")

    await hook("run_command", _fn, {}, agent=agent)
    out = await hook("notion_search", _fn, {}, agent=agent)

    assert "TOOL BUDGET REACHED" in out


@pytest.mark.asyncio
async def test_a_limit_of_one_still_fires(monkeypatch):
    """max(limit - reserve, 1) — a budget of 1 must not compute a threshold of 0 and
    fire before the agent has fetched anything at all."""
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit", lambda t, r: 1)
    hook = _make_tool_budget_guard_hook("engineering")

    out = await hook("get_file_content", _fn, {}, agent=_Agent("Researcher"))

    assert "TOOL BUDGET REACHED" in out


@pytest.mark.asyncio
async def test_activity_records_which_roles_were_forced(monkeypatch):
    monkeypatch.setattr("swarm.team._resolve_tool_call_limit", lambda t, r: 2)
    activity: dict = {}
    hook = _make_tool_budget_guard_hook("engineering", activity)

    await hook("get_file_content", _fn, {}, agent=_Agent("Researcher"))

    assert activity["tool_budget_forced"] == ["Researcher"]


def test_the_guard_is_appended_last_and_bound_per_role():
    """Two properties in one place, both load-bearing.

    ORDERING: agno reverses tool_hooks and reduces from the entrypoint outward, so
    tool_hooks[0] is OUTERMOST and the last entry is innermost. The guard must be last
    so it never counts a call an outer gate is about to block or stub - those return
    their own message without calling `function`.

    BINDING: each agent gets its OWN instance with its role bound at construction. The
    first version discovered the role from `agent`, which agno stores on the Function
    object - shared across every agent listing the same tool, and live-measured as None
    for every call. That attributed member calls to "Coordinator" and counted a whole
    team into one bucket against the wrong ceiling, which is why the guard never fired
    on a run that made 67 calls against a limit of 25.
    """
    import inspect

    from swarm import team

    src = inspect.getsource(team._build_team)
    shared = src[src.index("tool_hooks = ["):]
    shared_block = shared[:shared.index("]")]

    # The guard is NOT in the shared list - it is appended per agent by _hooks_for.
    assert "_make_tool_budget_guard_hook" not in shared_block
    assert "return tool_hooks + [_make_tool_budget_guard_hook(" in src, "must be appended LAST"
    assert "role=role" in src, "role must be bound at construction, not discovered"

    for site in ('_hooks_for(spec.name)', '_hooks_for("Coder")',
                 '_hooks_for("Reviewer")', '_hooks_for("Coordinator")'):
        assert site in src, f"missing bound hook list: {site}"


@pytest.mark.asyncio
async def test_a_bound_role_wins_over_a_missing_agent_object():
    """The exact production shape: agno hands the hook agent=None (identity lives on a
    Function object shared by every agent), so discovery would say "Coordinator" for a
    member's call and charge it to the wrong budget."""
    hook = _make_tool_budget_guard_hook("engineering", role="Executor")

    # Collected, not just the last: the notice fires ONCE (see test_it_fires_once_per_role),
    # so checking only the final call would look like a failure when it worked.
    outs = [await hook("list_processes", _fn, {}, agent=None) for _ in range(30)]
    notices = [o for o in outs if "TOOL BUDGET REACHED" in o]

    assert len(notices) == 1, "should fire exactly once"
    assert "Executor has used" in notices[0], \
        "must count against Executor's budget, not the Coordinator's"
    # Executor's real budget is config's 25, not the Coordinator's 60 — the exact
    # mis-attribution that let a 67-call run sail past the guard.
    assert f"of its {config.tool_call_limit} tool calls" in notices[0]
