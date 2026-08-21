"""Tests: tool_registry stays current from a swarm run's own live MCP enumeration.

Motivation (2026-08-21): the startup health check added the same day reported, on
real ZGX, that `tool_registry` held only names from the first-run seed — it had
never been refreshed from a live enumeration, because the only mechanism to do so
was an admin endpoint nobody calls. Practical consequence: granting a genuinely
real hive-mcp tool that no team happens to use yet (`search_files_batch`,
`index_project`) returns 400 "unregistered".

The fix harvests rather than pushes. `swarm/team.py`'s connect loop already holds
`MCPTools.functions` — the live list, keyed by name — and used it only to print a
tool count. Harvesting there records what the swarm actually REACHED, which is the
property the registry needs: it validates grants, and a grant is only meaningful
if the tool is reachable. hive-mcp self-reporting at its own bootstrap would
instead record what it HAS, which stays true right up until it isn't reachable.
"""
import pytest

from swarm import team, team_config


@pytest.fixture(autouse=True)
def clean_registry():
    team_config._tool_registry_cache.clear()
    team_config._skill_registry_cache.clear()
    yield
    team_config._tool_registry_cache.clear()
    team_config._skill_registry_cache.clear()


@pytest.fixture
def spy_refresh(monkeypatch):
    """Records refresh_registry() calls without touching a database."""
    calls = []

    async def fake(tool_names, skill_names):
        calls.append((list(tool_names), list(skill_names)))

    monkeypatch.setattr(team_config, "refresh_registry", fake)
    return calls


class _Mcp:
    """Stands in for agno's MCPTools — only `.functions` is read, and only its keys."""
    def __init__(self, *names):
        self.functions = {n: object() for n in names}


# ── sync_registry_from_live ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_tool_triggers_a_refresh(spy_refresh):
    team_config._tool_registry_cache.update({"get_file_content"})

    added = await team_config.sync_registry_from_live(
        ["get_file_content", "search_files_batch"], []
    )

    assert added == {"tools_added": ["search_files_batch"], "skills_added": []}
    assert "search_files_batch" in team_config._tool_registry_cache


@pytest.mark.asyncio
async def test_nothing_new_writes_nothing(spy_refresh):
    """The common path — every run, forever. It must not touch the DB."""
    team_config._tool_registry_cache.update({"get_file_content", "search_files"})

    added = await team_config.sync_registry_from_live(
        ["get_file_content", "search_files"], []
    )

    assert added is None
    assert spy_refresh == []


@pytest.mark.asyncio
async def test_a_narrower_enumeration_never_removes_anything(spy_refresh):
    """A run connected to fewer servers than usual must not de-register a real
    tool. refresh_registry() upserts for exactly this reason; sync only ever adds."""
    team_config._tool_registry_cache.update({"get_file_content", "apply_diff", "run_command"})

    added = await team_config.sync_registry_from_live(["get_file_content"], [])

    assert added is None
    assert {"apply_diff", "run_command"} <= team_config._tool_registry_cache
    assert spy_refresh == []


@pytest.mark.asyncio
async def test_the_full_list_is_upserted_when_something_changed(spy_refresh):
    """Only the NEW names are reported, but the FULL list goes to the DB — so
    last_seen_at is refreshed across the board exactly when the surface changed,
    and never on the common path."""
    team_config._tool_registry_cache.update({"get_file_content"})

    await team_config.sync_registry_from_live(["get_file_content", "new_tool"], [])

    assert len(spy_refresh) == 1
    assert spy_refresh[0][0] == ["get_file_content", "new_tool"]


@pytest.mark.asyncio
async def test_new_skills_are_synced_too(spy_refresh):
    team_config._skill_registry_cache.update({"verification-discipline"})

    added = await team_config.sync_registry_from_live([], ["verification-discipline", "brand-new"])

    assert added["skills_added"] == ["brand-new"]
    assert "brand-new" in team_config._skill_registry_cache


# ── _sync_tool_registry (the team.py caller) ──────────────────────────────────

@pytest.mark.asyncio
async def test_names_are_unioned_across_every_connected_server(spy_refresh):
    await team._sync_tool_registry(
        [_Mcp("get_file_content", "apply_diff"), _Mcp("get_context_section")], None
    )

    assert team_config._tool_registry_cache == {
        "get_file_content", "apply_diff", "get_context_section",
    }


@pytest.mark.asyncio
async def test_skill_catalog_entries_are_read_by_name(spy_refresh):
    await team._sync_tool_registry(
        [_Mcp("get_file_content")],
        [{"name": "verification-discipline", "description": "..."}],
    )

    assert team_config._skill_registry_cache == {"verification-discipline"}


@pytest.mark.asyncio
async def test_a_malformed_skill_entry_is_skipped_not_fatal(spy_refresh):
    await team._sync_tool_registry(
        [_Mcp("get_file_content")], [{"description": "no name key"}, {"name": "ok"}]
    )

    assert team_config._skill_registry_cache == {"ok"}


@pytest.mark.asyncio
async def test_no_skill_catalog_is_fine(spy_refresh):
    """_fetch_skill_catalog returns [] rather than raising when hive-mcp is
    unreachable — that must not stop the tool half from syncing."""
    await team._sync_tool_registry([_Mcp("get_file_content")], [])

    assert team_config._tool_registry_cache == {"get_file_content"}


@pytest.mark.asyncio
async def test_a_db_failure_never_propagates(monkeypatch, capsys):
    """The registry is write-time grant validation, not something a task depends
    on. A failure here must not fail a run that was otherwise fine."""
    async def boom(*a):
        raise RuntimeError("db is down")

    monkeypatch.setattr(team_config, "sync_registry_from_live", boom)

    await team._sync_tool_registry([_Mcp("get_file_content")], None)   # must not raise

    assert "sync skipped" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_server_with_no_tools_is_harmless(spy_refresh):
    await team._sync_tool_registry([_Mcp()], None)

    assert team_config._tool_registry_cache == set()
    assert spy_refresh == []


@pytest.mark.asyncio
async def test_the_sync_reports_what_it_added(spy_refresh, capsys):
    await team._sync_tool_registry([_Mcp("brand_new_tool")], None)

    out = capsys.readouterr().out
    assert "[registry] synced from live MCP" in out
    assert "brand_new_tool" in out


@pytest.mark.asyncio
async def test_a_no_op_sync_prints_nothing(spy_refresh, capsys):
    """Every run hits this path; it must stay silent or the log becomes noise."""
    team_config._tool_registry_cache.add("get_file_content")

    await team._sync_tool_registry([_Mcp("get_file_content")], None)

    assert "[registry]" not in capsys.readouterr().out
