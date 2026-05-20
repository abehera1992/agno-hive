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
│       ├── files.py         # write_file (new files only — blocked on existing), apply_diff, run_command (WRITE_REVIEW-aware)
│       ├── shell.py         # run_shell (write guard when WRITE_REVIEW=true), run_docker, get_env_info, check_port, list_processes
│       ├── git.py           # git_status, git_log, git_diff, git_log_file, git_blame
│       ├── index.py         # index_project — os.walk + dir pruning, mtime+size change detection, single LightRAG session, time-budgeted passes
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
| `LEADER_MODEL` | `qwen2.5-coder:32b` | Coordinator agent model (overridden per-team via `coordinator_model` in YAML) |
| `CODER_MODEL` | `qwen2.5-coder:32b` | Coder agent model |
| `REVIEWER_MODEL` | `qwen2.5-coder:32b` | Reviewer agent model |
| `PLANNER_MODEL` | `mistral-small3.1:24b` | Planner agent model |
| `RESEARCHER_MODEL` | `devstral:24b` | Researcher agent model |
| `ROUTER_MODEL` | `llama3.1:8b` | ContextRouter agent model (also used for session compaction) |
| `EXECUTOR_MODEL` | `llama3.1:8b` | Executor agent model |
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
| `AGNO_SESSION_WINDOW` | `6` | Verbatim messages injected into coordinator per request (recommended: set to `12` for multi-turn debugging sessions) |
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
| `mode` | string | — | Override team mode: `"coordinate"` (default), `"collaborate"`, `"route"` |
| `agents` | array | — | Inline agent specs (overrides team) |
| `mcp_url` | string | — | Primary MCP (project context); overrides `MCP_URL` env var |
| `mcp_urls` | list[string] | — | Additional MCPs (e.g. hive-mcp for host actions) |
| `session_id` | string | — | Resume an existing session |
| `persist` | bool | `false` | Mark new session as permanent |

**Resolution order for team:** `agents` inline > `team` named > default `engineering` team.

**Mode resolution order:** `mode` in request > `mode` in team YAML > default `"coordinate"`.

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

### `POST /feedback`

Submit human feedback on a hive output. Closes the self-improving loop beyond just technical errors.

**Request:**
```json
{
  "session_id": "optional-uuid",
  "task": "description of the task that was run",
  "project_id": "EkamApp",
  "rating": "bad",
  "notes": "specific correction — e.g. __table_args__ tuple must have dict last, not first"
}
```

| Field | Type | Description |
|---|---|---|
| `session_id` | string | Optional — links feedback to a specific run |
| `task` | string | Task description (used as the failure_log `task` field) |
| `project_id` | string | Project namespace — must match the project used in `/run` |
| `rating` | string | `"bad"` or `"good"` |
| `notes` | string | Correction text (bad) or confirmation note (good) |

**Behaviour:**
- `rating=bad` → writes `[USER FEEDBACK] <notes>` to `failure_log` with `agent=output_quality` → injected into coordinator instructions on the next `/run` for this project
- `rating=good` → calls `record_success()` → stores pattern to LightRAG for future recall

## Team YAML Format

Files in `teams/*.yaml` define reusable agent configurations.

```yaml
name: coding
description: General-purpose coding assistant.
coordinator_model: qwen3:30b-a3b   # optional, falls back to LEADER_MODEL
mode: coordinate                   # optional: coordinate (default) | collaborate | route

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

**Top-level fields:**

| Field | Required | Description |
|---|---|---|
| `name` | yes | Team name — used in `POST /run` `team` field and `GET /teams` |
| `description` | yes | One-line summary shown in `GET /teams` |
| `coordinator_model` | no | Coordinator Ollama model; falls back to `LEADER_MODEL` env var |
| `mode` | no | Agno Team mode: `coordinate` (default), `collaborate`, `route`. Can be overridden per-request via `mode` in `POST /run`. |

**Agent fields:**

| Field | Required | Description |
|---|---|---|
| `name` | yes | Agent name — shown in team output and used for delegation |
| `model` | yes | Ollama model ID |
| `role` | yes | Short label the coordinator sees when picking which agent to delegate to |
| `instructions` | yes | List of instruction strings appended to the agent's system message |
| `description` | no | One-sentence description prepended to the agent's own system message |
| `tools` | no | Allowlist of MCP tool names. Only matching `Function` objects from connected MCPs are passed to the model. If absent or no names match, all MCP tools are used as fallback. |

### Team modes

| Mode | Behaviour |
|---|---|
| `coordinate` | Coordinator delegates to one member at a time sequentially (default) |
| `collaborate` | All members receive the task simultaneously and work in parallel — best for read-only analysis where agents cover different concerns independently |
| `route` | Task routed to a single best-fit agent with no coordinator overhead |

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
  1. [parallel] load_failure_context() — PostgreSQL failure_log → failure_context
  1. [parallel] get_session_context()  — PostgreSQL → (summary, recent_messages)
  2. AsyncExitStack opens ALL MCP connections simultaneously:
       - mcp_list[0] = MCPTools(mcp_urls[0])        # primary: hive-mcp (host actions + reads)
       - mcp_list[1] = MCPTools(mcp_url)            # supplementary: project MCP (memory, app tools)
         (exclude_tools=["agno_run","agno_list_teams"] applied to project-mcp only)
       - ...
  3. Build team members from agent_specs:
       - make_agent_from_spec(spec, *mcp_list) for each spec
       - if spec.tools is set: filter mcp.functions to only those names (tool scoping)
       - each agent also gets description, markdown=True, add_name_to_context=True
  4. Build Team (coordinator + members + all MCP tools + instructions + context)
       Team flags: share_member_interactions=True, add_member_tools_to_context=True,
                   markdown=True, show_members_responses=True
  5. Coordinator instructions include:
       - "Call get_file_content('hive.md') as first action" — on-demand context load (not pre-injected)
       - ACTION APPROVAL block (go-ahead messages → always delegate to Coder, never conversational)
       - REJECT/CANCEL block (reject/cancel/abort messages → STOP, tell user to use /reject or /cleanup)
       - scan-first routing rules
       - multi-MCP tool selection ("PROJECT MCP vs hive-mcp")
       - file edit rules (apply_diff for existing, write_file for new only)
       - review_pending handling (STOP immediately, do not call any other tool)
       - .hive_proposed deletion guidance (tell user to use /reject <path> or /cleanup — agents cannot delete proposed files)
  6. team.arun(task)
       ├── success → record_success() → LightRAG insert
       └── failure → record_failure() → PostgreSQL failure_log
  7. return result
```

Failure context and session context load in parallel (`asyncio.gather`). Bootstrap is no longer pre-loaded — the coordinator reads `hive.md` on demand as its first tool action, which prevents "I already know" no-tool-call loops with large-context models.

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
- **HARD RULE** — never fabricate file paths, function names, or line numbers; every claim must be backed by an actual `get_file_content()` or `search_files()` result from this session; fabricating is worse than saying "I don't know"
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

**Bootstrap is no longer pre-loaded into coordinator instructions.** Injecting pre-built context caused all coordinator models to answer from the injected text without making any tool calls ("I already know from the context…"). The fix: context is loaded on demand.

### On-demand context (current behaviour)

The coordinator receives a single instruction: `"Call get_file_content('hive.md') as first action"`. It fetches the snapshot itself as the first tool call of each run, which:
- Forces the model through the tool-call pathway (preventing the no-tool-call loop)
- Keeps the MCP connection warm for subsequent calls
- Allows the coordinator to skip the fetch if task context makes it unnecessary

### `swarm/bootstrap.py` (retained for CLI use)

`bootstrap.py` still exists and is used by `hive --scan` and `hive --bootstrap`. It opens a raw `mcp.ClientSession` via **Streamable HTTP** (`streamablehttp_client`) and reads context in priority order:

1. **`hive.md`** — if present, read first via `get_file_content("hive.md")`
2. **Pattern files** — `find_files(patterns_glob)` discovers `patterns/**/*.md`; each is read with `get_file_content()`
3. **Fallback** — if neither found, `get_project_context()` is called

`bootstrap()` accepts `extra_urls` — tries hive-mcp first, falls back to project MCP. It is no longer called from `run_task_async`.

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

## Ollama Performance Tuning

Ollama performance is configured via environment variables in the systemd drop-in file at `/etc/systemd/system/ollama.service.d/override.conf`.

### Current settings (ZGX GB10)

```ini
[Service]
Environment="OLLAMA_HOST=100.96.86.82"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_NUM_PARALLEL=4"
Environment="OLLAMA_MAX_LOADED_MODELS=4"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

| Variable | Value | Effect |
|---|---|---|
| `OLLAMA_FLASH_ATTENTION` | `1` | Enables FlashAttention-2 on GPU — 20–40% faster on long-context inference (Blackwell) |
| `OLLAMA_NUM_PARALLEL` | `4` | Handles up to 4 concurrent inference requests — reduces queuing when multiple agents call Ollama simultaneously |
| `OLLAMA_MAX_LOADED_MODELS` | `4` | Keeps 4 models hot in memory — eliminates model-swap latency between agent calls |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | 8-bit KV cache (vs default 16-bit) — halves KV memory, fits longer contexts, faster cache reads |

With 128GB unified memory on the GB10, `MAX_LOADED_MODELS=4` holds the four main models (~57GB total) simultaneously with no swapping.

### Applying changes

```bash
# Edit the drop-in
sudo nano /etc/systemd/system/ollama.service.d/override.conf

# Reload and restart
sudo systemctl daemon-reload && sudo systemctl restart ollama

# Verify
systemctl show ollama --property=Environment | tr ' ' '\n' | grep OLLAMA
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

Two paths to index a project into LightRAG:

### 1. ZGX-side (`python main.py --index`)

Runs directly on ZGX, calls `rag.ainsert()` without going through MCP. Use when ZGX has direct filesystem access to the project (e.g. indexing agno-hive itself).

```bash
python main.py --index --path /path/to/repo --project-id myproject
python main.py --index --path /path/to/repo --project-id myproject --force
```

State: `~/.agno-hive/index-state/{project_id}.json`

### 2. Client-side (`hive --bootstrap` via hive-mcp)

Runs inside the hive-mcp container on the client machine. Calls `index_project` **directly on hive-mcp** (bypasses the AGNOHive agent pipeline — no 180s `_MCP_TIMEOUT` applies). Loops automatically on `Partial` results until `Done`. This is the primary path for EkamApp and any project where ZGX has no direct filesystem access.

```bash
hive --bootstrap                                    # index all source files (default glob: **/*)
hive --bootstrap --lightrag-url http://<zgx>:9002/mcp
hive --bootstrap --glob "Client/**/*.ts"            # scoped to a specific directory + extension
hive --bootstrap --force                            # reindex everything (ignores state)
```

**Directory-scoped globs** — `Client/**/*.ts` correctly limits the scan to `Client/` only (does not match `signoz/foo.ts`). Uses `PurePosixPath.match()` for full path matching.

**`AGNO_PROJECT` must be set in the active shell session** — the env var is read at Python import time so a User-level `SetEnvironmentVariable` only takes effect in new terminals:
```powershell
$env:AGNO_PROJECT = "ekam"   # set for this session
hive --bootstrap
```
Without this, `detect_project()` falls back to the git remote name (e.g. `EkamApp`) and creates a separate LightRAG workspace.

**How `index_project` works (current implementation):**
- **File scan**: `os.walk` with in-place directory pruning — ignored dirs are never descended into
- **Ignored directories**: `node_modules`, `.next`, `__pycache__`, `.venv`, `dist`, `build`, `signoz`, `graphify-out`, `infra`, and all hidden dirs (`.git`, etc.)
- **Skipped extensions**: binaries, media, archives, `.lock`, `.pem`, `.key`, `.crt`, `.cer`, `.p12`, `.pfx`
- **Change detection**: `mtime+size` via `os.stat()` — no file read required
- **Single LightRAG session**: one `streamablehttp_client` session shared across all inserts
- **Incremental saves**: state written after every file — `Ctrl+C` is safe, next run resumes from last completed file
- **Time budget**: stops at `time_budget_seconds` (default 480s) and returns `Partial — N files remaining` so the CLI can loop

State: `PROJECT_ROOT/.hive-index-state/{project_id}.json` (on host, persisted via `/project` Docker volume — survives container restarts)

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
# On ZGX — how many docs are processed for a specific workspace
docker exec agno-postgres-age psql -U agno -d agno_graph \
  -c "SELECT status, count(*) FROM agno.lightrag_doc_status WHERE workspace='ekam' GROUP BY status ORDER BY status;"

# Tail live log (ZGX-side indexer)
tail -f /tmp/agno-hive-index.log
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
| `project_{id}` | `lightrag_vdb_*_{id}_1024d` | Per-project code knowledge |
| `global` | `lightrag_vdb_*_global_1024d` | Cross-project patterns and lessons |

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

### Per-Project Storage Isolation

Each `project_id` gets fully isolated storage across both backends:

| Backend | Isolation mechanism | Collection/workspace naming |
|---|---|---|
| Qdrant | `model_name=project_id` on `EmbeddingFunc` | `lightrag_vdb_chunks_{id}_1024d`, `lightrag_vdb_entities_{id}_1024d`, `lightrag_vdb_relationships_{id}_1024d` |
| PostgreSQL | `POSTGRES_WORKSPACE=project_id` set before LightRAG init | `workspace` column = project_id in all lightrag_* tables |

This is automatic — `get_rag("ekam")` always writes to `ekam`-suffixed Qdrant collections and `workspace=ekam` in PostgreSQL. No configuration needed per project.

**Consistent project_id**: `hive --bootstrap` derives `project_id` from `AGNO_PROJECT` env var, falling back to `Path.cwd().name` from the git remote URL. Always set `AGNO_PROJECT` explicitly:
```powershell
# Current session (required — User-level env vars only apply to new terminals)
$env:AGNO_PROJECT = "ekam"

# Permanent (new terminals only)
[System.Environment]::SetEnvironmentVariable("AGNO_PROJECT", "ekam", "User")
```
If `AGNO_PROJECT` is unset, `detect_project()` may return the git repo name (e.g. `EkamApp`) and create a separate `EkamApp` workspace in LightRAG. Clean up with `DELETE FROM agno.lightrag_doc_status WHERE workspace='EkamApp'` + delete the `ekamapp_*` Qdrant collections.

### Wiping and Re-indexing

> ⚠️ **Always ask the user for confirmation before running any of these commands.** See the DB Safety Rule in the Troubleshooting section.

Use **scoped DELETE** (not TRUNCATE) to remove data for a single workspace without affecting others:

```bash
# 1. Delete PostgreSQL data for one workspace only
docker exec agno-postgres-age psql -U agno -d agno_graph -c "
DELETE FROM agno.lightrag_doc_status WHERE workspace='ekam';
DELETE FROM agno.lightrag_doc_chunks WHERE workspace='ekam';
DELETE FROM agno.lightrag_doc_full WHERE workspace='ekam';
DELETE FROM agno.lightrag_entity_chunks WHERE workspace='ekam';
DELETE FROM agno.lightrag_full_entities WHERE workspace='ekam';
DELETE FROM agno.lightrag_full_relations WHERE workspace='ekam';
DELETE FROM agno.lightrag_llm_cache WHERE workspace='ekam';
DELETE FROM agno.lightrag_relation_chunks WHERE workspace='ekam';"

# 2. Delete Qdrant collections for this workspace
for col in lightrag_vdb_chunks_ekam_1024d lightrag_vdb_entities_ekam_1024d \
           lightrag_vdb_relationships_ekam_1024d; do
  curl -s -X DELETE "http://localhost:6333/collections/$col"
done

# 3. Delete index state files
rm ~/.agno-hive/index-state/ekam.json               # ZGX-side state
# On Windows client: delete EkamApp/.hive-index-state/ekam.json

# 4. Restart LightRAG MCP server (clears cached RAG instances)
pkill -f "serve-lightrag"
cd ~/agno-hive && nohup python3 main.py --serve-lightrag >> /tmp/lightrag-server.log 2>&1 &

# 5. Re-index via hive --bootstrap (client machine)
$env:AGNO_PROJECT = "ekam"
hive --bootstrap
```

> **Warning:** `TRUNCATE` without a `WHERE` clause wipes ALL workspaces (ekam, agno-hive, global, etc.). Always use `DELETE FROM ... WHERE workspace='x'` for scoped removal.

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

When total message count exceeds `AGNO_COMPACT_THRESHOLD` (default: 20), a fire-and-forget `asyncio.create_task` calls `config.router_model` to summarise older messages. The summary is stored in `chat_sessions.summary`.

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
| `compact_session(session_id)` | `None` | Summarise via `config.router_model` (fire-and-forget) |
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

### write_file blocked on existing files

`write_file` is hard-blocked when the target file already exists — it returns an error and instructs the agent to use `apply_diff` instead. This prevents accidental full rewrites of large files like `docker-compose.yml`. `write_file` is only valid for brand-new files.

```
write_file blocked: 'docker-compose.yml' already exists.
Use apply_diff() to make surgical edits to existing files —
call get_file_content('docker-compose.yml') first to get the exact text to replace.
```

### run_command and run_shell write guard

When `WRITE_REVIEW=true`, both `run_command` and `run_shell` block any shell command that writes to files. Blocked patterns: `>`, `>>`, `sed -i`, `perl -i`, `tee`, `truncate`, `dd of=`.

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
| `LIGHTRAG_LLM_MODEL` | `llama3.1:8b` | Entity/relation extraction model during insert |
| `LIGHTRAG_EMBED_MODEL` | `qwen3-embedding:0.6b` | Ollama embedding model (must be pulled first) |
| `LIGHTRAG_EMBED_DIM` | `1024` | Must match embed model output dimension |
| `LIGHTRAG_WORKING_DIR` | `~/.agno-hive/lightrag` | Base dir for per-project file-based KV storage |

**Constraint:** LightRAG requires a 32K+ context window model for entity extraction. Do not use reasoning/chain-of-thought models (e.g. DeepSeek R1) for `LIGHTRAG_LLM_MODEL`.

**Note on host/port:** The `mcp` package's `FastMCP.run()` does not accept `host`/`port` arguments — they are passed to the `FastMCP(name, host=..., port=...)` constructor. `LIGHTRAG_MCP_HOST` and `LIGHTRAG_MCP_PORT` are read at module import time and passed to the constructor.

### Initialization requirement

`lightrag_mcp/server.py` must call `initialize_storages()` on every LightRAG instance before calling `aquery()` or `ainsert()`. This is handled via `_get_ready_rag(project_id)` which calls `initialize_storages()` once per project and caches the result in `_initialized: set[str]`. Skipping this step causes `'NoneType' object has no attribute 'query'` errors on every tool call.

### Legacy Qdrant collection workspace patching

If Qdrant collections were created without `model_name` set on `EmbeddingFunc`, points are stored without a `workspace_id` payload field. LightRAG queries filter by `workspace_id="_"` for legacy collections, so all existing points must be patched:

```bash
for col in lightrag_vdb_entities lightrag_vdb_relationships lightrag_vdb_chunks; do
  curl -s -X POST http://localhost:6333/collections/$col/points/payload \
    -H 'Content-Type: application/json' \
    -d '{"payload": {"workspace_id": "_"}, "filter": {}}'
done
```

Run this once after initial index if queries return `[no results]` despite data being present in Qdrant.

## Troubleshooting

### ⚠️ Database Safety Rule — Read Before Any DB Cleanup

**Never truncate, drop, or delete data from PostgreSQL or Qdrant without explicit confirmation from the user.**
This applies to all destructive operations: `TRUNCATE`, `DROP TABLE`, `DELETE FROM`, Qdrant `DELETE /collections/{name}`, and any bulk data removal.
Always describe exactly what will be deleted (workspace, row count, collections) and wait for "yes, proceed" before executing.
Workspace-scoped deletes (`DELETE FROM ... WHERE workspace='x'`) are still destructive and still require confirmation.

> **Why:** A `TRUNCATE` without a `WHERE` clause wiped all LightRAG workspaces (ekam + agno-hive) when only ekam data was intended to be removed. Use `DELETE FROM ... WHERE workspace='x'` for scoped removal, and always confirm first.

### Agent returns HTTP 400 for tool calls

Some Ollama models do not support tool/function calling. Confirmed broken:
- `deepseek-r1`, `gemma3:27b`, `mixtral:8x7b` — all return HTTP 400 for tool calls in Ollama; replaced with `mistral-small3.1:24b` (Planner), `qwen2.5-coder:32b` (Reviewer+Coder), `devstral:24b` (Researcher)

### qwen3 models crash on GB10 ARM runner (Ollama ≤0.23.4)

`qwen3:30b-a3b` (MoE) and similar MoE variants cause a hardware-level segfault in the new `ollama-engine` on NVIDIA GB10 ARM64. Root cause: `ollama-engine` (required for MoE architectures) is not yet stable on ARM64 GPU backends. Dense qwen3 models (`qwen3:30b`, `qwen3:8b`) used the legacy llama.cpp runner and may also be affected depending on Ollama version.

**Fix available:** Ollama 0.24.0 (released 2026-05-14) includes improved GB10 / Blackwell support and explicit qwen3 fixes. After upgrading, test `qwen3:30b` (dense, NOT `a3b`).

**Current fix (in effect):** `qwen2.5-coder:32b` as coordinator for all teams — set via `coordinator_model` in each `teams/*.yaml`. `nemotron3:33b` was initially used as coordinator but was found to get stuck at 100–130% CPU in Ollama 0.24.0 when the `--ollama-engine` backend is selected for large file reads + large diff generation tasks (the llama.cpp runner handles these correctly). `qwen2.5-coder:32b` uses llama.cpp on ARM64 GB10 and is stable across all task types.

If Ollama becomes unresponsive due to a stuck runner, restart the service (requires sudo):
```bash
sudo systemctl restart ollama
```
Then restart the hive server:
```bash
pkill -f "python3 main.py --serve$" && cd ~/agno-hive && nohup python3 main.py --serve >> /tmp/hive-serve.log 2>&1 &
```

### `exclude_tools` causes 0 tools loaded from hive-mcp

In agno 2.5.17, `MCPTools(exclude_tools=[...])` returns an empty tool list when any listed name is not present in that MCP server's tool list. **`exclude_tools` must only be applied to the project MCP** (which has `agno_run` and `agno_list_teams`), never to hive-mcp.

The fix in `swarm/team.py`:
```python
_project_mcp_url = effective_mcp_url
for url in all_mcp_urls:
    _exclude = ["agno_run", "agno_list_teams"] if url == _project_mcp_url else None
    mcp = await stack.enter_async_context(
        MCPTools(url=url, transport="streamable-http", exclude_tools=_exclude)
    )
```

If you see `[team] MCP connected: http://...:9003/mcp (0 tools)` in the log, check that `exclude_tools` is not being passed to hive-mcp.


If an agent silently fails (no tool calls made, no error surfaced to the user), check `agno.log` for `Error in Agent run: ... does not support tools (status code: 400)`. Fix by replacing the model in `teams/engineering.yaml`.

### MCP tool calls timing out

The AGNOHive server uses `_MCP_TIMEOUT` (in `swarm/team.py`) as the `timeout_seconds` for all `MCPTools` connections. Default is 180s. If `lightrag_query` or large `get_file_content` calls are timing out, ensure the server is running with the latest code (i.e., was restarted after any `team.py` change).

The old value was 60s. A server started before the 180s change was pushed will still use 60s until restarted.

### LightRAG queries returning `[no results]`

Three possible causes in order:

1. **`initialize_storages()` not called** — check `lightrag.log` for `'NoneType' object has no attribute 'query'`. Fix: ensure the server was restarted after the `_get_ready_rag()` fix in `lightrag_mcp/server.py`.

2. **Missing `workspace_id` on Qdrant points** — if points were indexed without `model_name` set on `EmbeddingFunc`, they have no `workspace_id` field. Run the workspace patch (see LightRAG MCP Server section above).

3. **Port still held by previous process** — a new server start fails silently if port 9002 is still occupied. Check with `lsof -i:9002` and kill the old process with `fuser -k 9002/tcp` before restarting.

### `hive --bootstrap` creates wrong workspace (`EkamApp` instead of `ekam`)

`detect_project()` reads `AGNO_PROJECT` at **Python import time**. A `User`-level `SetEnvironmentVariable` on Windows only takes effect in new terminals — the current PowerShell session won't see the change until you restart it. Always set it explicitly for the current session before running bootstrap:

```powershell
$env:AGNO_PROJECT = "ekam"
hive --bootstrap --glob "**/*.py"
```

Symptom: bootstrap header shows `Bootstrapping project EkamApp` (capital letters). Any data written to `EkamApp` workspace is wasted — clean it up:
```sql
-- PostgreSQL (ZGX)
DELETE FROM agno.lightrag_doc_status WHERE workspace = 'EkamApp';
DELETE FROM agno.lightrag_doc_chunks WHERE workspace = 'EkamApp';
-- (repeat for all lightrag_* tables)
```
```bash
# Qdrant (ZGX)
curl -X DELETE http://localhost:6333/collections/lightrag_vdb_chunks_ekamapp_1024d
curl -X DELETE http://localhost:6333/collections/lightrag_vdb_entities_ekamapp_1024d
curl -X DELETE http://localhost:6333/collections/lightrag_vdb_relationships_ekamapp_1024d
```

### LightRAG entity extraction model gets stuck (infinite generation)

`LIGHTRAG_LLM_MODEL` models with large entity-extraction prompts (full file chunk + instructions) can enter infinite generation loops at 100% CPU. Symptom: Ollama runner at 99% CPU for 3+ minutes, LightRAG logs `Merging stage failed`.

**Fix:** Use `llama3.1:8b` as `LIGHTRAG_LLM_MODEL` — proven stable on ZGX GB10. Set in `~/agno-hive/.env`:
```bash
LIGHTRAG_LLM_MODEL=llama3.1:8b
```
Then restart LightRAG: `pkill -f "serve-lightrag" && cd ~/agno-hive && nohup python3 main.py --serve-lightrag >> /tmp/lightrag-server.log 2>&1 &`

Kill stuck runners: `sudo kill <runner-pid>` (find with `ps aux | grep "ollama runner"`).

### `hive --bootstrap` times out on large projects

**Symptom:** `Error: tool call failed — timed out` after 600s with no inserts.

**Causes and fixes:**
1. `**/*` glob on Docker Windows mounts visits all files (including hidden dirs) before filtering — 10k+ files × stat calls = minutes. **Fix:** use targeted globs: `--glob "**/*.py"`, `--glob "**/*.ts"`, etc.
2. Hashing all files before filtering was slow (64s for 10k files). **Fixed in current version:** mtime+size replaces SHA-256 (~23s for same file count).
3. LightRAG entity extraction on first file (cold model start) takes 60–90s. With `time_budget_seconds=480` and `call_timeout=600`, total per pass should be ~550s — within the socket timeout.

If timeouts persist after the glob fix, check that the hive-mcp container is running the latest image (`docker compose -f docker-compose.hive.yml pull && docker compose up -d`).

### `record_success` LightRAG warning — FIXED

`swarm/feedback.py`'s `record_success()` now calls `initialize_storages()` before `ainsert()`. The previous silent NoneType failure has been resolved — successful run patterns are now stored correctly to LightRAG.

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

## Session Chaining

Every `POST /run` response includes a `session.session_id` field. Pass it back in the next request to resume context across calls.

```json
// Request 1 — new session
{ "task": "Read businessApi.ts then scaffold emailApi.ts", "project_id": "ekam", ... }

// Response 1
{ "result": "...", "session": { "session_id": "abc123", "turn": 1, "context_size": 0 }, ... }

// Request 2 — resumed session; coordinator sees turn 1 findings
{ "task": "Add tabs to page.tsx", "project_id": "ekam", "session_id": "abc123", ... }

// Response 2
{ "result": "...", "session": { "session_id": "abc123", "turn": 2, "context_size": 2 }, ... }
```

**Without `session_id`:** each call is stateless — the coordinator starts with an empty context window. Equivalent to `hive "task"` one-shot mode.

**With `session_id`:** prior messages (last `session_window` pairs, default 6) are injected into the coordinator's instructions. Equivalent to staying in `hive` REPL mode.

**From the `agno_run` MCP tool:** the `[session: <uuid>]` line at the end of every result is the `session_id` to pass to the next call:

```python
result1 = agno_run("Read businessApi.ts then scaffold emailApi.ts")
# result1 ends with: [session: abc123-...]

result2 = agno_run("Add Inbox/Compose/Settings tabs to page.tsx", session_id="abc123-...")
```

Sessions expire after 30 days unless marked permanent (`persist=true` in the request or `/persist` REPL command).