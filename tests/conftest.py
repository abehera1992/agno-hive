import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Remove real env vars so tests use explicit values only."""
    for var in ("OLLAMA_HOST", "MCP_URL", "DB_URL", "PATTERNS_GLOB",
                "LEADER_MODEL", "CODER_MODEL", "REVIEWER_MODEL",
                "MEMORY_NAMESPACE", "STREAM", "MAX_ITERATIONS",
                "SESSION_TTL_DAYS", "AGNO_SESSION_WINDOW",
                "AGNO_COMPACT_THRESHOLD", "SESSION_CLEANUP_INTERVAL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def load_cli_hive():
    """Dynamically load cli/hive (no .py extension, not a package) as a fresh
    module object each time it's called, so tests can inspect/monkeypatch its
    module-level functions without polluting sys.modules across tests."""
    def _load():
        path = Path(__file__).resolve().parent.parent / "cli" / "hive"
        # cli/hive has no .py suffix, so spec_from_file_location can't infer a
        # loader on its own (it returns None) -- pass SourceFileLoader explicitly.
        loader = SourceFileLoader("cli_hive_under_test", str(path))
        spec = importlib.util.spec_from_file_location("cli_hive_under_test", path, loader=loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cli_hive_under_test"] = module
        spec.loader.exec_module(module)
        return module
    return _load
