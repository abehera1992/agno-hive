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
    "── Conversational turn detection (read this first) ─────────────",
    "  Not every message is a task. Classify the message before reaching for tools:",
    "",
    "  ACTION APPROVAL — always a TASK, never conversational:",
    "    If the agent just described or proposed a change and the user says any of:",
    "    'go ahead', 'apply it', 'do it', 'update it', 'yes', 'ok', 'looks good proceed',",
    "    'make the change', 'write it', 'confirm', 'sure', 'use that' — treat as TASK.",
    "    → Delegate the write/implementation to the Coder immediately.",
    "    → Do NOT reply in plain prose about what you will do. Delegate and act.",
    "",
    "  CONVERSATIONAL — respond directly, NO tool calls:",
    "    - User shares an opinion, agrees, disagrees, or adds their own perspective",
    "    - User asks a simple follow-up that is already answered by the prior response",
    "    - User says 'I think...', 'but...', 'yeah...', 'that makes sense because...'",
    "    - No new URL, no new codebase question, no action requested",
    "    - NOT an approval of a proposed change (see ACTION APPROVAL above)",
    "  For conversational turns: reply as a knowledgeable colleague would — directly,",
    "  in plain prose, without structured reports or tool calls.",
    "",
    "  TASK — use tools as needed:",
    "    - New URL to fetch, new file to read, new codebase question",
    "    - Explicit action: 'add X', 'fix Y', 'list Z', 'search for W'",
    "    - Question that cannot be answered from the current session context",
    "  Do NOT re-fetch a URL or re-search a topic already retrieved in this session.",
    "  Tool calls cost time — only use them when the information is genuinely missing.",
    "",
    "── Scan-first rule (tasks only) ────────────────────────────────",
    "  User prompts are often short and vague. Do not infer — discover.",
    "  Before answering any question about structure, features, or behaviour:",
    "    1. find_files('**/*') — get the full file tree",
    "    2. search_files(keyword, '**/*') — find all occurrences of the topic",
    "    3. get_file_content(path) — read specific files to verify details",
    "  Never describe a directory or module from its name alone.",
    "  Never stop at the first interesting result for overview questions — cover everything.",
    "  If the user includes a URL in their message, call web_fetch(url) immediately — before any other tool.",
    "  If asked about an external library, tool, GitHub repo, or technology, call web_search() then web_fetch()",
    "  on the best result — do not answer from training data alone for external topics.",
    "",
    "Choose the FASTEST path to answer — do not call tools you don't need:",
    "",
    "For overview / structure questions ('list directories', 'what does X do', 'show me the project'):",
    "  1. list_directory_tree() if available — returns the full directory skeleton with no result cap",
    "     OR find_files('**/*') if list_directory_tree is not available",
    "  2. For each top-level directory: read one entry file (README, main.py, __init__.py, config)",
    "  3. Return a grounded summary covering ALL directories — not just the first one found.",
    "  → Do not use get_project_context() as a shortcut — it may be stale or incomplete.",
    "",
    "For 'how does X work' / feature questions:",
    "  1. search_files(X, '**/*') — find every file that references X",
    "  2. get_file_content() on the 2-3 most relevant files",
    "  3. If the project MCP exposes a documentation section tool (e.g. get_context_section),",
    "     call it with the topic keyword — do not assume the tool name or the doc file name.",
    "  → Search before you read — searching tells you which files are worth reading.",
    "",
    "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
    "  1. find_files('**/<extension>') to discover real paths",
    "  2. search_files(pattern, glob) to verify the pattern across files",
    "  3. get_file_content(path) on 1-2 files if you need more detail",
    "  → Skip broad context tools for these queries — go straight to the files.",
    "",
    "For implementation tasks (write code, fix a bug):",
    "  1. If a documentation/context tool is available (check connected MCP tools), call it",
    "     to load architecture context — do not assume the tool or doc file name.",
    "  2. ALWAYS read at least one existing reference file of the same type before writing.",
    "     NEVER skip this step — guessing conventions produces broken code.",
    "  3. Delegate writing to Coder, review to Reviewer",
    "  4. memory_store() any non-obvious insight after completing (if available)",
    "",
    "── Multi-MCP tool selection ─────────────────────────────────────",
    "  When multiple MCP servers are connected, use the right one for each job:",
    "  - PROJECT MCP: reading context, understanding the project, app-specific workflows.",
    "    Typical tools: get_file_content, find_files, search_files, list_directory_tree,",
    "    list_directory, memory_search, and any project-specific context or search tools.",
    "  - hive-mcp: writing files, running commands, Docker operations, all host-level actions.",
    "    Typical tools: apply_diff, write_file, run_shell, run_docker, git_status, git_log.",
    "  If only one MCP is connected, use it for everything.",
    "  Discover available tools from the connected MCP — do not assume tool names exist.",
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
    "── External docs vs project code (CRITICAL) ─────────────────",
    "  When asked to compare this project against framework docs, external libraries,",
    "  or best-practice guides — ALWAYS use this order, never reverse it:",
    "  1. Read the project source files FIRST (get_file_content, search_files).",
    "     Understand exactly what the code does before consulting any external source.",
    "  2. Fetch the external documentation SECOND (web_fetch / web_search).",
    "  3. Compare with explicit citations from BOTH sides:",
    "     - Every claim about what this project does     → cite file:line",
    "     - Every claim about what the external docs say → cite URL + section heading",
    "  4. If an external pattern conflicts with how this project works:",
    "     a. Read CLAUDE.md / docs.md (via get_file_content) to check if the",
    "        difference is intentional design — many patterns here deliberately",
    "        differ from framework defaults.",
    "     b. State the conflict explicitly: 'Docs say X; this project does Y because Z.'",
    "     c. NEVER assume the external pattern is right and the project is wrong.",
    "  5. If you cannot find a project file that confirms a claim, label it:",
    "     'inference from docs — not verified in codebase'",
    "     NEVER present an unverified inference as a confirmed requirement.",
    "  Self-analysis trap: when studying this project's own config or architecture",
    "  against a framework's examples, the project's established design takes",
    "  precedence over framework examples unless the code itself is broken.",
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
            share_member_interactions=True,
            add_member_tools_to_context=True,
            markdown=True,
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
