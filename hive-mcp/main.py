"""hive-mcp — generic MCP server for AGNOHive.

Exposes file system, shell, Docker, and git tools over Streamable HTTP.
Mount your project directory as /project and point AGNOHive at this server.

Start:
    python main.py                    # Streamable HTTP on 0.0.0.0:9000
    MCP_PORT=9001 python main.py      # custom port

ZGX connects via:
    http://<tailscale-ip>:9000/mcp
"""
import sys
import os
from pathlib import Path

_server_dir = Path(__file__).parent
sys.path.insert(0, str(_server_dir))
os.chdir(_server_dir)

from fastmcp import FastMCP
import config

from tools.context import (
    get_project_context,
    get_file_content,
    find_files,
    search_files,
    list_directory,
    list_directory_tree,
)
from tools.files import write_file, apply_diff, run_command
from tools.shell import run_shell, run_docker, get_env_info, check_port, list_processes
from tools.git import git_status, git_log, git_diff, git_log_file, git_blame
from tools.index import index_project
from tools.scan import scan_project_context
from tools.web import web_search, web_fetch
from tools.integrations import confirm_action, reject_action

_INTEGRATION_TOOLS = []
if config.NOTION_API_KEY:
    from tools.integrations.notion import (
        notion_search,
        notion_get_page,
        notion_get_database_schema,
        notion_query_database,
        notion_create_page,
        notion_update_page_props,
        notion_append_blocks,
        notion_trash_page,
    )
    _INTEGRATION_TOOLS += [
        notion_search, notion_get_page, notion_get_database_schema, notion_query_database,
        notion_create_page, notion_update_page_props, notion_append_blocks, notion_trash_page,
    ]

if config.MIGRATIONS_ENABLED:
    from tools.integrations.migrations import run_migration
    _INTEGRATION_TOOLS += [run_migration]

_instructions = (
    "You are connected to a project via hive-mcp. "
    "The project files are at the root level — use get_file_content, find_files, "
    "and search_files to explore before making any changes. "
    ""
    "File editing rules: "
    "Use apply_diff() for ALL edits to existing files — surgical, line-level replacement. "
    "Use write_file() ONLY for brand-new files. "
    "Use run_command() for read-only checks (tests, linters). "
    "Use run_shell() when you need to run install commands or start services. "
    "Use run_docker() to inspect or manage Docker containers and compose services. "
    ""
    "IMPORTANT: If apply_diff() or write_file() returns 'review_pending', "
    "STOP immediately. Do not call any other tool. "
    "Tell the human the change is staged for review — they approve via the hive CLI. "
    "confirm_write and reject_write do not exist as tools. "
    ""
    "IMPORTANT: If a Notion/Google tool returns 'action_pending', "
    "STOP immediately. Do not call any other tool. "
    "Tell the human the action is staged — they approve via the hive CLI. "
    "Do NOT call confirm_action yourself."
)

mcp = FastMCP(config.MCP_NAME, instructions=_instructions)

# ── Context + file reading ────────────────────────────────────────────────────
mcp.tool()(get_project_context)
mcp.tool()(get_file_content)
mcp.tool()(find_files)
mcp.tool()(search_files)
mcp.tool()(list_directory)
mcp.tool()(list_directory_tree)

# ── File writing (WRITE_REVIEW-aware) + read-only shell ──────────────────────
mcp.tool()(write_file)
mcp.tool()(apply_diff)
mcp.tool()(run_command)

# ── Shell + Docker + environment ─────────────────────────────────────────────
mcp.tool()(run_shell)
mcp.tool()(run_docker)
mcp.tool()(get_env_info)
mcp.tool()(check_port)
mcp.tool()(list_processes)

# ── Git ───────────────────────────────────────────────────────────────────────
mcp.tool()(git_status)
mcp.tool()(git_log)
mcp.tool()(git_diff)
mcp.tool()(git_log_file)
mcp.tool()(git_blame)

# ── Semantic indexing (bootstrap) ─────────────────────────────────────────────
mcp.tool()(index_project)

# ── Project context snapshot ──────────────────────────────────────────────────
mcp.tool()(scan_project_context)

# ── Web search + fetch (gated by WEB_SEARCH_ENABLED) ─────────────────────────
mcp.tool()(web_search)
mcp.tool()(web_fetch)

# ── External integrations (activated by env vars) ─────────────────────────────
# confirm/reject are always registered so the CLI confirm flow works regardless
# of which platforms are active.
mcp.tool()(confirm_action)
mcp.tool()(reject_action)

for _tool in _INTEGRATION_TOOLS:
    mcp.tool()(_tool)

# ── HTTP routes for CLI confirm/reject (bypasses agent pipeline) ──────────────
# The hive CLI calls these directly: POST /actions/confirm  {"action_id": "..."}
#                                    POST /actions/reject   {"action_id": "..."}
try:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/actions/confirm", methods=["POST"])
    async def _http_confirm(request: Request) -> JSONResponse:
        body      = await request.json()
        action_id = body.get("action_id", "")
        if not action_id:
            return JSONResponse({"error": "action_id required"}, status_code=400)
        result = confirm_action(action_id)
        return JSONResponse({"result": result})

    @mcp.custom_route("/actions/reject", methods=["POST"])
    async def _http_reject(request: Request) -> JSONResponse:
        body      = await request.json()
        action_id = body.get("action_id", "")
        if not action_id:
            return JSONResponse({"error": "action_id required"}, status_code=400)
        result = reject_action(action_id)
        return JSONResponse({"result": result})

except (AttributeError, TypeError):
    # FastMCP version doesn't support custom_route — confirm/reject still work
    # as MCP tools; the CLI falls back to instructing the agent to call them.
    log = __import__("structlog").get_logger()
    log.warning("hive_mcp_custom_route_unavailable",
                 note="confirm/reject HTTP shortcuts not available; MCP tool fallback active")


if __name__ == "__main__":
    import structlog
    log = structlog.get_logger()
    log.info(
        "hive_mcp_starting",
        host=config.MCP_HOST,
        port=config.MCP_PORT,
        project_root=str(config.PROJECT_ROOT),
        write_review=config.WRITE_REVIEW,
    )
    mcp.run(
        transport="streamable-http",
        host=config.MCP_HOST,
        port=config.MCP_PORT,
    )
