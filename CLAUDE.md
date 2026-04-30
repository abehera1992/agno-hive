# AGNOHive

@docs.md

A generic, model-agnostic agentic swarm built on [Agno](https://github.com/agno-agi/agno). Pairs a coordinator with specialized worker agents and connects to any project via MCP (Model Context Protocol).

## What It Does

AGNOHive runs on ZGX (a local workstation with 128GB RAM and NVIDIA GB10 GPU). It connects to a client project's MCP server to get tools (file read/write, search, run), then coordinates a team of Ollama-backed agents to complete coding tasks. All inference is local via Ollama — no cloud API calls.

## Architecture in One Sentence

**Client MCP Server** (stateless, exposes project tools) → **AGNOHive on ZGX** (coordinator + agents, all using MCP tools) → **ZGX Storage** (Qdrant for vector memory, PostgreSQL/AGE for graph reasoning).

## Key Design Decisions

- **Agents never touch files directly** — everything goes through MCP tools (`get_file_content`, `find_files`, `search_files`, `run_command`)
- **Teams are YAML-configurable** — no code changes needed to define a new agent lineup (`teams/*.yaml`)
- **Models are swappable via env vars** — all model names are config-driven, no hardcoded values
- **Bootstrap runs before team construction** — fetches project patterns from MCP server to inject context into coordinator
- **OllamaToolFix wraps all models** — handles every Ollama tool-call format (native, `<tool_call>` tags, bare JSON) so agent code stays clean

## Infrastructure (ZGX)

| Service | Purpose | Access |
|---|---|---|
| Ollama | Local LLM inference (native, not Docker) | `OLLAMA_HOST` |
| Qdrant | Vector memory for LightRAG + project memory | `localhost:6333` |
| PostgreSQL + AGE | Graph reasoning (LightRAG graph store) | `localhost:5432` |

ZGX infra is managed via `docker/docker-compose.zgx.yml`. Ollama runs natively for GPU access.

## Agent Roster

Current (implemented): **Coordinator → Coder → Reviewer**

Planned (Phase 2): **Coordinator → Planner → Researcher → Coder → Executor → Reviewer → Context Router**

## Running AGNOHive

```bash
# Single task
python main.py "refactor the auth module"

# Interactive loop
python main.py

# FastAPI server (default port 9001)
python main.py --serve
```

API endpoints: `GET /health`, `GET /teams`, `POST /run`

## Git Workflow

Changes are made locally on Windows, committed, pushed to remote, then pulled on ZGX. **Never edit files directly on ZGX.**

```bash
# On ZGX to pick up changes
git -C ~/agno-hive pull
```

## What's Built vs Planned

| Component | Status |
|---|---|
| Coordinator + Coder + Reviewer | Done |
| Dynamic YAML team specs | Done |
| FastAPI server + `/run` endpoint | Done |
| Bootstrap project context from MCP | Done |
| OllamaToolFix (all tool-call formats) | Done |
| ZGX infra (Qdrant + PostgreSQL/AGE) | Done (Phase 1) |
| Expanded agent roster (Phase 2) | Planned |
| LightRAG MCP server (Phase 3) | Planned |
| Automated code indexer (Phase 4) | Planned |
| Self-improving loop (Phase 5) | Planned |
| OTel instrumentation → SigNoz (Phase 6) | Planned |
| Cost-aware model routing (Phase 7) | Planned |
