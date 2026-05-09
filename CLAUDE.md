# AGNOHive

@docs.md

A generic, model-agnostic agentic swarm built on [Agno](https://github.com/agno-agi/agno). Coordinates a full engineering team of Ollama-backed agents connected to any project via MCP (Model Context Protocol). All inference is local — no cloud API calls.

## Architecture in One Sentence

**Client project** → **hive-mcp** (host actions: files, shell, Docker, git) + **project MCP** (context: code search, memory) → **AGNOHive on ZGX** (coordinator + 6 agents) → **ZGX Storage** (Qdrant + PostgreSQL/AGE) → **SigNoz** (OTel traces).

## Key Design Decisions

- **Dual-MCP architecture** — two MCP connections per run: `mcp_url` (primary, project context) and `mcp_urls` (secondary list, host actions via hive-mcp); agents choose the right one per operation
- **hive-mcp is project-agnostic** — Docker image on GHCR, runs on any client machine; provides raw filesystem, shell, Docker, git, and `index_project` bootstrap
- **Tailscale as mandatory transport** — stable IPs, WireGuard security, no NAT issues; hive CLI auto-detects Tailscale IP for both MCP URLs
- **Agents never touch files directly** — everything goes through MCP tools (`get_file_content`, `find_files`, `search_files`, `run_command` for reads; `apply_diff`, `write_file` for writes via hive-mcp)
- **Streamable HTTP transport** — AGNOHive connects to all MCP servers via Streamable HTTP (`/mcp` endpoint); the deprecated SSE (`/sse`) endpoint is not used
- **Teams are YAML-configurable** — no code changes needed to define a new agent lineup (`teams/*.yaml`)
- **Models are swappable via env vars** — all model names are config-driven, no hardcoded values
- **Bootstrap runs before team construction** — fetches project patterns from MCP server to inject context into coordinator
- **OllamaToolFix wraps all models** — handles every Ollama tool-call format (native, `<tool_call>` tags, bare JSON) so agent code stays clean
- **Self-improving loop** — successes stored in LightRAG (queryable via `memory_search`), failures stored in PostgreSQL and injected into the next run's coordinator instructions
- **Global memory** — `lightrag_query` merges per-project + shared `global` namespace; `lightrag_insert_global` stores cross-project insights
- **HITL plan review** — `POST /plan` runs planning team only; `hive --review` shows plan and requires approval before execution
- **VSCode diff review** — hive-mcp enables `WRITE_REVIEW=true` to stage every file write as a `.hive_proposed` file; hive CLI shows inline terminal diff + arrow-key selector (confirm / reject / skip); confirm/reject are local file operations in the CLI — agents cannot confirm or reject (those tools are not exposed)
- **run_command write guard** — when `WRITE_REVIEW=true`, `run_command` blocks any command that writes to files (`>`, `>>`, `sed -i`, `tee`, etc.); agents are forced to use `apply_diff` or `write_file` for all file changes
- **apply_diff always surgical** — agents must use `apply_diff` (not `write_file`) for existing files; to append, include the anchor line in both `old_string` and `new_string`
- **Persistent chat sessions** — every `POST /run` creates or resumes a session in PostgreSQL; last 6 messages injected into coordinator; sessions older than 20 messages are compacted by `llama3.1:8b`; 30-day TTL with optional `persist` flag for permanent sessions
- **hive bootstrap** — `hive --bootstrap` calls `index_project` on hive-mcp, which chunks the project with AST (Python) or text windows (other files) and inserts into LightRAG via Streamable HTTP
- **Web search + fetch via hive-mcp** — `web_search(query)` uses DuckDuckGo (no API key); `web_fetch(url)` fetches any URL with cleaned text extraction; GitHub repo URLs return README + metadata via GitHub API; gated by `WEB_SEARCH_ENABLED=true` on the hive-mcp container; agents auto-fetch URLs shared in prompts and search when asked about external tools/libraries
- **Scan-first prompt engineering** — agents are instructed to discover before inferring: for overview/structure prompts `find_files('**/*')` runs first; for "how does X work" prompts `search_files(X, '**/*')` runs first; the Researcher must cover every top-level directory and ground every claim in a real file read — stopping at the first interesting result is treated as a failure
- **Project-agnostic agent instructions** — no hardcoded directory names, file names, or tool names anywhere in coordinator instructions, team YAMLs, or agent factory functions; all tool references use "if available" qualifiers; directory names are always derived at runtime via `list_directory_tree()` or `find_files()` rather than assumed; this ensures the same team specs work across any project connected via MCP

## Agent Instruction Design Principles

Rules for writing or modifying coordinator instructions (`swarm/team.py`), team specs (`teams/*.yaml`), and agent factories (`swarm/agents.py`). Apply these whenever enhancing prompt engineering.

**1. Never hardcode paths or filenames**
Instructions must not reference specific directory names (`API/`, `services/`), file names (`DOCS.md`, `main.py`), or file extensions as fixed values. Use patterns (`**/*.py`, `**/routes*`) or derive the target at runtime.

```yaml
# Bad — breaks on any other project
"call list_directory('API/') to enumerate services"

# Good — derives the directory from what the project actually exposes
"derive the target directory from list_directory_tree() or find_files() first, then call list_directory(target)"
```

**2. Never hardcode tool names**
Different project MCPs expose different tools. Always qualify with "if available" and instruct agents to discover what tools are actually connected.

```yaml
# Bad — assumes every project has this tool
"call get_context_section(topic) for architecture context"

# Good — degrades gracefully when the tool isn't present
"if the project MCP exposes a documentation section tool, call it — do not assume the tool name"
```

**3. Scan before inferring**
Agents must call file discovery tools before answering. Describing a module from its directory name alone is forbidden. The order is always: discover → search → read → answer.

```
list_directory_tree()           # full skeleton, no cap
  → list_directory(target)      # enumerate one directory's children
    → search_files(X, '**/*')   # find relevant files
      → get_file_content(path)  # read to verify
        → answer
```

**4. Enumerate fully before answering**
For any task covering a directory (list APIs, list services, map routes), the agent must enumerate ALL subdirectories first, process each one, and only write the answer after the last one. Skipping a subdirectory because "enough results" were found from a previous one is a failure.

**5. Tool call limits are query-type dependent**
Not all queries need the same number of tool calls. The limit should reflect the query type:
- Specific file/symbol lookup: 1 call
- Feature/pattern questions: 2–3 calls (search → read)
- Overview/enumeration questions: up to the number of subdirectories × 2

Applying a single blanket limit (e.g. "one tool call maximum") breaks comprehensive tasks.

**6. Routing degrades gracefully**
ContextRouter routing rules must work whether or not optional backends (LightRAG, memory) are connected. Each routing tier should have a fallback:

```yaml
# Semantic questions
→ lightrag_query() if available, else memory_search() if available, else search_files()
```

**7. Result caps must match the task scope**
`find_files` and `search_files` have per-call caps. For large projects a cap of 50 means the agent sees only a fraction of the codebase. Current caps: `find_files` 200, `search_files` 80. Prefer `list_directory_tree()` (no cap) for structure questions. Raise caps before adding more instructions — hitting the cap silently is worse than a slower scan.

## Infrastructure (ZGX)

| Service | Purpose | Access |
|---|---|---|
| Ollama | Local LLM inference (native, not Docker) | `OLLAMA_HOST` |
| Qdrant | Vector memory for LightRAG + project memory | `localhost:6333` |
| PostgreSQL + AGE | Graph reasoning (LightRAG) + failure log + chat sessions | `localhost:5432` |
| LightRAG MCP | Streamable HTTP MCP server, port 9002 | `http://localhost:9002/mcp` |

ZGX infra is managed via `docker/docker-compose.zgx.yml`. Ollama runs natively for GPU access.

## Client Machine

| Component | Purpose | Port |
|---|---|---|
| hive-mcp | Docker container, host actions MCP | 9000 (project) or 9003 (when project MCP also on 9000) |
| project MCP | App-specific context tools | 9000 (typical) |

## Agent Roster (all implemented)

**Coordinator → ContextRouter → Researcher → Planner → Coder → Executor → Reviewer**

| Agent | Model | Role |
|---|---|---|
| Coordinator | `qwen3:30b-a3b` | Routes tasks, synthesises results |
| ContextRouter | `qwen3:8b` | Picks right memory/search backend |
| Researcher | `devstral:24b` | Reads and summarises codebase |
| Planner | `mistral-small3.1:24b` | Breaks tasks into ordered steps |
| Coder | `qwen2.5-coder:32b` | Implements features and fixes |
| Executor | `llama3.1:8b` | Runs commands and validates results |
| Reviewer | `qwen2.5-coder:32b` | Reviews code for correctness and security |

## Running AGNOHive

```bash
# FastAPI server (default port 9001)
python main.py --serve

# LightRAG MCP server (default port 9002, Streamable HTTP)
python main.py --serve-lightrag

# ZGX-side code indexer (direct filesystem access)
python main.py --index --path /path/to/repo --project-id myproject

# MCP-based indexer — reads files via project MCP, pre-splits large files (EkamApp primary path)
python index_via_mcp.py                    # incremental (hash-checked)
python index_via_mcp.py --force            # full reindex
python index_via_mcp.py --force --progress # full reindex with live progress bar

# Single task
python main.py "refactor the auth module"

# Interactive loop
python main.py
```

API endpoints: `GET /health`, `GET /teams`, `POST /run`, `POST /plan`, `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}`, `PATCH /sessions/{id}/persist`

## POST /run Request Shape

```json
{
  "task": "...",
  "project_id": "EkamApp",
  "team": "engineering",
  "mcp_url": "http://<tailscale-ip>:9000/mcp",
  "mcp_urls": ["http://<tailscale-ip>:9003/mcp"],
  "session_id": "optional-uuid",
  "persist": false
}
```

`mcp_url` = primary (project context). `mcp_urls` = additional MCPs (e.g. hive-mcp for host actions). All are opened simultaneously via `AsyncExitStack`.

## hive CLI Key Flags

```bash
hive "task"                     # one-shot
hive                            # REPL (auto-resumes last session)
hive --review "task"            # HITL: plan → approve → execute
hive -r                         # review REPL
hive --bootstrap                # index project into LightRAG via hive-mcp
hive --bootstrap --force        # full reindex
hive --lightrag-url <url>       # LightRAG MCP URL for bootstrap
hive --mcp-status               # show both MCP connection statuses
hive --mcp-url <url>            # override project MCP URL
hive --mcp-port <port>          # Tailscale auto-detect port for project MCP
hive --list-sessions            # list sessions and exit
hive --delete-all-sessions      # delete all sessions for this project
hive confirm [path]             # apply pending .hive_proposed file
hive reject [path]              # discard pending .hive_proposed file
```

REPL slash commands: `/new`, `/sessions`, `/history`, `/persist`, `/delete <id>`, `/delete-all`, `/diff`, `/confirm [path]`, `/reject [path]`, `/cleanup`, `/mcp`, `/exit`

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
| LightRAG MCP server (Streamable HTTP) | Done (Phase 3) |
| Automated code indexer (ZGX-side + hive-mcp bootstrap) | Done (Phase 4) |
| MCP-based indexer (`index_via_mcp.py`) — file hash tracker, progress bar, large-file pre-splitter | Done |
| LightRAG MCP server fix — `initialize_storages()` via `_get_ready_rag()` before all tool calls | Done |
| MCP tool call timeout raised 60s → 180s (`_MCP_TIMEOUT` in `swarm/team.py`) | Done |
| Engineering team model fix — broken Ollama models replaced: `deepseek-r1`→`mistral-small3.1:24b` (Planner), `gemma3:27b`→`qwen2.5-coder:32b` (Reviewer), `mixtral:8x7b`→`devstral:24b` (Researcher), `kimi-k2.6:cloud`→`qwen2.5-coder:32b` (Coder), `llama3.1:8b`→`qwen3:8b` (ContextRouter) | Done |
| Self-improving loop | Done (Phase 5) |
| OTel instrumentation → SigNoz | Done (Phase 6) |
| Global memory (cross-project namespace) | Done |
| HITL plan review (`POST /plan` + `hive --review`) | Done |
| VSCode diff + CLI arrow-key review | Done |
| Persistent chat sessions (PostgreSQL, TTL, compaction) | Done |
| hive-mcp (generic Docker host-action MCP, GHCR image) | Done |
| Dual-MCP architecture (project context + host actions) | Done |
| Tailscale auto-detection for MCP URLs | Done |
| `hive bootstrap` / `index_project` (LightRAG via hive-mcp) | Done |
| Web search + fetch (`web_search`, `web_fetch` via hive-mcp, `WEB_SEARCH_ENABLED`) | Done |
| Cost-aware model routing | Planned (Phase 7) |
