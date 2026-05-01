# AGNOHive

A generic, model-agnostic agentic swarm built on [Agno](https://github.com/agno-agi/agno). Runs on a dedicated ZGX workstation, connects to any project via its MCP server, and coordinates a full engineering team of local Ollama-backed agents — no cloud API calls.

## How It Works

```
Client project  ──task──►  AGNOHive (ZGX)
                               │
                    ┌──────────▼──────────────┐
                    │  Coordinator (qwen3:30b) │
                    │  ├─ ContextRouter        │  ──MCP──►  Project MCP server
                    │  ├─ Researcher           │            find_files
                    │  ├─ Planner              │            get_file_content
                    │  ├─ Coder               │            search_files
                    │  ├─ Executor             │            run_command
                    │  └─ Reviewer             │            memory_store / memory_search
                    └─────────────────────────┘
                               │
                    ┌──────────▼──────────────┐
                    │  ZGX Storage             │
                    │  ├─ Qdrant  (vectors)    │
                    │  └─ PostgreSQL/AGE (graph)│
                    └─────────────────────────┘
```

1. Bootstrap: fetches project patterns from the MCP server to inject into the coordinator
2. Failure context from past runs is loaded and injected before every task
3. The coordinator routes to the right agents based on task type
4. After each run, successes go to LightRAG (vector memory) and failures go to PostgreSQL (failure log)
5. OTel traces flow to your existing SigNoz instance

---

## Prerequisites

### ZGX Workstation
- Ubuntu / Linux with Python 3.12+
- [Miniforge](https://github.com/conda-forge/miniforge) or standard venv
- Ollama running natively (for GPU access)
- Docker + Docker Compose (for Qdrant and PostgreSQL/AGE)
- Tailscale or network access to the client project machine

### Ollama Models (pull before first run)
```bash
ollama pull qwen3:30b-a3b          # Coordinator
ollama pull mistral-small3.1:24b   # Coder + LightRAG entity extraction
ollama pull gemma3:27b             # Reviewer
ollama pull deepseek-r1            # Planner
ollama pull mixtral:8x7b           # Researcher
ollama pull llama3.1:8b            # Executor + ContextRouter
ollama pull qwen3-embedding:0.6b   # LightRAG embeddings
```

### Client Project
- A FastMCP server exposing file/search/run tools over Streamable HTTP (e.g. at `http://<host>:9000/mcp`)
- Reachable from ZGX over Tailscale or local network

---

## Installation (on ZGX)

```bash
git clone <repo-url> ~/agno-hive
cd ~/agno-hive
pip install -r requirements.txt
```

---

## Infrastructure Setup (ZGX)

Start Qdrant and PostgreSQL/AGE via Docker:

```bash
docker compose -f docker/docker-compose.zgx.yml up -d

# Verify both are healthy
docker ps --filter "name=agno-"
curl http://localhost:6333/healthz
docker exec agno-postgres-age psql -U agno -d agno_graph -c "SELECT * FROM ag_catalog.ag_graph;"
```

---

## Configuration

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Minimum required `.env` on ZGX:

```env
# Ollama (running natively on ZGX)
OLLAMA_HOST=http://<zgx-ip>:11434

# Client project MCP server (Streamable HTTP)
MCP_URL=http://<project-host>:9000/mcp

# Storage (Docker on ZGX)
QDRANT_URL=http://localhost:6333
POSTGRES_URI=postgresql://agno:agno@localhost:5432/agno_graph

# PostgreSQL individual vars (required by LightRAG)
POSTGRES_USER=agno
POSTGRES_PASSWORD=agno
POSTGRES_DATABASE=agno_graph
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# LightRAG MCP
LIGHTRAG_MCP_PORT=9002
LIGHTRAG_MCP_URL=http://localhost:9002/sse
LIGHTRAG_LLM_MODEL=mistral-small3.1:24b
LIGHTRAG_EMBED_MODEL=qwen3-embedding:0.6b
LIGHTRAG_EMBED_DIM=1024

# Observability → SigNoz (optional, omit to disable)
OTEL_EXPORTER_OTLP_ENDPOINT=http://<signoz-host>:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_RESOURCE_ATTRIBUTES=service.name=agno-hive,deployment.environment=dev
```

---

## Running AGNOHive

### 1. Start the LightRAG MCP Server (optional but recommended)
```bash
python main.py --serve-lightrag
# → FastMCP SSE server on port 9002
```

### 2. Start the AGNOHive API Server
```bash
python main.py --serve
# → FastAPI on port 9001
```

### 3. Single task (CLI)
```bash
python main.py "How does authentication work in this project?"
```

### 4. Interactive loop
```bash
python main.py
# > your task here
# > exit
```

### 5. Index a codebase into LightRAG
```bash
# First run — indexes everything
python main.py --index --path /path/to/repo --project-id myproject

# Subsequent runs — only changed files
python main.py --index --path /path/to/repo --project-id myproject

# Force full reindex
python main.py --index --path /path/to/repo --project-id myproject --force
```

---

## CLI Client (`hive`)

AGNOHive ships a zero-dependency CLI client (`cli/hive`) that lets you use the swarm from any terminal that can reach ZGX — the same feel as an AI coding assistant in your project directory.

### Installation

```bash
# Copy to your PATH
mkdir -p ~/.local/bin
cp /path/to/agno-hive/cli/hive ~/.local/bin/hive
chmod +x ~/.local/bin/hive

# Or fetch directly from the repo
curl -o ~/.local/bin/hive https://raw.githubusercontent.com/<your-repo>/agno-hive/main/cli/hive
chmod +x ~/.local/bin/hive

# Ensure ~/.local/bin is in PATH (add to ~/.bashrc or ~/.zshrc)
export PATH="$HOME/.local/bin:$PATH"
```

### Configuration

```bash
# Add to ~/.bashrc or ~/.zshrc
export AGNO_HOST=http://<zgx-ip>:9001    # AGNOHive server address
export AGNO_PROJECT=myproject             # override project auto-detection
export AGNO_TEAM=engineering              # team to use (default: engineering)
```

`AGNO_PROJECT` is auto-detected from `git remote get-url origin` if not set — so running `hive` inside a git repo will automatically use that repo's name as the project id.

### Usage

```bash
# Single task
hive "how does authentication work in this project?"

# Interactive REPL
hive
> what models are in the User table?
> write a test for the login endpoint
> exit

# Explicit project and team
hive --project myapp --team engineering "fix the rate limiting bug"

# Connect to a different AGNOHive instance
hive --host http://other-host:9001 "explain the auth flow"
```

### Features

- **Auto-detects project** from `git remote get-url origin` in your current directory
- **Readline history** — arrow keys, Ctrl+R search, persisted in `~/.agno_history`
- **Shows agents + duration** after every response
- **Health check** on startup — warns if ZGX is unreachable before you type anything
- **Zero dependencies** — pure Python 3 stdlib, works on any machine with Python installed

---

## API Usage

### Health check
```bash
curl http://localhost:9001/health
# {"status": "ok", "mcp_url": "http://..."}
```

### List teams
```bash
curl http://localhost:9001/teams
```

### Run a task
```bash
curl -X POST http://localhost:9001/run \
  -H "Content-Type: application/json" \
  -d '{
    "task": "What files exist in the project root?",
    "project_id": "myproject",
    "team": "engineering"
  }'
```

**Request fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `task` | string | required | The task or question |
| `project_id` | string | `"default"` | Namespace for memory and failure tracking |
| `team` | string | `"engineering"` | Team spec from `teams/*.yaml` |
| `agents` | array | — | Inline agent specs (overrides team) |
| `mcp_url` | string | — | Override `MCP_URL` for this request |

**Response:**

```json
{
  "result": "...",
  "team": "engineering",
  "agents_used": ["ContextRouter", "Researcher", "Planner", "Coder", "Executor", "Reviewer"],
  "models_pulled": [],
  "duration_seconds": 30.7
}
```

---

## Agent Roster

| Agent | Model | Role |
|---|---|---|
| Coordinator | `qwen3:30b-a3b` | Routes tasks, synthesises results |
| ContextRouter | `llama3.1:8b` | Picks the right memory/search backend |
| Researcher | `mixtral:8x7b` | Reads and summarises the codebase |
| Planner | `deepseek-r1` | Breaks tasks into ordered steps |
| Coder | `mistral-small3.1:24b` | Implements features and fixes |
| Executor | `llama3.1:8b` | Runs commands and validates results |
| Reviewer | `gemma3:27b` | Reviews code for correctness and security |

---

## Teams

Teams are defined in `teams/*.yaml`. The default team is `engineering` (all 6 agents). Create a new team by adding a YAML file — no code changes needed.

```yaml
name: my-team
description: Custom team.
coordinator_model: qwen3:30b-a3b
agents:
  - name: Coder
    model: mistral-small3.1:24b
    role: Senior engineer.
    instructions:
      - Write clean, idiomatic code.
```

---

## MCP Tools AGNOHive Expects

Works with any subset — missing tools are handled gracefully:

| Tool | Purpose | Required |
|---|---|---|
| `find_files(pattern)` | Discover files by glob | Recommended |
| `get_file_content(path)` | Read a file | Recommended |
| `search_files(pattern, glob)` | Search across codebase | Recommended |
| `run_command(cmd)` | Run tests / linters | Optional |
| `memory_store(key, value)` | Persist a finding | Optional |
| `memory_search(query)` | Recall prior findings | Optional |
| `get_context_section(topic)` | Targeted docs section | Optional |
| `get_project_context()` | Full project overview | Optional |

> **Transport:** AGNOHive connects via **Streamable HTTP** (the current MCP standard). Your MCP server must expose a `/mcp` endpoint, not `/sse`.

---

## Git Workflow

All file changes are made on Windows, committed, pushed to remote, then pulled on ZGX:

```bash
# On ZGX — pick up latest changes
git -C ~/agno-hive pull
```

**Never edit files directly on ZGX.**

---

## What's Built

| Component | Status |
|---|---|
| Engineering team (6 agents) | Done |
| Dynamic YAML team specs | Done |
| FastAPI server + `/run` endpoint | Done |
| Bootstrap project context from MCP | Done |
| OllamaToolFix (all tool-call formats) | Done |
| ZGX infra — Qdrant + PostgreSQL/AGE (Docker) | Done |
| LightRAG MCP server (Qdrant + AGE backends) | Done |
| Automated code indexer (AST + incremental) | Done |
| Self-improving loop (success → LightRAG, failure → Postgres) | Done |
| OTel instrumentation → SigNoz | Done |
| Cost-aware model routing | Planned |

---

## Running Tests

```bash
pytest tests/ -v
```
