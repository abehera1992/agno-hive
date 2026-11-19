"""hive-mcp — generic MCP server for AGNOHive.

Exposes file system, shell, Docker, and git tools over Streamable HTTP.
Mount your project directory as /project and point AGNOHive at this server.

Start:
    python main.py                    # Streamable HTTP on 0.0.0.0:9000
    MCP_PORT=9001 python main.py      # custom port

ZGX connects via:
    http://<tailscale-ip>:9000/mcp
"""
import atexit
import signal
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
    project_map,
    get_file_content,
    get_files_batch,
    get_symbol,
    find_files,
    search_files,
    search_files_batch,
    count_matches,
    list_directory,
    list_directory_tree,
)
from tools.compare import compare_enumerations
from tools.files import write_file, apply_diff, run_command
from tools.verification import verify_project
from tools import bash as _bash_tools
from tools.bash import (
    bash_session_start, bash_run, bash_session_close,
    bash_job_status, bash_job_kill,
)
from tools.verify import verify_claims
from tools.symbol_index import structural_index
from tools.skills import list_skills, load_skill
from tools.skill_registry_mcp import (
    skill_registry_list, skill_registry_info, skill_registry_import, skill_registry_resolve,
    skill_registry_get_content,
    skill_registry_import_begin, skill_registry_import_batch,
    skill_registry_import_finalize, skill_registry_import_abort,
)
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

if config.DB_CONN_URL:
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

    Dispatches on `inspect.iscoroutinefunction(fn)` (added 2026-08-02). `index_project`
    is `async def`; the original single sync `wrapper` called it as `fn(*args, **kwargs)`
    without `await`, which for a coroutine function only constructs a coroutine object —
    the function body never runs inline. The log fired immediately with the coroutine's
    repr as the "result" (~50 chars, ~0.00s) instead of the real outcome; FastMCP's own
    tool-invocation layer happened to await the returned coroutine separately (indexing
    still worked), but every trace line for an async tool was meaningless. Caught while
    verifying `index_project` actually excluded the new EXCLUDE_GLOBS-listed training
    data — the log claimed 0.00s/50 chars while the index state file kept growing for
    minutes afterward.
    """
    import functools
    import inspect
    import time as _t

    def _shown(args, kwargs):
        return ", ".join(
            [repr(a)[:60] for a in args]
            + [f"{k}={repr(v)[:60]}" for k, v in kwargs.items()]
        )[:180]

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            shown = _shown(args, kwargs)
            t0 = _t.time()
            try:
                result = await fn(*args, **kwargs)
                print(f"[tool] {fn.__name__}({shown}) -> {len(str(result)):,} chars "
                      f"in {_t.time() - t0:.2f}s", flush=True)
                return result
            except Exception as e:
                print(f"[tool] {fn.__name__}({shown}) RAISED {type(e).__name__}: {e} "
                      f"after {_t.time() - t0:.2f}s", flush=True)
                raise
        return wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        shown = _shown(args, kwargs)
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
_tool(project_map)
_tool(get_file_content)
_tool(get_files_batch)
_tool(get_symbol)
_tool(find_files)
_tool(search_files)
_tool(search_files_batch)
_tool(count_matches)
_tool(compare_enumerations)
_tool(verify_claims)
_tool(structural_index)
_tool(list_skills)
_tool(load_skill)
_tool(list_directory)
_tool(list_directory_tree)

# ── Persistent skills registry (Phase 1.57b) — CLI-only, not for agent use ───
# Registered here only so cli/hive's /skill commands can reach them via
# _mcp_call_direct's own raw MCP session (bypasses agno's MCPTools/
# team_role_tools allowlist entirely, same path index_project/
# scan_project_context already use for CLI-direct calls). Deliberately NOT
# added to _instructions above and NOT granted to any team's
# team_role_tools allowlist — an agent's own tool surface is unaffected by
# this registration (see swarm/team_config.py: a role's live tools come from
# its explicit DB/YAML allowlist, never "every tool hive-mcp happens to
# expose").
_tool(skill_registry_list)
_tool(skill_registry_info)
_tool(skill_registry_import)
# skill_registry_resolve (Phase 1.57c) -- also CLI-only by the rule above, but
# in practice its real caller is swarm/team.py's _resolve_requested_skills(),
# which calls it directly over a bespoke MCP session (never through an
# LLM/agent), the same pattern _fetch_skill_text already uses for load_skill.
_tool(skill_registry_resolve)
# skill_registry_get_content (Phase 1.58.1) -- the content-delivery tool
# skill_registry_resolve's own docstring deferred as "Phase 1.57d". Same
# CLI-only rule as every tool in this block; its real caller is
# swarm/team.py's _fetch_resolved_skill_content(), over the SAME bespoke MCP
# session pattern _resolve_requested_skills() already uses -- never through
# an LLM/agent, never granted to any team's team_role_tools allowlist.
_tool(skill_registry_get_content)
# Staged import (Phase H.2) -- fixes the /skill import HTTP 413 failure for
# packages whose encoded payload would exceed the mcp SDK's 4 MiB Streamable
# HTTP request ceiling. CLI-only, same reasoning as the four tools above.
_tool(skill_registry_import_begin)
_tool(skill_registry_import_batch)
_tool(skill_registry_import_finalize)
_tool(skill_registry_import_abort)

# ── File writing (WRITE_REVIEW-aware) + read-only shell ──────────────────────
_tool(write_file)
_tool(apply_diff)
_tool(run_command)

# ── Project-declared mechanical verification (Experiment 5 Phase 2) ──────────
# Runs only a NAMED check from the project's own committed .hive-verify.json;
# never accepts a command string. See tools/verification.py's module docstring.
_tool(verify_project)

# ── Shell + Docker + environment ─────────────────────────────────────────────
_tool(run_shell)
_tool(run_docker)
_tool(get_env_info)
_tool(check_port)
_tool(list_processes)

# ── Persistent bash sessions + background jobs (see tools/bash.py) ───────────
if config.HIVE_BASH_TOOL_ENABLED:
    _tool(bash_session_start)
    _tool(bash_run)
    _tool(bash_session_close)
    _tool(bash_job_status)
    _tool(bash_job_kill)

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

def _register_integration_tools(tools: list) -> None:
    """Register integration tools (db_query, db_schema, notion_*, run_migration)
    through the same _tool() tracing every other tool gets.

    Previously this was a bare `for _tool in _INTEGRATION_TOOLS: mcp.tool()(_tool)`
    — the loop variable `_tool` shadowed this module's own `_tool()` tracing
    helper of the same name, so it silently fell back to raw `mcp.tool()(...)` and
    never wrapped these tools in `_traced()`. Confirmed 2026-08-01: a live
    groundedness test could not tell whether db_query had actually been called,
    because it never appeared in the [tool] log regardless of whether it ran.
    """
    for integration_tool in tools:
        _tool(integration_tool)


_register_integration_tools(_INTEGRATION_TOOLS)

# ── Shutdown cleanup for tools/bash.py's live sessions/jobs ───────────────────
# Dockerfile uses exec-form CMD ["python", "main.py"], so this process is PID 1
# and receives SIGTERM directly from `docker stop` -- no shell wrapper swallows
# it. atexit alone would not fire on a signal-based exit (Python only runs atexit
# handlers on a normal interpreter shutdown, e.g. sys.exit()), hence the explicit
# SIGTERM handler below that calls sys.exit() itself. A SIGKILL or a `docker stop`
# past its grace period skips both -- no pure-Python fix for that case.
atexit.register(_bash_tools.cleanup_all)


def _handle_sigterm(signum, frame):
    _bash_tools.cleanup_all()
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)

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

    # Phase H.1 — emit every persistence-path decision through this SAME
    # existing logger (never a new framework; structlog was already in use for
    # the startup banner two lines above). Each HIVE_PERSISTENCE_NOTES entry is
    # already a dict of exactly the fields required (subsystem, resolved_path,
    # decision, legacy_path, legacy_has_data, new_default_path, new_has_data,
    # message) — none of them are credentials, skill content, or database
    # content; they are only paths and short decision labels, by construction
    # (see config.py's own _resolve_persistent_path docstring).
    for note in config.HIVE_PERSISTENCE_NOTES:
        log.info("hive_persistence_decision", **note)

    # Phase H.1 — fail startup clearly rather than silently running against
    # ephemeral container storage. This is a BLANKET gate on HIVE_DATA_ROOT
    # itself (distinct from the per-subsystem conflict check above, which
    # blocks only one subsystem at actual-use time) — if the data root isn't
    # genuinely persistent, EVERYTHING downstream of it is unsound, so this
    # stops the process before it ever starts serving, not after.
    _data_root_check = config.validate_data_root()
    log.info(
        "hive_data_root_check",
        data_root=str(config.HIVE_DATA_ROOT),
        ok=_data_root_check["ok"],
        reason=_data_root_check["reason"],
        checks=_data_root_check["checks"],
        dev_override=config.HIVE_DATA_ROOT_ALLOW_EPHEMERAL,
    )
    if not _data_root_check["ok"]:
        log.error("hive_data_root_check_failed", reason=_data_root_check["reason"])
        print(
            f"\nFATAL: HIVE_DATA_ROOT persistence check failed — refusing to start.\n"
            f"  {_data_root_check['reason']}\n"
            f"  Set HIVE_DATA_ROOT_ALLOW_EPHEMERAL=true to run without a mounted "
            f"persistent volume (development only).\n",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)

    mcp.run(
        transport="streamable-http",
        host=config.MCP_HOST,
        port=config.MCP_PORT,
    )
