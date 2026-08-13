"""Tests for api.server._run_worker_subprocess -- Phase 1 of process-boundary
cancellation (see DOCS.md "Process-Boundary Cancellation"). Uses
tests/fixtures/fake_worker.py instead of the real `python main.py --run-worker`
so these run without the actual agno/MCP/vLLM stack.

Why this design exists: four rounds of cooperative cancellation in this
codebase (agno's own acancel_run/araise_if_cancelled, generic asyncio
task.cancel(), a shared claimed-flag between two independent checkers, then
tracking every observed run_id) each closed one specific way cancellation
could land wrong inside agno + MCP + anyio's nested async call graph, and each
was followed by a new way it still didn't -- most recently, cancelling
several run_ids from the same disconnect in rapid succession still corrupted
anyio's cancel-scope bookkeeping. A hard OS-level process kill doesn't depend
on anything being cancelled behaving correctly; the kernel reclaims every
socket and scope unconditionally when the process dies. These tests prove the
subprocess plumbing itself -- spawn, round-trip, and (critically) kill a
genuinely unresponsive child -- works before that guarantee is trusted live.
"""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from api.server import _run_worker_subprocess

_FIXTURE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_worker.py")]


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


@pytest.mark.asyncio
async def test_success_round_trips_content_tokens_and_clarification():
    http_request = _FakeHTTPRequest()
    payload = {"task": "do the thing", "_test_mode": "success"}

    content, tokens, clarification = await asyncio.wait_for(
        _run_worker_subprocess(http_request, payload, argv=_FIXTURE), timeout=10.0
    )

    assert content == "echo: do the thing"
    assert tokens == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    assert clarification is None


@pytest.mark.asyncio
async def test_worker_reported_error_raises_500_with_the_error_detail():
    http_request = _FakeHTTPRequest()
    payload = {"task": "x", "_test_mode": "error"}

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(
            _run_worker_subprocess(http_request, payload, argv=_FIXTURE), timeout=10.0
        )

    assert exc_info.value.status_code == 500
    assert "simulated internal failure" in exc_info.value.detail


@pytest.mark.asyncio
async def test_worker_crash_raises_500_naming_the_exit_code():
    """A crash exits non-zero with non-JSON stdout -- must be caught by the
    returncode check before ever attempting to json.loads() the garbage."""
    http_request = _FakeHTTPRequest()
    payload = {"task": "x", "_test_mode": "crash"}

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(
            _run_worker_subprocess(http_request, payload, argv=_FIXTURE), timeout=10.0
        )

    assert exc_info.value.status_code == 500
    assert "exited with code" in exc_info.value.detail


@pytest.mark.asyncio
async def test_disconnect_kills_a_genuinely_hung_worker(monkeypatch):
    """The entire point of this design: a worker that never writes to stdout,
    never checks anything, never cooperates in any way must still be
    terminated. wait_for bounds the failure mode -- if kill() doesn't
    actually work, this fails with a clear TimeoutError instead of hanging
    the test run indefinitely."""
    import api.server as server
    monkeypatch.setattr(server, "_WORKER_POLL_S", 0.05)

    http_request = _FakeHTTPRequest(disconnected_after=1)
    payload = {"task": "x", "_test_mode": "hang"}

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(
            _run_worker_subprocess(http_request, payload, argv=_FIXTURE), timeout=10.0
        )

    assert exc_info.value.status_code == 499


@pytest.mark.asyncio
async def test_liveness_kill_terminates_a_stalled_worker_even_without_a_disconnect(monkeypatch):
    """Recommendation #2 (see DOCS.md "Liveness-Based Auto-Kill"): a worker whose
    own heartbeat reports genuine staleness must be killed even though the client
    never disconnected -- a second trigger condition on the SAME actuator
    (proc.kill()) the disconnect path already uses, not a new one.

    Patches server.config (the object api/server.py's own module namespace
    already holds), NOT a freshly `from config.config import config`-ed one --
    if this test suite's own test_config.py ran its _reload_config() helper
    earlier in the same pytest session (it deletes config.config from
    sys.modules), a fresh in-function import here would silently resolve to a
    DIFFERENT object than the one _run_worker_subprocess actually reads,
    making the monkeypatch a no-op that happens to still pass by coincidence
    for the False case and fail outright for the True one. Confirmed live:
    this was the exact failure mode before switching to server.config."""
    import api.server as server
    monkeypatch.setattr(server, "_WORKER_POLL_S", 0.05)
    monkeypatch.setattr(server.config, "enable_liveness_autokill", True)

    http_request = _FakeHTTPRequest()  # never disconnects -- only liveness can end this
    payload = {"task": "x", "_test_mode": "stale"}

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(
            _run_worker_subprocess(http_request, payload, argv=_FIXTURE), timeout=10.0
        )

    assert exc_info.value.status_code == 504
    assert "auto-terminated" in exc_info.value.detail


@pytest.mark.asyncio
async def test_liveness_kill_leaves_no_snapshot_file_behind(monkeypatch, tmp_path):
    """Cleanup is the parent's job -- a SIGKILLed child never reaches its own
    finally, so the snapshot file must not linger on disk after the kill."""
    import api.server as server
    monkeypatch.setattr(server, "_WORKER_POLL_S", 0.05)
    monkeypatch.setattr(server.config, "enable_liveness_autokill", True)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    http_request = _FakeHTTPRequest()
    payload = {"task": "x", "_test_mode": "stale"}

    with pytest.raises(HTTPException):
        await asyncio.wait_for(
            _run_worker_subprocess(http_request, payload, argv=_FIXTURE), timeout=10.0
        )

    assert list(tmp_path.glob("agnohive-liveness-*.json")) == []


@pytest.mark.asyncio
async def test_liveness_kill_disabled_by_default_even_for_a_stale_worker(monkeypatch):
    """Off by default (config.enable_liveness_autokill=False) -- matches the same
    rollout discipline use_worker_process_isolation had: ship off, validate live,
    flip on. wait_for's own TimeoutError firing (not an HTTPException) confirms
    genuinely no kill happened via this path, not just that a different one did;
    the CancelledError this raises is still handled by _run_worker_subprocess's
    own except-CancelledError cleanup, so the child process itself doesn't leak
    even though this specific mechanism stayed inert."""
    import api.server as server
    monkeypatch.setattr(server, "_WORKER_POLL_S", 0.05)
    monkeypatch.setattr(server.config, "enable_liveness_autokill", False)

    http_request = _FakeHTTPRequest()  # never disconnects
    payload = {"task": "x", "_test_mode": "stale"}

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _run_worker_subprocess(http_request, payload, argv=_FIXTURE), timeout=0.5
        )


@pytest.mark.asyncio
async def test_default_argv_is_the_real_worker_command(monkeypatch):
    """Confirms the production default (no argv override) points at the real
    entrypoint `python main.py --run-worker` -- only tests pass argv to
    substitute the fixture script. Captures the actual argv
    create_subprocess_exec was called with rather than inspecting source
    text, so this fails if the default ever silently drifts."""
    captured_argv = []
    original_create_subprocess_exec = asyncio.create_subprocess_exec

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured_argv.extend(argv)
        return await original_create_subprocess_exec(*_FIXTURE, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    http_request = _FakeHTTPRequest()
    payload = {"task": "x", "_test_mode": "success"}

    await asyncio.wait_for(_run_worker_subprocess(http_request, payload), timeout=10.0)

    assert captured_argv == [sys.executable, "main.py", "--run-worker"]
