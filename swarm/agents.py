from agno.agent import Agent
from agno.tools.mcp import MCPTools

from .memory import memory_search, memory_store
from .tool_fix import OllamaToolFix
from config.config import config

_BASE_PREAMBLE = [
    "At the start of every task:",
    "  1. Call get_project_context() to load full project context (CLAUDE.md + DOCS.md).",
    "  2. Call memory_search() with relevant keywords to recall prior swarm findings.",
    "  3. Call search_knowledge_graph() to understand code structure.",
    "After completing the task:",
    "  4. Call memory_store() with a descriptive key and any non-obvious insight.",
]


def make_leader(mcp: MCPTools) -> Agent:
    return Agent(
        name="Architect",
        model=OllamaToolFix(id=config.leader_model, host=config.ollama_host),
        tools=[mcp, memory_search, memory_store],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the swarm coordinator. Decompose complex tasks and delegate to Coder or Reviewer.",
            "Synthesise all member outputs into a single coherent final response.",
            "Prioritise correctness and architectural consistency over speed.",
        ],
    )


def make_coder(mcp: MCPTools) -> Agent:
    return Agent(
        name="Coder",
        model=OllamaToolFix(id=config.coder_model, host=config.ollama_host),
        tools=[mcp, memory_search, memory_store],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the implementation specialist. Write clean, idiomatic code.",
            "Always read relevant files via get_file_content() before writing or modifying code.",
            "Follow patterns already established in the codebase.",
        ],
    )


def make_reviewer(mcp: MCPTools) -> Agent:
    return Agent(
        name="Reviewer",
        model=OllamaToolFix(id=config.reviewer_model, host=config.ollama_host),
        tools=[mcp, memory_search],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the code reviewer. Check for correctness, security issues, and consistency.",
            "Be concise — flag real problems only, not style preferences.",
            "If the implementation looks correct, say so explicitly.",
        ],
    )
