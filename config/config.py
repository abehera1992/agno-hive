import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Ollama
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://100.96.86.82:11434")

    # Models
    leader_model: str = os.getenv("LEADER_MODEL", "deepseek-r1:8b")
    coder_model: str = os.getenv("CODER_MODEL", "qwen2.5-coder:32b")
    reviewer_model: str = os.getenv("REVIEWER_MODEL", "qwen2.5-coder:7b")

    # MCP context server — point at any project's MCP server
    mcp_url: str = os.getenv("MCP_URL", "http://100.87.159.86:9000/sse")

    # Shared conscience — PostgreSQL claude_flow schema
    db_url: str = os.getenv(
        "DB_URL",
        "postgresql://abehera1992:posadmin2025@100.87.159.86:5432/ekamApp",
    )
    memory_namespace: str = os.getenv("MEMORY_NAMESPACE", "agno-hive")

    # Swarm behaviour
    stream: bool = os.getenv("STREAM", "true").lower() == "true"
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "10"))


config = Config()
