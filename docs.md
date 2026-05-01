# AGNOHive — Technical Reference

## Project Structure

```
agno-hive/
├── main.py                  # Entry point: CLI, interactive loop, --serve, --serve-lightrag, --index
├── config/
│   └── config.py            # All config via env vars, dataclass, loaded from .env
├── swarm/
│   ├── agents.py            # Agent factory functions (all 6 agents + make_agent_from_spec)
│   ├── team.py              # run_task_async — builds and runs the Team
│   ├── bootstrap.py         # Pre-team MCP session via Streamable HTTP to fetch project patterns
│   ├── feedback.py          # Self-improving loop: record_success, record_failure, load_failure_context
│   ├── sessions.py          # Chat session persistence: CRUD, TTL cleanup, compaction
│   ├── ollama.py            # Ollama model list/pull via httpx
│   └── tool_fix.py          # OllamaToolFix: normalises all Ollama tool-call formats
├── api/
│   ├── server.py            # FastAPI app: /health, /teams, /run, /plan, /sessions
│   └── models.py            # Pydantic models: AgentSpec, RunRequest, RunResponse, SessionMeta, SessionDetail
├── teams/
│   └── engineering.yaml     # Full 6-agent engineering team spec
├── lightrag_mcp/
│   ├── server.py            # FastMCP SSE server: lightrag_insert, lightrag_query tools
│   └── rag.py               # LightRAG instance factory (per-project cache, Qdrant + AGE backends)
├── indexer/
│   ├── cli.py               # Code indexer entry point and orchestration
│   ├── parser.py            # Python AST chunker + generic text chunker
│   └── tracker.py           # SHA-256 hash state for incremental indexing
├── observability/
│   ├── setup.py             # setup_telemetry() singleton — reads standard OTEL_* env vars
│   └── metrics.py           # task_duration histogram + task_counter
├── docker/
│   ├── docker-compose.zgx.yml   # ZGX infra stack (Qdrant + PostgreSQL/AGE)
│   └── init/
│       └── 01_age.sql           # Runs once on first postgres-age start; creates AGE extension + agno graph
├── tests/
│   ├── conftest.py
│   ├── test_bootstrap.py
│   └── test_config.py
├── .env.example             # All env vars with descriptions
├── requirements.txt
├── CLAUDE.md                # High-level project context for Claude
└── docs.md                  # This file
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `` | Ollama server URL, e.g. `http://<zgx-ip>:11434` |
| `LEADER_MODEL` | `qwen3:30b-a3b` | Coordinator agent model |
| `CODER_MODEL` | `mistral-small3.1:24b` | Coder agent model |
| `REVIEWER_MODEL` | `gemma3:27b` | Reviewer agent model |
| `PLANNER_MODEL` | `deepseek-r1` | Planner agent model (Phase 2) |
| `RESEARCHER_MODEL` | `mixtral:8x7b` | Researcher agent model (Phase 2) |
| `EXECUTOR_MODEL` | `llama3.1:8b` | Executor agent model (Phase 2) |
| `ROUTER_MODEL` | `llama3.1:8b` | Context Router agent model (Phase 2) |
| `MCP_URL` | `` | Client project MCP server endpoint (Streamable HTTP), e.g. `http://<host>:9000/mcp` |
| `PATTERNS_GLOB` | `patterns/**/*.md` | Glob for bootstrap pattern files on the MCP server |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant REST API (ZGX Docker) |
| `POSTGRES_URI` | `` | PostgreSQL connection string, e.g. `postgresql://agno:agno@localhost:5432/agno_graph` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `` | OTel collector endpoint, e.g. `http://<signoz-host>:4317` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | Transport: `grpc` (port 4317) or `http/protobuf` (port 4318) |
| `OTEL_RESOURCE_ATTRIBUTES` | `` | e.g. `service.name=agno-hive,deployment.environment=dev` |
| `OTEL_SDK_DISABLED` | `false` | Set to `true` to disable telemetry entirely |
| `POSTGRES_USER` | `` | PostgreSQL user (required by LightRAG separately from `POSTGRES_URI`) |
| `POSTGRES_PASSWORD` | `` | PostgreSQL password |
| `POSTGRES_DATABASE` | `` | PostgreSQL database name |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `AGNO_PORT` | `9001` | FastAPI server port |
| `STREAM` | `false` | Enable streaming output |
| `MAX_ITERATIONS` | `5` | Max coordinator iterations per task |
| `SESSION_TTL_DAYS` | `30` | Days before unpersisted sessions are deleted |
| `AGNO_SESSION_WINDOW` | `6` | Verbatim messages injected into coordinator per request |
| `AGNO_COMPACT_THRESHOLD` | `20` | Total messages before compaction triggers |
| `SESSION_CLEANUP_INTERVAL` | `3600` | Seconds between TTL cleanup sweeps |

## API Reference

### `GET /health`
Returns server status and configured MCP URL.
```json
{ "status": "ok", "mcp_url": "http://..." }
```

### `GET /teams`
Lists all available team specs from `teams/*.yaml`.
```json
{
  "teams": [
    { "name": "coding", "description": "...", "agents": ["Coder", "Reviewer"] }
  ]
}
```

### `POST /run`
Runs a task. Returns the result with metadata.

**Request:**
```json
{
  "task": "Refactor the auth module to use JWT",
  "team": "coding",           // optional: named team from teams/*.yaml
  "agents": [...],            // optional: inline AgentSpec list (overrides team)
  "mcp_url": "http://..."     // optional: override MCP_URL for this request
}
```

**Response:**
```json
{
  "result": "...",
  "team": "coding",
  "agents_used": ["Coder", "Reviewer"],
  "models_pulled": [],
  "duration_seconds": 12.4
}
```

**Resolution order for team:** `agents` inline > `team` named > default `coding` team.

## Team YAML Format

Files in `teams/*.yaml` define reusable agent configurations.

```yaml
name: coding
description: General-purpose coding assistant.
coordinator_model: qwen3:30b-a3b   # optional, falls back to LEADER_MODEL

agents:
  - name: Coder
    model: mistral-small3.1:24b
    role: Senior software engineer who implements features and fixes bugs.
    instructions:
      - If memory_search is available via MCP, call it with relevant keywords before starting.
      - Write clean, idiomatic code.

  - name: Reviewer
    model: gemma3:27b
    role: Senior engineer who reviews code for correctness and security.
    instructions:
      - Be concise — flag real problems only.
```

## How run_task_async Works

```
run_task_async(task, agent_specs, coordinator_model, mcp_url, project_id)
  1. [parallel] bootstrap() — Streamable HTTP MCP session, reads patterns_glob → project_context
  1. [parallel] load_failure_context(project_id) — queries PostgreSQL failure_log → failure_context
  2. MCPTools(url, transport="streamable-http") — opens persistent MCP connection for agents
  3. Build team members (from agent_specs or default engineering team)
  4. Build Team (coordinator + members + MCP tools + instructions + project_context + failure_context)
  5. team.arun(task)
     ├── success → record_success() → LightRAG insert
     └── failure → record_failure() → PostgreSQL failure_log
  6. return result
```

Bootstrap and failure context loading run in parallel (`asyncio.gather`) — no latency penalty.

## How Bootstrap Works

`swarm/bootstrap.py` opens a raw `mcp.ClientSession` via **Streamable HTTP** (`streamablehttp_client`), calls `find_files(patterns_glob)` to discover pattern files, then reads each with `get_file_content()`. Falls back to `get_project_context()` if pattern files aren't found. Returns a combined string injected into the coordinator's instructions.

> **Transport note:** AGNOHive uses Streamable HTTP (the current MCP standard) for all MCP connections. Your MCP server must expose a `/mcp` endpoint. The deprecated `/sse` endpoint will return `400 Bad Request`.

## How OllamaToolFix Works

`swarm/tool_fix.py` wraps the Ollama model and normalises tool calls across four formats that different Ollama models emit:
1. Native OpenAI-compatible `tool_calls` array
2. `<tool_call>...</tool_call>` XML tags in content
3. `` <|python_tag|> `` delimited JSON
4. Bare JSON object in content

All formats are parsed and converted to the standard format before passing to Agno's agent loop.

## ZGX Infrastructure

Managed via `docker/docker-compose.zgx.yml`. Run from the repo root on ZGX:

```bash
docker compose -f docker/docker-compose.zgx.yml up -d
docker compose -f docker/docker-compose.zgx.yml down       # stop
docker compose -f docker/docker-compose.zgx.yml down -v    # stop + delete volumes
```

| Container | Image | Ports | Volume |
|---|---|---|---|
| `agno-qdrant` | `qdrant/qdrant:latest` | `6333` (REST), `6334` (gRPC) | `qdrant_data` |
| `agno-postgres-age` | `apache/age:latest` | `5432` | `pgdata` (mounted at `/var/lib/postgresql`) |

`docker/init/01_age.sql` runs once on first start, loads the AGE extension, and creates the `agno` graph.

**Note:** `pgdata` volume must be mounted at `/var/lib/postgresql` (not `/var/lib/postgresql/data`) due to PostgreSQL 18+ directory layout change in the `apache/age:latest` image.

### Checking Container Status (via SSH or ZGX terminal)

```bash
# Quick status — just agno containers
docker ps --filter "name=agno-"

# All running containers
docker ps

# Check logs if something looks wrong
docker logs agno-qdrant --tail 20
docker logs agno-postgres-age --tail 20

# Verify Qdrant health
curl http://localhost:6333/healthz

# Verify AGE graph exists in Postgres
docker exec agno-postgres-age psql -U agno -d agno_graph -c "SELECT * FROM ag_catalog.ag_graph;"

# Restart the stack
docker compose -f ~/agno-hive/docker/docker-compose.zgx.yml restart

# Stop the stack
docker compose -f ~/agno-hive/docker/docker-compose.zgx.yml down
```

## Self-Improving Loop (Phase 5)

Every task run feeds back into memory so the coordinator learns from past outcomes.

### Flow

```
run_task_async(task, project_id)
  ├── load_failure_context(project_id)   → recent failures injected into coordinator instructions
  ├── bootstrap()                        → project patterns from MCP
  ├── team.arun(task)
  │     ├── success → record_success()  → LightRAG insert (agents can query via memory_search)
  │     └── failure → record_failure()  → PostgreSQL failure_log table
  └── return result
```

### Success Path

Outcome stored in LightRAG via `rag.ainsert()` — tagged with project_id and task description. Agents retrieve past successes via `memory_search` MCP tool in subsequent runs.

### Failure Path

Failures written to a `failure_log` PostgreSQL table (auto-created on first use):

| Column | Type | Description |
|---|---|---|
| `project_id` | TEXT | Project namespace |
| `task` | TEXT | Task description (truncated to 300 chars) |
| `error_type` | TEXT | Exception class name |
| `error_message` | TEXT | Error message (truncated to 500 chars) |
| `agent` | TEXT | Agent that failed |
| `created_at` | TIMESTAMPTZ | Timestamp |

### Context Injection

Before every `team.arun()`, the 3 most recent failures for the project are loaded and appended to the coordinator's instructions:

```
── Past failures — avoid repeating these mistakes ──────────
  Task:  refactor the auth module
  Error: RuntimeError: get_file_content returned empty for src/auth.py

  Task:  add rate limiting to /api/login
  Error: TimeoutError: MCP connection timed out after 60s
```

### API Change

`POST /run` now accepts `project_id` (default: `"default"`):

```json
{ "task": "...", "project_id": "ekam", "team": "engineering" }
```

### Files

```
swarm/feedback.py   # record_success, record_failure, load_failure_context, _ensure_table
```

## Automated Code Indexer (Phase 4)

Walks a repository, chunks source files, and inserts them into LightRAG. Runs on ZGX where LightRAG and Qdrant/PostgreSQL are available. No additional dependencies — Python files are parsed with the built-in `ast` module; all other types use fixed-size text chunking.

### Usage

```bash
# First run — indexes everything
python main.py --index --path /path/to/repo --project-id ekam

# Subsequent runs — only changed/new files are reindexed
python main.py --index --path /path/to/repo --project-id ekam

# Force full reindex (ignores cached state)
python main.py --index --path /path/to/repo --project-id ekam --force

# Or run directly as a module
python -m indexer.cli --path /path/to/repo --project-id ekam
```

### How It Works

1. Walks all files, skipping `.git`, `__pycache__`, `node_modules`, binaries, etc.
2. Computes SHA-256 hash of each file
3. Compares against cached state in `~/.agno-hive/index-state/{project_id}.json`
4. Only processes changed or new files (incremental by default)
5. Python files: `ast` module extracts module docstrings, functions, and classes as individual chunks
6. All other files: split into 4000-char chunks with file/type headers
7. Each chunk inserted via `rag.ainsert()` — LightRAG extracts entities/relations into Qdrant + AGE
8. Saves updated state after a successful run

### Chunk Format (Python)

```
File: src/models/user.py
Type: class
Name: User
Docstring: ORM model for authenticated users.

class User(Base):
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True)
```

### File Structure

```
indexer/
  __init__.py
  cli.py       # Entry point, orchestration, argparse
  parser.py    # Python AST chunker + generic text chunker
  tracker.py   # SHA-256 hash state (incremental indexing)
```

State files: `~/.agno-hive/index-state/{project_id}.json` — delete to force full reindex.

## OTel Instrumentation (Phase 6)

AGNOHive uses the OpenTelemetry Python SDK with a configurable `OTLP_ENDPOINT`. If the env var is empty, telemetry is completely disabled — zero overhead, no errors.

### File Structure

```
observability/
  __init__.py
  setup.py     # setup_telemetry() singleton — call once at process startup
  metrics.py   # task_duration (histogram) and task_counter (counter) instruments
```

### What Gets Traced

| Span | Attributes |
|---|---|
| `agno.task` (root) | `project_id`, `coordinator_model`, `agent_count`, `task` (first 120 chars) |
| `agno.team.run` (child) | — |
| HTTP requests | Auto-instrumented via `FastAPIInstrumentor` |

Failures set `ERROR` status on the span and call `span.record_exception()`.

### What Gets Measured

| Metric | Type | Labels |
|---|---|---|
| `agno.task.duration` | Histogram (seconds) | `project_id` |
| `agno.task.count` | Counter | `project_id`, `outcome` (success/failure) |

### Enabling Telemetry

Set `OTLP_ENDPOINT` in `.env` and restart:

```bash
# SigNoz self-hosted on Ekam host
OTLP_ENDPOINT=http://<ekam-host-ip>:4318

# Any other OTel-compatible backend
OTLP_ENDPOINT=http://localhost:4317
```

`setup_telemetry()` is called at startup in both `--serve` (FastAPI) and CLI/interactive modes. Metrics are exported every 60 seconds.

## Observability

AGNOHive will use the OpenTelemetry Python SDK (`opentelemetry-sdk`, `opentelemetry-exporter-otlp`) with a configurable `OTLP_ENDPOINT`. Set this to the SigNoz OTLP HTTP endpoint on the Ekam host:

```
OTLP_ENDPOINT=http://<ekam-host-ip>:4318
```

SigNoz runs in Docker on the Ekam host (separate `signoz-network` from `ekam-network`). ZGX reaches it via the host machine's exposed port — Docker network isolation is irrelevant for external machine access. Verify with:

```bash
curl http://<ekam-host-ip>:4318/v1/traces -d '{}' -H 'Content-Type: application/json'
# Expect 400 (bad payload), not connection refused
```

## Global Memory

LightRAG maintains two namespaces queried on every lookup:

| Namespace | Qdrant collection | Scope |
|---|---|---|
| `project_{id}` | `project_ekam`, etc. | Per-project code knowledge |
| `global` | `project_global` | Cross-project patterns and lessons |

### How It Works

`lightrag_query` runs both namespace queries in parallel (`asyncio.gather`) and merges results with labelled sections. An empty namespace returns no output and is omitted from the merge.

```python
# lightrag_mcp/server.py
project_result, global_result = await asyncio.gather(
    project_rag.aquery(query, param=param),
    global_rag.aquery(query, param=param),
    return_exceptions=True,
)
```

### Tools

| Tool | Purpose |
|---|---|
| `lightrag_insert(text, project_id)` | Index into project namespace |
| `lightrag_insert_global(text)` | Index into shared global namespace |
| `lightrag_query(query, project_id, mode)` | Query both namespaces, merged |

### ContextRouter Routing Rules

```
Specific file/symbol questions   → lightrag_query(query, project_id, mode='local')
Cross-module/thematic context    → lightrag_query(query, project_id, mode='global')
Cross-project patterns/lessons   → lightrag_query(query, 'global', mode='hybrid')
```

---

## Human-in-the-Loop (HITL) Plan Review

### Architecture

```
hive --review "task"
  ├─ POST /plan  → planning team (ContextRouter + Researcher + Planner)
  │               returns plan text
  ├─ show plan to user
  ├─ Proceed? [Y/n]
  │   ├─ Y → POST /run → full engineering team → execute
  │   └─ N → abort, nothing executed
```

### Planning Team

`teams/planning.yaml` — ContextRouter + Researcher + Planner only. Produces a numbered step list naming responsible agent, files to touch, and risks. Does **not** implement anything.

### API Endpoints

`POST /plan` — same request body as `/run`, returns:
```json
{ "plan": "1. Researcher: ...\n2. Coder: ...", "duration_seconds": 18.2 }
```

### CLI Flags

```bash
hive --review "task"   # single task with approval gate
hive -r                # review mode REPL
> ! task               # skip review for one task in review REPL
```

---

## Persistent Chat Sessions

Every `POST /run` call creates or resumes a session in PostgreSQL. The coordinator receives the last N messages (verbatim window) plus a compacted summary of older turns so follow-up prompts always have full context.

### Database Schema

Two tables auto-created on first server start (same pattern as `failure_log`):

```sql
chat_sessions
  id              UUID PRIMARY KEY
  project_id      TEXT NOT NULL
  title           TEXT NOT NULL          -- first 80 chars of first user message
  created_at      TIMESTAMPTZ
  updated_at      TIMESTAMPTZ
  expires_at      TIMESTAMPTZ            -- NULL when persist = TRUE
  persist         BOOLEAN DEFAULT FALSE
  summary         TEXT                   -- compacted summary of older messages
  summary_through INT DEFAULT 0          -- message id up to which summary covers

session_messages
  id          SERIAL PRIMARY KEY
  session_id  UUID REFERENCES chat_sessions(id) ON DELETE CASCADE
  role        TEXT                       -- "user" | "assistant"
  content     TEXT
  created_at  TIMESTAMPTZ
```

### Context Injection

Injected into the coordinator's instructions after project context and failure context:

```
── Session summary (older turns) ──────────────────────────────────
The user is adding seller documents. Key decisions: use UUID primary
keys, POST /sellers/{id}/documents, return 201 on success.
───────────────────────────────────────────────────────────────────
── Recent messages ─────────────────────────────────────────────────
[user] add the POST endpoint for seller documents
[assistant] Done. Added POST /sellers/{seller_id}/documents to...
───────────────────────────────────────────────────────────────────
```

### Compaction

When total message count exceeds `AGNO_COMPACT_THRESHOLD` (default: 20), a fire-and-forget `asyncio.create_task` calls `llama3.1:8b` via Ollama to summarise the older messages outside the verbatim window. The summary is stored in `chat_sessions.summary` and used on all subsequent runs.

### TTL and Persistence

- Non-persisted sessions expire after `SESSION_TTL_DAYS` (default: 30 days)
- Server runs `DELETE WHERE expires_at < NOW() AND persist = FALSE` every `SESSION_CLEANUP_INTERVAL` seconds (default: 3600)
- Persisted sessions are only deleted via `DELETE /sessions/{id}`

### Session API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/sessions?project_id=X&limit=20` | List sessions for a project |
| `GET` | `/sessions/{id}` | Full session with all messages and summary |
| `DELETE` | `/sessions/{id}` | Hard delete (including persisted sessions) |
| `PATCH` | `/sessions/{id}/persist` | Mark session as permanent |

### `swarm/sessions.py` Public Interface

| Function | Returns | Description |
|---|---|---|
| `create_session(project_id, title, persist)` | `str` | Create session, return UUID |
| `append_message(session_id, role, content)` | `None` | Add one message, bump updated_at |
| `get_history(session_id, limit)` | `list[dict]` | Last N messages oldest-first |
| `get_context(session_id)` | `tuple[str, list]` | (summary, recent_messages) for injection |
| `get_session(session_id)` | `dict \| None` | Session metadata + message count |
| `list_sessions(project_id, limit)` | `list[dict]` | Session summaries, newest first |
| `delete_session(session_id)` | `bool` | Hard delete, True if deleted |
| `persist_session(session_id)` | `bool` | Mark permanent, True if updated |
| `compact_session(session_id)` | `None` | Summarise via llama3.1:8b (fire-and-forget) |
| `_cleanup_expired()` | `int` | Delete expired sessions, return count |

All functions fail silently — a PostgreSQL outage never blocks task runs.

### CLI Session State

The CLI stores the last active session in `~/.agno_last_session`:

```json
{"session_id": "a3f7c2d1-...", "project_id": "EkamApp"}
```

REPL mode reads this on start and auto-resumes if the project matches. One-shot mode never writes this file.

---

## VSCode Code Diff (Client MCP Integration)

Enabled via `WRITE_REVIEW=true` on the client MCP server. Intercepts `write_file()` and `apply_diff()` calls on existing files.

### Flow

```
agent calls write_file(path, content)
  ├─ WRITE_REVIEW=false (default) → writes immediately, returns "written: path"
  └─ WRITE_REVIEW=true + file exists
       ├─ writes proposed content to path + ".hive_proposed"
       ├─ opens: code --diff <original> <proposed>   (non-blocking)
       └─ returns "review_pending: path — diff opened in VSCode"
              ↓
       user reviews diff in VSCode
              ↓
       user tells agent: confirm or reject
              ↓
       agent calls confirm_write(path) → copies .hive_proposed → original
       OR     reject_write(path)  → deletes .hive_proposed
```

### New Tools (Client MCP)

| Tool | Args | Purpose |
|---|---|---|
| `confirm_write(relative_path)` | path | Apply the pending `.hive_proposed` file |
| `reject_write(relative_path)` | path | Delete the pending `.hive_proposed` file |

Agent instructions are updated to **never auto-confirm** — it always waits for the user to explicitly say "confirm" or "reject" after reviewing the VSCode diff.

### Enabling

Add to your client MCP server's environment:
```bash
WRITE_REVIEW=true
```

No code changes needed — `write_file` and `apply_diff` check this env var on every call.

---

## Running Tests

```bash
pytest tests/ -v
```

## Adding a New Agent (Quick Reference)

1. Add a `make_<agent>()` factory function in `swarm/agents.py` following the `make_coder` pattern
2. Add the model env var to `config/config.py` and `.env.example`
3. Reference the agent in a team YAML or wire it into `run_task_async` in `swarm/team.py`

## Adding a New Team

Create `teams/<name>.yaml` with the format above. It's immediately available via `GET /teams` and `POST /run` with `"team": "<name>"` — no code changes needed.

## LightRAG MCP Server (Phase 3)

A standalone FastMCP server (`lightrag_mcp/`) running on ZGX. Agents call it via two MCP tools:

| Tool | Args | Purpose |
|---|---|---|
| `lightrag_insert` | `text, project_id` | Index text — LLM extracts entities/relations → Qdrant + AGE |
| `lightrag_query` | `query, project_id, mode` | Retrieve context using `local`, `global`, or `hybrid` mode |

**Retrieval modes:**
- `local` — entity-centric: vector search over entity index → 1-hop graph traversal. Best for specific file/symbol questions.
- `global` — relationship-centric: vector search over relationship summaries → edge centrality ranking. Best for cross-module/thematic questions.
- `hybrid` — runs both and merges (default, recommended).

**Storage backends:**
- Vector: `QdrantVectorDBStorage` → Qdrant at `QDRANT_URL`, one collection per project (`project_{project_id}`)
- Graph: `PGGraphStorage` → PostgreSQL + AGE at `POSTGRES_URI`, shared `agno` graph

**Project isolation:** Qdrant collections are fully isolated per project. The AGE graph is shared with `project_id` as node metadata (full graph-level isolation planned for Phase 4).

### Running the LightRAG MCP Server

```bash
# Prerequisites — pull the embedding model in Ollama first
ollama pull qwen3-embedding:0.6b

# Start the server (SSE on port 9002)
python main.py --serve-lightrag

# Or directly
python -m lightrag_mcp.server
```

### LightRAG MCP Config

| Variable | Default | Description |
|---|---|---|
| `LIGHTRAG_MCP_PORT` | `9002` | SSE server port |
| `LIGHTRAG_MCP_URL` | `http://localhost:9002/sse` | URL agents use to connect |
| `LIGHTRAG_LLM_MODEL` | `mistral-small3.1:24b` | Model for entity/relation extraction during insert |
| `LIGHTRAG_EMBED_MODEL` | `qwen3-embedding:0.6b` | Ollama embedding model (must be pulled) |
| `LIGHTRAG_EMBED_DIM` | `1024` | Must match the embed model's output dimension |
| `LIGHTRAG_WORKING_DIR` | `~/.agno-hive/lightrag` | Base dir for per-project file-based KV storage |

**Constraint:** LightRAG requires a 32K+ context window model for entity extraction during insert. Do not use reasoning/chain-of-thought models (e.g. DeepSeek R1) for `LIGHTRAG_LLM_MODEL` — they slow indexing dramatically.

### File Structure

```
lightrag_mcp/
  __init__.py
  server.py    # FastMCP app, tool definitions, SSE entry point
  rag.py       # LightRAG instance factory and per-project cache
```
