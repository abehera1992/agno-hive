# agno-hive: Generic MCP Project Design

**Date:** 2026-04-26  
**Status:** Approved

## Goal

Make agno-hive a generic agentic swarm that works with any project by pointing `MCP_URL` at that project's MCP server — no code changes required per project.

## Problem

The current codebase has three project-specific coupling points:

1. `patterns/ekam-*.md` — eKam coding rules loaded from local files into every task prompt
2. `swarm/team.py` — hardcoded eKam-specific coordinator instructions (RTK Query, SQLAlchemy, SCSS, FastAPI)
3. `config/config.py` — hardcoded Tailscale IPs and `ekamApp` DB name as defaults

## Architecture

### Data Flow (after)

```
main.py → run_task_async(task)
            └─ bootstrap(mcp_url)
            │    ├─ find_files('patterns/**/*.md') → get_file_content() per file
            │    ├─ fallback: get_project_context() if no pattern files found
            │    └─ list MCP tools → detect memory capability
            │    returns (project_context: str, has_mcp_memory: bool)
            │
            └─ resolve_memory_fns(db_url, has_mcp_memory)
            │    returns (store_fn, search_fn)
            │
            └─ Team(instructions=[...generic routing..., project_context])
                 └─ MCPTools (runtime tool calls)
```

Project context loads **once at startup**, not per-task, and not from local files.

## Components

### New: `swarm/bootstrap.py`

Single async function: `bootstrap(mcp_url: str, timeout: int, patterns_glob: str) -> tuple[str, bool]`

**Responsibilities:**
- Opens a raw MCP client session using the `mcp` package (already a transitive dependency of `agno[mcp]`)
- Discovers pattern files: calls `find_files(patterns_glob)` on the MCP server
- Reads each pattern file via `get_file_content(path)` and concatenates them
- Falls back to `get_project_context()` if no pattern files are found
- Lists all available MCP tools to detect whether `memory_search`/`memory_store` are present
- Returns `(project_context, has_mcp_memory)`

**Pattern discovery order:**
1. `find_files(patterns_glob)` → `get_file_content()` per file (primary)
2. `get_project_context()` alone (fallback)

The `patterns_glob` defaults to `patterns/**/*.md` and is configurable via `PATTERNS_GLOB` env var, allowing different projects to use different directory conventions.

### Modified: `swarm/memory.py`

Add `resolve_memory_fns(db_url: str | None, has_mcp_memory: bool) -> tuple[callable, callable]`

**Memory tier resolution:**

| Condition | store_fn | search_fn |
|---|---|---|
| `db_url` is set | asyncpg INSERT (existing) | asyncpg ILIKE (existing) |
| No `db_url`, MCP has memory tools | no-op | no-op |
| Neither | no-op | no-op |

When MCP memory tools are present and no DB is configured, agents call `memory_search`/`memory_store` directly via `MCPTools` — no Python wrapper needed. The no-op functions keep call sites clean.

The existing ILIKE text search is preserved. Upgrading to `pgvector` `<=>` cosine similarity is out of scope for this change.

### Modified: `swarm/team.py`

- Call `bootstrap()` at the top of `run_task_async()` before constructing `Team`
- Call `resolve_memory_fns()` to get the correct `(store_fn, search_fn)` pair
- Replace the hardcoded eKam instruction block with: generic tool-routing rules (kept) + `project_context` string appended at end
- Pass resolved `store_fn`/`search_fn` to agents instead of always importing `memory_store`/`memory_search`
- Remove `build_swarm()` legacy sync wrapper — it duplicates the async path and hardcodes eKam rules

### Modified: `config/config.py`

| Field | Before | After |
|---|---|---|
| `ollama_host` default | `http://100.96.86.82:11434` | `""` |
| `mcp_url` default | `http://100.87.159.86:9000/sse` | `""` |
| `db_url` default | hardcoded connection string | `None` |
| `db_url` type | `str` | `str \| None` |
| new: `patterns_glob` | — | `os.getenv("PATTERNS_GLOB", "patterns/**/*.md")` |

### Modified: `main.py`

- Remove `PATTERNS_DIR`, `load_preamble()`, `build_task()` — local pattern loading eliminated
- Pass `user_task` directly to `run_task_async(user_task)`
- Interactive loop unchanged

### Modified: `patterns/` directory

- eKam pattern files already moved to the ekam project repo
- Add `patterns/README.md` explaining the convention: pattern files live in the target project, discovered via MCP file tools

## Convention for Target Projects

For agno-hive to auto-discover patterns from a connected project, the target project should:

1. Store pattern markdown files under a `patterns/` directory (or configure `PATTERNS_GLOB` to match its structure)
2. Have those files accessible via the MCP server's `find_files` and `get_file_content` tools

No new MCP tool is required. The existing file navigation tools are sufficient.

## `.env` After

```
OLLAMA_HOST=http://<zgx-tailscale-ip>:11434

LEADER_MODEL=qwen3:30b-a3b
CODER_MODEL=mistral-small3.1:24b
REVIEWER_MODEL=gemma3:27b

MCP_URL=http://<project-tailscale-ip>:9000/sse
PATTERNS_GLOB=patterns/**/*.md   # optional, this is the default

# Optional — omit if the project's MCP server has memory tools
DB_URL=postgresql://user:password@host:5432/db
MEMORY_NAMESPACE=agno-hive

STREAM=false
MAX_ITERATIONS=5
```

## Files Changed

| File | Change type |
|---|---|
| `swarm/bootstrap.py` | New |
| `swarm/memory.py` | Modified (add `resolve_memory_fns`, no-op tier) |
| `swarm/team.py` | Modified (bootstrap call, dynamic instructions, remove eKam rules, remove `build_swarm`) |
| `config/config.py` | Modified (remove hardcoded defaults, add `patterns_glob`, `db_url` optional) |
| `main.py` | Modified (remove local pattern loading) |
| `patterns/README.md` | New |
| `patterns/ekam-*.md` | Deleted (moved to ekam repo) |

## Out of Scope

- Upgrading memory search from ILIKE to `pgvector` cosine similarity
- Supporting non-Ollama model providers (the `get_model()` factory is already provider-aware by design)
- Adding `get_patterns()` tool to the ekam MCP server (file tool fallback covers it)
