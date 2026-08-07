"""Confirms the new bash_* tools are registered through main.py's _tool() tracing
wrapper (not bypassed) when HIVE_BASH_TOOL_ENABLED is on, and skipped entirely when
it's off. Same fastmcp-stub + direct-module-load approach as
test_main_integration_tools.py / test_main_traced.py (fastmcp is not installed in
this local dev environment, only inside the deployed hive-mcp Docker image)."""
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


def _load_main(monkeypatch, recorded: list, bash_tool_enabled: bool):
    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.FastMCP = _FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp)

    import config
    monkeypatch.setattr(config, "HIVE_BASH_TOOL_ENABLED", bash_tool_enabled)

    spec = importlib.util.spec_from_file_location("hive_main_bash_under_test", _MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hive_main_bash_under_test"] = module
    real_tool_registrations = []

    # Patch _tool AFTER module exec would normally run it -- instead, load the module
    # with a spying decorator substituted in place of mcp.tool(), since module-level
    # code calls _tool(fn) = mcp.tool()(_traced(fn)) directly at import time.
    orig_tool_method = _FakeFastMCP.tool

    def _spy_tool(self):
        def decorator(fn):
            real_tool_registrations.append(fn)
            return fn
        return decorator

    monkeypatch.setattr(_FakeFastMCP, "tool", _spy_tool)
    spec.loader.exec_module(module)
    monkeypatch.setattr(_FakeFastMCP, "tool", orig_tool_method)

    recorded.extend(real_tool_registrations)
    return module


_BASH_TOOL_NAMES = {
    "bash_session_start", "bash_run", "bash_session_close",
    "bash_job_status", "bash_job_kill",
}


def test_bash_tools_registered_when_enabled(monkeypatch):
    recorded = []
    _load_main(monkeypatch, recorded, bash_tool_enabled=True)

    names = {getattr(fn, "__name__", None) for fn in recorded}
    assert _BASH_TOOL_NAMES <= names


def test_bash_tools_not_registered_when_disabled(monkeypatch):
    recorded = []
    _load_main(monkeypatch, recorded, bash_tool_enabled=False)

    names = {getattr(fn, "__name__", None) for fn in recorded}
    assert not (_BASH_TOOL_NAMES & names)
