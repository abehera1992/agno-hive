import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Ollama inference server (native on ZGX, not in Docker)
    ollama_host: str = os.getenv("OLLAMA_HOST", "")

    # Models — coordinator + full agent roster
    # NOTE: For API/hive calls the YAML team spec (teams/*.yaml) takes precedence.
    # These defaults apply only to CLI runs (python3 main.py "task") and custom agent calls.
    leader_model: str = os.getenv("LEADER_MODEL", "qwen2.5-coder:32b")
    coder_model: str = os.getenv("CODER_MODEL", "qwen2.5-coder:32b")
    reviewer_model: str = os.getenv("REVIEWER_MODEL", "qwen2.5-coder:32b")
    planner_model: str = os.getenv("PLANNER_MODEL", "qwen2.5-coder:32b")
    researcher_model: str = os.getenv("RESEARCHER_MODEL", "qwen2.5-coder:32b")
    executor_model: str = os.getenv("EXECUTOR_MODEL", "llama3.1:8b")
    router_model: str = os.getenv("ROUTER_MODEL", "llama3.1:8b")  # ContextRouter agent (swarm/agents.py) + session compaction (swarm/sessions.py). Cheap 8b is intended — do NOT raise it.
    router_classifier_model: str = os.getenv("ROUTER_CLASSIFIER_MODEL", "qwen3-coder:30b")  # router-of-teams (EK-88) classifier in api/server.py only. Needs a strong model: llama3.1:8b mis-routes (1/5 — just picks the longest description); qwen3-coder:30b routes 5/5. Override via ROUTER_CLASSIFIER_MODEL.

    # MCP context server — point at any project's MCP server
    mcp_url: str = os.getenv("MCP_URL", "")

    # Pattern discovery glob — relative to the connected project root
    patterns_glob: str = os.getenv("PATTERNS_GLOB", "patterns/**/*.md")

    # Storage — ZGX-local services (docker/docker-compose.zgx.yml)
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    # Legacy Postgres-only DSN (psycopg style, e.g. "postgresql://user:pass@host:5432/db").
    # Superseded by database_url below; kept only as its fallback so an existing ZGX
    # deployment's .env needs no change on upgrade.
    postgres_uri: str = os.getenv("POSTGRES_URI", "")

    # App storage DB (sessions, feedback log, model routing) — SQLAlchemy URL, engine-
    # agnostic by design so agno-hive ships runnable out of the box. Default is a local
    # SQLite file (zero provisioning: no server, no credentials, works the moment the
    # repo is cloned). Set DATABASE_URL to point at Postgres/MySQL/anything SQLAlchemy
    # has a dialect for instead — e.g. "postgresql+psycopg://user:pass@host:5432/db".
    # If DATABASE_URL is unset but the legacy POSTGRES_URI IS set, that value is reused
    # (translated to the "postgresql+psycopg://" dialect prefix SQLAlchemy expects) so
    # existing ZGX deployments keep working without touching their .env.
    database_url: str = os.getenv("DATABASE_URL", "")

    # LightRAG MCP server
    lightrag_mcp_port: int = int(os.getenv("LIGHTRAG_MCP_PORT", "9002"))
    lightrag_mcp_url: str = os.getenv("LIGHTRAG_MCP_URL", "http://localhost:9002/mcp")
    lightrag_llm_model: str = os.getenv("LIGHTRAG_LLM_MODEL", "llama3.1:8b")
    lightrag_embed_model: str = os.getenv("LIGHTRAG_EMBED_MODEL", "qwen3-embedding:0.6b")
    lightrag_embed_dim: int = int(os.getenv("LIGHTRAG_EMBED_DIM", "1024"))
    lightrag_working_dir: str = os.getenv("LIGHTRAG_WORKING_DIR", os.path.expanduser("~/.agno-hive/lightrag"))

    # Inference backend — Ollama->vLLM migration (EK-105). "ollama" (default) or "vllm".
    # Both code paths stay live in rag.py; flip this + restart to switch (revert = set ollama).
    inference_backend: str = os.getenv("INFERENCE_BACKEND", "ollama")
    # LightRAG LLM (extraction + query synthesis) shares the resident 30B coordinator via LiteLLM.
    # IMPORTANT: it must be the 30B (qwen3-coder, A3B MoE ~3B active) and NOT the dense 32B —
    # LightRAG generates long answers and the dense 32B is ~5x slower per token on the GB10
    # (measured: 37s on the 30B vs 186s on the 32B for the same query). Contention with the
    # coordinator on the 30B is the lesser evil vs the dense model's generation latency.
    vllm_llm_base_url: str = os.getenv("VLLM_LLM_BASE_URL", "http://localhost:4000/v1")
    vllm_llm_model: str = os.getenv("VLLM_LLM_MODEL", "qwen3-coder-30b")
    vllm_embed_base_url: str = os.getenv("VLLM_EMBED_BASE_URL", "http://localhost:8002/v1")
    vllm_embed_model: str = os.getenv("VLLM_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    # Agno swarm gateway — LiteLLM proxy (:4000) -> llama-swap (:9100, on-demand swap) -> vLLM.
    # agno talks OpenAI to LiteLLM; LiteLLM gives aliases/fallbacks/observability.
    vllm_gateway_url: str = os.getenv("VLLM_GATEWAY_URL", "http://localhost:4000/v1")
    # LightRAG EXTRACT role — 7B/8B fast model for entity extraction (role_llm_configs, v1.5.0+).
    # Routes through LiteLLM (:4000) alias "llama3.1-8b" → vllm-extract on port 9100
    # (Meta-Llama-3.1-8B-Instruct-FP8). Port 9100 must be running before this takes effect.
    vllm_extract_base_url: str = os.getenv("VLLM_EXTRACT_BASE_URL", "http://localhost:4000/v1")
    vllm_extract_model: str = os.getenv("VLLM_EXTRACT_MODEL", "llama3.1-8b")

    # Cloud model providers (AGNOHive 2.3.2) — OpenAI/Anthropic/Gemini/Perplexity/
    # HuggingFace, routed through the same LiteLLM gateway as vLLM (see
    # zgx-ai-setup/litellm-config.yaml's "Cloud providers" section). This is an
    # explicit opt-in gate, not a default — get_model() (swarm/agents.py) raises if a
    # cloud-aliased model is requested while this is false, so a YAML/config mistake
    # can never silently send a request (and whatever source code that agent has read
    # via MCP tools) to a third-party API. Default false: local-only behavior for
    # EkamApp and any other existing deployment is completely unaffected.
    allow_cloud_models: bool = os.getenv("ALLOW_CLOUD_MODELS", "false").lower() == "true"

    # Observability — OTLP endpoint for any OTel-compatible backend
    # e.g. existing SigNoz: http://<ekam-host>:4318
    otlp_endpoint: str = os.getenv("OTLP_ENDPOINT", "")

    # API server
    api_port: int = int(os.getenv("AGNO_PORT", "9001"))

    # Swarm behaviour
    stream: bool = os.getenv("STREAM", "false").lower() == "true"
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "25"))
    # Caps total tool calls within ONE agent/team run, enforced by agno itself at the
    # model-call layer (Agent/Team's own tool_call_limit kwarg) -- NOT the same thing
    # as max_iterations above. max_iterations bounds the COORDINATOR's own decision
    # loop (how many times it delegates and re-decides); it does nothing for tool
    # calls made INSIDE a single delegation to a member agent (Coder, Reviewer, ...).
    # Measured live 2026-08-06: a Coder made 18+ consecutive apply_diff calls with an
    # identical, hallucinated old_string, each one correctly refused, each refusal
    # ignored -- 36+ total tool calls in what the coordinator still counted as ONE of
    # its own iterations, because tool_call_limit was never set on any Agent/Team
    # construction in this codebase (agno's own default is None -- unbounded).
    tool_call_limit: int = int(os.getenv("TOOL_CALL_LIMIT", "25"))
    # The coordinator's model temperature -- unset anywhere else in this stack (agno's
    # OpenAIChat.temperature defaults to None, meaning omitted from the request entirely,
    # so vLLM/LiteLLM fall back to the OpenAI API spec default of 1.0). Confirmed live
    # 2026-08-10: the SAME task produced wildly different coordinator behavior across
    # back-to-back runs -- ask immediately (3.89s), full research then a prose question
    # instead of the clarification tool (140s), and a run that wandered into unrelated
    # files then generated for 18+ minutes without resolving. A low, pinned temperature
    # targets exactly this run-to-run inconsistency in which path the coordinator takes.
    # Applied only to the coordinator (swarm/team.py's _build_team), not member agents --
    # Researcher/Coder plausibly benefit from more sampling variance; this is scoped to
    # the coordinator's own decision-making role specifically.
    coordinator_temperature: float = float(os.getenv("COORDINATOR_TEMPERATURE", "0.2"))
    # Output-length cap on the coordinator's own completions -- unset anywhere else in
    # this stack (agno's OpenAIChat.max_tokens defaults to None, meaning omitted from
    # the request, so vLLM lets the model generate up to its own context-length ceiling
    # with NO client-side bound). py-spy'd a live 30+ minute run 2026-08-10: the process
    # was genuinely idle in the asyncio event loop the whole time -- not a code bug, not
    # a retry loop, just waiting on network I/O for vLLM to keep sending tokens, while
    # vLLM's own logs showed continuous non-zero generation throughput and a growing GPU
    # KV cache the entire time. The standard explanation for that exact signature is an
    # unbounded single completion (most commonly a repetition loop that never emits a
    # stop token) with nothing to cap it. 4096 is generous for a coordinator answer or
    # tool call (a normal full-pipeline answer that same day used ~946 output tokens)
    # but bounds worst case to a few minutes instead of tens. Coordinator only, same
    # scoping rationale as coordinator_temperature above -- member agents (Coder
    # especially) can legitimately need longer completions for large diffs.
    coordinator_max_tokens: int = int(os.getenv("COORDINATOR_MAX_TOKENS", "4096"))
    # Repetition penalty on the coordinator's completions -- unset anywhere else in this
    # stack (agno's OpenAIChat.frequency_penalty defaults to None -- omitted from the
    # request). Confirmed live 2026-08-10 with real content visibility (not inferred):
    # the coordinator decided on an approach, announced "Let me implement this change
    # now", then never called apply_diff -- it kept re-generating the same three-step
    # plan ("I'll implement the caching by: 1. Importing the AICache class... 2.
    # Creating a cache instance... 3. Adding cache lookup...") verbatim across multiple
    # content chunks, a genuine repetition loop, until coordinator_max_tokens cut it off.
    # max_tokens only bounds the blast radius of that loop (chops it into repeated
    # capped segments); it does nothing to stop the model wanting to repeat itself in
    # the first place. frequency_penalty penalizes a token proportionally to how many
    # times it has already appeared in this response -- the standard, targeted lever for
    # exactly this failure mode, unlike temperature (general randomness, not specifically
    # repetition). Coordinator only, same scoping rationale as
    # coordinator_temperature/coordinator_max_tokens above.
    #
    # Lowered 0.4 -> 0.15 same day: a live re-test at 0.4 showed a DIFFERENT failure --
    # ~192 consecutive TeamRunContent stream events over 5+ minutes with BOTH .content
    # and .reasoning_content genuinely empty every single time, zero tool calls
    # dispatched. Working hypothesis, not confirmed (the raw pre-parse token stream
    # isn't visible to us): frequency_penalty penalizes ANY repeated token, including
    # the structural tokens valid JSON needs (repeated quotes, braces, commas) -- a
    # value strong enough to suppress prose repetition may be strong enough to also
    # interfere with the model completing a well-formed tool call, trading "repeats
    # itself in prose" for "never finishes forming a parseable tool call at all," which
    # is arguably worse (the prior failure at least sometimes escaped via max_tokens
    # forcing a fresh attempt). 0.15 is a gentler starting point to test whether it
    # still discourages prose repetition without visibly breaking tool-call formation.
    coordinator_frequency_penalty: float = float(os.getenv("COORDINATOR_FREQUENCY_PENALTY", "0.15"))

    # Session persistence
    session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "30"))
    session_window: int = int(os.getenv("AGNO_SESSION_WINDOW", "6"))
    compact_threshold: int = int(os.getenv("AGNO_COMPACT_THRESHOLD", "20"))
    session_cleanup_interval: int = int(os.getenv("SESSION_CLEANUP_INTERVAL", "3600"))

    # Self-improvement loop — how many recent failures load_failure_context replays
    # into the coordinator. Previously hard-coded at 3; now tunable per deployment.
    #
    # Measured injected size (ekam, 2026-07-30): limit=3 -> ~2.2k chars (~540 tok);
    # limit=10 -> ~6.2k chars (~1.6k tok). Against a 262k window the size delta is
    # negligible (~0.4%) — the real cost of a high value is PROMPT DILUTION: ten
    # competing corrections compete for attention and weaken adherence to any one.
    # Default 3 for signal density. Raise only if corrections are demonstrably
    # rolling off before they stick.
    failure_context_limit: int = int(os.getenv("AGNO_FAILURE_CONTEXT_LIMIT", "3"))


config = Config()
