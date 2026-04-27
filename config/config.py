import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Ollama inference server
    ollama_host: str = os.getenv("OLLAMA_HOST", "")

    # Models
    leader_model: str = os.getenv("LEADER_MODEL", "qwen3:30b-a3b")
    coder_model: str = os.getenv("CODER_MODEL", "mistral-small3.1:24b")
    reviewer_model: str = os.getenv("REVIEWER_MODEL", "gemma3:27b")

    # MCP context server — point at any project's MCP server
    mcp_url: str = os.getenv("MCP_URL", "")

    # Pattern discovery glob — relative to the connected project root
    patterns_glob: str = os.getenv("PATTERNS_GLOB", "patterns/**/*.md")

    # API server
    api_port: int = int(os.getenv("AGNO_PORT", "9001"))

    # Swarm behaviour
    stream: bool = os.getenv("STREAM", "false").lower() == "true"
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "5"))


config = Config()
