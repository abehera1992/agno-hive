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
    "Do NOT call confirm_action yourself. "
    ""
    "Notion GROUNDING rules (MANDATORY — read before you write, never guess): "
    "1. NEVER fabricate or guess a Notion page_id. Resolve real ids first: "
    "notion_find_work_item(query) for a work item (e.g. 'Phase 6'), "
    "notion_items_in_sprint(...) / notion_search() / notion_query_database() for the rest. "
    "2. BEFORE any notion_update_page_props or relation change, call "
    "notion_get_item_with_relations(page_id) to READ the page's current properties and relations. "
    "Never set a relation (Parent item 1, Sprint, Work Items) you have not just read. "
    "3. Do NOT confuse 'Spec' (a doc-link property) with 'Parent item 1' (the work-item parent). "
    "Change a parent only if the task explicitly asks, and only to a page you confirmed is a Work Item "
    "via notion_get_item_with_relations — never to a Spec/doc URL. "
    "4. In notion_update_page_props send ONLY the properties the task names. Do NOT re-send "
    "Parent item 1 or any relation you were not asked to change (omitted properties are left as-is). "
    "5. Never report an item as 'orphaned'/missing a value from assumption — read it first and "
    "report the actual current state. "
    ""
    "Verification & completion-claim discipline (MANDATORY - applies to ALL claims, not just Notion): "
    "When you state whether something is implemented / done / removed / present / fixed, base it "
    "ONLY on code you actually READ this run (get_file_content / search_files) and cite the exact "
    "file path + line + the literal code as evidence. "
    "BEFORE returning any answer that names a symbol, a file:line, or an API route, call "
    "verify_claims(your_draft_answer). It greps every claim against the repo and reports what does "
    "not exist. If it returns NOT FOUND or BAD, the claim is fabricated - fix the answer, do not "
    "return it. The most common failure is naming a symbol that merely RESEMBLES the answer: a "
    "single-item function offered when asked about a batch operation, or a neighbouring symbol "
    "from the same file. Existing is not the same as doing what was asked, and verify_claims "
    "cannot catch that - it only proves the name exists. "
    "NEVER claim something was removed/added/completed unless the CURRENT code shows that state: if "
    "the code still calls or contains X, it is NOT removed - say 'still present at <file>:<line>'. "
    "Do NOT infer 'done' from a task title, a filename, a plausible assumption, or what you expected. "
    "If you did not read decisive evidence, answer 'could not verify' - never guess DONE. "
    ""
    "Counting & exhaustiveness (MANDATORY): for ANY count, total, 'how many', 'all', or exhaustive "
    "enumeration over a file or the codebase, derive the number from a DETERMINISTIC tool and report "
    "that tool's ACTUAL output - PREFER count_matches(pattern, glob_filter) which returns the exact "
    "'TOTAL: <n>' computed by ripgrep; or run_command with grep -c / grep -oE '...' | wc -l / wc -l. "
    "NEVER read a large file partially and estimate, extrapolate, sample, or eyeball a "
    "count. If the target is a big literal (a large dict / list / table / seed block), GREP it - do not "
    "scroll it and guess. Research thoroughly first: a value may live in more than one place (e.g. a DB "
    "table AND a code fallback), so search_files across the WHOLE repo to confirm you found every "
    "occurrence before stating a total, and state which sources you checked. "
    ""
    "Database-backed facts (when db_query / db_schema are available): if a value is stored "
    "in a database table (a count of rows, the current value of a column, 'how many X have "
    "status Y'), the LIVE TABLE is the source of truth — a file grep of a seed/migration/code "
    "fallback can be stale or incomplete. Call db_schema(table) to confirm the exact schema + "
    "column names, then db_query with an aggregate (SELECT col, count(*) ... GROUP BY col) to "
    "get the authoritative number. Report the DB result as the total; treat file greps as "
    "SUPPLEMENTARY subtotals (and note when the DB and the code/seed disagree — they often do)."
)


def _conventions_block() -> str:
    """State the project's code conventions up front, from the configured lint rules.

    verify_claims already checks emitted code against CODE_LINT_FORBID / CODE_LINT_REQUIRE
    AFTER the fact. That is necessary but it is not where the cost is: measured 2026-07-31,
    a "write a component following this project's conventions" task spent its whole run
    HUNTING for the rules — "let me check the project's documentation… let me check the
    CLAUDE.md" — and the outcome depended on whether the hunt happened to succeed. Across
    three passes the same case produced a pass, a failure, and a 600s non-termination.

    The rules are already in the loaded project context, but one line inside ~40K tokens is
    not findable in practice. Restating them here costs ~30 tokens and removes the search.
    hive-mcp still ships NO rules of its own — this only echoes what the project configured,
    so the server stays project-independent.
    """
    req = [r.partition("::") for r in config.CODE_LINT_REQUIRE]
    forb = [r.partition("::") for r in config.CODE_LINT_FORBID]
    if not (req or forb):
        return ""
    parts = ["Project code conventions — these are AUTHORITATIVE and complete. Apply them "
             "directly when writing or editing code; do NOT go searching the repo or the "
             "docs for a styling/convention guide, and do not infer conventions from "
             "unrelated files. "]
    # No ALWAYS/NEVER prefixes. The configured messages are already written as
    # directives, and a polarity prefix inverts them: a FORBID rule whose message reads
    # "use styles.x, not a bare className string" becomes "NEVER: use styles.x" —
    # instructing the exact opposite of the rule. Emit the messages verbatim.
    for _, _, msg in req + forb:
        if msg:
            parts.append(f"- {msg.rstrip('.')}. ")
    return "".join(parts)


_instructions = _instructions + " " + _conventions_block()

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
