# agno-hive

Stateless agentic swarm for codebase-aware AI tasks. Runs on a dedicated workstation (ZGX) and connects to any project via its MCP server — no code changes needed per project, only `.env`.

## How it works

```
Client app  ──task──►  agno-hive (ZGX)  ──MCP──►  Project MCP server (client machine)
                            │                              │
                       Coordinator                  find_files
                       Coder                        get_file_content
                       Reviewer                     search_files
                                                    memory_store / memory_search
                                                    run_command
                                                    … any tool the project exposes
```

1. On startup, agno-hive bootstraps project context by fetching pattern files from the MCP server.
2. The coordinator routes the task to the appropriate tool path (pattern lookup, architecture query, or implementation).
3. Coder and Reviewer agents are delegated to as needed, all operating through the project's MCP tools.
4. Session memory and persistence are owned by the client's MCP server — agno-hive is fully stateless.

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running on the ZGX workstation with the models pulled (see model stack below)
- A project MCP server exposing file and optional memory tools (running on the client machine, reachable from ZGX over Tailscale or local network)

---

## Installation

```bash
git clone <repo-url> agno-hive
cd agno-hive
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

`.env` reference:

```env
# Ollama inference server (ZGX)
OLLAMA_HOST=http://<zgx-ip>:11434

# Models — defaults shown, override to swap models
LEADER_MODEL=qwen3:30b-a3b
CODER_MODEL=mistral-small3.1:24b
REVIEWER_MODEL=gemma3:27b

# MCP server of the project you want to work on
MCP_URL=http://<project-machine-ip>:9000/sse

# Pattern file discovery glob — relative to the project root on the MCP server
# Default is patterns/**/*.md — only set this if your project uses a different layout
PATTERNS_GLOB=patterns/**/*.md

# Swarm behaviour
STREAM=false
MAX_ITERATIONS=5
```

### Pull the Ollama models (ZGX)

```bash
ollama pull qwen3:30b-a3b
ollama pull mistral-small3.1:24b
ollama pull gemma3:27b
```

---

## Usage

**Single task:**

```bash
python main.py "How do we handle async database queries in this project?"
```

**Interactive loop:**

```bash
python main.py
# AgnoHive - type 'exit' to quit.
# > your task here
```

---

## Model stack

| Role | Model | Notes |
|---|---|---|
| Coordinator | `qwen3:30b-a3b` | MoE, 30B params / 3B active — fast tool routing |
| Coder | `mistral-small3.1:24b` | Implementation specialist |
| Reviewer | `gemma3:27b` | Code review and correctness |

## Tool routing

The coordinator picks the fastest path based on query type:

| Query type | Tool path | Latency |
|---|---|---|
| Code pattern / convention | `find_files` → `search_files` → `get_file_content` | ~26s |
| Architecture / feature | `get_context_section(topic)` | ~19s |
| Implementation task | context section + file reads → Coder → Reviewer | varies |

---

## Project patterns

Pattern files live in the **target project**, not in this repo. agno-hive discovers
them at startup by calling `find_files(PATTERNS_GLOB)` on the connected MCP server
and reading each file via `get_file_content()`.

Store patterns in your project under `patterns/*.md` (or set `PATTERNS_GLOB` to match
your layout). The loaded content is injected into the coordinator's instructions before
the first task runs.

### MCP tools agno-hive expects

agno-hive works with any subset of these — missing tools are handled gracefully:

| Tool | Purpose | Required |
|---|---|---|
| `find_files(pattern)` | Discover files by glob | Recommended |
| `get_file_content(path)` | Read a file | Recommended |
| `search_files(pattern, glob)` | Grep across codebase | Recommended |
| `get_context_section(topic)` | Targeted DOCS section | Recommended |
| `get_project_context()` | Full project overview (fallback) | Optional |
| `write_file(path, content)` | Create or overwrite a file | Optional |
| `apply_diff(path, diff)` | Surgical edits | Optional |
| `run_command(cmd)` | Run tests / linters / build | Optional |
| `memory_store(key, value)` | Persist a finding (session owned by client) | Optional |
| `memory_search(query)` | Recall prior findings | Optional |

---

## Running tests

```bash
pytest tests/ -v
```
