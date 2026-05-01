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

AGNOHive ships a zero-dependency CLI client (`cli/hive`) that lets you use the swarm from any terminal that can reach ZGX — the same feel as an AI coding assistant in your project directory. Every run is backed by a **persistent chat session** stored server-side in PostgreSQL, so follow-up prompts always have full conversation context.

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

---

### Command Glossary

#### One-Shot Commands

Run a single task and exit. Each one-shot always creates a **new** session. The session ID is printed in the footer so you can resume later.

| Command | Description |
|---|---|
| `hive "task"` | Run a single task |
| `hive --review "task"` | Show plan, ask for approval, then execute |
| `hive --session <id> "task"` | Run a task in an existing session |
| `hive --persist "task"` | Run a task in a new permanent session (never auto-deleted) |
| `hive --list-sessions` | Print recent sessions for this project and exit |
| `hive --project <name> "task"` | Override project auto-detection |
| `hive --team <name> "task"` | Use a specific team (default: `engineering`) |
| `hive --host <url> "task"` | Connect to a different AGNOHive instance |

#### Interactive REPL

```bash
hive          # start REPL — auto-resumes last session for this project
hive -r       # start REPL in review mode (plan shown before every task)
hive --session <id>   # start REPL resuming a specific session
hive --persist        # start REPL with a permanent session
```

#### REPL Slash Commands

| Command | Description |
|---|---|
| `/new` | Start a fresh session (next prompt creates it) |
| `/sessions` | List recent sessions for this project |
| `/history` | Print all messages in the current session |
| `/persist` | Mark the current session as permanent |
| `/delete <id>` | Delete a session by ID |
| `/exit` | Save session ID to `~/.agno_last_session` and quit |
| `! task` | Run a single task without review (review REPL only) |

---

### Usage Examples

#### Basic tasks

```bash
# Ask a question about the codebase
hive "how does authentication work in this project?"

# Ask about a specific file
hive "what does the write_file function in mcp-server/tools/write.py do?"

# Request a code change
hive "add input validation to the POST /sellers endpoint"
```

#### Sessions — resuming context

```bash
# First task — creates a new session and prints the session ID
hive "explain the seller registration flow"

# Output footer example:
# ── ContextRouter, Researcher · 38.2s  ·  session a3f7c2d1  ·  turn 1  ·  0 msgs in context  ·  expires 2026-05-31
#   resume: hive --session a3f7c2d1-8b3e-4f2a-9c1d-000000000000

# Resume and follow up — agents remember the previous exchange
hive --session a3f7c2d1-8b3e-4f2a-9c1d-000000000000 "now add unit tests for that flow"
```

#### REPL with session continuity

```bash
# Enter REPL — auto-resumes last session for this project
hive

# Banner shows which session is active:
# AGNOHive  project EkamApp  mode engineering  http://100.96.86.82:9001
#   resuming session a3f7c2d1  (last used this project)
#   /new  /sessions  /history  /persist  /delete <id>  /exit

> explain the write_file function
> now add type hints to it
> what other functions are in the same file?
> /history           # review everything said so far
> /exit              # saves session, prints resume command
```

#### Persistent sessions

```bash
# Create a session that never expires
hive --persist "start working on the seller documents feature"

# Or mark an existing REPL session as permanent
hive
> /persist           # marks current session as permanent
> /sessions          # shows [persistent] badge next to it
```

#### Plan review (HITL)

```bash
# See the plan before anything runs
hive --review "add rate limiting to the login endpoint"

# Review REPL — every task shows a plan and asks approval
hive --review
> refactor the User model to use UUIDs
# → plan is shown, then Proceed? [Y/n]

# Skip review for a quick question inside a review REPL
> ! what files are in the auth module?
```

#### Session management

```bash
# List all sessions for this project
hive --list-sessions

# Output:
# ID          Title                                                 Msgs  Status
# ──────────────────────────────────────────────────────────────────────────────
# a3f7c2d1    explain the seller registration flow                     4  expires 2026-05-31
# b8e1f902    add rate limiting to the login endpoint                  2  persistent

# Delete a session
hive
> /delete a3f7c2d1-8b3e-4f2a-9c1d-000000000000

# Resume a specific session by ID
hive --session a3f7c2d1-8b3e-4f2a-9c1d-000000000000
```

#### Footer explained

Every response prints an informative footer:

```
── Coder, Reviewer · 42.3s  ·  session a3f7c2d1  ·  turn 3  ·  4 msgs in context  ·  expires 2026-05-31
```

| Field | Meaning |
|---|---|
| `Coder, Reviewer` | Agents that ran |
| `42.3s` | Total wall time |
| `session a3f7c2d1` | First 8 chars of session UUID |
| `turn 3` | Number of prompt/response pairs in this session |
| `4 msgs in context` | Verbatim messages injected into the coordinator |
| `summary + N msgs` | Older turns were compacted — summary + recent messages injected |
| `expires 2026-05-31` | TTL expiry date |
| `[persistent]` | Session will never auto-delete |

---

### Session Behaviour

| Mode | New session? | Saves to `~/.agno_last_session`? |
|---|---|---|
| `hive "task"` (one-shot) | Always | No |
| `hive` (REPL) | Only if no prior session for this project | Yes, on `/exit` |
| `hive --session <id>` | No — resumes specified session | Yes (REPL), No (one-shot) |
| `hive --persist` | Yes, permanent | Yes (REPL) |
| `/new` in REPL | Yes | On next `/exit` |

Sessions expire after **30 days** unless marked persistent. Expired sessions are cleaned up automatically by the server every hour.

---

### Features

- **Persistent sessions** — full conversation history stored in PostgreSQL on ZGX, resumable by session ID
- **Auto-resume** — REPL auto-resumes the last session for the current project
- **Compaction** — sessions longer than 20 messages are automatically summarised by `llama3.1:8b` so context never bloats
- **Auto-detects project** from `git remote get-url origin` in your current directory
- **Readline history** — arrow keys, Ctrl+R search, persisted in `~/.agno_history`
- **Informative footer** — shows agents, duration, session ID, turn count, context window, expiry
- **Health check** on startup — warns if ZGX is unreachable before you type anything
- **HITL review mode** (`--review`) — plan shown before every task executes, requires your approval
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
| `session_id` | string | — | Resume an existing session |
| `persist` | bool | `false` | Mark new session as permanent |

**Response:**

```json
{
  "result": "...",
  "team": "engineering",
  "agents_used": ["ContextRouter", "Researcher", "Planner", "Coder", "Executor", "Reviewer"],
  "models_pulled": [],
  "duration_seconds": 30.7,
  "session": {
    "session_id": "a3f7c2d1-8b3e-4f2a-9c1d-...",
    "turn": 1,
    "context_size": 0,
    "compacted": false,
    "persist": false,
    "expires_at": "2026-05-31T14:45:00+00:00"
  }
}
```

### Session endpoints

```bash
# List sessions for a project
curl "http://localhost:9001/sessions?project_id=myproject"

# Get full session with all messages
curl "http://localhost:9001/sessions/<session-id>"

# Delete a session
curl -X DELETE "http://localhost:9001/sessions/<session-id>"

# Mark a session as permanent
curl -X PATCH "http://localhost:9001/sessions/<session-id>/persist"
```

### Get a plan only (HITL)

Returns a step-by-step plan from the planning team without executing. Used by `hive --review`.

```bash
curl -X POST http://localhost:9001/plan \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Add rate limiting to the login endpoint",
    "project_id": "myproject"
  }'
```

**Response:**

```json
{
  "plan": "1. Researcher: read API/auth-service/routes.py ...\n2. Coder: implement ...",
  "duration_seconds": 18.2
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

Teams are defined in `teams/*.yaml`. Two built-in teams ship with AGNOHive:

| Team | Agents | Used for |
|---|---|---|
| `engineering` | All 6 (default) | Full implementation tasks |
| `planning` | ContextRouter + Researcher + Planner | HITL plan review via `POST /plan` |

Create a new team by adding a YAML file — no code changes needed.

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

## Global Memory (Cross-Project Knowledge)

AGNOHive maintains two memory namespaces in LightRAG/Qdrant:

| Namespace | Scope | Use case |
|---|---|---|
| `project_{id}` | Per-project | Code-specific knowledge, file patterns, project conventions |
| `global` | Shared across all projects | Architectural patterns, debugging approaches, reusable solutions |

Every `lightrag_query` call automatically searches **both** namespaces and merges the results — agents get project-specific and cross-project context in one call.

Agents store cross-project insights via the `lightrag_insert_global` MCP tool:
```
lightrag_insert_global("Always use UUIDs for primary keys in FastAPI services — avoids int enumeration attacks.")
```

The ContextRouter knows when to query global memory:
- Project-specific questions → `lightrag_query(query, project_id, mode='local')`
- Cross-project patterns → `lightrag_query(query, 'global', mode='hybrid')`

---

## Human-in-the-Loop (HITL) Plan Review

Before AGNOHive executes any implementation task, you can require a plan approval step. The planning team (ContextRouter + Researcher + Planner) produces a step-by-step plan — you review it and approve or reject before the full engineering team runs.

### Via CLI

```bash
# Single task with review
hive --review "add rate limiting to the login endpoint"

# Review mode REPL — every task requires approval
hive --review

# Skip review for one task in a review session
> ! what is in the auth module?  # the ! prefix bypasses review
```

### Via API

```bash
# Step 1: Get the plan
curl -X POST http://localhost:9001/plan \
  -H "Content-Type: application/json" \
  -d '{"task": "Add rate limiting", "project_id": "myproject"}'

# Step 2: Review the plan, then execute if approved
curl -X POST http://localhost:9001/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Add rate limiting", "project_id": "myproject"}'
```

---

## VSCode Code Diff Before Edits (Client MCP)

If your client project MCP server supports it, AGNOHive can show a VSCode diff of every proposed file change **before applying it** — giving you final say on every edit.

Enable on the client MCP server side with `WRITE_REVIEW=true` in the server's environment. When an agent calls `write_file()` or `apply_diff()` on an existing file:

1. A `.hive_proposed` temp file is created with the proposed content
2. VSCode opens a diff automatically (original ← → proposed)
3. The agent receives `review_pending: path/to/file` and waits
4. You review the diff in VSCode, then tell the agent to proceed:
   - **Apply:** ask the agent to call `confirm_write('path/to/file')`
   - **Reject:** ask the agent to call `reject_write('path/to/file')`

The agent's instructions prevent it from auto-confirming — it always waits for your explicit approval.

> This feature requires `confirm_write` and `reject_write` tools to be registered in your client project's MCP server. See `tools/write.py` in your MCP server for the implementation.

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
| Global memory (cross-project shared namespace) | Done |
| HITL plan review (`POST /plan` + `hive --review`) | Done |
| VSCode diff before edits (client MCP integration) | Done |
| Persistent chat sessions (PostgreSQL, TTL, compaction) | Done |
| Cost-aware model routing | Planned |

---

## Running Tests

```bash
pytest tests/ -v
```
