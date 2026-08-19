"""Regression tests: _verify_claims / _fetch_skill_catalog / _fill_count_markers
each open their OWN separate, one-shot streamablehttp_client(hive_mcp_url)
session -- distinct from the agent's persistent MCPTools instance _MCP_TIMEOUT
already protects. Confirmed live 2026-08-19 (T6 follow-up, task koi6p1bkd): a
py-spy dump mid-run showed the MainThread idle in select() -- the exact same
"waiting on a socket that will never deliver another byte" signature as the
original T6 hang -- during what turned out to be a verify_claims-shaped quiet
stretch. That specific run went on to complete successfully (one idle snapshot
proves nothing was running at that instant, not that it was stuck forever), but
the underlying gap is real regardless: none of these three functions had any
defense against a genuine hang before this fix, protected only by a broad
`except Exception` a hung await never reaches. These tests confirm
asyncio.wait_for(..., timeout=_BESPOKE_MCP_SESSION_TIMEOUT) actually cuts a
stuck session off instead of hanging forever, and that a normal, fast session
is unaffected.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from swarm import team


class _FakeToolResult:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(text=text)]


class _FakeStreamCtx:
    async def __aenter__(self):
        return (None, None, None)

    async def __aexit__(self, *exc):
        return False


class _HangingSession:
    """initialize() never returns within any reasonable test window -- simulates
    the live py-spy finding: MainThread idle in select(), nothing to time it out
    from inside the hung coroutine itself."""

    async def initialize(self):
        await asyncio.sleep(10)

    async def call_tool(self, name, args):
        raise AssertionError("must never reach call_tool -- initialize() itself hangs")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FastSession:
    def __init__(self, tool_result_text: str):
        self._text = tool_result_text
        self.called_with: tuple | None = None

    async def initialize(self):
        return None

    async def call_tool(self, name: str, args: dict):
        self.called_with = (name, args)
        return _FakeToolResult(self._text)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch):
    """Every test in this file exercises the real asyncio.wait_for code path --
    lowering the timeout to a few ms keeps the hanging-session tests fast
    instead of actually waiting out the real 90s production value."""
    monkeypatch.setattr(team, "_BESPOKE_MCP_SESSION_TIMEOUT", 0.05)


# ── _verify_claims ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_claims_times_out_instead_of_hanging_forever(monkeypatch):
    monkeypatch.setattr("mcp.ClientSession", lambda *a, **k: _HangingSession())
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", lambda url: _FakeStreamCtx())

    report, bad = await team._verify_claims("some answer text", "http://fake/mcp")

    assert report == ""
    assert bad is False  # degrades to "no problems found" per the function's own contract


@pytest.mark.asyncio
async def test_verify_claims_still_works_normally_within_the_timeout(monkeypatch):
    session = _FastSession("VERDICT: all claims verified")
    monkeypatch.setattr("mcp.ClientSession", lambda *a, **k: session)
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", lambda url: _FakeStreamCtx())

    report, bad = await team._verify_claims("some answer text", "http://fake/mcp")

    assert report == "VERDICT: all claims verified"
    assert bad is False
    assert session.called_with == ("verify_claims", {"answer": "some answer text"})


# ── _fetch_skill_catalog ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_skill_catalog_times_out_instead_of_hanging_forever(monkeypatch):
    monkeypatch.setattr("mcp.ClientSession", lambda *a, **k: _HangingSession())
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", lambda url: _FakeStreamCtx())

    assert await team._fetch_skill_catalog("http://fake/mcp") == []


@pytest.mark.asyncio
async def test_fetch_skill_catalog_still_works_normally_within_the_timeout(monkeypatch):
    catalog_json = json.dumps([{"name": "db-facts", "description": "x", "source": "hive-mcp"}])
    session = _FastSession(catalog_json)
    monkeypatch.setattr("mcp.ClientSession", lambda *a, **k: session)
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", lambda url: _FakeStreamCtx())

    result = await team._fetch_skill_catalog("http://fake/mcp")

    assert result == [{"name": "db-facts", "description": "x", "source": "hive-mcp"}]


# ── _fill_count_markers ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fill_count_markers_times_out_instead_of_hanging_forever(monkeypatch):
    monkeypatch.setattr("mcp.ClientSession", lambda *a, **k: _HangingSession())
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", lambda url: _FakeStreamCtx())

    content = "There are [[COUNT pattern=`foo` glob=`**/*.py`]] matches."
    result = await team._fill_count_markers(content, "http://fake/mcp")

    assert "[count unavailable]" in result
    assert "[[COUNT" not in result  # marker was replaced, not left dangling


@pytest.mark.asyncio
async def test_fill_count_markers_still_works_normally_within_the_timeout(monkeypatch):
    session = _FastSession("TOTAL: 7")
    monkeypatch.setattr("mcp.ClientSession", lambda *a, **k: session)
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", lambda url: _FakeStreamCtx())

    content = "There are [[COUNT pattern=`foo` glob=`**/*.py`]] matches."
    result = await team._fill_count_markers(content, "http://fake/mcp")

    assert result == "There are 7 matches."
