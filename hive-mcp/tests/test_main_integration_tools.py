"""Regression test for the integration-tool tracing gap.

`main.py` used `for _tool in _INTEGRATION_TOOLS: mcp.tool()(_tool)` to register
db_query/db_schema/notion_*/run_migration — the loop variable `_tool` shadowed the
module's own `_tool()` tracing helper of the same name, so it silently fell back to
raw `mcp.tool()(...)` and never wrapped these tools in `_traced()`. Confirmed
2026-08-01: a live groundedness test could not tell whether `db_query` had actually
fired, because it never appeared in the `[tool]` log regardless of whether it ran.

This test exercises the extraction directly rather than importing the whole of
main.py (which has side-effecting module-level config/FastMCP setup) — it proves
the registration helper calls the SAME `_tool()` used by every other tool in the
file, by name, not a bypass.
"""
import importlib.util
import sys
import types
from pathlib import Path

_MAIN_PATH = Path(__file__).parent.parent / "main.py"


class _FakeFastMCP:
    """Minimal stand-in for fastmcp.FastMCP — just enough surface for main.py to
    import and register tools. `fastmcp` is not installed in this local dev
    environment (only inside the deployed hive-mcp Docker image), so importing
    main.py directly requires stubbing its one external dependency."""

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


def _load_register_integration_tools(monkeypatch, recorded: list):
    """Import main.py with fastmcp stubbed and `_tool` replaced by a recording spy,
    and hand back the module's `_register_integration_tools` function. No real
    Notion/DB/migration integration activates (no such env vars are set in the test
    environment, so `_INTEGRATION_TOOLS` starts empty regardless of the stub)."""
    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.FastMCP = _FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp)

    spec = importlib.util.spec_from_file_location("hive_main_under_test", _MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hive_main_under_test"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_tool", lambda fn: recorded.append(fn))
    return module


def test_register_integration_tools_calls_the_traced_tool_helper(monkeypatch):
    recorded = []
    module = _load_register_integration_tools(monkeypatch, recorded)

    def fake_integration_tool():
        return "ok"

    module._register_integration_tools([fake_integration_tool])

    assert recorded == [fake_integration_tool]
