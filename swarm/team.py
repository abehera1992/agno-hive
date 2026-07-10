import asyncio
import time
from contextlib import AsyncExitStack

from opentelemetry import trace
from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer, make_agent_from_spec, get_model
from .feedback import record_success, record_success_bg, record_failure, load_failure_context
from config.config import config

_tracer = trace.get_tracer("agno-hive.team")

_MCP_TIMEOUT = 300  # lightrag_query synthesis ~90-120s; large file reads over Docker bind mounts can be slow — headroom so multi-read tasks don't die mid-read

_COORDINATOR_INSTRUCTIONS = [
    "── Tool restrictions ────────────────────────────────────────────",
    "  NEVER call the `agno_run` tool — you are the top-level coordinator;",
    "  calling agno_run would recurse back into this same swarm and deadlock.",
    "  NEVER output a JSON object as a delegation mechanism (e.g. {\"name\": \"delegate_task_to_member\", ...}).",
    "  You have DIRECT access to all MCP tools (get_file_content, apply_diff, write_file, etc.).",
    "  For tasks that involve reading files and making changes: call MCP tools DIRECTLY.",
    "  CRITICAL: When making code changes, you MUST call apply_diff() — NEVER return modified file",
    "  content as text output. The workflow is: get_file_content() → analyze → apply_diff() → done.",
    "  NEVER write out the new file content as a response. ONLY call apply_diff() to stage changes.",
    "  When updating an import line: use the EXACT existing import line from the file as old_string.",
    "  Do NOT guess or hallucinate import paths — copy them verbatim from get_file_content() output.",
    "  Delegate to team members (ContextRouter, Researcher, Planner, Coder, Executor, Reviewer)",
    "  only for complex multi-file research or when a specialist skill is genuinely needed.",
    "",
    "── Honesty & execution — NEVER fabricate work (read this) ─────",
    "  NEVER claim you created, updated, moved, deleted, or marked anything unless the actual",
    "  write tool was CALLED and returned success OR a staged/pending result (e.g.",
    "  'action_pending', 'review_pending', a pending action_id). Describing or narrating an",
    "  action is NOT performing it. If you did not call a write tool, you changed NOTHING —",
    "  say so plainly. Do NOT emit a confident list of 'updates I made' that you did not execute.",
    "  After every write (direct or delegated), VERIFY: re-read the item, or cite the tool's",
    "  success/pending result, BEFORE reporting it done. If a tool returned an error, an empty",
    "  result, or you called no tool, report the FAILURE / no-op — never a success.",
    "  If you were asked to update records but could not determine what to change (or the writes",
    "  were not staged), state that explicitly instead of inventing changes. A partial or staged",
    "  result is NOT 'done' — report it as staged/pending awaiting approval.",
    "",
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
    "  REJECT / CANCEL — user cancels a proposed action:",
    "    If the user says 'reject', 'cancel', 'no don't', 'don't apply', 'stop', 'abort',",
    "    'undo', 'revert', 'discard', 'roll back' in response to a proposed change → STOP.",
    "    Do NOT delegate to Coder. Do NOT call apply_diff or write_file.",
    "    If a .hive_proposed file was staged, reply exactly:",
    "      'Understood — no changes applied. To discard the staged file, type /reject or /cleanup in your hive CLI.'",
    "    If nothing was staged yet, reply: 'Understood — no changes applied.'",
    "    Do NOT attempt to delete .hive_proposed files via run_command, run_shell, or any tool.",
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
    "",
    "── Project context (fetch on demand — NOT pre-loaded) ───────────",
    "  Project context is NEVER injected into your prompt automatically.",
    "  You MUST call a tool to see it — do this BEFORE answering any task:",
    "    1. call get_file_content('hive.md')  → project snapshot (tree + summaries)",
    "    2. If hive.md not found: call get_project_context() as fallback",
    "    3. For any code-writing task: call get_file_content('patterns/ekam-code-generation-guards.md')",
    "       if that file exists — it lists exact anti-patterns with code examples that MUST be avoided.",
    "  This is your first action for any non-trivial task. Skipping it means",
    "  answering blindly from training data — never do this.",
    "",
    "── Past failure corrections — FORWARD TO CODER (CRITICAL) ───────",
    "  When past failure corrections appear above (── Past failures section), you MUST:",
    "  1. Read every correction in full before delegating any code task.",
    "  2. Include the relevant corrections VERBATIM in your delegation message to the Coder.",
    "     Example: 'CORRECTIONS FROM PAST RUNS: [paste the corrections here] — do not repeat these bugs.'",
    "  3. Do NOT assume the Coder has seen the corrections — it has not.",
    "     The Coder only knows what you tell it in your delegation message.",
    "  Skipping this means the Coder repeats the same bugs every run.",
    "",
    "── Multi-MCP tool selection ─────────────────────────────────────",
    "  hive-mcp is the PRIMARY server — use it for ALL file reads AND writes.",
    "  Typical hive-mcp tools: find_files, search_files, get_file_content, list_directory_tree,",
    "  list_directory, apply_diff, write_file, run_shell, run_docker, git_status, git_log,",
    "  web_search, web_fetch.",
    "  Project MCP is SUPPLEMENTARY — use only for tools not in hive-mcp:",
    "  search_knowledge_graph, get_context_section, and other project-specific tools.",
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
    "  - For apply_diff on the SAME file: you MAY continue calling apply_diff",
    "    on that file — each call accumulates into the same .hive_proposed file.",
    "    AFTER each apply_diff, read the staged file (<path>.hive_proposed) via",
    "    get_file_content to verify what is already applied. Then apply ONLY the",
    "    NEXT distinct change not yet in the staged file. NEVER repeat a change",
    "    already staged — always check the staged file before each diff.",
    "    Correct pattern (import + function body):",
    "      1st call: update import line  → read .hive_proposed → verify import added",
    "      2nd call: add usage in body   → review_pending (now STOP)",
    "  - STOP and report 'review_pending: <path>' ONLY when:",
    "      a) All changes to the current file are staged, OR",
    "      b) You are about to write a DIFFERENT file.",
    "  - confirm_write and reject_write do NOT exist — you cannot approve writes.",
    "  - The human selects confirm/reject in their CLI — your job ends when you report.",
    "  - If the user asks to 'delete', 'undo', or 'reject' the .hive_proposed file:",
    "    Do NOT call run_command, run_shell, or any agent tool.",
    "    Reply: 'Type /reject <path> or /cleanup in your hive CLI to discard the pending change.'",
    "    Agents cannot delete .hive_proposed files — all confirm/reject operations are CLI-only.",
    "",
    "── Output format guard ─────────────────────────────────────────",
    "  NEVER output raw model template tokens such as <|im_start|>, <|im_end|>, <|endoftext|>",
    "  or any similar special tokens. If you find yourself about to output these, stop and",
    "  reformulate your response in plain text. These are internal tokens that must never",
    "  appear in your output.",
]


def _scope_coordinator_tools(tool_names: list[str] | None, mcp_list: list):
    """Scope the coordinator's direct MCP tool surface to an explicit allowlist.

    Mirrors make_agent_from_spec's per-agent scoping (swarm/agents.py) — without this,
    the coordinator receives every tool from every connected MCP unfiltered, including
    write/staging tools (apply_diff, write_file, notion_*, confirm_action/reject_action)
    that read-only teams (planning, parallel-review) must never call. Falls back to the
    full mcp_list when no allowlist is given (preserves existing engineering-team behavior)
    or when none of the named tools are found on the connected MCPs.
    """
    if not tool_names:
        return mcp_list
    all_funcs: dict = {}
    for mcp in mcp_list:
        all_funcs.update(mcp.functions)
    scoped = [all_funcs[t] for t in tool_names if t in all_funcs]
    return scoped if scoped else mcp_list


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


def _extract_handoff_summary(task: str, content: str) -> str:
    """Extract a compact chain-boundary handoff block from a completed run's output.

    Stored as the session summary so the next chained call gets a small structured
    digest instead of the full message history — preventing context overflow.
    """
    import re
    from datetime import datetime, timezone

    # File paths: backtick-quoted OR bare paths with a known extension and a slash
    backtick_paths = re.findall(r"`([^`\n]+)`", content)
    bare_paths = re.findall(r"(?<!\w)([\w./\\-]+/[\w./\\-]+\.(?:py|ts|tsx|scss|json|md|yaml|yml))\b", content)
    all_paths = backtick_paths + bare_paths
    file_refs = list(dict.fromkeys(p for p in all_paths if ("/" in p or "\\" in p) and "." in p.split("/")[-1]))

    # review_pending paths
    pending = re.findall(r"review_pending[:\s]+([^\s\n`'\"]+)", content)
    pending = list(dict.fromkeys(pending))

    status = "PENDING_REVIEW" if ("review_pending" in content or pending) else "COMPLETE"

    # Key outcomes: bullet points (-, *, 1.) that are long enough to be meaningful
    bullets = re.findall(r"^(?:[-*]|\d+\.)\s+(.+)$", content, re.MULTILINE)
    key_outcomes = [b.lstrip("*# ") for b in bullets if len(b.strip()) > 15][:5]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    task_short = task[:200].replace("\n", " ")

    lines = [
        f"── Chain handoff ({ts}) ──────────────────────────────────────────",
        f"Task: {task_short}",
        f"Status: {status}",
    ]
    if file_refs:
        lines.append(f"Files referenced: {', '.join(file_refs[:8])}")
    if pending:
        lines.append(f"Pending reviews: {', '.join(pending)}")
    if key_outcomes:
        lines.append("Key outcomes:")
        for b in key_outcomes:
            lines.append(f"  - {b[:100]}")
    lines.append("──────────────────────────────────────────────────────────────────")

    return "\n".join(lines)


def _build_team(
    agent_specs: list | None,
    coordinator_model: str,
    coordinator_tools: list[str] | None,
    mode: str,
    mcp_list: list,
    instructions: list,
    *,
    name: str = "AgnoHive",
    description: str | None = None,
) -> Team:
    """Build a coordinator Team from agent specs (or the default Coder+Reviewer), sharing the
    already-connected `mcp_list`. Factored out of run_task_async / run_task_stream so the same
    build is reusable for router sub-teams (EK-88). `coordinator_model` is the already-resolved
    model name. `description` (default None = previous behaviour) lets the router leader route to
    this team. Behaviour is identical to the previous inline Team(...) construction when omitted."""
    if agent_specs:
        members = [make_agent_from_spec(spec, *mcp_list) for spec in agent_specs]
    else:
        members = [make_coder(*mcp_list), make_reviewer(*mcp_list)]
    return Team(
        name=name,
        description=description,
        mode=mode,
        model=get_model(coordinator_model, config.ollama_host),
        members=members,
        tools=_scope_coordinator_tools(coordinator_tools, mcp_list),
        instructions=instructions,
        show_members_responses=True,
        share_member_interactions=True,
        add_member_tools_to_context=True,
        markdown=True,
        max_iterations=config.max_iterations,
    )


async def run_task_stream(
    task: str,
    agent_specs: list | None = None,
    coordinator_model: str | None = None,
    coordinator_tools: list[str] | None = None,
    mcp_url: str | None = None,
    mcp_urls: list[str] | None = None,
    project_id: str = "default",
    session_id: str | None = None,
    mode: str = "coordinate",
):
    """Same setup as run_task_async but yields text chunks as the coordinator generates them.

    Yields:
      str  — content chunks from the coordinator as they arrive
      dict — final sentinel {"__done__": True, "content": str, "tokens": dict}
    """
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

    failure_context, (session_summary, session_messages) = (
        await asyncio.gather(
            load_failure_context(project_id),
            _load_session_context(),
        )
    )

    instructions = list(_COORDINATOR_INSTRUCTIONS)
    if failure_context:
        instructions += ["", failure_context]
    if session_summary:
        is_chain_handoff = session_summary.startswith("── Chain handoff")
        instructions += [
            "", "── Session summary (older turns) ─────────────────────────────────",
            session_summary, "──────────────────────────────────────────────────────────────────",
        ]
        # For chain-boundary handoffs, skip full message history — the compact digest
        # above replaces it. Injecting both causes context overflow on long chains.
        if not is_chain_handoff and session_messages:
            lines = ["── Recent messages ───────────────────────────────────────────────"]
            for msg in session_messages:
                lines.append(f"[{msg['role']}] {msg['content'][:800]}")
            lines.append("──────────────────────────────────────────────────────────────────")
            instructions += [""] + lines
    elif session_messages:
        lines = ["── Recent messages ───────────────────────────────────────────────"]
        for msg in session_messages:
            lines.append(f"[{msg['role']}] {msg['content'][:800]}")
        lines.append("──────────────────────────────────────────────────────────────────")
        instructions += [""] + lines

    # hive-mcp first (primary — full read+write+shell+ripgrep), project-mcp second (supplementary)
    all_mcp_urls = [u for u in (mcp_urls or []) + [effective_mcp_url] if u]

    async with AsyncExitStack() as stack:
        mcp_list = []
        # exclude_tools only for project-mcp (which exposes agno_run/agno_list_teams)
        # hive-mcp does not have these tools — passing exclude_tools causes agno to return 0 tools
        _project_mcp_url = effective_mcp_url
        for url in all_mcp_urls:
            _exclude = ["agno_run", "agno_list_teams"] if url == _project_mcp_url else None
            try:
                mcp = await stack.enter_async_context(
                    MCPTools(url=url, transport="streamable-http", timeout_seconds=_MCP_TIMEOUT, exclude_tools=_exclude)
                )
                mcp_list.append(mcp)
                print(f"[team] MCP connected: {url} ({len(mcp.functions)} tools)")
            except Exception as e:
                print(f"[team] MCP unavailable, skipping ({url}): {e}")
        if not mcp_list:
            raise RuntimeError("No MCP server available — check hive-mcp and project MCP are running")

        team = _build_team(
            agent_specs, effective_coordinator, coordinator_tools, mode, mcp_list, instructions
        )

        full_content: list[str] = []
        last_event = None

        with _tracer.start_as_current_span("agno.task.stream", attributes={
            "project_id": project_id,
            "coordinator_model": effective_coordinator,
            "agent_count": len(team.members),
            "task": task[:120],
        }):
            from observability.metrics import task_duration, task_counter
            t0 = time.perf_counter()
            try:
                async for event in team.arun(task, stream=True):
                    last_event = event
                    event_type = getattr(event, "event", "")
                    chunk = getattr(event, "content", None)
                    if isinstance(chunk, str) and chunk and event_type == "TeamRunContent":
                        full_content.append(chunk)
                        yield chunk
                combined = "".join(full_content) or "(no response)"
                tokens = _extract_tokens(last_event)
                task_counter.add(1, {"project_id": project_id, "outcome": "success"})
                # Fire-and-forget: don't block the response on post-run experience indexing.
                record_success_bg(task, combined, project_id)
                # Save a compact chain-boundary handoff summary so the next chained call
                # gets a small structured digest instead of the full message history.
                if session_id:
                    from swarm.sessions import save_handoff_summary
                    handoff = _extract_handoff_summary(task, combined)
                    asyncio.ensure_future(save_handoff_summary(session_id, handoff))
                yield {"__done__": True, "content": combined, "tokens": tokens}
            except Exception as exc:
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                try:
                    await record_failure(task, str(exc), project_id)
                except Exception:
                    pass  # LightRAG indexing is best-effort; never crash the run
                raise
            finally:
                task_duration.record(time.perf_counter() - t0, {"project_id": project_id})


async def run_task_async(
    task: str,
    agent_specs: list | None = None,
    coordinator_model: str | None = None,
    coordinator_tools: list[str] | None = None,
    mcp_url: str | None = None,
    mcp_urls: list[str] | None = None,   # secondary MCPs (e.g. hive-mcp for host actions)
    project_id: str = "default",
    session_id: str | None = None,
    mode: str = "coordinate",
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

    failure_context, (session_summary, session_messages) = (
        await asyncio.gather(
            load_failure_context(project_id),
            _load_session_context(),
        )
    )

    instructions = list(_COORDINATOR_INSTRUCTIONS)
    if failure_context:
        instructions += ["", failure_context]
    if session_summary:
        is_chain_handoff = session_summary.startswith("── Chain handoff")
        instructions += [
            "",
            "── Session summary (older turns) ─────────────────────────────────",
            session_summary,
            "──────────────────────────────────────────────────────────────────",
        ]
        # For chain-boundary handoffs, skip full message history — the compact digest
        # above replaces it. Injecting both causes context overflow on long chains.
        if not is_chain_handoff and session_messages:
            lines = ["── Recent messages ───────────────────────────────────────────────"]
            for msg in session_messages:
                lines.append(f"[{msg['role']}] {msg['content'][:800]}")
            lines.append("──────────────────────────────────────────────────────────────────")
            instructions += [""] + lines
    elif session_messages:
        lines = ["── Recent messages ───────────────────────────────────────────────"]
        for msg in session_messages:
            lines.append(f"[{msg['role']}] {msg['content'][:800]}")
        lines.append("──────────────────────────────────────────────────────────────────")
        instructions += [""] + lines

    # Collect all MCP URLs: primary (project context) + secondary (host actions)
    # hive-mcp first (primary — full read+write+shell+ripgrep), project-mcp second (supplementary)
    all_mcp_urls = [u for u in (mcp_urls or []) + [effective_mcp_url] if u]

    async with AsyncExitStack() as stack:
        mcp_list = []
        # exclude_tools only for project-mcp (which exposes agno_run/agno_list_teams)
        # hive-mcp does not have these tools — passing exclude_tools causes agno to return 0 tools
        _project_mcp_url = effective_mcp_url
        for url in all_mcp_urls:
            _exclude = ["agno_run", "agno_list_teams"] if url == _project_mcp_url else None
            try:
                mcp = await stack.enter_async_context(
                    MCPTools(url=url, transport="streamable-http", timeout_seconds=_MCP_TIMEOUT, exclude_tools=_exclude)
                )
                mcp_list.append(mcp)
                print(f"[team] MCP connected: {url} ({len(mcp.functions)} tools)")
            except Exception as e:
                print(f"[team] MCP unavailable, skipping ({url}): {e}")
        if not mcp_list:
            raise RuntimeError("No MCP server available — check hive-mcp and project MCP are running")

        team = _build_team(
            agent_specs, effective_coordinator, coordinator_tools, mode, mcp_list, instructions
        )

        span_attrs = {
            "project_id": project_id,
            "coordinator_model": effective_coordinator,
            "agent_count": len(team.members),
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
                # Fire-and-forget: don't block the response on post-run experience indexing.
                record_success_bg(task, content, project_id)
                # Save a compact chain-boundary handoff summary so the next chained call
                # gets a small structured digest instead of the full message history.
                if session_id:
                    from swarm.sessions import save_handoff_summary
                    handoff = _extract_handoff_summary(task, content)
                    asyncio.ensure_future(save_handoff_summary(session_id, handoff))
                return content, tokens
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                try:
                    await record_failure(task, str(exc), project_id)
                except Exception:
                    pass  # LightRAG indexing is best-effort; never crash the run
                raise  # callers receive (content, tokens) on success; exception on failure
            finally:
                task_duration.record(
                    time.perf_counter() - t0,
                    {"project_id": project_id},
                )
