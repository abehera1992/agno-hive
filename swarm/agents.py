from agno.agent import Agent
from agno.tools.mcp import MCPTools
from .memory import memory_search, memory_store
from .tool_fix import OllamaToolFix
from config.config import config


def get_model(model_id: str, host: str):
    """Always use OllamaToolFix — it safely handles both native tool_calls and
    JSON-in-content tool call patterns (llama3.3, qwen2.5, mistral, gemma3, etc.)."""
    return OllamaToolFix(id=model_id, host=host)


_BASE_PREAMBLE = [
    "When working on a task:",
    "  1. Call get_project_context() to load full project context if not already loaded.",
    "  2. Call memory_search() with relevant keywords to recall prior findings.",
    "  3. Call search_knowledge_graph() to understand code structure.",
    "After completing the task:",
    "  4. Call memory_store() with a descriptive key and any non-obvious insight.",
]


def make_coder(mcp: MCPTools) -> Agent:
    return Agent(
        name="Coder",
        model=get_model(config.coder_model, config.ollama_host),
        tools=[mcp, memory_search, memory_store],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the implementation specialist. Write clean, idiomatic code.",
            "Always read relevant files via get_file_content() before modifying code.",
            "Follow patterns already established in the codebase.",
        ],
        role="Senior software engineer who implements features and fixes bugs.",
    )


def make_reviewer(mcp: MCPTools) -> Agent:
    return Agent(
        name="Reviewer",
        model=get_model(config.reviewer_model, config.ollama_host),
        tools=[mcp, memory_search],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the code reviewer. Check for correctness, security, and consistency.",
            "Be concise — flag real problems only, not style preferences.",
            "If the implementation looks correct, say so explicitly.",
        ],
        role="Senior engineer who reviews code for correctness and security.",
    )
