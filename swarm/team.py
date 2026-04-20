from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer
from .memory import memory_search, memory_store
from .tool_fix import OllamaToolFix
from config.config import config


def build_swarm() -> Team:
    mcp = MCPTools(url=config.mcp_url, transport="sse")
    return Team(
        name="AgnoHive",
        mode="coordinate",
        model=OllamaToolFix(id=config.leader_model, host=config.ollama_host),
        members=[make_coder(mcp), make_reviewer(mcp)],
        tools=[mcp, memory_search, memory_store],
        instructions=[
            "At the start of every task:",
            "  1. Call get_project_context() to load full project context.",
            "  2. Call memory_search() with relevant keywords to recall prior findings.",
            "  3. Call search_knowledge_graph() to understand code structure.",
            "Decompose complex tasks and delegate to Coder or Reviewer as appropriate.",
            "Synthesise all member outputs into a single coherent final response.",
            "After completing the task, call memory_store() with any non-obvious insight.",
        ],
        show_members_responses=True,
        max_iterations=config.max_iterations,
    )
