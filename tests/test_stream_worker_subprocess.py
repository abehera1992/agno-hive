"""Tests for api.server._stream_worker_subprocess -- Phase 3 of process-boundary
cancellation (see DOCS.md "Process-Boundary Cancellation"). Uses
tests/fixtures/fake_stream_worker.py instead of the real
`python main.py --stream-worker` so these run without the actual agno/MCP/vLLM
stack. Mirrors tests/test_run_worker_subprocess.py's structure -- see that
file's own docstring for why this design exists (four rounds of cooperative
cancellation each closing one way cancellation could land wrong, each followed
by a new one).
"""
import asyncio
import sys
from pathlib import Path

import pytest

from api.server import _stream_worker_subprocess

_FIXTURE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_stream_worker.py")]


class _FakeHTTPRequest:
    """Minimal stand-in for a Starlette Request -- only is_disconnected() is used."""

    def __init__(self, disconnected_after: int | None = None):
        self.calls = 0
        self._disconnected_after = disconnected_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        if self._disconnected_after is None:
            return False
        return self.calls > self._disconnected_after


async def _collect(agen, limit: float = 10.0) -> list:
    async def _gather():
        return [chunk async for chunk in agen]
    return await asyncio.wait_for(_gather(), timeout=limit)


@pytest.mark.asyncio
async def test_success_yields_every_chunk_in_order_str_and_dict_both():
    http_request = _FakeHTTPRequest()
    payload = {"task": "do the thing", "_test_mode": "success"}

    chunks = await _collect(_stream_worker_subprocess(http_request, payload, argv=_FIXTURE))

    assert chunks == [
        "echo: do the thing",
        {"__tool_event__": "start", "name": "search_files", "args": {}},
        {"__done__": True, "content": "final answer", "tokens": {"total_tokens": 5}, "clarification": None},
    ]


@pytest.mark.asyncio
async def test_mid_stream_error_raises_runtimeerror_after_the_partial_chunks():
    http_request = _FakeHTTPRequest()
    payload = {"task": "x", "_test_mode": "error"}

    received = []
    with pytest.raises(RuntimeError, match="simulated mid-stream failure"):
        async for chunk in _stream_worker_subprocess(http_request, payload, argv=_FIXTURE):
            received.append(chunk)

    assert received == ["partial chunk"]


@pytest.mark.asyncio
async def test_disconnect_kills_a_genuinely_hung_worker_mid_stream(monkeypatch):
    """The entire point of this design: a worker that never writes to stdout,
    never checks anything, never cooperates in any way must still be
    terminated. wait_for (via _collect) bounds the failure mode -- if kill()
    doesn't actually work, this fails with a clear TimeoutError instead of
    hanging the test run indefinitely."""
    import api.server as server
    monkeypatch.setattr(server, "_WORKER_POLL_S", 0.05)

    http_request = _FakeHTTPRequest(disconnected_after=1)
    payload = {"task": "x", "_test_mode": "hang"}

    chunks = await _collect(_stream_worker_subprocess(http_request, payload, argv=_FIXTURE))

    # A disconnect mid-stream ends the generator quietly (no chunks, no
    # exception) -- the caller (api/server.py's /stream generate()) is a
    # StreamingResponse whose connection is already gone; there is nothing
    # left to report the disconnect to.
    assert chunks == []


@pytest.mark.asyncio
async def test_default_argv_is_the_real_stream_worker_command(monkeypatch):
    """Confirms the production default (no argv override) points at the real
    entrypoint `python main.py --stream-worker`."""
    captured_argv = []
    original_create_subprocess_exec = asyncio.create_subprocess_exec

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured_argv.extend(argv)
        return await original_create_subprocess_exec(*_FIXTURE, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    http_request = _FakeHTTPRequest()
    payload = {"task": "x", "_test_mode": "success"}

    await _collect(_stream_worker_subprocess(http_request, payload))

    assert captured_argv == [sys.executable, "main.py", "--stream-worker"]
