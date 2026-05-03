import asyncio
import time
from contextlib import AsyncExitStack

from opentelemetry import trace
from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer, make_agent_from_spec, get_model
from .bootstrap import bootstrap
from .feedback import record_success, record_failure, load_failure_context
from config.config import config

_tracer = trace.get_tracer("agno-hive.team")

_MCP_TIMEOUT = 60

_COORDINATOR_INSTRUCTIONS = [
    "── Scan-first rule (read this before anything else) ────────────",
    "  User prompts are often short and vague. Do not infer — discover.",
    "  Before answering any question about structure, features, or behaviour:",
    "    1. find_files('**/*') — get the full file tree",
    "    2. search_files(keyword, '**/*') — find all occurrences of the topic",
    "    3. get_file_content(path) — read specific files to verify details",
    "  Never describe a directory or module from its name alone.",
    "  Never stop at the first interesting result for overview questions — cover everything.",
    "",
    "Choose the FASTEST path to answer — do not call tools you don't need:",
    "",
    "For overview / structure questions ('list directories', 'what does X do', 'show me the project'):",
    "  1. find_files('**/*') to discover the complete file tree",
    "  2. For each top-level directory: read one entry file (README, main.py, __init__.py, config)",
    "  3. Return a grounded summary covering ALL directories — not just the first one found.",
    "  → Do not use get_project_context() as a shortcut — it may be stale or incomplete.",
    "",
    "For 'how does X work' / feature questions:",
    "  1. search_files(X, '**/*') — find every file that references X",
    "  2. get_file_content() on the 2-3 most relevant files",
    "  3. get_context_section(topic) if a DOCS.md section exists",
    "  → Search before you read — searching tells you which files are worth reading.",
    "",
    "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
    "  1. find_files('**/<extension>') to discover real paths",
    "  2. search_files(pattern, glob) to verify the pattern across files",
    "  3. get_file_content(path) on 1-2 files if you need more detail",
    "  → Skip get_project_context and memory_search for these queries.",
    "",
    "For implementation tasks (write code, fix a bug):",
    "  1. get_context_section(topic) for relevant architecture context",
    "  2. ALWAYS read at least one existing reference file of the same type before writing.",
    "     NEVER skip this step — guessing conventions produces broken code.",
    "  3. Delegate writing to Coder, review to Reviewer",
    "  4. memory_store() any non-obvious insight after completing (if available)",
    "",
    "── Multi-MCP tool selection ─────────────────────────────────────",
    "  When multiple MCP servers are connected, use the right one for each job:",
    "  - PROJECT MCP (e.g. get_project_context, memory_search, search_knowledge_graph,",
    "    get_file_content, find_files): reading context, understanding the project,",
    "    app-specific workflows.",
    "  - hive-mcp (e.g. apply_diff, write_file, run_shell, run_docker, git_status):",
    "    writing files, running commands, Docker operations, all host-level actions.",
    "  If only one MCP is connected, use it for everything.",
    "",
    "── Editing files (CRITICAL) ────────────────────────────────────",
    "  - For existing files: ALWAYS use apply_diff(), NEVER write_file().",
    "    apply_diff makes surgical line-level changes; write_file rewrites the whole file.",
    "  - Only use write_file() when creating a brand-new file that does not exist yet.",
    "  - Read the file first (get_file_content) to get the exact old_string to replace.",
    "  - To APPEND content: include the anchor line in BOTH old_string AND new_string,",
    "    then add the new content after it. Example:",
    "      old_string = 'last_line'",
    "      new_string = 'last_line\\nnew_content'",
    "    Never drop existing lines from new_string unless intentionally deleting them.",
    "",
    "── run_command is READ-ONLY (CRITICAL) ─────────────────────────",
    "  - run_command is for tests, linters, grep, git status ONLY.",
    "  - NEVER use run_command to modify files — no >, >>, sed -i, tee, perl -i.",
    "  - 'add a line', 'update a comment', 'change X to Y' → use apply_diff().",
    "  - Attempting to write via run_command will be BLOCKED by the server.",
    "  - For full shell access (npm install, docker compose, etc.) use run_shell().",
    "",
    "── General rules ──────────────────────────────────────────────",
    "  - Base answers on file contents, not assumptions",
    "  - Synthesise member outputs into one coherent response",
    "",
    "── File write review (CRITICAL) ───────────────────────────────",
    "  - If write_file() or apply_diff() returns 'review_pending',",
    "    the proposed change is staged for human review.",
    "  - STOP immediately — do NOT call any other tool.",
    "  - confirm_write and reject_write do NOT exist — you cannot approve writes.",
    "  - Tell the human: 'review_pending: <path>' and wait.",
    "  - The human selects confirm/reject in their CLI — your job ends at 'review_pending'.",
]


def _extract_tokens(result) -> dict:
    """Pull input/output/total token counts from an Agno RunResponse metrics object."""
    try:
        m = getattr(result, "metrics", None)
        if not m:
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        def _sum(val):
            if isinstance(val, list):
                return sum(v for v in val if isinstance(v, (int, float)))
            return int(val) if val else 0

        if isinstance(m, dict):
            return {
                "input_tokens":  _sum(m.get("input_tokens",  0)),
                "output_tokens": _sum(m.get("output_tokens", 0)),
                "total_tokens":  _sum(m.get("total_tokens",  0)),
            }
        return {
            "input_tokens":  _sum(getattr(m, "input_tokens",  0)),
            "output_tokens": _sum(getattr(m, "output_tokens", 0)),
            "total_tokens":  _sum(getattr(m, "total_tokens",  0)),
        }
    except Exception:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


async def run_task_async(
    task: str,
    agent_specs: list | None = None,
    coordinator_model: str | None = None,
    mcp_url: str | None = None,
    mcp_urls: list[str] | None = None,   # secondary MCPs (e.g. hive-mcp for host actions)
    project_id: str = "default",
    session_id: str | None = None,
) -> str:
    """Run a task with the given team spec, or fall back to default Coder+Reviewer."""
    effective_mcp_url = mcp_url or config.mcp_url
    effective_coordinator = coordinator_model or config.leader_model

    from swarm.sessions import get_context as get_session_context

    async def _load_session_context():
        if session_id:
            try:
                return await get_session_context(session_id)
            except Exception as exc:
                print(f"[team] session context warning: {exc}")
        return "", []

    project_context, failure_context, (session_summary, session_messages) = (
        await asyncio.gather(
            bootstrap(effective_mcp_url, _MCP_TIMEOUT, config.patterns_glob),
            load_failure_context(project_id),
            _load_session_context(),
        )
    )

    instructions = list(_COORDINATOR_INSTRUCTIONS)
    if project_context:
        instructions += ["", "── Project rules (loaded from MCP) ──────────────────", project_context]
    if failure_context:
        instructions += ["", failure_context]
    if session_summary:
        instructions += [
            "",
            "── Session summary (older turns) ─────────────────────────────────",
            session_summary,
            "──────────────────────────────────────────────────────────────────",
        ]
    if session_messages:
        lines = ["── Recent messages ───────────────────────────────────────────────"]
        for msg in session_messages:
            lines.append(f"[{msg['role']}] {msg['content'][:800]}")
        lines.append("──────────────────────────────────────────────────────────────────")
        instructions += [""] + lines

    # Collect all MCP URLs: primary (project context) + secondary (host actions)
    all_mcp_urls = [u for u in [effective_mcp_url] + (mcp_urls or []) if u]

    async with AsyncExitStack() as stack:
        mcp_list = []
        for url in all_mcp_urls:
            mcp = await stack.enter_async_context(
                MCPTools(url=url, transport="streamable-http", timeout_seconds=_MCP_TIMEOUT)
            )
            mcp_list.append(mcp)

        if agent_specs:
            members = [make_agent_from_spec(spec, *mcp_list) for spec in agent_specs]
        else:
            members = [make_coder(*mcp_list), make_reviewer(*mcp_list)]

        team = Team(
            name="AgnoHive",
            mode="coordinate",
            model=get_model(effective_coordinator, config.ollama_host),
            members=members,
            tools=mcp_list,
            instructions=instructions,
            show_members_responses=True,
            max_iterations=config.max_iterations,
        )

        span_attrs = {
            "project_id": project_id,
            "coordinator_model": effective_coordinator,
            "agent_count": len(members),
            "task": task[:120],
        }

        with _tracer.start_as_current_span("agno.task", attributes=span_attrs) as span:
            from observability.metrics import task_duration, task_counter
            t0 = time.perf_counter()
            try:
                with _tracer.start_as_current_span("agno.team.run"):
                    result = await team.arun(task)
                content = result.content if hasattr(result, "content") else str(result)
                # Fallback: if content is empty, pull from the last message in the run
                if not content and hasattr(result, "messages") and result.messages:
                    for msg in reversed(result.messages):
                        msg_content = getattr(msg, "content", None)
                        if msg_content and isinstance(msg_content, str):
                            content = msg_content
                            break
                content = content or "(no response)"
                tokens = _extract_tokens(result)
                span.set_status(trace.StatusCode.OK)
                task_counter.add(1, {"project_id": project_id, "outcome": "success"})
                await record_success(task, content, project_id)
                return content, tokens
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                await record_failure(task, str(exc), project_id)
                raise  # callers receive (content, tokens) on success; exception on failure
            finally:
                task_duration.record(
                    time.perf_counter() - t0,
                    {"project_id": project_id},
                )
