"""hive-mcp configuration — reads from environment with sensible defaults."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

PROJECT_ROOT       = Path(os.getenv("PROJECT_ROOT", "/project"))
MCP_HOST           = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT           = int(os.getenv("MCP_PORT", "9000"))
MCP_NAME           = "hive-mcp"
WRITE_REVIEW       = os.getenv("WRITE_REVIEW", "true").lower() == "true"
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "false").lower() == "true"

# ── External platform integrations (optional — activate by setting the env var) ─
NOTION_API_KEY             = os.getenv("NOTION_API_KEY", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # path to JSON file
