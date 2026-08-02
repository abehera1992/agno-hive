"""Regression test for the async-tool tracing bug.

`_traced()`'s original `wrapper` was a plain `def` that called `fn(*args, **kwargs)`
without `await`. For a sync tool that's correct; for an `async def` tool (e.g.
`index_project`) it only constructs a coroutine object — the function body never
runs inline. The log fired immediately with the coroutine's own repr as the
"result" (~50 chars, ~0.00s) instead of the real outcome. FastMCP's own
tool-invocation layer happened to await the returned coroutine separately, so the
tool still worked — but every trace line for an async tool was meaningless, and the
premature log line landed before the real work even started.

Caught 2026-08-02 while verifying `index_project` correctly excluded the new
EXCLUDE_GLOBS-listed agno-hive training data: the log claimed 0.00s/50 chars while
the index state file kept growing for minutes afterward.

Uses the same fastmcp-stub + direct-module-load approach as
test_main_integration_tools.py (fastmcp is not installed in this local dev
environment, only inside the deployed hive-mcp Docker image).
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

_MAIN_PATH = Path(__file__).parent.parent / "main.py"


class _FakeFastMCP:
    def __init__(self, name, instructions=None):
        self.name = name
        self.instructions = instructions

    def tool(self):
        def decorator(fn):
            return fn
        return decorator

    def custom_route(self, path, methods=None):
        def decorator(fn):
            return fn
        return decorator

    def run(self, **kwargs):
        raise AssertionError("mcp.run() must never execute on import — only under __main__")


def _load_traced(monkeypatch):
    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.FastMCP = _FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp)

    spec = importlib.util.spec_from_file_location("hive_main_traced_under_test", _MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hive_main_traced_under_test"] = module
    spec.loader.exec_module(module)
    return module._traced


def test_traced_awaits_an_async_tool_and_returns_its_real_result(monkeypatch, capsys):
    traced = _load_traced(monkeypatch)
    calls = []

    async def fake_async_tool(x):
        calls.append(x)
        await asyncio.sleep(0)  # force a real await point, not a same-tick return
        return "Done — indexed 42 files"

    wrapped = traced(fake_async_tool)
    result = asyncio.run(wrapped("agno-hive"))

    assert result == "Done — indexed 42 files"
    assert calls == ["agno-hive"]
    log = capsys.readouterr().out
    assert "coroutine object" not in log
    assert "-> 23 chars" in log  # len("Done — indexed 42 files")


def test_traced_still_works_for_a_sync_tool(monkeypatch):
    traced = _load_traced(monkeypatch)

    def fake_sync_tool(x):
        return f"got {x}"

    wrapped = traced(fake_sync_tool)
    result = wrapped("hello")

    assert result == "got hello"
    assert not asyncio.iscoroutine(result)


def test_traced_propagates_exceptions_from_an_async_tool(monkeypatch):
    traced = _load_traced(monkeypatch)

    async def fake_failing_tool():
        raise ValueError("boom")

    wrapped = traced(fake_failing_tool)

    try:
        asyncio.run(wrapped())
        assert False, "expected ValueError to propagate"
    except ValueError as e:
        assert str(e) == "boom"
