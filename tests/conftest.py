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
