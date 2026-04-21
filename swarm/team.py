import asyncio
from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer, get_model
from .memory import memory_search, memory_store
from config.config import config

_MCP_TIMEOUT = 60


async def run_task_async(task: str) -> str:
    async with MCPTools(url=config.mcp_url, transport="sse", timeout_seconds=_MCP_TIMEOUT) as mcp:
        team = Team(
            name="AgnoHive",
            mode="coordinate",
            model=get_model(config.leader_model, config.ollama_host),
            members=[make_coder(mcp), make_reviewer(mcp)],
            tools=[mcp, memory_search, memory_store],
            instructions=[
                "Choose the FASTEST path to answer — do not call tools you don't need:",
                "",
                "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
                "  1. find_files('**/<extension>') to discover real paths",
                "  2. search_files(pattern, glob) to verify the pattern across files",
                "  3. get_file_content(path) on 1-2 files if you need more detail",
                "  → Skip get_project_context and memory_search for these queries.",
                "",
                "For feature / architecture questions ('how does auth work', 'what is the seller flow'):",
                "  1. get_context_section(topic) — returns only the relevant DOCS.md section",
                "  2. memory_search(keywords) if context section is insufficient",
                "  → Use get_project_context() only when you need the full overview.",
                "",
                "For implementation tasks (write code, fix a bug):",
                "  1. get_context_section(topic) for relevant architecture context",
                "  2. find_files / get_file_content to read existing code before writing",
                "  3. Delegate writing to Coder, review to Reviewer",
                "  4. memory_store() any non-obvious insight after completing",
                "",
                "General rules:",
                "  - SCSS Modules (styles.className) is the styling pattern — never suggest Tailwind utilities",
                "  - Base answers on file contents, not assumptions",
                "  - Synthesise member outputs into one coherent response",
            ],
            show_members_responses=True,
            max_iterations=config.max_iterations,
        )
        result = await team.arun(task)
        return result.content if hasattr(result, "content") else str(result)


def build_swarm():
    """Legacy sync wrapper — use run_task_async for direct async calls."""
    mcp = MCPTools(url=config.mcp_url, transport="sse", timeout_seconds=_MCP_TIMEOUT)
    return Team(
        name="AgnoHive",
        mode="coordinate",
        model=get_model(config.leader_model, config.ollama_host),
        members=[make_coder(mcp), make_reviewer(mcp)],
        tools=[mcp, memory_search, memory_store],
        instructions=[
            "Choose the FASTEST path to answer — do not call tools you don't need:",
            "",
            "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
            "  1. find_files('**/<extension>') to discover real paths",
            "  2. search_files(pattern, glob) to verify the pattern across files",
            "  3. get_file_content(path) on 1-2 files if you need more detail",
            "  → Skip get_project_context and memory_search for these queries.",
            "",
            "For feature / architecture questions ('how does auth work', 'what is the seller flow'):",
            "  1. get_context_section(topic) — returns only the relevant DOCS.md section",
            "  2. memory_search(keywords) if context section is insufficient",
            "  → Use get_project_context() only when you need the full overview.",
            "",
            "For implementation tasks (write code, fix a bug):",
            "  1. get_context_section(topic) for relevant architecture context",
            "  2. find_files / get_file_content to read existing code before writing",
            "  3. Delegate writing to Coder, review to Reviewer",
            "  4. memory_store() any non-obvious insight after completing",
            "",
            "General rules:",
            "  - SCSS Modules (styles.className) is the styling pattern — never suggest Tailwind utilities",
            "  - Base answers on file contents, not assumptions",
            "  - Synthesise member outputs into one coherent response",
        ],
        show_members_responses=True,
        max_iterations=config.max_iterations,
    )
