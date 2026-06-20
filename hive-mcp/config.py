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

# ── Migration runner (optional, review-gated — activate with MIGRATIONS_ENABLED=true) ─
# Lets hive APPLY Alembic migrations as the DB owner via the mounted docker socket.
# The owner password is read from the db container at run time (never stored here).
MIGRATIONS_ENABLED  = os.getenv("MIGRATIONS_ENABLED", "false").lower() == "true"
MIGRATION_DB_CONTAINER = os.getenv("MIGRATION_DB_CONTAINER", "")   # e.g. ekamapp-postgres-1
MIGRATION_DB_OWNER     = os.getenv("MIGRATION_DB_OWNER", "")       # e.g. abehera1992 (owner/superuser)
MIGRATION_DB_NAME      = os.getenv("MIGRATION_DB_NAME", "")        # e.g. ekamApp
MIGRATION_DB_HOST      = os.getenv("MIGRATION_DB_HOST", "postgres")  # host as seen from the service container
MIGRATION_DB_PORT      = os.getenv("MIGRATION_DB_PORT", "5432")
MIGRATION_SERVICES     = os.getenv("MIGRATION_SERVICES", "")       # "name:container,name:container"
