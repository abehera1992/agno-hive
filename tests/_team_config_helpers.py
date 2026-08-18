"""Shared test helper for AGNOHive 2.3.3 (2026-08-18): resolves a role's
effective tools/skills the same way api/server.py's _load_team() does, so
tests that assert on "does this role have tool X" check runtime truth
instead of a raw YAML field. All 4 shipped teams/*.yaml had their
tools:/skills: fields removed in favor of the DB as the actual runtime
source — see swarm/team_config.py's _DEFAULT_TOOL_GRANTS/_DEFAULT_SKILL_GRANTS.

Named with a leading underscore (not conftest.py) so pytest never tries to
auto-load it as a fixture provider — it's a plain importable module."""


def effective_tools(team_name: str, role_name: str, yaml_tools) -> set[str]:
    if yaml_tools:
        return set(yaml_tools)
    from swarm.team_config import _DEFAULT_TOOL_GRANTS
    return {n for (t, r, n) in _DEFAULT_TOOL_GRANTS if t == team_name and r == role_name}


def effective_skills(team_name: str, role_name: str, yaml_skills) -> set[str]:
    if yaml_skills:
        return set(yaml_skills)
    from swarm.team_config import _DEFAULT_SKILL_GRANTS
    return {n for (t, r, n) in _DEFAULT_SKILL_GRANTS if t == team_name and r == role_name}
