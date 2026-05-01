import asyncio
import time

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
    "Choose the FASTEST path to answer — do not call tools you don't need:",
    "",
    "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
    "  1. find_files('**/<extension>') to discover real paths",
    "  2. search_files(pattern, glob) to verify the pattern across files",
    "  3. get_file_content(path) on 1-2 files if you need more detail",
    "  → Skip get_project_context and memory_search for these queries.",
    "",
    "For feature / architecture questions ('how does auth work', 'what is the X flow'):",
    "  1. get_context_section(topic) — returns only the relevant DOCS.md section",
    "  2. memory_search(keywords) if context section is insufficient",
    "  → Use get_project_context() only when you need the full overview.",
    "",
    "For implementation tasks (write code, fix a bug):",
    "  1. get_context_section(topic) for relevant architecture context",
    "  2. ALWAYS read at least one existing reference file of the same type before writing.",
    "     NEVER skip this step — guessing conventions produces broken code.",
    "  3. Delegate writing to Coder, review to Reviewer",
    "  4. memory_store() any non-obvious insight after completing (if available)",
    "",
    "── General rules ──────────────────────────────────────────────",
    "  - Base answers on file contents, not assumptions",
    "  - Synthesise member outputs into one coherent response",
]


async def run_task_async(
    task: str,
    agent_specs: list | None = None,
    coordinator_model: str | None = None,
    mcp_url: str | None = None,
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

    async with MCPTools(url=effective_mcp_url, transport="streamable-http", timeout_seconds=_MCP_TIMEOUT) as mcp:
        if agent_specs:
            members = [make_agent_from_spec(spec, mcp) for spec in agent_specs]
        else:
            members = [make_coder(mcp), make_reviewer(mcp)]

        team = Team(
            name="AgnoHive",
            mode="coordinate",
            model=get_model(effective_coordinator, config.ollama_host),
            members=members,
            tools=[mcp],
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
                span.set_status(trace.StatusCode.OK)
                task_counter.add(1, {"project_id": project_id, "outcome": "success"})
                await record_success(task, content, project_id)
                return content
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                await record_failure(task, str(exc), project_id)
                raise
            finally:
                task_duration.record(
                    time.perf_counter() - t0,
                    {"project_id": project_id},
                )
