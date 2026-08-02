← [Back to guide index](README.md) · [Main README](../../README.md)

# 🖥️ CLI Client (`hive`)

AGNOHive ships a zero-dependency CLI client (`cli/hive`) that lets you use the swarm from any terminal. Every run is backed by a **persistent chat session** stored server-side in PostgreSQL. The CLI auto-detects your Tailscale IP to connect to both your project MCP and hive-mcp.

## Contents
- [Installation](#installation)
- [Configuration](#configuration)
- [Command glossary](#command-glossary)
- [Interactive REPL](#interactive-repl)
- [Usage examples](#usage-examples)
- [Footer explained](#footer-explained)
- [Session behaviour](#session-behaviour)
- [Features](#features)

---

## Installation

```bash
# Copy to your PATH
cp /path/to/agno-hive/cli/hive ~/.local/bin/hive
chmod +x ~/.local/bin/hive          # Linux/Mac
```

On Windows, the `hive` file is a Python script — either add it to your PATH or run it with `python cli/hive`.

## Configuration

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

## Command glossary

### One-shot commands

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

### Interactive REPL

```bash
hive                      # start REPL — auto-resumes last session for this project
hive -r                   # start REPL in review mode (plan shown before every task)
hive --session <id>       # start REPL resuming a specific session
hive --persist            # start REPL with a permanent session
```

### REPL slash commands

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

## Usage examples

### Basic tasks

```bash
hive "how does authentication work in this project?"
hive "add input validation to the POST /sellers endpoint"
hive "what files are in the auth module?"
```

### Sessions — resuming context

```bash
# First task — creates a new session, prints the ID in the footer
hive "explain the seller registration flow"
# ── 38.2s  ·  session a3f7c2d1  ·  turn 1  ·  0 msgs in context  ·  expires 2026-05-31
#   resume: hive --session a3f7c2d1-8b3e-4f2a-9c1d-000000000000

# Resume — agents remember the previous exchange
hive --session a3f7c2d1-8b3e-4f2a-9c1d-000000000000 "now add unit tests for that flow"
```

### REPL

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

### Plan review (HITL)

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

### Write review (WRITE_REVIEW mode)

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

### Multi-step changes to the same file

When the Coder needs to make two related changes to one file (e.g. add an import AND add a function call), it makes two sequential `apply_diff` calls. Each call accumulates into the same `.hive_proposed` file:

1. First `apply_diff` → import line updated → staged in `.hive_proposed` → `review_pending`
2. Coder reads `.hive_proposed` to verify → applies second diff on top → `review_pending`
3. You confirm **once** and both changes land together

The review dialog fires only after the task completes — not between individual diffs — so you see the combined result. This is handled by Guard 11 in `patterns/ekam-code-generation-guards.md`.

### Updating hive-mcp after code changes

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

### Index project into LightRAG

Two paths — choose based on whether ZGX has direct filesystem access to the project:

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

Full indexing details (state file format, throughput tuning, document-identity fix): **[🚀 Running AGNOHive → Index a codebase](running.md#index-a-codebase-into-lightrag)**

### MCP status

```bash
hive --mcp-status
#   project MCP           +  12ms  http://100.87.159.1:9000/mcp
#     source: Tailscale auto-detect (port 9000)
#
#   hive-mcp (system)     +  8ms   http://100.87.159.1:9003/mcp
#     source: Tailscale auto-detect (port 9003)
```

### Session management

```bash
hive --list-sessions
# ID          Title                                                 Msgs  Status
# ──────────────────────────────────────────────────────────────────────────────
# a3f7c2d1    explain the seller registration flow                     4  expires 2026-05-31
# b8e1f902    add rate limiting to the login endpoint                  2  persistent

hive --delete-all-sessions    # prompts for confirmation
```

---

## Footer explained

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
| `chain handoff` | Chain-boundary digest from prior run — ~20 lines (task, files, outcomes, status). No message history injected. |
| `expires 2026-05-31` | TTL expiry date |
| `[persistent]` | Session will never auto-delete |

---

## Session behaviour

| Mode | New session? | Saves to `~/.agno_last_session`? |
|---|---|---|
| `hive "task"` (one-shot) | Always | No |
| `hive` (REPL) | Only if no prior session for this project | Yes, on `/exit` |
| `hive --session <id>` | No — resumes specified session | Yes (REPL), No (one-shot) |
| `hive --persist` | Yes, permanent | Yes (REPL) |
| `/new` in REPL | Yes | On next `/exit` |

Sessions expire after **30 days** unless marked persistent.

---

## Features

- **hive.md context snapshot** (`--scan`) — one-time project scan writes a structured context file; auto-injected into every session bootstrap; incremental updates cover committed + staged + unstaged + untracked changes so agents always see the current project state
- **Per-agent tool scoping** — each YAML agent only sees the MCP tools it needs (Reviewer can't call `apply_diff`, Executor can't call `find_files`); reduces tool-misuse with local Ollama models
- **Grounding rules** — coordinator and Researcher are instructed to read project files before fetching external docs, cite file:line + doc URL for any comparison claim, and check CLAUDE.md before flagging a difference as a misconfiguration
- **Dual-MCP with graceful fallback** — hive-mcp is primary (reads + writes + ripgrep + web); project MCP is supplementary (memory_search, project-specific tools); if hive-mcp is down, agents fall back to project MCP automatically; coordinator sees all tools from both
- **Tailscale auto-detection** — no manual URL config; CLI discovers both MCPs via `tailscale ip -4`
- **Persistent sessions** — full conversation history in PostgreSQL, resumable by ID; `session_id` from any `/run` response can be passed back to chain context across API calls (equivalent to REPL mode)
- **Auto-resume** — REPL auto-resumes last session for the current project
- **Chain-boundary handoff** — after each successful run with a `session_id`, a compact structured digest (task, files referenced, key outcomes, status) is saved to the session's `summary` column. On the next chained call this digest replaces the full message history in the coordinator's context — preventing context-window overflow on long multi-step chains while preserving prior-run awareness. Footer shows `context: 0 msgs` to distinguish from injected message history.
- **Compaction** — sessions longer than 20 messages are summarised automatically by `config.router_model` (default: `llama3.1:8b`; was `qwen3:8b` but changed due to ARM incompatibility)
- **HITL review mode** (`--review`) — plan shown before every task, requires your approval
- **Write review** — every file write staged as `.hive_proposed`; arrow-key selector in CLI; VS Code diff via IPC if available
- **Semantic bootstrap** (`--bootstrap`) — index project into LightRAG for knowledge graph queries
- **MCP status** (`--mcp-status`) — connectivity check for both MCPs with latency
- **Readline history** — arrow keys, Ctrl+R search, persisted in `~/.agno_history`
- **Auto-detects project** from `git remote get-url origin`
- **Zero dependencies** — pure Python 3 stdlib, works on any machine with Python installed

---

**Next:** [🔌 API Usage](api.md) · [🤖 Agents & Teams](agents-and-teams.md)
