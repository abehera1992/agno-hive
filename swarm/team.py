import asyncio
from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer, get_model
from .memory import memory_search, memory_store
from config.config import config

_MCP_TIMEOUT = 60  # search_files / find_files can take a few seconds on large trees


async def run_task_async(task: str) -> str:
    async with MCPTools(url=config.mcp_url, transport="sse", timeout_seconds=_MCP_TIMEOUT) as mcp:
        team = Team(
            name="AgnoHive",
            mode="coordinate",
            model=get_model(config.leader_model, config.ollama_host),
            members=[make_coder(mcp), make_reviewer(mcp)],
            tools=[mcp, memory_search, memory_store],
            instructions=[
                "At the start of every task:",
                "  1. Call get_project_context() to load full project context.",
                "  2. Call memory_search() with relevant keywords to recall prior findings.",
                "For ANY question about coding patterns, conventions, or 'how do we do X':",
                "  - Use find_files() to discover real file paths (e.g. find_files('**/*.module.scss')).",
                "  - Use search_files() to verify patterns across the codebase before concluding.",
                "  - Use get_file_content() to read a specific file once you have its path.",
                "  - Use list_directory() to explore unfamiliar areas of the project tree.",
                "  - Base your answer on what the files show, not assumptions.",
                "  - If files show SCSS Modules (styles.className), say so explicitly.",
                "  - Never assume Tailwind utility classes are used unless you see them in files.",
                "Decompose complex tasks and delegate to Coder or Reviewer as appropriate.",
                "Synthesise all member outputs into a single coherent final response.",
                "After completing the task, call memory_store() with any non-obvious insight.",
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
            "At the start of every task:",
            "  1. Call get_project_context() to load full project context.",
            "  2. Call memory_search() with relevant keywords to recall prior findings.",
            "For ANY question about coding patterns, conventions, or 'how do we do X':",
            "  - Use find_files() to discover real file paths (e.g. find_files('**/*.module.scss')).",
            "  - Use search_files() to verify patterns across the codebase before concluding.",
            "  - Use get_file_content() to read a specific file once you have its path.",
            "  - Use list_directory() to explore unfamiliar areas of the project tree.",
            "  - Base your answer on what the files show, not assumptions.",
            "  - If files show SCSS Modules (styles.className), say so explicitly.",
            "  - Never assume Tailwind utility classes are used unless you see them in files.",
            "Decompose complex tasks and delegate to Coder or Reviewer as appropriate.",
            "Synthesise all member outputs into a single coherent final response.",
            "After completing the task, call memory_store() with any non-obvious insight.",
        ],
        show_members_responses=True,
        max_iterations=config.max_iterations,
    )
