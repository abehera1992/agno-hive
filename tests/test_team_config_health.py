"""Tests for the startup config-health diagnostic and the seed relocation.

Motivation (2026-08-21): `_DEFAULT_TOOL_GRANTS` lived in swarm/team_config.py
right beside the live cache, which made it read like editable runtime config.
It is not — it is a first-run seed seen only by a deployment whose
team_role_tools is completely empty. The confusion is not academic: the same
mapping ended up written in two places (the Python literal and ZGX's live
SQLite rows), kept in sync only by someone remembering to do both.

Deleting the seed outright was the obvious fix and is the wrong one, which is
what `test_a_missing_seed_is_reported_as_fail_open` pins: teams/*.yaml had
their `tools:` fields removed on 2026-08-18, so with no seed a fresh clone
grants nothing, and _load_team() reads "nothing" as UNRESTRICTED. The failure
mode of removing the seed is every agent holding apply_diff and run_command,
not every agent holding none.

So the seed stays, moves to seeds/team_config.yaml where its role is legible,
and a startup check reports the states that are actually broken.
"""
import pytest
import yaml

from swarm import team_config

# A real hive-mcp tool that no team grants — standing in for the names a LIVE
# enumeration returns but the seed never mentions, which is the whole signal
# finding 4 relies on. Asserted against the seed so a future seed that starts
# granting it fails loudly here instead of silently weakening these tests.
_NOT_IN_SEED = "index_project"


@pytest.fixture(autouse=True)
def clean_caches():
    """check_config_health() is cache-only; give every test a known cache."""
    team_config._tools_cache.clear()
    team_config._tool_registry_cache.clear()
    yield
    team_config._tools_cache.clear()
    team_config._tool_registry_cache.clear()


def _teams_dir(tmp_path, monkeypatch, **teams):
    """Each team defaults to `coordinator_tools: []` so a test only varies the
    one thing it is about. Every team has a Coordinator, and one left
    unconfigured is legitimately flagged — see
    test_a_team_with_an_unconfigured_coordinator_is_flagged."""
    d = tmp_path / "teams"
    d.mkdir(exist_ok=True)
    for name, data in teams.items():
        data = {"coordinator_tools": [], **data}
        (d / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(team_config, "_TEAMS_DIR", d)
    return d


# ── The seed file itself ──────────────────────────────────────────────────────

def test_the_shipped_seed_is_tracked_and_loadable():
    """The whole point of moving it out of swarm/: it must still SHIP. `data/`
    is gitignored in this repo, so a seed placed there would vanish from a fresh
    clone and silently produce the fail-open state below."""
    assert team_config._SEED_PATH.exists(), f"{team_config._SEED_PATH} missing"
    tools, skills = team_config._load_seed_grants()
    assert len(tools) > 100, "seed lost its tool grants"
    assert skills, "seed lost its skill grants"


def test_the_not_in_seed_stand_in_really_is_absent_from_the_seed():
    """Guards the other tests: if a future seed grants _NOT_IN_SEED, every
    "registry has live-only names" assertion below silently stops testing
    anything, and finding 4 would go unexercised without a single failure."""
    tools, _ = team_config._load_seed_grants()
    assert _NOT_IN_SEED not in {n for (_, _, n) in tools}


def test_seed_covers_every_shipped_team():
    tools, _ = team_config._load_seed_grants()
    assert {t for (t, _, _) in tools} == {
        "engineering", "planning", "parallel-review", "sprint-master",
    }


def test_a_missing_seed_never_raises(tmp_path, monkeypatch):
    """load_cache() runs on the startup path — a broken seed must not make the
    service unbootable. It degrades to empty, which the health check reports."""
    monkeypatch.setattr(team_config, "_SEED_PATH", tmp_path / "nope.yaml")
    assert team_config._load_seed_grants() == (set(), set())


def test_malformed_seed_never_raises(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("tools: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(team_config, "_SEED_PATH", bad)
    assert team_config._load_seed_grants() == (set(), set())


# ── Finding 1: no grants at all ───────────────────────────────────────────────

def test_a_missing_seed_is_reported_as_fail_open():
    """The finding must say what actually happens, not "no tools configured" —
    an operator who reads it as "tools are off" will de-prioritize the one
    condition that hands every agent write and shell access."""
    findings = team_config.check_config_health()

    assert len(findings) == 1
    assert "UNRESTRICTED" in findings[0]
    assert "apply_diff" in findings[0] and "run_command" in findings[0]


def test_empty_grants_outranks_every_other_finding():
    """Severity order is the contract: with no grants at all, per-role findings
    are noise on top of a deployment-wide failure."""
    team_config._tool_registry_cache.add("stale_tool")
    assert len(team_config.check_config_health()) == 1


# ── Finding 2: a role resolving to unrestricted ───────────────────────────────

def test_role_with_no_yaml_tools_and_no_db_rows_is_flagged(tmp_path, monkeypatch):
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [{"name": "Researcher"}]})
    team_config._tools_cache[("other", "Coder")] = {"get_file_content"}
    team_config._tool_registry_cache.add("get_file_content")

    findings = team_config.check_config_health()

    assert any("demo/Researcher resolves to UNRESTRICTED" in f for f in findings)


def test_an_explicitly_empty_yaml_list_is_not_flagged(tmp_path, monkeypatch):
    """`tools: []` is a deliberate disarm, not an omission. Flagging it would
    train the operator to ignore this check — and it is exactly the distinction
    the _load_team() bug fixed the same day got wrong."""
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [{"name": "Researcher", "tools": []}]})
    team_config._tools_cache[("other", "Coder")] = {"get_file_content"}
    team_config._tool_registry_cache.add("get_file_content")

    assert not [f for f in team_config.check_config_health() if "demo/Researcher" in f]


def test_an_empty_coordinator_allowlist_is_not_flagged(tmp_path, monkeypatch):
    """engineering.yaml's real shape since 2026-08-20."""
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [], "coordinator_tools": []})
    team_config._tools_cache[("other", "Coder")] = {"get_file_content"}
    team_config._tool_registry_cache.add("get_file_content")

    assert not [f for f in team_config.check_config_health() if "demo/Coordinator" in f]


def test_a_team_with_an_unconfigured_coordinator_is_flagged(tmp_path, monkeypatch):
    """No coordinator_tools: and no DB rows is the coordinator's own fail-open
    case — the one 2026-08-20's `coordinator_tools: []` was added to close for
    engineering after it answered a db_schema question itself instead of
    delegating."""
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [], "coordinator_tools": None})
    team_config._tools_cache[("other", "Coder")] = {"get_file_content"}
    team_config._tool_registry_cache.add("get_file_content")

    findings = team_config.check_config_health()

    assert any("demo/Coordinator resolves to UNRESTRICTED" in f for f in findings)


def test_a_role_granted_via_db_is_not_flagged(tmp_path, monkeypatch):
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [{"name": "Researcher"}]})
    team_config._tools_cache[("demo", "Researcher")] = {"get_file_content"}
    team_config._tool_registry_cache.update({"get_file_content", _NOT_IN_SEED})

    assert not team_config.check_config_health()


# ── Finding 3: a grant naming an unregistered tool ────────────────────────────

def test_grant_naming_a_tool_absent_from_the_registry_is_flagged(tmp_path, monkeypatch):
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [{"name": "Researcher"}]})
    team_config._tools_cache[("demo", "Researcher")] = {"renamed_away"}
    team_config._tool_registry_cache.add("get_file_content")

    findings = team_config.check_config_health()

    assert any("renamed_away" in f and "tool_registry" in f for f in findings)


# ── Finding 4: a never-refreshed registry ─────────────────────────────────────

def test_registry_holding_only_seed_names_is_flagged(tmp_path, monkeypatch):
    """last_seen_at cannot distinguish seeded from refreshed (it has a
    server_default), so the signal is containment: a live hive-mcp enumeration
    returns tools no team grants, a seed-only registry never does."""
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [{"name": "Researcher"}]})
    seed_tools, _ = team_config._load_seed_grants()
    team_config._tools_cache[("demo", "Researcher")] = {"get_file_content"}
    team_config._tool_registry_cache.update(n for (_, _, n) in seed_tools)

    assert any("never" in f and "refreshed" in f for f in team_config.check_config_health())


def test_a_registry_with_live_only_names_is_not_flagged(tmp_path, monkeypatch):
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [{"name": "Researcher"}]})
    seed_tools, _ = team_config._load_seed_grants()
    team_config._tools_cache[("demo", "Researcher")] = {"get_file_content"}
    team_config._tool_registry_cache.update(n for (_, _, n) in seed_tools)
    team_config._tool_registry_cache.add(_NOT_IN_SEED)

    assert not [f for f in team_config.check_config_health() if "refreshed" in f]


# ── The never-block contract ──────────────────────────────────────────────────

def test_the_check_never_raises(tmp_path, monkeypatch):
    """A diagnostic that can crash startup is worse than the gap it closes —
    check_coordinator_readiness()'s own stated rule, applied here."""
    team_config._tools_cache[("demo", "Researcher")] = {"x"}
    monkeypatch.setattr(team_config, "_TEAMS_DIR", tmp_path / "does-not-exist")

    team_config.check_config_health()   # must not raise

    monkeypatch.setattr(
        team_config, "_load_seed_grants", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    findings = team_config.check_config_health()
    assert any("could not complete" in f for f in findings)


def test_an_unreadable_team_yaml_is_skipped_not_fatal(tmp_path, monkeypatch):
    d = _teams_dir(tmp_path, monkeypatch, good={"agents": [{"name": "Researcher"}]})
    (d / "broken.yaml").write_text("agents: [unclosed\n", encoding="utf-8")
    team_config._tools_cache[("good", "Researcher")] = {"get_file_content"}
    team_config._tool_registry_cache.add("get_file_content")

    assert not [f for f in team_config.check_config_health() if "broken" in f]


def test_the_real_shipped_config_is_clean():
    """The check earns its place only if it is quiet on a correct deployment.
    Run against this repo's actual teams/*.yaml and actual seed — a fresh clone
    seeded from them must report nothing at all. If this starts failing, either
    a team YAML lost a roster entry the seed still covers, or the reverse."""
    seed_tools, _ = team_config._load_seed_grants()
    for (t, r, n) in seed_tools:
        team_config._tools_cache.setdefault((t, r), set()).add(n)
    team_config._tool_registry_cache.update(n for (_, _, n) in seed_tools)
    team_config._tool_registry_cache.add(_NOT_IN_SEED)

    assert team_config.check_config_health() == []


def test_clean_config_reports_nothing(tmp_path, monkeypatch):
    _teams_dir(tmp_path, monkeypatch, demo={"agents": [{"name": "Researcher"}]})
    team_config._tools_cache[("demo", "Researcher")] = {"get_file_content"}
    team_config._tool_registry_cache.update({"get_file_content", _NOT_IN_SEED})

    assert team_config.check_config_health() == []
