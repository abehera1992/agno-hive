# AGNOHive

A generic, model-agnostic agentic swarm built on [Agno](https://github.com/agno-agi/agno). Runs on a dedicated ZGX workstation, connects to any project via MCP, and coordinates a full engineering team of local Ollama-backed agents — no cloud API calls.

## How It Works

```
Client machine                           ZGX (AGNOHive)
──────────────                           ──────────────────────────────────────
hive-mcp  ◄─────────────────────────────  ContextRouter
  apply_diff                              Researcher
  write_file                              Planner
  run_shell                               Coder          ──► Qdrant (vectors)
  run_docker                              Executor       ──► PostgreSQL/AGE (graph)
  git_*                                   Reviewer       ──► SigNoz (OTel)
  index_project
  scan_project_context → hive.md
  web_search / web_fetch                ──────────────────────────────────────
                                           Coordinator (ibm/granite4.1:30b)
project MCP  ◄───────────────────────────  orchestrates all agents
  get_file_content
  find_files
  search_files
  memory_search
  (app-specific tools)
```

**Two MCP connections per run** (dual-MCP):
- **hive-mcp** (primary) — all file reads + writes + shell + Docker + git + ripgrep + web. Used for everything by default.
- **Project MCP** (supplementary) — project-specific tools not in hive-mcp: `memory_search`, `get_context_section`, app workflows. Optional — hive works without it.

**Graceful fallback**: if hive-mcp is unreachable, agents automatically fall back to project MCP for reads. If both are down, the run fails with a clear error. If only one MCP is provided, it handles everything.

1. Coordinator's first action is `get_file_content('hive.md')` — grounded project context loaded on demand, not pre-injected (prevents models from answering without tool calls)
2. Failure context from past runs is injected into the coordinator's instructions
3. The coordinator routes operations to the right MCP — member agents see only their scoped tool subset
4. After each run, successes go to LightRAG (vector memory) and failures go to PostgreSQL (failure log)
5. OTel traces flow to SigNoz

---

## Prerequisites

### ZGX Workstation
- Ubuntu / Linux with Python 3.12+
- Miniforge or standard venv
- Ollama running natively (for GPU access)
- Docker + Docker Compose (for Qdrant and PostgreSQL/AGE)
- Tailscale

### Ollama Models (pull before first run)
```bash
ollama pull ibm/granite4.1:30b     # Coordinator — native tool calling, dense model, ARM64 safe
ollama pull llama3.1:8b            # ContextRouter + Executor + session compaction
# ollama pull devstral:24b        # Researcher — replaced by qwen2.5-coder:32b (2026-05-24)
ollama pull qwen2.5-coder:32b      # Coder + Reviewer + Planner
ollama pull qwen2.5-coder:32b      # Coder + Reviewer
ollama pull qwen3-embedding:0.6b   # LightRAG embeddings
```

All agents run local Ollama models. Set any model via env var (e.g. `CODER_MODEL=qwen2.5-coder:32b`) or in `teams/engineering.yaml`.

> **Note:** `deepseek-r1`, `gemma3:27b`, and `mixtral:8x7b` are no longer used — all return HTTP 400 for tool calls in Ollama.
>
> **ARM64 GB10 model compatibility:**
> - `ibm/granite4.1:30b` — ✅ Dense model, native tool calling, stable on GB10. Current coordinator.
> - `qwen2.5-coder:32b` — ✅ Reliable tool use but causes CUDA crash after ~14 min continuous inference on GB10.
> - `devstral:24b` — Previously Researcher; replaced by `qwen2.5-coder:32b` (2026-05-24). Failed tool-calling discipline: hallucinated file content instead of reading via get_file_content().
> - `mistral-small3.1:24b` — Previously used for Planner; replaced by `qwen2.5-coder:32b` (2026-05-24). 21% speed improvement from eliminating model-swap overhead.
> - `lfm2:24b` — ✅ Reads files correctly but cannot orchestrate as coordinator.
>
> **Ollama upgraded 0.24.0 → 0.30.6 (2026-06-07):** the new build ships `cuda_v13` libraries that correctly list GB10's compute capability (`cc=1210`) — the old `cuda_v12` libs' compiled architecture list (`[500 520 600 610 700 750 800 860 890 900 1200]`) didn't include 1210, which was likely the root cause of several MoE crashes (`qwen3:30b-a3b`, `gemma4` MoE, `nemotron3:33b` instability). Post-upgrade, `agno_run` was re-verified end-to-end (planning team, byte-for-byte grounded results, 77s vs ~268s pre-upgrade for a comparable task) — full functionality confirmed, no regressions. See [Ollama Upgrade & GB10 Compatibility](docs.md#ollama-upgrade--gb10-compatibility-20260607) in docs.md for the full writeup including a binary-swap gotcha.

### Client Machine
- Docker (for hive-mcp)
- **Tailscale** — mandatory; ZGX and client machines must be on the same Tailscale network
- Python 3.8+ (for the `hive` CLI — stdlib only, no pip install needed)

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

# Verify
docker ps --filter "name=agno-"
curl http://localhost:6333/healthz
docker exec agno-postgres-age psql -U agno -d agno_graph -c "SELECT * FROM ag_catalog.ag_graph;"
```

---

## Client Setup: hive-mcp

hive-mcp is a Docker container that runs on your local machine and gives AGNOHive host-level access via Tailscale.

```bash
# Copy the compose file into your project directory
cp /path/to/agno-hive/hive-mcp/docker-compose.hive.yml .

# Pull and start
docker compose -f docker-compose.hive.yml up -d

# Verify
docker ps --filter "name=hive-mcp"
```

ZGX reaches it via your Tailscale IP: `http://<your-tailscale-ip>:9000/mcp`

The `hive` CLI auto-detects your Tailscale IP — no manual URL configuration needed.

### Enabling web search

Add `WEB_SEARCH_ENABLED=true` to the container to give agents access to `web_search` and `web_fetch`:

```bash
# docker run
docker run -d --name hive-mcp ... -e WEB_SEARCH_ENABLED=true ghcr.io/abehera1992/hive-mcp:latest

# docker compose — set in shell or .env before running
WRITE_REVIEW=true WEB_SEARCH_ENABLED=true docker compose -f docker-compose.hive.yml up -d
```

When enabled, agents will:
- **Auto-fetch any URL** the user shares in a prompt
- **Read GitHub repos** (README + metadata) when a repo URL or name is mentioned
- **Search DuckDuckGo** when asked about unfamiliar libraries, tools, or technologies
- **Chain search → fetch** — find the best result, then read the full page for grounded answers

Uses the client machine's network. No API key required.

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

# Client project MCP server (Streamable HTTP, /mcp endpoint)
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

# LightRAG MCP (Streamable HTTP, port 9002)
LIGHTRAG_MCP_PORT=9002
LIGHTRAG_MCP_URL=http://localhost:9002/mcp
LIGHTRAG_LLM_MODEL=llama3.1:8b
LIGHTRAG_EMBED_MODEL=qwen3-embedding:0.6b
LIGHTRAG_EMBED_DIM=1024

# Observability → SigNoz (optional, omit to disable)
OTEL_EXPORTER_OTLP_ENDPOINT=http://<signoz-host>:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_RESOURCE_ATTRIBUTES=service.name=agno-hive,deployment.environment=dev
```

---

## Running AGNOHive

### 1. Start the LightRAG MCP Server (ZGX)
```bash
python main.py --serve-lightrag
# → Streamable HTTP on 0.0.0.0:9002  (/mcp endpoint)
```

### 2. Start the AGNOHive API Server (ZGX)
```bash
python main.py --serve
# → FastAPI on 0.0.0.0:9001
```

### 3. Start hive-mcp (client machine)
```bash
docker compose -f docker-compose.hive.yml up -d
```

### 4. Single task (CLI)
```bash
python main.py "How does authentication work in this project?"
```

### 5. Interactive loop
```bash
python main.py
```

### 6. Index a codebase into LightRAG

**Option A — hive-mcp bootstrap** (primary path — runs on client machine, no ZGX filesystem access needed):
```powershell
$env:AGNO_PROJECT = "ekam"
hive --bootstrap                          # index all source files
hive --bootstrap --force                  # full reindex from scratch
hive --bootstrap --glob "Client/**/*.ts"  # scoped to a specific directory + extension
```

State file: `PROJECT_ROOT/.hive-index-state/{project_id}.json` — entries are
`"<mtime_ns>:<size>|<sha256>"`. The fast mtime+size key is checked first (no file
read); on mismatch the content SHA-256 decides, so metadata-only churn
(`git reset --hard`, checkout, `touch`) never triggers a re-index. Legacy
mtime-only entries upgrade in place on the next pass.

Excluded automatically: `node_modules`, `.next`, `dist`, `build`, `signoz`, `graphify-out`, `infra`, `backups` (DB dumps — never index), `.hive-index-state`, hidden dirs, binaries, certs (`.pem`, `.key`, `.crt`).

Throughput (2026-06-10): chunk inserts run 4-concurrent over the shared MCP
session, matched by LightRAG-side tuning (`max_parallel_insert=4`,
`entity_extract_max_gleaning=0`, `embedding_batch_num=32`, dynamic `num_ctx`
8K/32K) — roughly 4–6× faster than the old serial pipeline.

**Option B — ZGX-side direct indexer** (use when ZGX has direct filesystem access to the repo):
```bash
python main.py --index --path /path/to/repo --project-id myproject
python main.py --index --path /path/to/repo --project-id myproject --force
```

State file: `~/.agno-hive/index-state/{project_id}.json`

---

## CLI Client (`hive`)

AGNOHive ships a zero-dependency CLI client (`cli/hive`) that lets you use the swarm from any terminal. Every run is backed by a **persistent chat session** stored server-side in PostgreSQL. The CLI auto-detects your Tailscale IP to connect to both your project MCP and hive-mcp.

### Installation

```bash
# Copy to your PATH
cp /path/to/agno-hive/cli/hive ~/.local/bin/hive
chmod +x ~/.local/bin/hive          # Linux/Mac
```

On Windows, the `hive` file is a Python script — either add it to your PATH or run it with `python cli/hive`.

### Configuration

```bash
# Add to ~/.bashrc / ~/.zshrc / PowerShell profile
export AGNO_HOST=http://<zgx-tailscale-ip>:9001   # AGNOHive server
export AGNO_PROJECT=myproject                       # optional — auto-detected from git remote; set explicitly to pin project_id (e.g. AGNO_PROJECT=ekam)
export AGNO_TEAM=engineering                        # optional — default team
export AGNO_MCP_URL=http://<ip>:9000/mcp           # optional — project MCP (auto-detected via Tailscale)
export AGNO_MCP_PORT=9000                           # optional — port for Tailscale auto-detection
export AGNO_SYSTEM_MCP_URL=http://<ip>:9003/mcp    # optional — hive-mcp (auto-detected via Tailscale)
export AGNO_SYSTEM_MCP_PORT=9003                    # optional — hive-mcp port for auto-detection
export AGNO_PROJECT_ROOT=/path/to/project           # optional — set when running hive from outside the project root; ensures .hive_proposed files are detected correctly
```

`AGNO_PROJECT` is auto-detected from `git remote get-url origin` — running `hive` inside a git repo uses that repo's name as the project id.

MCP URLs are auto-detected via `tailscale ip -4` — no manual configuration needed if Tailscale is installed.

---

### Command Glossary

#### One-Shot Commands

| Command | Description |
|---|---|
| `hive "task"` | Run a single task (new session each time) |
| `hive --review "task"` | Show plan, ask for approval, then execute |
| `hive -r "task"` | Alias for `--review` |
| `hive --session <id> "task"` | Run in an existing session |
| `hive --persist "task"` | Run in a new permanent session |
| `hive --project <name> "task"` | Override project auto-detection |
| `hive --team <name> "task"` | Use a specific team (default: `engineering`) |
| `hive --host <url> "task"` | Connect to a different AGNOHive instance |
| `hive --mcp-url <url> "task"` | Override project MCP URL |
| `hive --mcp-port <port> "task"` | Override Tailscale auto-detect port for project MCP |
| `hive --list-sessions` | Print recent sessions for this project and exit |
| `hive --delete-all-sessions` | Delete all sessions for this project (prompts for confirmation) |
| `hive --mcp-status` | Show connection status of both MCPs and exit |
| `hive --scan` | Generate or update `hive.md` project context file (incremental) |
| `hive --scan --force` | Rebuild `hive.md` from scratch (full rescan) |
| `hive --bootstrap` | Index this project into LightRAG for semantic search |
| `hive --bootstrap --lightrag-url <url>` | Specify LightRAG MCP URL (default: auto-derived from ZGX host) |
| `hive --bootstrap --glob "**/*.py"` | Index only matching files |
| `hive --bootstrap --force` | Re-index all files, ignore cached checksums |
| `hive confirm [path]` | Apply a pending `.hive_proposed` file |
| `hive reject [path]` | Discard a pending `.hive_proposed` file |

#### Interactive REPL

```bash
hive                      # start REPL — auto-resumes last session for this project
hive -r                   # start REPL in review mode (plan shown before every task)
hive --session <id>       # start REPL resuming a specific session
hive --persist            # start REPL with a permanent session
```

#### REPL Slash Commands

| Command | Description |
|---|---|
| `/new` | Start a fresh session (created on next prompt) |
| `/sessions` | List recent sessions for this project |
| `/history` | Print all messages in the current session |
| `/persist` | Mark the current session as permanent |
| `/delete <id>` | Delete a session by ID |
| `/delete-all` | Delete all sessions for this project (prompts) |
| `/plan <question>` | Research and plan without executing — uses planning team (600s timeout) |
| `/review <task>` | HITL: generate plan, approve, then execute |
| `/diff` | Open VS Code diff for all pending `.hive_proposed` files |
| `/confirm [path]` | Apply pending proposed file (auto-detects if only one pending) |
| `/reject [path]` | Discard pending proposed file |
| `/cleanup` | List and delete all stale `.hive_proposed` files with confirmation |
| `/mcp` | Show connection status of both MCPs |
| `/exit` | Save session to `~/.agno_last_session` and quit |
| `! task` | Run one task without review (review REPL only) |

---

### Usage Examples

#### Basic tasks

```bash
hive "how does authentication work in this project?"
hive "add input validation to the POST /sellers endpoint"
hive "what files are in the auth module?"
```

#### Sessions — resuming context

```bash
# First task — creates a new session, prints the ID in the footer
hive "explain the seller registration flow"
# ── 38.2s  ·  session a3f7c2d1  ·  turn 1  ·  0 msgs in context  ·  expires 2026-05-31
#   resume: hive --session a3f7c2d1-8b3e-4f2a-9c1d-000000000000

# Resume — agents remember the previous exchange
hive --session a3f7c2d1-8b3e-4f2a-9c1d-000000000000 "now add unit tests for that flow"
```

#### REPL

```bash
hive
# AGNOHive  project EkamApp  mode engineering  http://100.96.86.82:9001
#   project:   http://100.87.159.1:9000/mcp   + 12ms
#   hive-mcp:  http://100.87.159.1:9003/mcp   + 8ms
#   resuming session a3f7c2d1  (last used this project)
#   /new  /sessions  /history  /persist  /delete <id>  /delete-all  /diff  /cleanup  /mcp  /confirm  /reject  /exit  ·  ESC to interrupt

> explain the write_file function
> now add type hints to it
> /history
> /exit
```

#### Plan review (HITL)

```bash
hive --review "add rate limiting to the login endpoint"
# Planning... (ContextRouter -> Researcher -> Planner)
# ────────────────────────────────────────────────────
# Proposed Plan
# ────────────────────────────────────────────────────
# 1. Researcher: read src/api/auth.py ...
# 2. Coder: implement rate limiting using Redis ...
# ── planned in 18.2s
# Proceed with this plan? [Y/n]

hive -r              # review REPL — every task asks approval
> ! what files are in the auth module?   # skip review for this one
```

#### Write review (WRITE_REVIEW mode)

When hive-mcp has `WRITE_REVIEW=true`, every file write is staged for your approval:

```bash
# Agent creates/edits a file → hive-mcp writes path.hive_proposed
# hive CLI auto-detects the pending file and shows:

  review pending  src/api/auth.py
  diff open in VS Code ↑        ← or inline terminal diff if IPC unavailable

  ❯ confirm  — apply this change
    reject   — discard
    skip     — decide later

# Arrow keys ↑/↓ to choose, Enter to confirm
```

#### Multi-step changes to the same file

When the Coder needs to make two related changes to one file (e.g. add an import AND add a function call), it makes two sequential `apply_diff` calls. Each call accumulates into the same `.hive_proposed` file:

1. First `apply_diff` → import line updated → staged in `.hive_proposed` → `review_pending`
2. Coder reads `.hive_proposed` to verify → applies second diff on top → `review_pending`
3. You confirm **once** and both changes land together

The review dialog fires only after the task completes — not between individual diffs — so you see the combined result. This is handled by Guard 11 in `patterns/ekam-code-generation-guards.md`.

#### Updating hive-mcp after code changes

`docker restart hive-mcp` does **NOT** pick up a newly built image. After any `hive-mcp/**` push (which CI auto-builds):

```powershell
docker pull ghcr.io/abehera1992/hive-mcp:latest
$env:PROJECT_PATH = "C:\path\to\your\project"
docker compose -f docker-compose.hive.yml up -d --force-recreate
```

REPL commands for managing proposed files:

```bash
> /diff              # open VS Code diff for all pending files
> /confirm           # apply (auto-selects if only one pending)
> /confirm src/api/auth.py   # explicit path
> /reject src/api/auth.py
> /cleanup           # list and delete all stale .hive_proposed files
```

Or without slash:

```bash
> confirm            # same as /confirm
> reject src/api/auth.py
```

#### Index project into LightRAG

Two paths — choose based on whether ZGX has direct filesystem access to the project:

**hive-mcp bootstrap — primary path (runs inside container on client machine):**
```bash
hive --bootstrap                           # index all source files
hive --bootstrap --force                   # full reindex
hive --bootstrap --glob "Client/**/*.ts"   # scoped to directory + extension
hive --bootstrap --lightrag-url http://<zgx-tailscale-ip>:9002/mcp
```

After indexing, agents query automatically when LightRAG MCP is connected:
```
lightrag_query("how does the auth middleware work", "ekam", mode="local")
lightrag_query("cross-service patterns", "ekam", mode="global")
```

#### MCP status

```bash
hive --mcp-status
#   project MCP           +  12ms  http://100.87.159.1:9000/mcp
#     source: Tailscale auto-detect (port 9000)
#
#   hive-mcp (system)     +  8ms   http://100.87.159.1:9003/mcp
#     source: Tailscale auto-detect (port 9003)
```

#### Session management

```bash
hive --list-sessions
# ID          Title                                                 Msgs  Status
# ──────────────────────────────────────────────────────────────────────────────
# a3f7c2d1    explain the seller registration flow                     4  expires 2026-05-31
# b8e1f902    add rate limiting to the login endpoint                  2  persistent

hive --delete-all-sessions    # prompts for confirmation
```

---

### Footer Explained

```
── 42.3s  ·  session a3f7c2d1  ·  turn 3  ·  4 msgs in context  ·  expires 2026-05-31
```

| Field | Meaning |
|---|---|
| `42.3s` | Total wall time |
| `session a3f7c2d1` | First 8 chars of session UUID |
| `turn 3` | Prompt/response pairs in this session |
| `4 msgs in context` | Verbatim messages injected into the coordinator |
| `summary + N msgs` | Older turns compacted — summary + recent messages injected |
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

Sessions expire after **30 days** unless marked persistent.

---

### Features

- **hive.md context snapshot** (`--scan`) — one-time project scan writes a structured context file; auto-injected into every session bootstrap; incremental updates cover committed + staged + unstaged + untracked changes so agents always see the current project state
- **Per-agent tool scoping** — each YAML agent only sees the MCP tools it needs (Reviewer can't call `apply_diff`, Executor can't call `find_files`); reduces tool-misuse with local Ollama models
- **Grounding rules** — coordinator and Researcher are instructed to read project files before fetching external docs, cite file:line + doc URL for any comparison claim, and check CLAUDE.md before flagging a difference as a misconfiguration
- **Dual-MCP with graceful fallback** — hive-mcp is primary (reads + writes + ripgrep + web); project MCP is supplementary (memory_search, project-specific tools); if hive-mcp is down, agents fall back to project MCP automatically; coordinator sees all tools from both
- **Tailscale auto-detection** — no manual URL config; CLI discovers both MCPs via `tailscale ip -4`
- **Persistent sessions** — full conversation history in PostgreSQL, resumable by ID; `session_id` from any `/run` response can be passed back to chain context across API calls (equivalent to REPL mode)
- **Auto-resume** — REPL auto-resumes last session for the current project
- **Compaction** — sessions longer than 20 messages are summarised automatically by `config.router_model` (default: `llama3.1:8b`; was `qwen3:8b` but changed due to ARM incompatibility)
- **HITL review mode** (`--review`) — plan shown before every task, requires your approval
- **Write review** — every file write staged as `.hive_proposed`; arrow-key selector in CLI; VS Code diff via IPC if available
- **Semantic bootstrap** (`--bootstrap`) — index project into LightRAG for knowledge graph queries
- **MCP status** (`--mcp-status`) — connectivity check for both MCPs with latency
- **Readline history** — arrow keys, Ctrl+R search, persisted in `~/.agno_history`
- **Auto-detects project** from `git remote get-url origin`
- **Zero dependencies** — pure Python 3 stdlib, works on any machine with Python installed

---

## API Usage

### Health check
```bash
curl http://localhost:9001/health
# {"status": "ok", "mcp_url": "http://..."}
```

### Run a task
```bash
curl -X POST http://localhost:9001/run \
  -H "Content-Type: application/json" \
  -d '{
    "task": "What files exist in the project root?",
    "project_id": "EkamApp",
    "team": "engineering",
    "mcp_url": "http://<tailscale-ip>:9000/mcp",
    "mcp_urls": ["http://<tailscale-ip>:9003/mcp"]
  }'
```

### Get a plan only (HITL)
```bash
curl -X POST http://localhost:9001/plan \
  -H "Content-Type: application/json" \
  -d '{"task": "Add rate limiting to login", "project_id": "EkamApp"}'
```

### Session chaining — carry context across API calls

Pass `session_id` from a previous `/run` response to resume context in the next call. Without it every call is stateless (`context_size=0`). With it, prior agent findings are injected into the coordinator — equivalent to staying in REPL mode.

```bash
# Step 1 — new session; response includes "session": {"session_id": "abc123-..."}
curl -X POST http://localhost:9001/run \
  -d '{"task": "Read businessApi.ts then scaffold emailApi.ts", "project_id": "EkamApp", ...}'

# Step 2 — resume; Coder inherits Researcher output from step 1
curl -X POST http://localhost:9001/run \
  -d '{"task": "Add tabs to page.tsx", "session_id": "abc123-...", "project_id": "EkamApp", ...}'
```

From the `agno_run` MCP tool the session UUID appears on the last result line as `[session: abc123-...]` — pass it as the `session_id` argument on the next call.

### Session management endpoints
```bash
curl "http://localhost:9001/sessions?project_id=EkamApp"
curl "http://localhost:9001/sessions/<id>"
curl -X DELETE "http://localhost:9001/sessions/<id>"
curl -X PATCH "http://localhost:9001/sessions/<id>/persist"
```

### Submit output feedback (self-improving loop)
```bash
# Mark an output as incorrect — correction is injected into next run for this project
curl -X POST http://localhost:9001/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session-id>",
    "task": "write migration for auth.users",
    "project_id": "EkamApp",
    "rating": "bad",
    "notes": "__table_args__ tuple must have dict last, not first — (UniqueConstraint(...), {schema})"
  }'

# Mark an output as correct — stored in LightRAG for future pattern recall
curl -X POST http://localhost:9001/feedback \
  -d '{"task": "...", "project_id": "EkamApp", "rating": "good", "notes": "migration applied cleanly"}'
```

---

## Agent Roster

| Agent | Model | Role |
|---|---|---|
| Coordinator | `ibm/granite4.1:30b` | Routes tasks, delegates to agents, synthesises results |
| ContextRouter | `llama3.1:8b` | Picks the right memory/search backend |
| Researcher | `qwen2.5-coder:32b` | Reads and summarises the codebase |
| Planner | `qwen2.5-coder:32b` | Breaks tasks into ordered steps |
| Coder | `qwen2.5-coder:32b` | Implements features and fixes |
| Executor | `llama3.1:8b` | Runs commands and validates results |
| Reviewer | `qwen2.5-coder:32b` | Reviews code for correctness and security |

All models are configurable via `.env` or `teams/*.yaml`. Models are swappable without code changes — the YAML spec drives which model each agent uses.

> **Coordinator model:** `ibm/granite4.1:30b` (IBM Granite 4.1) — selected for native OpenAI-compatible tool calling (no custom parsing), dense architecture (ARM64 GB10 safe, no segfault), and 30B reasoning capacity for multi-step orchestration. Runs each turn in ~2-4 minutes. Previous coordinator `qwen2.5-coder:32b` was reliable but caused CUDA illegal memory access after ~14 minutes of continuous inference.

---

## Teams

| Team | Agents | Mode | Used for |
|---|---|---|---|
| `engineering` | All 6 (default) | `coordinate` | Full implementation tasks |
| `planning` | ContextRouter + Researcher + Planner | `coordinate` | HITL plan review via `POST /plan` |
| `parallel-review` | Researcher + SecurityReviewer + PerformanceReviewer | `collaborate` | Read-only parallel analysis — all agents run simultaneously |

Create a new team by adding a YAML file in `teams/` — no code changes needed. Set `mode: collaborate` in the YAML to run all agents in parallel, or override per-request via the `mode` field in `POST /run`.

---

## MCP Tools

### hive-mcp (primary — generic, works with any project)

| Tool | Purpose |
|---|---|
| `find_files(pattern)` | Glob file discovery — uses ripgrep, respects .gitignore |
| `search_files(pattern, glob)` | Regex content search — uses ripgrep, falls back to Python re |
| `get_file_content(path)` | Read a file |
| `list_directory_tree(depth)` | Full directory skeleton, dirs only, no cap |
| `list_directory(path)` | Immediate children of a directory |
| `get_project_context()` | Reads CLAUDE.md / AGENTS.md / README.md / DOCS.md if present |
| `apply_diff(path, old_string, new_string)` | Surgical file edit (WRITE_REVIEW-aware) |
| `write_file(path, content)` | Create a new file (WRITE_REVIEW-aware) |
| `run_command(cmd)` | Read-only shell (tests, linters — writes blocked when WRITE_REVIEW=true) |
| `run_shell(cmd)` | Full shell access (npm install, docker compose, etc.) |
| `run_docker(cmd)` | Docker / docker compose commands |
| `git_status/log/diff/blame` | Git operations |
| `scan_project_context(force)` | Generate/update `hive.md` — full scan or incremental |
| `index_project(project_id, lightrag_url, ...)` | Semantic bootstrap into LightRAG |
| `web_search(query, max_results)` | DuckDuckGo search (requires `WEB_SEARCH_ENABLED=true`) |
| `web_fetch(url, max_chars)` | Fetch a URL — GitHub repos return README + metadata via API (requires `WEB_SEARCH_ENABLED=true`) |

### Project MCP (supplementary — app-specific tools only)

| Tool | Purpose | Required |
|---|---|---|
| `memory_search(query)` | pgvector semantic search over project history | Optional |
| `get_context_section(topic)` | Targeted DOCS.md section by keyword | Optional |
| `search_knowledge_graph(query)` | Graph search (graphify) | Optional |
| Any other project-specific tools | App workflows, custom context | Optional |

> Agents use hive-mcp for all reads and writes. Project MCP is only consulted for tools not present in hive-mcp. If project MCP is unavailable, agents continue with hive-mcp alone.

> **Transport:** All MCP servers must use **Streamable HTTP** (`/mcp` endpoint). The deprecated `/sse` transport is not used.

---

## Global Memory

| Namespace | Scope |
|---|---|
| `project_{id}` | Per-project code knowledge |
| `global` | Shared across all projects |

Every `lightrag_query` searches both namespaces and merges results automatically.

---

## Git Workflow

All file changes are made on Windows, committed, pushed, then pulled on ZGX:

```bash
git -C ~/agno-hive pull   # on ZGX
```

**Never edit files directly on ZGX.**

---

## External Platform Integrations

hive-mcp can connect to external platforms (Notion, Google, etc.) using API keys. All platform writes go through the same human-approval gate as file writes.

### Activating integrations

Set the relevant env var before starting hive-mcp:

```bash
# Notion — integration token from notion.so/my-integrations
export NOTION_API_KEY=secret_xxxx

docker compose -f docker-compose.hive.yml pull
docker rm -f hive-mcp
docker compose -f docker-compose.hive.yml up -d
```

### Approval flow

```
agent calls notion_create_page(parent_id, title, ...)
  └─ WRITE_REVIEW=true
       ├─ writes .hive_pending_actions/<id>.json to project volume
       └─ returns "action_pending: notion/create_page — ..."
              ↓
       agent STOPS and tells the user the action is staged
              ↓
       hive CLI detects new .json files in .hive_pending_actions/
              ↓
       CLI shows action summary + arrow-key selector:
         ❯ confirm  — POST /actions/confirm → platform API call executes
           reject   — discard
           skip     — decide later
```

Read operations (`notion_search`, `notion_get_page`) pass through immediately — no approval required.

### Adding a new platform

1. Create `hive-mcp/tools/integrations/<platform>.py`
2. Implement `_execute(tool, args)` and call `register_executor("<platform>", _execute)` at module level
3. Add read and write tool functions — write tools call `_stage_action()` when `WRITE_REVIEW=true`
4. Guard activation in `hive-mcp/main.py` with the env var check
5. Add the env var to `hive-mcp/config.py` and `docker-compose.hive.yml`

### Available platforms

| Platform | Env var | Read tools | Write tools |
|---|---|---|---|
| Notion | `NOTION_API_KEY` | `notion_search`, `notion_get_page` | `notion_create_page`, `notion_update_page_props`, `notion_append_blocks` |

---

## Running Tests

```bash
pytest tests/ -v
```
