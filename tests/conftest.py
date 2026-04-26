import pytest


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Remove real env vars so tests use explicit values only."""
    for var in ("OLLAMA_HOST", "MCP_URL", "DB_URL", "PATTERNS_GLOB",
                "LEADER_MODEL", "CODER_MODEL", "REVIEWER_MODEL",
                "MEMORY_NAMESPACE", "STREAM", "MAX_ITERATIONS"):
        monkeypatch.delenv(var, raising=False)
