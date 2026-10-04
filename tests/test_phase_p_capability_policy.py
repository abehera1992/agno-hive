"""Tests for Phase P's capability-oriented policy layer (swarm/team_config.py's
resolve_effective_policy() and the five new routing tables in swarm/db.py).
Mirrors tests/test_team_config.py's fixture pattern and split-engine isolation
exactly -- these tables live in the SAME routing_metadata, loaded by the SAME
load_cache(), so the SAME in-memory-sqlite + reset_cache_for_tests() fixture
gives correct, hermetic isolation per test with no new infrastructure.
"""
import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db, team_config as tc


@pytest.fixture(autouse=True)
async def _fresh_state(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await tc.reset_cache_for_tests()
    yield


async def _insert_capability(conn, capability_id: str, enabled: bool = True) -> None:
    await conn.execute(db.capabilities.insert().values(
        capability_id=capability_id, description=None, enabled=enabled))


async def _insert_tool_capability(conn, tool_name: str, capability_id: str, enabled: bool = True) -> None:
    await conn.execute(db.tool_capabilities.insert().values(
        tool_name=tool_name, capability_id=capability_id, enabled=enabled))


async def _insert_agent_policy(conn, team: str, role: str, capability_id: str, mode: str,
                                enabled: bool = True) -> None:
    await conn.execute(db.agent_capability_policy.insert().values(
        team_name=team, role_name=role, capability_id=capability_id, mode=mode, enabled=enabled))


async def _insert_task_policy(conn, task_class: str, policy_version: int = 1) -> None:
    await conn.execute(db.task_policies.insert().values(
        task_class=task_class, description=None, enabled=True, policy_version=policy_version))


async def _insert_task_capability(conn, task_class: str, capability_id: str, requirement: str) -> None:
    await conn.execute(db.task_capabilities.insert().values(
        task_class=task_class, capability_id=capability_id, requirement=requirement))


# ── seeding ───────────────────────────────────────────────────────────────────

async def test_load_cache_seeds_the_real_member_forwarding_requirement():
    """The one row this migration ships as a real, evidenced protocol
    requirement -- Phase O.1 live-proved forcing forward_member_answer at the
    member-result-landing point converts the historical bypass into a real
    tool call."""
    await tc.load_cache()
    policy = tc.resolve_effective_policy("engineering", "Coordinator")
    assert "forward_member_answer" in policy.required_tools


async def test_load_cache_does_not_reseed_a_non_empty_capabilities_table():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        before = (await conn.execute(sa.select(db.capabilities))).mappings().all()
        await _insert_capability(conn, "a.custom.capability")
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        after = (await conn.execute(sa.select(db.capabilities))).mappings().all()
    assert len(after) == len(before) + 1


async def test_seed_runs_even_though_team_role_tools_is_already_populated():
    """The seed check for capabilities must be INDEPENDENT of team_role_tools'
    own emptiness check -- see load_cache()'s own comment for why a shared
    check would mean this feature never activates on an already-deployed
    instance. Simulate that: populate team_role_tools FIRST (as every real
    deployment already has), then confirm the capability seed still runs."""
    async with db.get_routing_engine().begin() as conn:
        await db.ensure_routing_schema()
        await conn.execute(db.team_role_tools.insert().values(
            team_name="engineering", role_name="Coder", tool_name="get_file_content"))
    await tc.load_cache()
    policy = tc.resolve_effective_policy("engineering", "Coordinator")
    assert "forward_member_answer" in policy.required_tools


# ── backward compatibility (the central invariant) ──────────────────────────

async def test_unknown_team_role_resolves_to_the_empty_policy():
    await tc.load_cache()
    policy = tc.resolve_effective_policy("some-other-team", "Researcher")
    assert policy.allowed_tools == frozenset()
    assert policy.required_tools == frozenset()
    assert policy.forbidden_tools == frozenset()


async def test_no_task_class_given_ignores_task_level_policy_entirely():
    await tc.load_cache()
    policy = tc.resolve_effective_policy("engineering", "Coordinator", task_class=None)
    # member.forwarding is still required (agent-level), task-level simply never consulted
    assert "forward_member_answer" in policy.required_tools
    assert policy.policy_version is None


# ── precedence (Phase P section 9) ───────────────────────────────────────────

async def test_forbidden_wins_over_allowed_across_agent_and_task_layers():
    """Spec's own worked example: one layer says ALLOWED, the other says
    FORBIDDEN for the SAME capability -- FORBIDDEN must win, at both the
    allowed_tools and required_tools level."""
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await _insert_capability(conn, "test.cap")
        await _insert_tool_capability(conn, "risky_tool", "test.cap")
        await _insert_agent_policy(conn, "t1", "Coordinator", "test.cap", "ALLOWED")
        await _insert_task_policy(conn, "risky_task")
        await _insert_task_capability(conn, "risky_task", "test.cap", "FORBIDDEN")
    await tc.load_cache()
    policy = tc.resolve_effective_policy("t1", "Coordinator", task_class="risky_task")
    assert "risky_tool" not in policy.allowed_tools
    assert "risky_tool" not in policy.required_tools
    assert "risky_tool" in policy.forbidden_tools


async def test_forbidden_wins_over_required():
    """FORBIDDEN must beat REQUIRED too, not just ALLOWED -- security
    restrictions are never weakened by a lower-level override (spec section 9),
    regardless of which mode is trying to override it."""
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await _insert_capability(conn, "test.cap")
        await _insert_tool_capability(conn, "risky_tool", "test.cap")
        await _insert_agent_policy(conn, "t1", "Coordinator", "test.cap", "REQUIRED")
        await _insert_task_policy(conn, "risky_task")
        await _insert_task_capability(conn, "risky_task", "test.cap", "FORBIDDEN")
    await tc.load_cache()
    policy = tc.resolve_effective_policy("t1", "Coordinator", task_class="risky_task")
    assert "risky_tool" not in policy.required_tools
    assert "risky_tool" in policy.forbidden_tools


async def test_agent_allowed_plus_task_required_becomes_required():
    """Spec's own worked example: Agent=ALLOWED, Task=REQUIRED -> the
    capability becomes required for this (team, role, task_class)."""
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await _insert_capability(conn, "test.cap")
        await _insert_tool_capability(conn, "some_tool", "test.cap")
        await _insert_agent_policy(conn, "t1", "Coordinator", "test.cap", "ALLOWED")
        await _insert_task_policy(conn, "needs_it")
        await _insert_task_capability(conn, "needs_it", "test.cap", "REQUIRED")
    await tc.load_cache()
    policy = tc.resolve_effective_policy("t1", "Coordinator", task_class="needs_it")
    assert "some_tool" in policy.required_tools
    assert "some_tool" in policy.allowed_tools  # required tools are always a subset of allowed


async def test_disabled_capability_contributes_no_tools_even_if_policy_requires_it():
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await _insert_capability(conn, "test.cap", enabled=False)
        await _insert_tool_capability(conn, "some_tool", "test.cap")
        await _insert_agent_policy(conn, "t1", "Coordinator", "test.cap", "REQUIRED")
    await tc.load_cache()
    policy = tc.resolve_effective_policy("t1", "Coordinator")
    assert "some_tool" not in policy.required_tools
    assert "some_tool" not in policy.allowed_tools


async def test_disabled_tool_capability_row_does_not_contribute_that_tool():
    """enabled=False on the tool_capabilities row itself (not the capability)
    excludes just that one tool<->capability edge, same contract."""
    await tc.load_cache()
    async with db.get_routing_engine().begin() as conn:
        await _insert_capability(conn, "test.cap")
        await _insert_tool_capability(conn, "tool_a", "test.cap", enabled=True)
        await _insert_tool_capability(conn, "tool_b", "test.cap", enabled=False)
        await _insert_agent_policy(conn, "t1", "Coordinator", "test.cap", "ALLOWED")
    await tc.load_cache()
    policy = tc.resolve_effective_policy("t1", "Coordinator")
    assert "tool_a" in policy.allowed_tools
    assert "tool_b" not in policy.allowed_tools


# ── determinism / purity (spec section 19's "same input -> same output") ───

def test_resolver_is_synchronous_and_pure_no_event_loop_needed():
    """resolve_effective_policy() must be callable with zero async machinery --
    it reads caches only. This test function is deliberately NOT async: if it
    needed a running event loop or DB connection, this call would fail before
    the assertion even runs."""
    policy_a = tc.resolve_effective_policy("nonexistent", "Nobody")
    policy_b = tc.resolve_effective_policy("nonexistent", "Nobody")
    assert policy_a == policy_b


async def test_two_resolutions_for_different_roles_do_not_affect_each_other():
    """Concurrency-safety-by-construction (spec section 10/19's 'Run A policy
    != Run B policy'): resolve_effective_policy() returns a fresh, frozen
    EffectivePolicy per call -- nothing mutates shared state, so resolving for
    one (team, role) cannot leak into or be affected by resolving for another,
    even back-to-back in the same process."""
    await tc.load_cache()
    policy_coordinator = tc.resolve_effective_policy("engineering", "Coordinator")
    policy_researcher = tc.resolve_effective_policy("engineering", "Researcher")
    assert "forward_member_answer" in policy_coordinator.required_tools
    assert "forward_member_answer" not in policy_researcher.required_tools
    assert policy_researcher.required_tools == frozenset()
