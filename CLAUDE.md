# AGNOHive

@docs.md

A generic, model-agnostic agentic swarm built on [Agno](https://github.com/agno-agi/agno). Coordinates a full engineering team of Ollama-backed agents connected to any project via MCP (Model Context Protocol). All inference is local — no cloud API calls.

## Architecture in One Sentence

**Client MCP Server** (stateless, exposes project tools) → **AGNOHive on ZGX** (coordinator + 6 agents) → **ZGX Storage** (Qdrant for vector memory, PostgreSQL/AGE for graph reasoning) → **SigNoz** (OTel traces via gRPC).

## Key Design Decisions

- **Agents never touch files directly** — everything goes through MCP tools (`get_file_content`, `find_files`, `search_files`, `run_command`)
- **Streamable HTTP transport** — AGNOHive connects to MCP servers via Streamable HTTP (`/mcp` endpoint), not the deprecated SSE transport
- **Teams are YAML-configurable** — no code changes needed to define a new agent lineup (`teams/*.yaml`)
- **Models are swappable via env vars** — all model names are config-driven, no hardcoded values
- **Bootstrap runs before team construction** — fetches project patterns from MCP server to inject context into coordinator
- **OllamaToolFix wraps all models** — handles every Ollama tool-call format (native, `<tool_call>` tags, bare JSON) so agent code stays clean
- **Self-improving loop** — successes stored in LightRAG (queryable via `memory_search`), failures stored in PostgreSQL and injected into the next run's coordinator instructions
- **Global memory** — `lightrag_query` merges per-project + shared `global` namespace; `lightrag_insert_global` stores cross-project insights
- **HITL plan review** — `POST /plan` runs planning team only; `hive --review` shows plan and requires approval before execution
- **VSCode diff review** — client MCP servers enable `WRITE_REVIEW=true` to stage every file write as a `.hive_proposed` file; VS Code opens a diff tab automatically; the hive CLI shows an arrow-key selector (confirm / reject / skip); confirm/reject are local file operations in the CLI — agents cannot confirm or reject (those tools are not exposed)
- **run_command write guard** — when `WRITE_REVIEW=true`, `run_command` blocks any command that writes to files (`>`, `>>`, `sed -i`, `tee`, etc.); agents are forced to use `apply_diff` or `write_file` for all file changes
- **apply_diff always surgical** — agents must use `apply_diff` (not `write_file`) for existing files; to append, include the anchor line in both `old_string` and `new_string`
- **Persistent chat sessions** — every `POST /run` creates or resumes a session in PostgreSQL; last 6 messages injected into coordinator; sessions older than 20 messages are compacted by `llama3.1:8b`; 30-day TTL with optional `persist` flag for permanent sessions

## Infrastructure (ZGX)

| Service | Purpose | Access |
|---|---|---|
| Ollama | Local LLM inference (native, not Docker) | `OLLAMA_HOST` |
| Qdrant | Vector memory for LightRAG + project memory | `localhost:6333` |
| PostgreSQL + AGE | Graph reasoning (LightRAG) + failure log + chat sessions | `localhost:5432` |

ZGX infra is managed via `docker/docker-compose.zgx.yml`. Ollama runs natively for GPU access.

## Agent Roster (all implemented)

**Coordinator → ContextRouter → Researcher → Planner → Coder → Executor → Reviewer**

| Agent | Model | Role |
|---|---|---|
| Coordinator | `qwen3:30b-a3b` | Routes tasks, synthesises results |
| ContextRouter | `llama3.1:8b` | Picks right memory/search backend |
| Researcher | `mixtral:8x7b` | Reads and summarises codebase |
| Planner | `deepseek-r1` | Breaks tasks into ordered steps |
| Coder | `mistral-small3.1:24b` | Implements features and fixes |
| Executor | `llama3.1:8b` | Runs commands and validates results |
| Reviewer | `gemma3:27b` | Reviews code for correctness and security |

## Running AGNOHive

```bash
# FastAPI server (default port 9001)
python main.py --serve

# LightRAG MCP server (default port 9002)
python main.py --serve-lightrag

# Code indexer
python main.py --index --path /path/to/repo --project-id myproject

# Single task
python main.py "refactor the auth module"

# Interactive loop
python main.py
```

API endpoints: `GET /health`, `GET /teams`, `POST /run`, `POST /plan`, `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}`, `PATCH /sessions/{id}/persist`

## Git Workflow

Changes are made locally on Windows, committed, pushed to remote, then pulled on ZGX. **Never edit files directly on ZGX.**

```bash
# On ZGX to pick up changes
git -C ~/agno-hive pull
```

## What's Built

| Component | Status |
|---|---|
| Engineering team (6 agents) | Done |
| Dynamic YAML team specs | Done |
| FastAPI server + `/run` endpoint | Done |
| Bootstrap project context from MCP | Done |
| OllamaToolFix (all tool-call formats) | Done |
| ZGX infra — Qdrant + PostgreSQL/AGE | Done (Phase 1) |
| Expanded agent roster | Done (Phase 2) |
| LightRAG MCP server | Done (Phase 3) |
| Automated code indexer | Done (Phase 4) |
| Self-improving loop | Done (Phase 5) |
| OTel instrumentation → SigNoz | Done (Phase 6) |
| Global memory (cross-project namespace) | Done |
| HITL plan review (`POST /plan` + `hive --review`) | Done |
| VSCode diff before edits (client MCP) | Done |
| Persistent chat sessions (PostgreSQL, TTL, compaction) | Done |
| Cost-aware model routing | Planned (Phase 7) |
