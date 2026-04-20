# agno-hive

Generic Agno agentic swarm with hivemind-style collaboration. Plugs into any project by pointing it at a project-specific MCP context server.

## Architecture

```
ZGX (agno-hive — inference + orchestration)
├── Architect  (deepseek-r1:8b  — reasoning, decomposition, synthesis)
├── Coder      (qwen2.5-coder:32b — implementation)
└── Reviewer   (qwen2.5-coder:7b  — fast review, validation)
        │
        │  HTTP SSE over Tailscale
        ▼
Dev PC — MCP Context Server (project-specific)
├── get_project_context()     → CLAUDE.md + DOCS.md
├── list_recent_files()       → git log
├── get_file_content()        → any project file
├── search_knowledge_graph()  → graphify-out/graph.json
├── memory_search()           → claude_flow.embeddings
└── memory_store()            → upsert conscience
```

The **shared conscience** lives in `claude_flow.embeddings` (PostgreSQL). Every agent reads prior findings at task start and writes new insights on completion. Knowledge accumulates across sessions.

## Setup

```bash
# On ZGX
git clone git@github.com:abehera1992/agno-hive.git ~/agno-hive
cd ~/agno-hive
pip3 install -r requirements.txt --break-system-packages
cp .env.example .env
# Edit .env — set MCP_URL to your project's context server
```

## Usage

```bash
# Single task
python3 main.py "describe the seller verification flow"

# Interactive loop
python3 main.py
```

## Wiring to a New Project

1. Deploy an MCP context server for the project (expose `get_project_context`, `get_file_content`, `memory_search`, etc.)
2. Set `MCP_URL` in `.env` to that server's SSE endpoint
3. Set `MEMORY_NAMESPACE` in `.env` to isolate conscience memory per project
4. Run `python3 main.py`

## OllamaToolFix

`qwen2.5-coder` emits tool calls as JSON in the `content` field rather than the `tool_calls` API field. `swarm/tool_fix.py` patches this transparently — no changes to Ollama or Agno required.
