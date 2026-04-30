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


def make_planner(mcp: MCPTools) -> Agent:
    return Agent(
        name="Planner",
        model=get_model(config.planner_model, config.ollama_host),
        tools=[mcp],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the planning specialist. Break complex tasks into clear, ordered steps.",
            "Before planning, call memory_search() to check if similar tasks were solved before.",
            "Output a numbered step list. Each step must name the responsible agent (Researcher, Coder, Executor, Reviewer).",
            "Do NOT implement anything yourself — plan only.",
            "If the task is simple enough for a single agent, say so and recommend skipping the plan.",
        ],
        role="Senior engineer who decomposes tasks into actionable steps for the team.",
    )


def make_researcher(mcp: MCPTools) -> Agent:
    return Agent(
        name="Researcher",
        model=get_model(config.researcher_model, config.ollama_host),
        tools=[mcp],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the research specialist. Understand the existing codebase before anyone writes code.",
            "Use find_files() and search_files() to locate relevant modules, patterns, and conventions.",
            "Use get_file_content() to read and summarise key files.",
            "Output a concise summary: what exists, what patterns are used, what the Coder needs to know.",
            "Do NOT implement anything — research and summarise only.",
        ],
        role="Senior engineer who investigates the codebase and surfaces relevant context.",
    )


def make_executor(mcp: MCPTools) -> Agent:
    return Agent(
        name="Executor",
        model=get_model(config.executor_model, config.ollama_host),
        tools=[mcp],
        instructions=[
            *_BASE_PREAMBLE,
            "You are the execution specialist. Run commands and validate results.",
            "Use run_command() to execute tests, linters, or build steps.",
            "Report stdout, stderr, and exit code verbatim — do not paraphrase errors.",
            "If a command fails, report the exact error and stop. Do not attempt fixes yourself.",
        ],
        role="Engineer who runs commands, executes tests, and reports results.",
    )


def make_context_router(mcp: MCPTools) -> Agent:
    return Agent(
        name="ContextRouter",
        model=get_model(config.router_model, config.ollama_host),
        tools=[mcp],
        instructions=[
            "You are a lightweight routing agent. Decide the fastest way to retrieve context for a query.",
            "Rules:",
            "  - Specific file/symbol questions → call find_files() or search_files() directly.",
            "  - Past task or lesson questions  → call memory_search() against Qdrant.",
            "  - Thematic/cross-module questions → call memory_search() with broad keywords.",
            "  (Phase 3: lightrag_query with mode=low / high / hybrid will replace memory_search)",
            "Return only the retrieved context. Do not answer the question yourself.",
            "Be fast — one tool call maximum. If unsure, use memory_search().",
        ],
        role="Routing agent that retrieves the right context from the right backend.",
    )
