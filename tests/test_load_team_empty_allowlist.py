"""Regression tests: in _load_team(), an explicitly EMPTY allowlist must win.

Found 2026-08-21 while auditing the seed/DB drift question. `_load_team()`
resolved both the per-agent `tools:` and the team's `coordinator_tools:` with a
truthiness check (`if not coordinator_tools:`), which cannot tell an explicit
`[]` from an absent field. Absent means "fall back to the DB"; `[]` means "hold
nothing, deliberately". Conflating them means the DB silently overrides a
deliberate disarm — no error, no log line, the role just gets its tools back.

This was live-bearing, not hypothetical. engineering.yaml's `coordinator_tools:
[]` (added 2026-08-20 to stop the coordinator answering db_schema questions
itself instead of delegating) is falsy, so it fell straight through to the DB
lookup on every single team load. It survived only because no (engineering,
Coordinator) rows happen to exist. One admin POST, or a re-seed that included
that pair, would have re-armed the coordinator and quietly undone the fix.

Same empty-vs-absent conflation `_scope_coordinator_tools` needed an early
return for (tests/test_coordinator_no_direct_tools.py) — one layer up, missed
at the time.
"""
import pytest
import yaml

from swarm import team_config


@pytest.fixture
def load_team(tmp_path, monkeypatch):
    """_load_team() against a throwaway teams dir and a controlled grant cache."""
    from api import server

    d = tmp_path / "teams"
    d.mkdir()
    monkeypatch.setattr(server, "_TEAMS_DIR", d)
    team_config._tools_cache.clear()
    team_config._skills_cache.clear()

    def run(yaml_data, grants=None):
        (d / "demo.yaml").write_text(yaml.safe_dump(yaml_data), encoding="utf-8")
        for (role, tools) in (grants or {}).items():
            team_config._tools_cache[("demo", role)] = set(tools)
        return server._load_team("demo")

    yield run
    team_config._tools_cache.clear()
    team_config._skills_cache.clear()


def _agent(**kw):
    return {"name": "Researcher", "model": "m", "role": "r", "instructions": ["i"], **kw}


# ── coordinator_tools ─────────────────────────────────────────────────────────

def test_empty_coordinator_tools_is_not_overridden_by_db_rows(load_team):
    """The live bug: `coordinator_tools: []` plus DB rows re-arms the coordinator."""
    _, _, _, coordinator_tools = load_team(
        {"agents": [_agent()], "coordinator_tools": []},
        grants={"Coordinator": ["db_schema", "get_file_content"]},
    )

    assert coordinator_tools == []


def test_absent_coordinator_tools_still_falls_back_to_db(load_team):
    """The behavior that must NOT change — planning/parallel-review/sprint-master
    all rely on it, having had the field removed from their YAML in 2026-08-18's
    migration."""
    _, _, _, coordinator_tools = load_team(
        {"agents": [_agent()]},
        grants={"Coordinator": ["notion_search"]},
    )

    assert coordinator_tools == ["notion_search"]


def test_absent_coordinator_tools_with_no_db_rows_stays_unrestricted(load_team):
    """None, not [] — a role nobody ever configured keeps seeing everything.
    Fail-open, deliberately preserved here (check_config_health() is what makes
    it visible rather than silent)."""
    _, _, _, coordinator_tools = load_team({"agents": [_agent()]})

    assert coordinator_tools is None


def test_a_nonempty_yaml_coordinator_list_still_wins(load_team):
    _, _, _, coordinator_tools = load_team(
        {"agents": [_agent()], "coordinator_tools": ["notion_search"]},
        grants={"Coordinator": ["db_schema"]},
    )

    assert coordinator_tools == ["notion_search"]


# ── per-agent tools ───────────────────────────────────────────────────────────

def test_empty_agent_tools_is_not_overridden_by_db_rows(load_team):
    agents, _, _, _ = load_team(
        {"agents": [_agent(tools=[])]},
        grants={"Researcher": ["apply_diff", "run_command"]},
    )

    assert agents[0].tools == []


def test_absent_agent_tools_still_falls_back_to_db(load_team):
    agents, _, _, _ = load_team(
        {"agents": [_agent()]},
        grants={"Researcher": ["get_file_content"]},
    )

    assert agents[0].tools == ["get_file_content"]


def test_empty_agent_skills_is_not_overridden_by_db_rows(load_team):
    team_config._skills_cache[("demo", "Researcher")] = {"verification-discipline"}

    agents, _, _, _ = load_team({"agents": [_agent(skills=[])]})

    assert agents[0].skills == []


def test_engineering_coordinator_allowlist_survives_a_db_grant(monkeypatch):
    """End-to-end on the real YAML: the disarm holds even if someone grants
    (engineering, Coordinator) rows tomorrow. Without the `is None` fix this
    returns the granted list instead of [].

    engineering.yaml omits every agent's model:, resolving it from
    team_role_models at load time — stubbed here so this stays a test about the
    allowlist rather than about model routing."""
    from api import server
    from swarm import model_routing

    monkeypatch.setattr(model_routing, "get_default_model", lambda *a: "stub-model")
    team_config._tools_cache[("engineering", "Coordinator")] = {"db_schema"}
    try:
        _, _, _, coordinator_tools = server._load_team("engineering")
        assert coordinator_tools == []
    finally:
        team_config._tools_cache.pop(("engineering", "Coordinator"), None)
