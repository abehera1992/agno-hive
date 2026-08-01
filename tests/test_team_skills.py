import json
from types import SimpleNamespace

import pytest

from swarm import team


class _FakeToolResult:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(text=text)]


class _FakeSession:
    def __init__(self, catalog_json: str):
        self._catalog_json = catalog_json

    async def initialize(self):
        return None

    async def call_tool(self, name: str, args: dict):
        assert name == "list_skills"
        return _FakeToolResult(self._catalog_json)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeStreamCtx:
    async def __aenter__(self):
        return (None, None, None)

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_fetch_skill_catalog_parses_list_skills_response(monkeypatch):
    catalog_json = json.dumps([{"name": "db-facts", "description": "x", "source": "hive-mcp"}])
    fake_session = _FakeSession(catalog_json)
    # _fetch_skill_catalog does a LOCAL `from mcp import ClientSession` inside the
    # function body (mirroring _verify_claims), so patching team.ClientSession would
    # have no effect — the local import always resolves fresh from the mcp package
    # itself at call time. Patch the real source attributes instead.
    monkeypatch.setattr("mcp.ClientSession", lambda *a, **k: fake_session)
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", lambda url: _FakeStreamCtx())

    result = await team._fetch_skill_catalog("http://fake/mcp")

    assert result == [{"name": "db-facts", "description": "x", "source": "hive-mcp"}]


@pytest.mark.asyncio
async def test_fetch_skill_catalog_returns_empty_list_when_no_url():
    assert await team._fetch_skill_catalog(None) == []


@pytest.mark.asyncio
async def test_fetch_skill_catalog_returns_empty_list_on_connection_failure(monkeypatch):
    def _raise(url):
        raise ConnectionError("no server")
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", _raise)

    assert await team._fetch_skill_catalog("http://fake/mcp") == []
