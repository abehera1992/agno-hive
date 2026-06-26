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
    postgres_uri: str = os.getenv("POSTGRES_URI", "")

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

    # Observability — OTLP endpoint for any OTel-compatible backend
    # e.g. existing SigNoz: http://<ekam-host>:4318
    otlp_endpoint: str = os.getenv("OTLP_ENDPOINT", "")

    # API server
    api_port: int = int(os.getenv("AGNO_PORT", "9001"))

    # Swarm behaviour
    stream: bool = os.getenv("STREAM", "false").lower() == "true"
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "25"))

    # Session persistence
    session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "30"))
    session_window: int = int(os.getenv("AGNO_SESSION_WINDOW", "6"))
    compact_threshold: int = int(os.getenv("AGNO_COMPACT_THRESHOLD", "20"))
    session_cleanup_interval: int = int(os.getenv("SESSION_CLEANUP_INTERVAL", "3600"))


config = Config()
