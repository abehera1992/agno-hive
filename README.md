# agno-hive

Generic agentic swarm for codebase-aware AI tasks. Connects to any project via MCP.

## Model Stack (ZGX)

| Role | Model | Notes |
|---|---|---|
| Coordinator | `qwen3:30b-a3b` | MoE, 30B/3B active — fast tool routing |
| Coder | `mistral-small3.1:24b` | Implementation specialist |
| Reviewer | `gemma3:27b` | Code review and correctness |

## Performance

| Query type | Latency |
|---|---|
| Code pattern (grep path) | ~26s |
| Architecture/feature (section path) | ~19s |

## Tool routing (coordinator instructions)

- **Pattern questions** → `find_files` + `search_files` (skip full context load)
- **Architecture questions** → `get_context_section(topic)` (targeted DOCS.md section)
- **Implementation tasks** → context section + file reads + delegate to Coder/Reviewer

## Config (.env)

```
OLLAMA_HOST=http://<zgx-tailscale-ip>:11434
LEADER_MODEL=qwen3:30b-a3b
CODER_MODEL=mistral-small3.1:24b
REVIEWER_MODEL=gemma3:27b
MCP_URL=http://<dev-pc-tailscale-ip>:9000/sse
DB_URL=postgresql://<user>:<pass>@<host>:5432/<db>
MEMORY_NAMESPACE=<project-namespace>
STREAM=false
MAX_ITERATIONS=5
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your values
python3 main.py "your task here"
```
