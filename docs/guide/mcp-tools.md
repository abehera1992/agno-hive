← [Back to guide index](README.md) · [Main README](../../README.md)

# 🔧 MCP Tools

## Contents
- [hive-mcp (primary)](#hive-mcp-primary--generic-works-with-any-project)
- [Project MCP (supplementary)](#project-mcp-supplementary--app-specific-tools-only)
- [Global memory](#global-memory)

---

## hive-mcp (primary — generic, works with any project)

| Tool | Purpose |
|---|---|
| `find_files(pattern)` | Glob file discovery — uses ripgrep, respects .gitignore |
| `search_files(pattern, glob)` | Regex content search — uses ripgrep, falls back to Python re |
| `count_matches(pattern, glob_filter)` | Deterministic occurrence count (ripgrep) — returns `TOTAL: <n>`; use for any "how many / count / all" instead of reading + tallying |
| `get_file_content(path)` | Read a file — a wrong-but-plausible directory guess self-heals: a unique filename match elsewhere in the project is read automatically (prefixed with a correction note); multiple matches are listed instead of a bare "not found" |
| `list_directory_tree(depth)` | Full directory skeleton, dirs only, no cap |
| `list_directory(path)` | Immediate children of a directory |
| `get_project_context()` | Reads CLAUDE.md / AGENTS.md / README.md / DOCS.md if present |
| `apply_diff(path, old_string, new_string)` | Surgical file edit (WRITE_REVIEW-aware) |
| `write_file(path, content)` | Create a new file (WRITE_REVIEW-aware) |
| `run_command(cmd)` | Read-only shell (tests, linters — writes blocked when WRITE_REVIEW=true) |
| `run_shell(cmd)` | Full shell access (npm install, docker compose, etc.) |
| `run_docker(cmd)` | Docker / docker compose commands |
| `bash_session_start(cwd)` | Create a persistent-cwd session — `cd` persists across `bash_run` calls in the same session (no env vars) |
| `bash_run(session_id, command, timeout, background)` | Run in a session's persisted cwd; blocking or `background=True` (returns a `job_id` immediately, `timeout` becomes max runtime) |
| `bash_job_status(job_id)` | Poll a background job — status/exit code/recent output |
| `bash_job_kill(job_id)` | Terminate a running background job |
| `bash_session_close(session_id)` | Close a session, killing any attached background jobs |
| `git_status/log/diff/blame` | Git operations |
| `scan_project_context(force)` | Generate/update `hive.md` — full scan or incremental |
| `index_project(project_id, lightrag_url, ...)` | Semantic bootstrap into LightRAG |
| `web_search(query, max_results)` | DuckDuckGo search (requires `WEB_SEARCH_ENABLED=true`) |
| `web_fetch(url, max_chars)` | Fetch a URL — GitHub repos return README + metadata via API (requires `WEB_SEARCH_ENABLED=true`) |
| `db_schema(table)` | List `schema.table` or describe a table's columns (requires `HIVE_DB_URL`) |
| `db_query(sql)` | Run one read-only `SELECT`/`WITH`/`EXPLAIN` — verify DB-backed facts against the live table, capped rows (requires `HIVE_DB_URL`) |

## Project MCP (supplementary — app-specific tools only)

| Tool | Purpose | Required |
|---|---|---|
| `get_context_section(topic)` | Targeted DOCS.md section by keyword | Optional |
| `search_knowledge_graph(query)` | Graph search (graphify) | Optional |
| Any other project-specific tools | App workflows, custom context | Optional |

> `memory_search`/`memory_store` are registered on project MCP (EkamApp) but
> excluded from every hive agent's connection since 2026-08-20
> (`_PROJECT_MCP_EXCLUDE_TOOLS` in `swarm/team.py`) — verified never actually
> called by a hive agent (EkamApp MCP's own logs, 5-day window, 191 tool calls,
> zero of them `memory_search`/`memory_store`), and the underlying pgvector
> table hadn't been written to since 2026-04-27. That tool is Claude Code/Cline's,
> not hive's — use `lightrag_query` for hive's equivalent pattern-recall need.

> Agents use hive-mcp for all reads and writes. Project MCP is only consulted for tools not present in hive-mcp. If project MCP is unavailable, agents continue with hive-mcp alone. If hive-mcp is unavailable, agents fall back to project MCP for reads. If both are down, the run fails with a clear error.

> **Transport:** All MCP servers must use **Streamable HTTP** (`/mcp` endpoint). The deprecated `/sse` transport is not used.

---

## Global memory

| Namespace | Scope |
|---|---|
| `project_{id}` | Per-project code knowledge |
| `global` | Shared across all projects |

Every `lightrag_query` searches both namespaces and merges results automatically.

---

**Next:** [🔗 Integrations](integrations.md) · [🛠️ Development](development.md)
