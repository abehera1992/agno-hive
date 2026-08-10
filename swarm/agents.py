from agno.agent import Agent
from agno.tools.mcp import MCPTools
from .tool_fix import OllamaToolFix
from config.config import config
from swarm import model_routing


def get_model(model_id: str, host: str, temperature: float | None = None):
    """Build the model object for an agent, honoring INFERENCE_BACKEND.

    `temperature` (default None = previous behaviour, i.e. omitted from the request,
    which means the OpenAI-API-spec default of 1.0 applies) is only wired into the
    OpenAILike/vLLM and cloud paths below -- agno's Ollama model class takes sampling
    params via a nested `options` dict rather than a top-level `temperature` field, a
    different enough shape that it's out of scope here while Ollama is a fallback
    backend, not the production path (see CLAUDE.md: ZGX runs vLLM+LiteLLM). Callers
    that want a pinned temperature must pass it explicitly per call; the default stays
    unset so every other caller of get_model() is unaffected.

    Routing lives in a DB-backed registry (AGNOHive 2.3.2 addendum, 2026-08-08),
    NOT hardcoded here — swarm/model_routing.py's in-process cache (populated from
    the model_catalog/team_role_models tables, swarm/db.py) replaces what used to
    be a hardcoded _VLLM_MODEL_MAP dict (local vLLM consolidation) + _CLOUD_ALIASES
    set (cloud gate check). This function stays synchronous and never touches the
    DB itself — model_routing.ensure_cache_loaded() (awaited once per process, at
    FastAPI startup and defensively before every _build_team() call in
    swarm/team.py) must have run first for DB-seeded routing to apply.

    An id with no model_catalog row (or a row marked inactive) falls back to
    today's pre-DB-routing behavior below — treated as local, INFERENCE_BACKEND-
    driven, no consolidation override — so a custom/unregistered model id never
    breaks silently.

    requires_cloud_gate is checked BEFORE INFERENCE_BACKEND dispatch — cloud
    routing is a per-agent choice (which model_id a team YAML names), not a global
    backend switch, so it resolves the same way regardless of whether the rest of
    the swarm is running INFERENCE_BACKEND=ollama or =vllm. Raises if
    ALLOW_CLOUD_MODELS is not set, rather than silently falling back to local or
    silently succeeding — a team YAML mistake (e.g. copy-pasting a cloud alias
    into a local-only team) must fail loudly, never send a request off-network by
    accident.

    vllm   -> llama-swap OpenAI gateway. vLLM serves native tool-calls via its
              per-model parsers (--tool-call-parser), so stock OpenAILike works with
              NO tool-fix. Model id is mapped to the llama-swap served name.
    ollama -> OllamaToolFix, which extracts tool calls from Ollama's text formats
              (native tool_calls, <tool_call> tags, <|python_tag|>, bare JSON, qwen3 XML).
    """
    route = model_routing.get_route(model_id)
    if route is None:
        route = model_routing.ModelRoute(
            model_id=model_id, kind="local", provider="local",
            vllm_served_as=None, requires_cloud_gate=False, active=True,
        )

    if route.requires_cloud_gate and not config.allow_cloud_models:
        raise RuntimeError(
            f"cloud model '{model_id}' requested but ALLOW_CLOUD_MODELS is not set — "
            f"see docs/guide/cloud-models.md before enabling cloud inference. This is a "
            f"deliberate safety gate: enabling it means this agent's requests (and any "
            f"file content it has read via MCP tools) are sent to a third-party API."
        )

    if route.kind == "cloud":
        from agno.models.openai.like import OpenAILike
        return OpenAILike(id=model_id, base_url=config.vllm_gateway_url, api_key="EMPTY", temperature=temperature)

    if config.inference_backend == "vllm":
        from agno.models.openai.like import OpenAILike
        served = route.vllm_served_as or model_id.replace(":", "-")
        return OpenAILike(id=served, base_url=config.vllm_gateway_url, api_key="EMPTY", temperature=temperature)
    return OllamaToolFix(id=model_id, host=host)


_BASE_PREAMBLE = [
    "SESSION CONTEXT: At session start, if project context hasn't been provided by the coordinator, try get_file_content('hive.md') once for a pre-built project overview (directory tree, per-module summaries). Skip silently if not found — it's optional.",
    "If lightrag_query is available via MCP, call it with relevant keywords before starting.",
    "Do NOT call lightrag_insert on the project namespace — successful outcomes are captured automatically into a separate experience namespace by the feedback loop. Free-text inserts into the project namespace poison code-grounding retrieval.",
    "HONESTY: never claim a change was made or that a task succeeded if a tool returned an error, an empty result, or did not apply. Report the exact failure instead. A partial result is a FAILURE, not a success.",
]


def format_skill_catalog(catalog: list[dict], names: list[str] | None) -> str:
    """Render the L1 skill catalog as one instruction-list entry.

    names=None means "show everything" (the coordinator's case — it can delegate
    to any skill-relevant task). A non-None list filters to those names only,
    mirroring how spec.tools already filters the MCP tool surface per agent.
    """
    if not catalog:
        return ""
    entries = catalog if names is None else [c for c in catalog if c["name"] in names]
    if not entries:
        return ""
    lines = [
        "Available skills — call load_skill(name) for the full text of ONE before "
        "acting on a task it covers. Do not load a skill unrelated to the task:"
    ]
    for e in sorted(entries, key=lambda c: c["name"]):
        lines.append(f"  - {e['name']}: {e['description']}")
    return "\n".join(lines)


def make_agent_from_spec(
    spec, *mcps: MCPTools, skill_catalog: list[dict] | None = None, tool_hooks: list | None = None
) -> Agent:
    """Build an Agent from a dynamic spec.

    If spec.tools lists tool names, only those Function objects are passed to the
    agent — everything else in the connected MCPs is hidden from the model.
    Falls back to all MCPs when spec.tools is absent or none of the names match.

    skill_catalog (already fetched once per run by swarm/team.py) is filtered to
    spec.skills and appended as one more instruction entry — the always-on L1
    index this agent sees. Omitted or empty catalog leaves instructions unchanged.

    tool_hooks (default None) is forwarded straight to agno's Agent constructor —
    swarm/team.py's _build_team passes the SAME hook instance to every member agent
    (plus the coordinator's own Team(...)) so a read-cache hook can share one dict
    across the whole run, not one per agent.
    """
    if spec.tools:
        all_funcs: dict = {}
        for mcp in mcps:
            all_funcs.update(mcp.functions)
        scoped = [all_funcs[t] for t in spec.tools if t in all_funcs]
        agent_tools = scoped if scoped else list(mcps)
    else:
        agent_tools = list(mcps)

    instructions = list(spec.instructions)
    catalog_text = format_skill_catalog(skill_catalog or [], getattr(spec, "skills", None))
    if catalog_text:
        instructions.append(catalog_text)

    return Agent(
        name=spec.name,
        model=get_model(spec.model, config.ollama_host),
        tools=agent_tools,
        instructions=instructions,
        role=spec.role,
        description=spec.description,
        markdown=True,
        add_name_to_context=True,
        tool_call_limit=config.tool_call_limit,
        tool_hooks=tool_hooks,
    )


_COMMON_AGENT_KWARGS = dict(markdown=True, add_name_to_context=True, tool_call_limit=config.tool_call_limit)


def make_coder(*mcps: MCPTools, tool_hooks: list | None = None) -> Agent:
    return Agent(
        name="Coder",
        model=get_model(config.coder_model, config.ollama_host),
        tools=list(mcps),
        tool_hooks=tool_hooks,
        description="Implementation specialist. Write clean, idiomatic code following existing patterns. Use apply_diff() for existing files, write_file() only for new ones.",
        instructions=[
            *_BASE_PREAMBLE,
            "Always read relevant files via get_file_content() before modifying code.",
            "Follow patterns already established in the codebase.",
            "File editing rules: use apply_diff() for ALL edits to existing files. Use write_file() ONLY for brand-new files.",
            "When apply_diff() returns 'review_pending': call get_file_content('<path>.hive_proposed') to read the current staged state. Then apply ONLY the NEXT distinct change not already in the staged file. DO NOT re-apply a change already staged — check first.",
            "Typical two-call pattern: (1) update the import line → check .hive_proposed → (2) add the usage in the function body. These are TWO DIFFERENT changes; never repeat the same change twice.",
            "NEVER drop existing code lines when building old_string: your old_string must match exactly what is in the file, and new_string must preserve all lines not being changed.",
            "If apply_diff() returns an error or its old_string did not match (i.e. NOT 'review_pending' and NOT success), STOP and report exactly which diff failed and why — never report that change as done. If a large multi-line block will not match, retry with a SMALLER, uniquely-anchored old_string covering only the lines that change.",
        ],
        role="Senior software engineer who implements features and fixes bugs.",
        **_COMMON_AGENT_KWARGS,
    )


def make_reviewer(*mcps: MCPTools, tool_hooks: list | None = None) -> Agent:
    return Agent(
        name="Reviewer",
        model=get_model(config.reviewer_model, config.ollama_host),
        tools=list(mcps),
        tool_hooks=tool_hooks,
        description="Code review specialist. Check correctness, security, and consistency. Flag real problems only — never style preferences.",
        instructions=[
            *_BASE_PREAMBLE,
            "Check for correctness, security, and consistency.",
            "Be concise — flag real problems only, not style preferences.",
            "Verify every change the Coder claimed was ACTUALLY applied — check the staged .hive_proposed file (or the file itself). If any apply_diff failed to match or a file was not written, report the task as INCOMPLETE and list what is missing; never confirm success on a partial apply.",
            "If the implementation looks correct, say so explicitly.",
        ],
        role="Senior engineer who reviews code for correctness and security.",
        **_COMMON_AGENT_KWARGS,
    )


def make_planner(*mcps: MCPTools) -> Agent:
    return Agent(
        name="Planner",
        model=get_model(config.planner_model, config.ollama_host),
        tools=list(mcps),
        description="Task decomposition specialist. Break complex tasks into numbered steps naming the responsible agent, files to touch, and risks.",
        instructions=[
            *_BASE_PREAMBLE,
            "Break complex tasks into clear, ordered steps.",
            "Before planning, call lightrag_query() to check if similar tasks were solved before.",
            "Output a numbered step list. Each step must name the responsible agent (Researcher, Coder, Executor, Reviewer).",
            "Do NOT implement anything yourself — plan only.",
            "If the task is simple enough for a single agent, say so and recommend skipping the plan.",
        ],
        role="Senior engineer who decomposes tasks into actionable steps for the team.",
        **_COMMON_AGENT_KWARGS,
    )


def make_researcher(*mcps: MCPTools) -> Agent:
    return Agent(
        name="Researcher",
        model=get_model(config.researcher_model, config.ollama_host),
        tools=list(mcps),
        description="Codebase investigation specialist. Read real files and ground every claim in file content — never describe from directory names alone.",
        instructions=[
            *_BASE_PREAMBLE,
            "Understand the existing codebase before anyone writes code.",
            "Use find_files() and search_files() to locate relevant modules, patterns, and conventions.",
            "Use get_file_content() to read and summarise key files.",
            "Output a concise summary: what exists, what patterns are used, what the Coder needs to know.",
            "Do NOT implement anything — research and summarise only.",
        ],
        role="Senior engineer who investigates the codebase and surfaces relevant context.",
        **_COMMON_AGENT_KWARGS,
    )


def make_executor(*mcps: MCPTools) -> Agent:
    return Agent(
        name="Executor",
        model=get_model(config.executor_model, config.ollama_host),
        tools=list(mcps),
        description="Execution and validation specialist. Run commands and report exact stdout/stderr — never paraphrase errors.",
        instructions=[
            *_BASE_PREAMBLE,
            "Run commands and validate results.",
            "Use run_shell() or run_command() to execute tests, linters, or build steps.",
            "Report stdout, stderr, and exit code verbatim — do not paraphrase errors.",
            "If a command fails, report the exact error and stop. Do not attempt fixes yourself.",
        ],
        role="Engineer who runs commands, executes tests, and reports results.",
        **_COMMON_AGENT_KWARGS,
    )


def make_context_router(*mcps: MCPTools) -> Agent:
    return Agent(
        name="ContextRouter",
        model=get_model(config.router_model, config.ollama_host),
        tools=list(mcps),
        description="Lightweight query router. Pick the fastest retrieval path and return raw results — never interpret or answer yourself.",
        instructions=[
            "Decide the fastest way to retrieve context for a query.",
            "Always use tools that are actually available — check the connected MCP, do not assume tool names.",
            "Routing rules:",
            "  - Overview/structure questions → list_directory_tree() if available, else find_files('**/*')",
            "  - 'How does X work' questions  → search_files(X, '**/*') across the whole codebase",
            "  - Specific file/symbol         → find_files() or search_files() with a targeted pattern",
            "  - Semantic/memory questions    → lightrag_query() if available, else search_knowledge_graph()",
            "  - Cross-project patterns       → lightrag_query(query, 'global', mode='hybrid') if available",
            "  - User shares a URL or GitHub link → web_fetch(url) immediately if available",
            "  - External tool/library/repo   → web_search(name) then web_fetch on best result if available",
            "Return only the retrieved context. Do not answer the question yourself.",
            "Tool call limit: 1 for specific lookups, up to 3 for overview/structure queries.",
        ],
        role="Routing agent that retrieves the right context from the right backend.",
        **_COMMON_AGENT_KWARGS,
    )
