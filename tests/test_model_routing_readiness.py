"""Tests for swarm/model_routing.py's check_coordinator_readiness() -- a
non-blocking startup diagnostic added 2026-08-16 after a real onboarding gap
was found: nothing in this codebase ever verified that a resolved model_id's
backend (Ollama or the vLLM/LiteLLM gateway) is actually reachable. A brand-new
user cloning the repo gets a silently, unconditionally auto-seeded
model_catalog regardless of whether Ollama/vLLM exist on their machine at all,
and the first real signal was previously a raw connection error deep inside
agno's model client mid-task.

Mocks only the HTTP layer (httpx.AsyncClient) -- DB state is real, via the
same in-memory-SQLite fixture pattern as tests/test_model_routing.py.
"""
import httpx
import pytest

from config.config import config
from swarm import db, model_routing as mr


@pytest.fixture(autouse=True)
async def _fresh_state(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    monkeypatch.setattr(config, "model_routing_database_url", "sqlite+aiosqlite:///:memory:")
    await db.reset_engine_for_tests()
    await mr.reset_cache_for_tests()
    yield


class _FakeResponse:
    def __init__(self, json_body: dict, status_code: int = 200):
        self._json = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient. `get_impl`
    is either a canned response/exception, or a callable(url) -> response for
    tests that need to branch on which endpoint was hit."""

    def __init__(self, get_impl, **kwargs):
        self._get_impl = get_impl

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *args, **kwargs):
        if callable(self._get_impl):
            return self._get_impl(url)
        if isinstance(self._get_impl, Exception):
            raise self._get_impl
        return self._get_impl


def _patch_client(monkeypatch, get_impl):
    monkeypatch.setattr(mr.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(get_impl, **kw))


async def _seed_coordinator(model_id: str, kind="local", provider="local",
                             vllm_served_as=None, active=True, team="engineering"):
    await db.ensure_routing_schema()
    async with db.get_routing_engine().begin() as conn:
        await conn.execute(db.model_catalog.insert().values(
            model_id=model_id, kind=kind, provider=provider,
            vllm_served_as=vllm_served_as, requires_cloud_gate=(kind == "cloud"), active=active,
        ))
        await conn.execute(db.team_role_models.insert().values(
            team_name=team, role_name="Coordinator", model_id=model_id,
        ))
    await mr.reload()


# ── early-exit cases: no HTTP call should even matter ────────────────────────

async def test_no_team_role_row_returns_none():
    await mr.ensure_cache_loaded()  # seeds real defaults, but "no-such-team" has none
    assert await mr.check_coordinator_readiness("no-such-team") is None


async def test_inactive_route_returns_none(monkeypatch):
    await _seed_coordinator("dead-model", active=False)
    assert await mr.check_coordinator_readiness("engineering") is None


async def test_cloud_route_returns_none_without_any_http_call(monkeypatch):
    def _fail_if_called(url):
        raise AssertionError(f"should never make an HTTP call for a cloud route, got {url}")
    _patch_client(monkeypatch, _fail_if_called)
    await _seed_coordinator("claude-sonnet-cloud", kind="cloud", provider="anthropic")
    assert await mr.check_coordinator_readiness("engineering") is None


# ── ollama backend ────────────────────────────────────────────────────────────

async def test_ollama_unreachable_returns_actionable_message(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "ollama")
    monkeypatch.setattr(config, "ollama_host", "http://localhost:11434")
    _patch_client(monkeypatch, httpx.ConnectError("refused"))
    await _seed_coordinator("qwen3-coder:30b")

    msg = await mr.check_coordinator_readiness("engineering")

    assert msg is not None
    assert "Ollama isn't reachable" in msg
    assert "qwen3-coder:30b" in msg
    assert "ollama pull qwen3-coder:30b" in msg


async def test_ollama_reachable_but_model_not_pulled(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "ollama")
    monkeypatch.setattr(config, "ollama_host", "http://localhost:11434")
    _patch_client(monkeypatch, _FakeResponse({"models": [{"name": "some-other-model:latest"}]}))
    await _seed_coordinator("qwen3-coder:30b")

    msg = await mr.check_coordinator_readiness("engineering")

    assert msg is not None
    assert "doesn't have `qwen3-coder:30b` pulled" in msg
    assert "ollama pull qwen3-coder:30b" in msg


async def test_ollama_reachable_and_model_pulled_returns_none(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "ollama")
    monkeypatch.setattr(config, "ollama_host", "http://localhost:11434")
    _patch_client(monkeypatch, _FakeResponse({"models": [{"name": "qwen3-coder:30b"}]}))
    await _seed_coordinator("qwen3-coder:30b")

    assert await mr.check_coordinator_readiness("engineering") is None


async def test_ollama_tag_suffix_variant_still_matches(monkeypatch):
    """Ollama often reports a pulled model with a tag suffix -- a prefix match
    must still recognize it, not false-positive "not pulled"."""
    monkeypatch.setattr(config, "inference_backend", "ollama")
    monkeypatch.setattr(config, "ollama_host", "http://localhost:11434")
    _patch_client(monkeypatch, _FakeResponse({"models": [{"name": "qwen3-coder:30b-instruct-fp16"}]}))
    await _seed_coordinator("qwen3-coder:30b")

    assert await mr.check_coordinator_readiness("engineering") is None


async def test_ollama_host_defaults_when_config_empty(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "ollama")
    monkeypatch.setattr(config, "ollama_host", "")
    seen_urls = []

    def _capture(url):
        seen_urls.append(url)
        return _FakeResponse({"models": [{"name": "qwen3-coder:30b"}]})

    _patch_client(monkeypatch, _capture)
    await _seed_coordinator("qwen3-coder:30b")

    await mr.check_coordinator_readiness("engineering")
    assert seen_urls == ["http://localhost:11434/api/tags"]


# ── vllm backend ──────────────────────────────────────────────────────────────

async def test_vllm_gateway_unreachable_returns_actionable_message(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://localhost:4000/v1")
    _patch_client(monkeypatch, httpx.ConnectError("refused"))
    await _seed_coordinator("qwen3-coder:30b", vllm_served_as="local-shared")

    msg = await mr.check_coordinator_readiness("engineering")

    assert msg is not None
    assert "vLLM/LiteLLM gateway isn't reachable" in msg
    assert "docker compose -f zgx-ai-setup/docker-compose.yml up -d" in msg


async def test_vllm_reachable_but_alias_not_served(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://localhost:4000/v1")
    _patch_client(monkeypatch, _FakeResponse({"data": [{"id": "some-other-alias"}]}))
    await _seed_coordinator("qwen3-coder:30b", vllm_served_as="local-shared")

    msg = await mr.check_coordinator_readiness("engineering")

    assert msg is not None
    assert "doesn't know about `local-shared`" in msg


async def test_vllm_reachable_and_alias_served_returns_none(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "vllm")
    monkeypatch.setattr(config, "vllm_gateway_url", "http://localhost:4000/v1")
    _patch_client(monkeypatch, _FakeResponse({"data": [{"id": "local-shared"}]}))
    await _seed_coordinator("qwen3-coder:30b", vllm_served_as="local-shared")

    assert await mr.check_coordinator_readiness("engineering") is None


# ── never raises ──────────────────────────────────────────────────────────────

async def test_missing_models_key_reports_not_pulled_instead_of_crashing(monkeypatch):
    """A response body missing the expected "models" key must degrade
    gracefully (.get("models", []) -> empty -> "not pulled"), not crash --
    this is a diagnostic, it must never be able to take down startup or
    block a task."""
    monkeypatch.setattr(config, "inference_backend", "ollama")
    monkeypatch.setattr(config, "ollama_host", "http://localhost:11434")
    _patch_client(monkeypatch, _FakeResponse({"unexpected": "shape, no models key"}))
    await _seed_coordinator("qwen3-coder:30b")

    msg = await mr.check_coordinator_readiness("engineering")
    assert msg is not None
    assert "doesn't have `qwen3-coder:30b` pulled" in msg


async def test_client_raising_an_unexpected_exception_type_returns_none(monkeypatch):
    """Anything NOT an httpx.HTTPError (e.g. a bug in the fake transport, or a
    genuinely unexpected error class) must still be swallowed by the outer
    try/except -- the function's hard guarantee is "never raises," not just
    "handles the errors I anticipated"."""
    monkeypatch.setattr(config, "inference_backend", "ollama")
    monkeypatch.setattr(config, "ollama_host", "http://localhost:11434")
    _patch_client(monkeypatch, ValueError("something unrelated broke"))
    await _seed_coordinator("qwen3-coder:30b")

    assert await mr.check_coordinator_readiness("engineering") is None


async def test_unknown_backend_value_returns_none(monkeypatch):
    monkeypatch.setattr(config, "inference_backend", "some-future-backend")
    await _seed_coordinator("qwen3-coder:30b")

    assert await mr.check_coordinator_readiness("engineering") is None
