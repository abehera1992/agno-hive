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
    count_matches,
    list_directory,
    list_directory_tree,
)
from tools.files import write_file, apply_diff, run_command
from tools.verify import verify_claims
from tools.skills import list_skills, load_skill
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
        notion_items_in_sprint,
        notion_get_item_with_relations,
        notion_find_work_item,
        notion_create_page,
        notion_update_page_props,
        notion_append_blocks,
        notion_append_markdown,
        notion_replace_section,
        notion_update_block,
        notion_delete_block,
        notion_trash_page,
        notion_update_content,
    )
    _INTEGRATION_TOOLS += [
        notion_search, notion_get_page, notion_get_database_schema, notion_query_database,
        notion_items_in_sprint,
        notion_get_item_with_relations,
        notion_find_work_item,
        notion_create_page, notion_update_page_props, notion_append_blocks, notion_append_markdown,
        notion_replace_section,
        notion_update_block, notion_delete_block, notion_trash_page, notion_update_content,
    ]

if config.MIGRATIONS_ENABLED:
    from tools.integrations.migrations import run_migration
    _INTEGRATION_TOOLS += [run_migration]

if config.HIVE_DB_URL:
    from tools.integrations.db import db_query, db_schema
    _INTEGRATION_TOOLS += [db_query, db_schema]

_instructions = (
    "You are connected to a project via hive-mcp. "
    "The project files are at the root level — use get_file_content, find_files, "
    "and search_files to explore before making any changes. "
    ""
    "Skills: call list_skills() to see the always-loaded catalog of available "
    "protocols (Notion grounding, DB-backed facts, counting, file-write review, "
    "verification discipline, and any project-specific skills). Call "
    "load_skill(name) to fetch ONE skill's full instructions before acting on a "
    "task its description matches — e.g. before writing to Notion, before citing "
    "a database-backed number, before making any file change, before claiming "
    "something is done/removed/verified. Do not load a skill unrelated to the "
    "current task."
)

mcp = FastMCP(config.MCP_NAME, instructions=_instructions)


def _traced(fn):
    """Log every tool call: name, args, duration, result size, and any exception.

    Without this the only evidence a tool ran is an anonymous `POST /mcp` in the uvicorn
    access log, so "did the agent actually read the file, or answer from priors?" could
    only be guessed at from response latency. That question came up repeatedly while
    diagnosing fabricated answers (2026-07-30) and was never answerable from the logs.

    Args are truncated hard — a write_file content payload or a large diff would
    otherwise dominate the log and bury the signal it exists to provide.
    """
    import functools
    import time as _t

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        shown = ", ".join(
            [repr(a)[:60] for a in args]
            + [f"{k}={repr(v)[:60]}" for k, v in kwargs.items()]
        )[:180]
        t0 = _t.time()
        try:
            result = fn(*args, **kwargs)
            print(f"[tool] {fn.__name__}({shown}) -> {len(str(result)):,} chars "
                  f"in {_t.time() - t0:.2f}s", flush=True)
            return result
        except Exception as e:
            print(f"[tool] {fn.__name__}({shown}) RAISED {type(e).__name__}: {e} "
                  f"after {_t.time() - t0:.2f}s", flush=True)
            raise

    return wrapper


def _tool(fn):
    """Register a tool with call tracing."""
    return mcp.tool()(_traced(fn))

# ── Context + file reading ────────────────────────────────────────────────────
_tool(get_project_context)
_tool(get_file_content)
_tool(find_files)
_tool(search_files)
_tool(count_matches)
_tool(verify_claims)
_tool(list_skills)
_tool(load_skill)
_tool(list_directory)
_tool(list_directory_tree)

# ── File writing (WRITE_REVIEW-aware) + read-only shell ──────────────────────
_tool(write_file)
_tool(apply_diff)
_tool(run_command)

# ── Shell + Docker + environment ─────────────────────────────────────────────
_tool(run_shell)
_tool(run_docker)
_tool(get_env_info)
_tool(check_port)
_tool(list_processes)

# ── Git ───────────────────────────────────────────────────────────────────────
_tool(git_status)
_tool(git_log)
_tool(git_diff)
_tool(git_log_file)
_tool(git_blame)

# ── Semantic indexing (bootstrap) ─────────────────────────────────────────────
_tool(index_project)

# ── Project context snapshot ──────────────────────────────────────────────────
_tool(scan_project_context)

# ── Web search + fetch (gated by WEB_SEARCH_ENABLED) ─────────────────────────
_tool(web_search)
_tool(web_fetch)

# ── External integrations (activated by env vars) ─────────────────────────────
# confirm/reject are always registered so the CLI confirm flow works regardless
# of which platforms are active.
_tool(confirm_action)
_tool(reject_action)

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
