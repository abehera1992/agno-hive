# AGNOHive — Technical Reference

## Project Structure

```
agno-hive/
├── main.py                  # Entry point: CLI, interactive loop, --serve, --serve-lightrag, --index
├── config/
│   └── config.py            # All config via env vars, dataclass, loaded from .env
├── swarm/
│   ├── agents.py            # Agent factory functions — all accept *mcps: MCPTools
│   ├── team.py              # run_task_async — dual-MCP via AsyncExitStack
│   ├── bootstrap.py         # Pre-team MCP session via Streamable HTTP to fetch project patterns
│   ├── feedback.py          # Self-improving loop: record_success, record_failure, load_failure_context
│   ├── sessions.py          # Chat session persistence: CRUD, TTL cleanup, compaction
│   ├── ollama.py            # Ollama model list/pull via httpx
│   └── tool_fix.py          # OllamaToolFix: normalises all Ollama tool-call formats
├── api/
│   ├── server.py            # FastAPI app: /health, /teams, /run, /plan, /sessions
│   └── models.py            # Pydantic models: AgentSpec, RunRequest, RunResponse, SessionMeta, SessionDetail
├── teams/
│   ├── engineering.yaml     # Full 6-agent engineering team spec
│   └── planning.yaml        # HITL planning team (ContextRouter + Researcher + Planner)
├── lightrag_mcp/
│   ├── server.py            # FastMCP Streamable HTTP server: lightrag_insert, lightrag_query tools
│   └── rag.py               # LightRAG instance factory (per-project cache, Qdrant + AGE backends)
├── indexer/
│   ├── cli.py               # Code indexer entry point (ZGX-side, direct LightRAG insert)
│   ├── parser.py            # Python AST chunker + generic text chunker
│   └── tracker.py           # SHA-256 hash state for incremental indexing
├── observability/
│   ├── setup.py             # setup_telemetry() singleton — reads standard OTEL_* env vars
│   └── metrics.py           # task_duration histogram + task_counter
├── docker/
│   ├── docker-compose.zgx.yml   # ZGX infra stack (Qdrant + PostgreSQL/AGE)
│   └── init/
│       └── 01_age.sql           # Runs once on first postgres-age start; creates AGE extension + agno graph
├── hive-mcp/                # Generic client-side Docker MCP server (see hive-mcp/README.md)
│   ├── main.py              # FastMCP Streamable HTTP server entry point
│   ├── config.py            # PROJECT_ROOT, MCP_HOST, MCP_PORT, WRITE_REVIEW
│   ├── Dockerfile
│   ├── docker-compose.hive.yml  # Drop into any project and run
│   ├── requirements.txt
│   ├── .env.example
│   └── tools/
│       ├── context.py       # get_project_context, get_file_content, find_files, search_files, list_directory
│       ├── files.py         # write_file, apply_diff, run_command (WRITE_REVIEW-aware)
│       ├── shell.py         # run_shell, run_docker, get_env_info, check_port, list_processes
│       ├── git.py           # git_status, git_log, git_diff, git_log_file, git_blame
│       ├── index.py         # index_project — walks project, chunks, inserts into LightRAG via MCP
│       └── scan.py          # scan_project_context — full/incremental project scan → hive.md
├── cli/
│   └── hive                 # Zero-dependency CLI client (pure Python stdlib)
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
| `PLANNER_MODEL` | `deepseek-r1` | Planner agent model |
| `RESEARCHER_MODEL` | `mixtral:8x7b` | Researcher agent model |
| `EXECUTOR_MODEL` | `llama3.1:8b` | Executor agent model |
| `ROUTER_MODEL` | `llama3.1:8b` | Context Router agent model |
| `MCP_URL` | `` | Primary MCP server (project context), e.g. `http://<host>:9000/mcp` |
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
    { "name": "engineering", "description": "...", "agents": ["ContextRouter", "Researcher", ...] }
  ]
}
```

### `POST /run`
Runs a task. Returns the result with session metadata.

**Request:**
```json
{
  "task": "Refactor the auth module to use JWT",
  "project_id": "EkamApp",
  "team": "engineering",
  "agents": [...],
  "mcp_url": "http://<tailscale-ip>:9000/mcp",
  "mcp_urls": ["http://<tailscale-ip>:9003/mcp"],
  "session_id": "optional-uuid",
  "persist": false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `task` | string | required | The task or question |
| `project_id` | string | `"default"` | Namespace for memory and failure tracking |
| `team` | string | `"engineering"` | Team spec from `teams/*.yaml` |
| `agents` | array | — | Inline agent specs (overrides team) |
| `mcp_url` | string | — | Primary MCP (project context); overrides `MCP_URL` env var |
| `mcp_urls` | list[string] | — | Additional MCPs (e.g. hive-mcp for host actions) |
| `session_id` | string | — | Resume an existing session |
| `persist` | bool | `false` | Mark new session as permanent |

**Resolution order for team:** `agents` inline > `team` named > default `engineering` team.

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

### `POST /plan`
Runs the planning team (ContextRouter + Researcher + Planner) and returns a step-by-step plan without executing. Used by `hive --review`.

**Request:** same shape as `/run`

**Response:**
```json
{ "plan": "1. Researcher: ...\n2. Coder: ...", "duration_seconds": 18.2 }
```

### Session endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/sessions?project_id=X&limit=20` | List sessions for a project |
| `GET` | `/sessions/{id}` | Full session with all messages and summary |
| `DELETE` | `/sessions/{id}` | Hard delete (including persisted sessions) |
| `PATCH` | `/sessions/{id}/persist` | Mark session as permanent |

## Team YAML Format

Files in `teams/*.yaml` define reusable agent configurations.

```yaml
name: coding
description: General-purpose coding assistant.
coordinator_model: qwen3:30b-a3b   # optional, falls back to LEADER_MODEL

agents:
  - name: Coder
    model: mistral-small3.1:24b
    description: Implementation specialist. Write clean, idiomatic code.   # prepended to system message
    role: Senior software engineer who implements features and fixes bugs.  # coordinator-visible label
    tools:                          # scoped tool list — only these functions reach the model
      - get_file_content
      - find_files
      - apply_diff
      - write_file
      - run_command
    instructions:
      - Always read relevant files via get_file_content() before modifying code.
      - Use apply_diff() for existing files, write_file() only for new ones.

  - name: Reviewer
    model: gemma3:27b
    description: Code review specialist. Flag real problems only.
    role: Senior engineer who reviews code for correctness and security.
    tools:
      - get_file_content
      - find_files
      - search_files
      - git_diff
    instructions:
      - Be concise — flag real problems only.
```

### Field reference

| Field | Required | Description |
|---|---|---|
| `name` | yes | Agent name — shown in team output and used for delegation |
| `model` | yes | Ollama model ID |
| `role` | yes | Short label the coordinator sees when picking which agent to delegate to |
| `instructions` | yes | List of instruction strings appended to the agent's system message |
| `description` | no | One-sentence description prepended to the agent's own system message |
| `tools` | no | Allowlist of MCP tool names. Only matching `Function` objects from connected MCPs are passed to the model. If absent or no names match, all MCP tools are used as fallback. |

### How tool scoping works

When `tools:` is specified, `make_agent_from_spec` (`swarm/agents.py`) collects all `Function` objects from every connected `MCPTools` instance via `mcp.functions`, then filters to only the names in the list:

```python
all_funcs = {}
for mcp in mcps:
    all_funcs.update(mcp.functions)   # mcp.functions: OrderedDict[str, Function]
scoped = [all_funcs[t] for t in spec.tools if t in all_funcs]
agent_tools = scoped if scoped else list(mcps)   # fallback if none match
```

Individual `Function` objects are passed to `Agent(tools=...)` — Agno explicitly supports `List[Union[Toolkit, Callable, Function, Dict]]`. The MCP session stays open via the enclosing `AsyncExitStack`, so the Function callables remain valid for the duration of the run.

## How run_task_async Works

```
run_task_async(task, agent_specs, coordinator_model, mcp_url, mcp_urls, project_id)
  1. [parallel] bootstrap()            — Streamable HTTP MCP session → project_context
  1. [parallel] load_failure_context() — PostgreSQL failure_log → failure_context
  1. [parallel] get_session_context()  — PostgreSQL → (summary, recent_messages)
  2. AsyncExitStack opens ALL MCP connections simultaneously:
       - mcp_list[0] = MCPTools(mcp_url)            # primary: project context
       - mcp_list[1] = MCPTools(mcp_urls[0])        # secondary: hive-mcp host actions
       - ...
  3. Build team members from agent_specs:
       - make_agent_from_spec(spec, *mcp_list) for each spec
       - if spec.tools is set: filter mcp.functions to only those names (tool scoping)
       - each agent also gets description, markdown=True, add_name_to_context=True
  4. Build Team (coordinator + members + all MCP tools + instructions + context)
       Team flags: share_member_interactions=True, add_member_tools_to_context=True,
                   markdown=True, show_members_responses=True
  5. Coordinator instructions include:
       - ACTION APPROVAL block (go-ahead messages → always delegate to Coder, never conversational)
       - scan-first routing rules
       - multi-MCP tool selection ("PROJECT MCP vs hive-mcp")
       - file edit rules (apply_diff for existing, write_file for new only)
       - review_pending handling (STOP immediately, do not call any other tool)
  6. team.arun(task)
       ├── success → record_success() → LightRAG insert
       └── failure → record_failure() → PostgreSQL failure_log
  7. return result
```

Bootstrap, failure context, and session context load in parallel (`asyncio.gather`) — no latency penalty.

## Scan-First Prompt Engineering

User prompts are almost always short and vague ("list the directories", "how does auth work"). Without explicit guidance, agents stop at the first plausible result — describing a directory by its name, or reading only the first service they find. The scan-first rules force discovery before inference.

### Coordinator (`swarm/team.py` — `_COORDINATOR_INSTRUCTIONS`)

Two top-level blocks run before all routing logic:

**1. Conversational turn detection — runs first**

```
ACTION APPROVAL — always a TASK, never conversational:
  If the agent just described or proposed a change and the user says any of:
  'go ahead', 'apply it', 'do it', 'update it', 'yes', 'ok', 'confirm', 'sure' → TASK.
  → Delegate the write/implementation to the Coder immediately.
  → Do NOT reply in plain prose. Delegate and act.

CONVERSATIONAL — respond directly, NO tool calls:
  - User shares an opinion, agrees, disagrees without requesting action
  - Simple follow-up already answered by the prior response
  - NOT an approval of a proposed change (see ACTION APPROVAL above)

TASK — use tools as needed:
  - New URL to fetch, new file to read, new codebase question
  - Explicit action: 'add X', 'fix Y', 'list Z', 'search for W'
```

This prevents "go ahead" / "apply it" messages from being swallowed as no-op conversational replies, which was causing the `confirm/reject/skip` UI never to appear after a proposed file change.

**2. Scan-first rule**

```
Before answering any question about structure, features, or behaviour:
  1. find_files('**/*')           — get the full file tree
  2. search_files(keyword, '**/*') — find all occurrences of the topic
  3. get_file_content(path)       — read specific files to verify details
Never describe a directory or module from its name alone.
Never stop at the first interesting result for overview questions.
```

Query-type routing then layers on top:

| Query type | Tool sequence |
|---|---|
| Overview / structure | `find_files('**/*')` → read one entry file per directory → grounded summary of ALL dirs |
| "How does X work" | `search_files(X, '**/*')` → `get_file_content()` on 2-3 most relevant files |
| Code pattern / convention | `find_files('**/<ext>')` → `search_files(pattern)` → `get_file_content()` on 1-2 files |
| Implementation task | `get_context_section()` → read one reference file → Coder → Reviewer |

### ContextRouter (`teams/engineering.yaml`)

Three tiers (replaces the old "one tool call, use memory_search if unsure"):

| User prompt shape | Action |
|---|---|
| "list directories", "what does X do", "show me the structure" | `find_files('**/*')` — full tree, pass raw results to Researcher |
| "how does X work", "what is the X flow" | `search_files(X, '**/*')` — all occurrences across the whole codebase |
| Specific file or symbol | targeted `find_files()` or `search_files()` — one call |
| Past task / lesson | `memory_search()` |

Tool call limit: 1 for specific lookups, up to 3 for overview/structure queries.

### Researcher (`teams/engineering.yaml`)

Rules in effect:

- **SCAN-FIRST** — must call `find_files('**/*')` before answering any structure or overview question; must read at least one file (README, main.py, config.py, `__init__.py`) per directory to ground the answer — never describe from the directory name alone
- **COVERAGE** — stopping at the first interesting directory is a failure; every top-level directory must appear in the response; subdirectory listings are included where they exist
- **SEARCH rule** — for "how does X work" questions, `search_files(X, '**/*')` runs first; files are read only after search identifies which ones are relevant
- **EVIDENCE rule** — any recommendation from external documentation must cite the specific doc URL + section AND the specific project file:line that was compared; unverified claims are labelled "inference from docs — not verified in codebase"
- **DESIGN-INTENT rule** — before flagging a difference between this project and a framework as a misconfiguration, read CLAUDE.md / docs.md to check if the difference is intentional design

### Result caps

`find_files` and `search_files` have per-call result caps. Exceeding them causes the agent to see a partial picture:

| Tool | Default cap | Behaviour when hit |
|---|---|---|
| `find_files` | 200 results | Stops — files beyond the cap are invisible |
| `search_files` | 80 matches | Stops — further occurrences are invisible |

`list_directory_tree()` has **no result cap** — it walks every directory recursively and returns the full skeleton. This is why agents prefer it for overview questions; `find_files('**/*')` is the fallback for MCP servers that don't expose `list_directory_tree`.

### Files changed

```
swarm/team.py              # _COORDINATOR_INSTRUCTIONS — ACTION APPROVAL block, scan-first rules,
                           # external-docs grounding rules (read code before docs, cite both sides),
                           # share_member_interactions, add_member_tools_to_context, markdown=True
swarm/agents.py            # make_agent_from_spec — tool scoping via mcp.functions; description,
                           # markdown=True, add_name_to_context=True on all agents
swarm/bootstrap.py         # reads hive.md first (priority context) before patterns glob
api/models.py              # AgentSpec — description and tools fields added
teams/engineering.yaml     # all agents: description + scoped tools list; Coder: apply_diff/review_pending rules;
                           # Researcher: EVIDENCE + DESIGN-INTENT rules against external-doc hallucination
teams/planning.yaml        # all agents: description + scoped tools list; Researcher: WEB rule +
                           # EVIDENCE + DESIGN-INTENT rules
hive-mcp/tools/scan.py     # new — scan_project_context(force) tool; full/incremental scan → hive.md
hive-mcp/main.py           # register scan_project_context tool
cli/hive                   # --scan / --scan --force flags; _ensure_hive_context auto-scan on startup
```

## How Bootstrap Works

`swarm/bootstrap.py` opens a raw `mcp.ClientSession` via **Streamable HTTP** (`streamablehttp_client`) and builds the coordinator's context in priority order:

1. **`hive.md`** — if present in the project root, read first via `get_file_content("hive.md")`. This pre-built snapshot (directory tree, root docs, per-directory summaries) is injected at the top of coordinator context, giving grounded project knowledge with zero extra MCP calls during the session.
2. **Pattern files** — `find_files(patterns_glob)` discovers `patterns/**/*.md` files; each is read with `get_file_content()` and appended.
3. **Fallback** — if neither `hive.md` nor pattern files are found, `get_project_context()` is called.

The combined string is injected into the coordinator's instructions before every task run.

> **Transport note:** AGNOHive uses Streamable HTTP (the current MCP standard) for all MCP connections. Your MCP server must expose a `/mcp` endpoint. The deprecated `/sse` endpoint is not used.

## hive.md Project Context Snapshot

`hive.md` is a structured markdown file written to the project root by `hive-mcp/tools/scan.py`. It gives the coordinator a grounded project overview at the start of every session — reducing repeated file reads and hallucination on structure and design questions.

### Generating hive.md

```bash
hive --scan            # incremental — re-reads only files changed since last scan
hive --scan --force    # full rescan from scratch
```

On first `hive` launch (REPL or one-shot), if `hive.md` is missing and hive-mcp is reachable, the CLI auto-triggers a full scan before starting.

### Incremental update strategy

`--scan` covers **four layers** of change to pick up all local modifications, not just committed ones:

| Layer | Git command |
|---|---|
| Committed since last scan | `git diff --name-only <stored-hash>..HEAD` |
| Staged (index) | `git diff --cached --name-only` |
| Unstaged (working tree) | `git diff --name-only` |
| Untracked new files | `git ls-files --others --exclude-standard` |

The stored commit hash lives in the `hive.md` header comment (`<!-- hive-scan: commit=<hash> timestamp=<iso> -->`). If nothing changed across all four layers, `--scan` returns instantly ("up to date"). Otherwise the full file is rebuilt (fast — all file reads are capped).

Falls back to a full scan if git is unavailable or `hive.md` doesn't exist yet.

### hive.md structure

```markdown
<!-- hive-scan: commit=a2b95be timestamp=2026-05-07T16:00:00Z -->
# Hive Project Context
**Project:** agno-hive
**Last scanned:** 2026-05-07 16:00 UTC (commit `a2b95be`)
**Uncommitted changes:** swarm/team.py, teams/engineering.yaml

## Project Structure
<directory tree, depth 3>

## CLAUDE.md
<first 3000 chars>

## README.md
<first 1500 chars>

## .env.example
<full content>

## Top-Level Directory Summaries
### `swarm/`
```python
<key file content, 800 char cap>
```
...
```

### `hive-mcp/tools/scan.py` — `scan_project_context(force)`

| Behaviour | Trigger |
|---|---|
| Full scan | `force=True` or `hive.md` missing |
| Incremental | default — checks all four git change layers |
| Up to date | nothing changed — returns immediately |

Works for any project, not just agno-hive. Falls back gracefully when git is unavailable.

## How OllamaToolFix Works

`swarm/tool_fix.py` wraps the Ollama model and normalises tool calls across four formats that different Ollama models emit:
1. Native OpenAI-compatible `tool_calls` array
2. `<tool_call>...</tool_call>` XML tags in content
3. `` <|python_tag|> `` delimited JSON
4. Bare JSON object in content

All formats are parsed and converted to the standard format before passing to Agno's agent loop.

## Dual-MCP Architecture

Every `run_task_async` call can receive multiple MCP URLs. All connections are opened simultaneously with `AsyncExitStack` before the team is built. Each agent receives only its declared tools via scoping:

```python
async with AsyncExitStack() as stack:
    mcp_list = []
    for url in all_mcp_urls:
        mcp = await stack.enter_async_context(
            MCPTools(url=url, transport="streamable-http", timeout_seconds=120)
        )
        mcp_list.append(mcp)

    # YAML-driven agents: tools scoped to spec.tools list
    members = [make_agent_from_spec(spec, *mcp_list) for spec in agent_specs]

    # Coordinator sees ALL tools from ALL MCPs (needs full visibility for routing)
    team = Team(
        tools=mcp_list,
        members=members,
        share_member_interactions=True,   # member responses visible to coordinator
        add_member_tools_to_context=True, # coordinator knows each member's tool set
        markdown=True,
        ...
    )
```

Coordinator routing instructions:
- **Project MCP** — `get_file_content`, `find_files`, `search_files`, `memory_search`, `lightrag_query`, and any app-specific workflow tools
- **hive-mcp** — `apply_diff`, `write_file`, `run_shell`, `run_docker`, `git_*`, `index_project`

The coordinator always receives all tools so it can route correctly. Member agents are scoped via their YAML `tools:` list so individual models see fewer options and are less likely to call the wrong tool.

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

### Checking Container Status

```bash
docker ps --filter "name=agno-"
curl http://localhost:6333/healthz
docker exec agno-postgres-age psql -U agno -d agno_graph -c "SELECT * FROM ag_catalog.ag_graph;"
docker compose -f ~/agno-hive/docker/docker-compose.zgx.yml restart
```

## Self-Improving Loop

Every task run feeds back into memory so the coordinator learns from past outcomes.

```
run_task_async(task, project_id)
  ├── load_failure_context(project_id)   → recent failures injected into coordinator instructions
  ├── bootstrap()                        → project patterns from MCP
  ├── team.arun(task)
  │     ├── success → record_success()  → LightRAG insert (agents can query via memory_search)
  │     └── failure → record_failure()  → PostgreSQL failure_log table
  └── return result
```

Failure log schema:

| Column | Type | Description |
|---|---|---|
| `project_id` | TEXT | Project namespace |
| `task` | TEXT | Task description (truncated to 300 chars) |
| `error_type` | TEXT | Exception class name |
| `error_message` | TEXT | Error message (truncated to 500 chars) |
| `agent` | TEXT | Agent that failed |
| `created_at` | TIMESTAMPTZ | Timestamp |

The 3 most recent failures are loaded and appended to the coordinator's instructions before every run.

## hive-mcp (Client-Side Docker MCP)

`hive-mcp/` is a standalone FastMCP server that runs on the client machine as a Docker container. It gives AGNOHive host-level access independent of what project MCP is installed.

### Image

```
ghcr.io/abehera1992/hive-mcp:latest
```

Rebuilt automatically on every push to `main` that changes `hive-mcp/**`.

### Running

```bash
# Using docker-compose (recommended — copy file into your project)
docker compose -f docker-compose.hive.yml up -d

# Or docker run
docker run -d --name hive-mcp --restart unless-stopped \
  -p 9000:9000 \
  -v "$(pwd):/project" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e PROJECT_ROOT=/project -e WRITE_REVIEW=true \
  ghcr.io/abehera1992/hive-mcp:latest
```

### Config

| Variable | Default | Description |
|---|---|---|
| `PROJECT_ROOT` | `/project` | Project path inside the container |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `9000` | Container port |
| `WRITE_REVIEW` | `true` | Stage writes as `.hive_proposed` for human review |
| `WEB_SEARCH_ENABLED` | `false` | Enable `web_search` and `web_fetch` tools (uses client network) |

### Endpoint

```
http://<tailscale-ip>:<port>/mcp
```

The hive CLI auto-detects the Tailscale IP. Default port for hive-mcp is 9003 on the host when the project MCP already occupies 9000 (docker `-p 9003:9000`).

### Tools exposed

See `hive-mcp/README.md` for the full tool list.

## Web Search and Fetch

`web_search` and `web_fetch` run inside the hive-mcp container and use the **client machine's network**. ZGX is not involved — no traffic goes through the inference server. Both tools are disabled by default and enabled with `WEB_SEARCH_ENABLED=true`.

### Tools

| Tool | Args | Purpose |
|---|---|---|
| `web_search` | `query, max_results=5` | DuckDuckGo full-text search — returns titles, URLs, snippets. No API key required. |
| `web_fetch` | `url, max_chars=8000` | Fetch a URL and return clean readable text. GitHub repos return README + metadata via GitHub API. |

### web_fetch URL handling

| URL type | Behaviour |
|---|---|
| `github.com/owner/repo` | GitHub API — repo description, language, stars, topics, full README |
| GitHub raw/blob file | Direct httpx fetch of file content |
| Documentation sites, blogs | httpx + BeautifulSoup — strips nav/scripts/ads, returns `<main>` or `<article>` content |
| Any other HTTP/HTTPS URL | httpx fetch + plain text extraction |

### Agent routing rules

Agents use web tools when (all conditional on `WEB_SEARCH_ENABLED`):
- **URL in user prompt** → `web_fetch(url)` immediately, before any other tool
- **GitHub repo mentioned** → `web_fetch(github_url)` for README + metadata
- **Unfamiliar library / tool / technology** → `web_search(name)` → `web_fetch` on best result
- **Codebase context insufficient** → `web_search` to fill the gap

Local file tools (`find_files`, `search_files`, `get_file_content`) always take priority for project questions. Web tools are for external context only.

### Dependencies added to hive-mcp

```
duckduckgo-search>=6.0.0   # DuckDuckGo search, no API key
beautifulsoup4>=4.12.0     # HTML content extraction
httpx>=0.27.0              # already present — used for web_fetch
```

### File

```
hive-mcp/tools/web.py   # web_search, web_fetch, _clean_html, _github_repo_summary
```

## Automated Code Indexer

Three paths to index a project into LightRAG:

### 1. MCP-based (`index_via_mcp.py`) — EkamApp primary path

Runs on ZGX. Reads every file via the **project's MCP server** (Streamable HTTP) so it works even when ZGX has no filesystem access to the client machine. Pre-splits large files before inserting to avoid LLM timeouts. Supports incremental runs via SHA-256 hash tracking.

```bash
# From ~/agno-hive on ZGX
python index_via_mcp.py                   # incremental — skips files unchanged since last run
python index_via_mcp.py --force           # full reindex (ignores hash cache)
python index_via_mcp.py --progress        # live progress bar (TTY); plain log lines otherwise
python index_via_mcp.py --force --progress
```

State: `~/.agno-hive/index-state/{project_id}.json`

**How it works:**
1. **Scan phase** — calls `find_files(glob)` for each configured glob, prints per-glob file counts and total before indexing starts
2. **Hash check** — skips files whose SHA-256 matches the saved state (incremental mode only)
3. **Pre-split** — files over 8 000 chars are split at language-aware boundaries (class/def for Python, export/function for TS/TSX, selectors for SCSS) before `ainsert()`. Each part is tagged `"File: path (part N/M)"` so LightRAG links them as siblings. Prevents LLM worker timeouts on large files.
4. **Insert** — `rag.ainsert()` per chunk; Qdrant + PostgreSQL/AGE updated in parallel

**Progress display (--progress):**
```
[indexing ekam]  ████████████░░░░░░░░  62.0%  200/321 files
  current : API/business-service/router/modules_api.py
  indexed : 142  (178 inserts)   skipped: 54   errors: 4
  elapsed : 2:18:42   eta: ~1:24:00
```

**Config (top of script):**
| Constant | Default | Description |
|---|---|---|
| `MCP_URL` | `http://100.87.159.86:9000/mcp` | EkamApp MCP server |
| `PROJECT_ID` | `ekam` | Qdrant collection prefix + Postgres workspace |
| `MAX_INSERT_CHARS` | `8000` | Max chars per `ainsert()` call before splitting |
| `GLOBS` | API/**/*.py, src/**/*.ts/tsx/scss, patterns/**/*.md, … | File patterns to index |

### 2. ZGX-side (`python main.py --index`)

Runs directly on ZGX, calls `rag.ainsert()` without going through MCP. Fastest option when ZGX has direct filesystem access to the project.

```bash
python main.py --index --path /path/to/repo --project-id ekam
python main.py --index --path /path/to/repo --project-id ekam --force
```

State: `~/.agno-hive/index-state/{project_id}.json`

### 3. Client-side (`hive --bootstrap` via hive-mcp)

Runs inside the hive-mcp container on the client machine. Calls `lightrag_insert` on the LightRAG MCP server via Streamable HTTP.

```bash
hive --bootstrap                                    # auto-detects lightrag URL from ZGX host
hive --bootstrap --lightrag-url http://<zgx>:9002/mcp
hive --bootstrap --glob "**/*.py"                   # only Python files
hive --bootstrap --force                            # reindex everything
```

State: `/tmp/hive-index/{project_id}.json` (inside container)

### Chunk Format (Python, ZGX-side indexer)

```
File: src/models/user.py
Type: class
Name: User
Docstring: ORM model for authenticated users.

class User(Base):
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True)
```

### Checking Index Progress

```bash
# On ZGX — how many docs are processed
docker exec agno-postgres-age psql -U agno -d agno_graph \
  -c "SELECT status, count(*) FROM agno.lightrag_doc_status GROUP BY status;"

# Tail live log (index_via_mcp.py)
tail -f ~/agno-hive/index.log | grep -v "^INFO"
```

### Re-indexing After Code Changes

Run incremental (default) after any multi-file change. Force-reindex only when the project structure has changed significantly (new service directory, new API slice) or after a large refactor. The hash state persists between runs — unchanged files are always skipped in incremental mode.

## OTel Instrumentation

| Span | Attributes |
|---|---|
| `agno.task` (root) | `project_id`, `coordinator_model`, `agent_count`, `task` (first 120 chars) |
| `agno.team.run` (child) | — |
| HTTP requests | Auto-instrumented via `FastAPIInstrumentor` |

| Metric | Type | Labels |
|---|---|---|
| `agno.task.duration` | Histogram (seconds) | `project_id` |
| `agno.task.count` | Counter | `project_id`, `outcome` (success/failure) |

Enable by setting `OTEL_EXPORTER_OTLP_ENDPOINT` in `.env`.

## Global Memory

LightRAG maintains two namespaces queried on every lookup:

| Namespace | Qdrant collection | Scope |
|---|---|---|
| `project_{id}` | `project_ekam`, etc. | Per-project code knowledge |
| `global` | `project_global` | Cross-project patterns and lessons |

`lightrag_query` runs both in parallel (`asyncio.gather`) and merges results. An empty namespace is omitted from the merge.

| Tool | Purpose |
|---|---|
| `lightrag_insert(text, project_id)` | Index into project namespace |
| `lightrag_insert_global(text)` | Index into shared global namespace |
| `lightrag_query(query, project_id, mode)` | Query both namespaces, merged |

ContextRouter routing:
```
Specific file/symbol        → lightrag_query(query, project_id, mode='local')
Cross-module/thematic       → lightrag_query(query, project_id, mode='global')
Cross-project patterns      → lightrag_query(query, 'global', mode='hybrid')
```

## Human-in-the-Loop (HITL) Plan Review

```
hive --review "task"
  ├─ POST /plan  → planning team (ContextRouter + Researcher + Planner)
  │               returns plan text
  ├─ show plan to user
  ├─ Proceed? [Y/n]
  │   ├─ Y → POST /run → full engineering team → execute
  │   └─ N → abort, nothing executed
```

`teams/planning.yaml` — produces a numbered step list naming responsible agent, files to touch, and risks. Does not implement anything.

## Persistent Chat Sessions

### Database Schema

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
  summary_through INT DEFAULT 0

session_messages
  id          SERIAL PRIMARY KEY
  session_id  UUID REFERENCES chat_sessions(id) ON DELETE CASCADE
  role        TEXT                       -- "user" | "assistant"
  content     TEXT
  created_at  TIMESTAMPTZ
```

### Compaction

When total message count exceeds `AGNO_COMPACT_THRESHOLD` (default: 20), a fire-and-forget `asyncio.create_task` calls `llama3.1:8b` to summarise older messages. The summary is stored in `chat_sessions.summary`.

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

## VSCode Diff Review (WRITE_REVIEW)

Enabled via `WRITE_REVIEW=true` on hive-mcp.

### Flow

```
agent calls apply_diff(path, old_string, new_string)
  ├─ WRITE_REVIEW=false → applies immediately → "applied: path"
  └─ WRITE_REVIEW=true
       ├─ writes proposed content to path + ".hive_proposed"
       └─ returns "review_pending: path — user will confirm/reject via CLI"
              ↓
       hive CLI detects new .hive_proposed files
              ↓
       VS Code IPC: opens diff tab in existing window (no new window)
       — or —
       inline terminal diff shown if IPC unavailable
              ↓
       CLI shows arrow-key selector:
         ❯ confirm  — apply this change
           reject   — discard
           skip     — decide later
              ↓
       user presses ↑/↓ then Enter
              ↓
       CLI applies or deletes .hive_proposed directly on local filesystem
       (agents have no involvement — confirm_write/reject_write are not MCP tools)
```

### VS Code IPC

When the terminal was opened by VS Code, `VSCODE_IPC_HOOK_CLI` is set. The CLI writes a JSON open/diff command directly to the VS Code named pipe socket — zero new process, zero extra window. Falls back to enumerating `\\.\pipe\vscode-ipc-*-sock` pipes. If no IPC socket is found, inline terminal diff is shown instead.

### run_command write guard

When `WRITE_REVIEW=true`, `run_command` blocks any shell command that writes to files. Blocked patterns: `>`, `>>`, `sed -i`, `perl -i`, `tee`, `truncate`, `dd of=`.

### apply_diff: edit vs. append

`apply_diff` makes a surgical replacement — `old_string` must appear exactly once.

**To replace a line:**
```
old_string = "old content"
new_string = "new content"
```

**To append after a line** (include the anchor in both):
```
old_string = "last_existing_line"
new_string = "last_existing_line\nnew_appended_line"
```

Omitting the anchor from `new_string` replaces the line instead of appending.

## LightRAG MCP Server

Standalone FastMCP server (`lightrag_mcp/`) running on ZGX over **Streamable HTTP**.

```bash
# Start (port 9002)
python main.py --serve-lightrag

# Endpoint
http://<zgx-tailscale-ip>:9002/mcp
```

| Variable | Default | Description |
|---|---|---|
| `LIGHTRAG_MCP_PORT` | `9002` | Server port |
| `LIGHTRAG_MCP_URL` | `http://localhost:9002/mcp` | URL agents use to connect |
| `LIGHTRAG_MCP_HOST` | `0.0.0.0` | Bind address (passed to FastMCP constructor) |
| `LIGHTRAG_LLM_MODEL` | `mistral-small3.1:24b` | Entity/relation extraction model during insert |
| `LIGHTRAG_EMBED_MODEL` | `qwen3-embedding:0.6b` | Ollama embedding model (must be pulled first) |
| `LIGHTRAG_EMBED_DIM` | `1024` | Must match embed model output dimension |
| `LIGHTRAG_WORKING_DIR` | `~/.agno-hive/lightrag` | Base dir for per-project file-based KV storage |

**Constraint:** LightRAG requires a 32K+ context window model for entity extraction. Do not use reasoning/chain-of-thought models (e.g. DeepSeek R1) for `LIGHTRAG_LLM_MODEL`.

**Note on host/port:** The `mcp` package's `FastMCP.run()` does not accept `host`/`port` arguments — they are passed to the `FastMCP(name, host=..., port=...)` constructor. `LIGHTRAG_MCP_HOST` and `LIGHTRAG_MCP_PORT` are read at module import time and passed to the constructor.

## Running Tests

```bash
pytest tests/ -v
```

## Adding a New Agent

**Option A — YAML only (preferred for new teams):**

Add the agent to a YAML file. No Python changes needed.

```yaml
- name: SecurityAuditor
  model: gemma3:27b
  description: Security review specialist. Identify vulnerabilities and misconfigurations.
  role: Security engineer who audits code for vulnerabilities and misconfigurations.
  tools:
    - get_file_content
    - find_files
    - search_files
    - git_diff
  instructions:
    - Focus on OWASP Top 10, injection, auth bypass, secrets in code.
    - Flag real vulnerabilities only — not theoretical risks.
```

**Option B — hardcoded factory (for agents used as default fallback):**

1. Add `make_<agent>(*mcps: MCPTools)` to `swarm/agents.py` following the `make_coder` pattern. Include `description`, `**_COMMON_AGENT_KWARGS`.
2. Add the model env var to `config/config.py` and `.env.example`.
3. Wire into the `else` branch in `run_task_async` (`swarm/team.py`) or reference in a YAML.

**Tool scoping note:** The `tools:` list in YAML must use exact MCP tool names (e.g. `apply_diff`, not `edit_file`). Names that don't match any connected MCP are silently skipped. If no names match, the agent falls back to all tools.

## Adding a New Team

Create `teams/<name>.yaml` with the format above. It's immediately available via `GET /teams` and `POST /run` with `"team": "<name>"` — no code changes needed.
