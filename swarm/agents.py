from agno.agent import Agent
from agno.tools.mcp import MCPTools
from .tool_fix import OllamaToolFix
from config.config import config


def get_model(model_id: str, host: str):
    """OllamaToolFix handles all Ollama tool call formats
    (native tool_calls, <tool_call> tags, <|python_tag|>, bare JSON)."""
    return OllamaToolFix(id=model_id, host=host)


_BASE_PREAMBLE = [
    "If memory_search is available via MCP, call it with relevant keywords before starting.",
    "If memory_store is available via MCP, call it with a descriptive key after completing.",
]


def make_agent_from_spec(spec, mcp: MCPTools) -> Agent:
    """Build an Agent from a dynamic spec (AgentSpec or any duck-typed object)."""
    return Agent(
        name=spec.name,
        model=get_model(spec.model, config.ollama_host),
        tools=[mcp],
        instructions=spec.instructions,
        role=spec.role,
    )


def make_coder(mcp: MCPTools) -> Agent:
    return Agent(
        name="Coder",
        model=get_model(config.coder_model, config.ollama_host),
        tools=[mcp],
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
        tools=[mcp],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the code reviewer. Check for correctness, security, and consistency.",
            "Be concise — flag real problems only, not style preferences.",
            "If the implementation looks correct, say so explicitly.",
        ],
        role="Senior engineer who reviews code for correctness and security.",
    )
