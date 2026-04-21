import asyncio
from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer, get_model
from .memory import memory_search, memory_store
from config.config import config


async def run_task_async(task: str) -> str:
    async with MCPTools(url=config.mcp_url, transport="sse") as mcp:
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
                "  3. Call search_knowledge_graph() to understand code structure.",
                "For ANY question about coding patterns, conventions, or 'how do we do X':",
                "  - You MUST call get_file_content() on 2-3 real files before answering.",
                "  - Read actual component files, not just the project summary.",
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
    mcp = MCPTools(url=config.mcp_url, transport="sse")
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
            "  3. Call search_knowledge_graph() to understand code structure.",
            "For ANY question about coding patterns, conventions, or 'how do we do X':",
            "  - You MUST call get_file_content() on 2-3 real files before answering.",
            "  - Read actual component files, not just the project summary.",
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
