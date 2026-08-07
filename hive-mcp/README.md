# hive-mcp

A generic, platform-agnostic Docker MCP server that gives AGNOHive control over your local machine — file system, shell, Docker, git, web, and external platform integrations — over Streamable HTTP via Tailscale.

## Why it exists

Project-specific MCP servers expose tools for reading and working within an app (routes, schemas, patterns). They deliberately don't expose raw filesystem or shell access. `hive-mcp` fills that gap: it gives agents surgical file editing, shell commands, Docker inspection, git operations, project bootstrapping, web search, and external platform integrations — regardless of what project you're working on.

## Architecture

```
ZGX (AGNOHive)
  │
  ├── project MCP  →  read context, memory, app-specific workflows
  └── hive-mcp     →  apply_diff, write_file, run_shell, run_docker, git_*
                        bash_session_start/bash_run/bash_job_status (persistent
                          cwd + background jobs)
                        index_project (bootstrap into LightRAG)
                        web_search, web_fetch
                        db_schema, db_query (read-only SQL grounding)
                        notion_*, google_* (external platform integrations)
```

Agents choose which MCP to use based on operation type. The coordinator instructions make the split explicit.

---

## Tools

### File reading (read-only)
| Tool | Description |
|---|---|
| `get_project_context()` | Full project overview — directory tree + key files |
| `get_file_content(path)` | Read a file |
| `find_files(pattern)` | Glob search (ripgrep-backed, with frontend prefix fallbacks) |
| `search_files(pattern, glob)` | Regex content search |
| `count_matches(pattern, glob_filter)` | **Deterministic occurrence count** (ripgrep `--count-matches`) — returns `TOTAL: <n>` + per-file breakdown. Use for ANY count/total/"how many" instead of reading and tallying (which models confabulate). |
| `list_directory(path)` | Immediate children of a directory |
| `list_directory_tree()` | Full directory skeleton (no depth cap) |
| `scan_project_context()` | Generate a `hive.md` project snapshot for fast context loading |

### File writing (WRITE_REVIEW-aware)
| Tool | Description |
|---|---|
| `apply_diff(path, old_string, new_string)` | Surgical replacement — use for ALL edits to existing files |
| `write_file(path, content)` | Create a new file (blocked if file already exists) |
| `run_command(cmd)` | Read-only shell (tests, linters) — write-blocked when `WRITE_REVIEW=true` |

### Shell + Docker + environment
| Tool | Description |
|---|---|
| `run_shell(cmd)` | Run any shell command (install, start services) |
| `run_docker(cmd)` | Docker and docker compose commands |
| `get_env_info()` | OS, Python, Node, Docker versions |
| `check_port(port)` | Check if a port is open |
| `list_processes()` | Running processes |

### Persistent bash sessions + background jobs
`run_command`/`run_shell` are stateless — every call is a fresh `subprocess.run(cwd=PROJECT_ROOT)`
with no `cd` persistence, no background execution, and no output cap. This is the more powerful
alternative: a session-scoped working directory that persists across calls, plus long-running
commands that run detached and get polled later instead of blocking the call. Modeled on Claude
Code's own Bash tool semantics (cwd persists between commands; background + later status checks
instead of blocking) — **not** a PTY, and no persisted environment variables (only cwd persists).

| Tool | Description |
|---|---|
| `bash_session_start(cwd="")` | Create a session, returns a `session_id`. `cwd` (optional) is resolved against `PROJECT_ROOT`; empty defaults to the project root. |
| `bash_run(session_id, command, timeout=120, background=False)` | Run a command in the session's persisted cwd. **Blocking** (default): waits, returns stdout+stderr+exit code. A bare `cd <path>` (the whole command) updates the session's cwd for future calls, only if it succeeds; a `cd` chained inside a larger command runs in that command's own subshell and does not leak out. **Background** (`background=True`): starts detached, returns a `job_id` immediately — `timeout` becomes the job's max runtime, not a wait. |
| `bash_job_status(job_id, tail_chars=4000)` | Poll a background job — status (`running`/`exited`/`timed_out`/`killed`), exit code once finished, and the most recent output. Safe to call repeatedly. |
| `bash_job_kill(job_id)` | Terminate a running background job. No-op with a clear message if it already finished. |
| `bash_session_close(session_id)` | Close a session, killing and removing any background jobs still attached to it. |

Session/job state is in-memory only (does not survive a container restart, and isn't meant to) — a
session-scoped id rather than a global mutable cwd, since hive-mcp is one long-lived process shared
by every concurrent tool call across every swarm run; a global cwd would let one task's `cd` bleed
into an unrelated concurrent task. Guardrails: output capped at `HIVE_BASH_MAX_OUTPUT_CHARS`
(`run_command`/`run_shell` have no cap today), the same `WRITE_REVIEW`-gated write-command blocklist
as `run_shell`/`run_command`, a concurrency cap on live sessions/jobs, and an idle-TTL reaper — a
**running** job is only ever reaped by its own timeout (never by idle/no-poll alone, since a
legitimately slow job going quiet isn't the same as a stuck one); a **finished** job is freed once
idle past the TTL. Exposed only to the Executor agent by default (`teams/engineering.yaml`) —
the agent already most privileged for command execution.

### Git
| Tool | Description |
|---|---|
| `git_status()` | Working tree status |
| `git_log(n)` | Recent commits |
| `git_diff(ref)` | Diff vs ref |
| `git_log_file(path)` | History for a specific file |
| `git_blame(path)` | Line-by-line authorship |

### Semantic indexing
| Tool | Description |
|---|---|
| `index_project(project_id, lightrag_url, glob_filter, force)` | Walk project, chunk files, insert into LightRAG |

### Web (gated by `WEB_SEARCH_ENABLED=true`)
| Tool | Description |
|---|---|
| `web_search(query)` | DuckDuckGo search — no API key required |
| `web_fetch(url)` | Fetch and clean any URL; GitHub repo URLs return README + metadata |

### Read-only SQL grounding (gated by `HIVE_DB_URL`)
Activated only when `HIVE_DB_URL` is set. Lets agents **verify facts against the live database**
instead of grepping files — a value stored in a table (a count, a current column value) is ground
truth only in the table; seed/migration/code text can be stale or incomplete. **Generic:** the tool
holds no project/schema knowledge — the access boundary is the DB role's grants, so point it at any
project's DB by changing the DSN.

| Tool | Approval | Description |
|---|---|---|
| `db_schema(table=None)` | None | No arg → list every `schema.table` (system schemas excluded). With a `schema.table` or bare table name → its columns, types, nullability. Call this first to confirm exact names before querying. |
| `db_query(sql)` | None | Run ONE read-only `SELECT` / `WITH … SELECT` / `EXPLAIN` / `TABLE` / `VALUES` / `SHOW`. Rows capped at `HIVE_DB_MAX_ROWS`; per-call `statement_timeout`. Use an aggregate (`SELECT col, count(*) … GROUP BY col`) for authoritative counts. |

**Defense in depth (all enforced):** (1) connect as a **read-only DB role** — recommended a member of
`pg_read_all_data` with `default_transaction_read_only = on`, so the database itself refuses any write;
(2) the psycopg connection is forced `read_only`; (3) a **single-statement allowlist** rejects writes,
DDL, and `;`-chained statements; (4) results capped + timed out. Writes are blocked at BOTH the allowlist
and the DB role.

Config (env): `HIVE_DB_URL` (a read-only DSN, e.g. `postgresql://hive_ro:<pw>@host.docker.internal:5433/mydb`;
on Docker Desktop the host DB is reachable at `host.docker.internal:<published-port>`), `HIVE_DB_MAX_ROWS`
(default 1000), `HIVE_DB_TIMEOUT_MS` (default 5000). One-time role setup (Postgres):
```sql
CREATE ROLE hive_ro LOGIN PASSWORD '<pw>';
ALTER ROLE hive_ro SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE "<db>" TO hive_ro;
GRANT pg_read_all_data TO hive_ro;   -- reads ALL schemas, present + future, no per-schema maintenance
```

> **Behavioral note:** the agent uses these tools when the prompt asks a **DB-targeted question**
> ("how many rows in table X have value Y"). A prompt that explicitly steers to `search_files` for a
> DB-backed fact will bypass them — so phrase DB-fact tasks as DB questions.

### External platform integrations
Activated by env var — tools only appear when the platform is configured.

**Notion** (`NOTION_API_KEY` required)

Write tools accept **simple values** — pass `properties` as a plain dict of field → value
(e.g. `{"Status": "Done", "Area": "Platform", "Sprint": "<page-url>"}`) and the tool reads the
target database schema and coerces each value into the correct Notion property shape
(title / rich_text / select / **status** / multi_select / number / checkbox / date / url /
relation / people). The caller never has to know whether a field is a select vs status vs
relation. Relation values may be a page id **or** full page URL (the id is extracted). Unknown
field names are skipped so a typo doesn't fail the whole write. Nested params may also arrive as
JSON strings — they are parsed automatically (the agno/ollama tool-call path stringifies
dicts/lists, which previously caused "expected dict, got string" failures).

**Board discovery — no hardcoded database ids (2026-07-18).** The delivery-board databases
("Work Items", "Sprints") are discovered **by name** at runtime via Notion search: exact-title
match, cached in-process, and automatically invalidated + rediscovered once if a query against
a cached id fails — so database renames or Notion id-scheme changes self-heal. This replaced
hardcoded ids that broke when Notion's 2025-09 API split database ids from data-source ids
(the pinned values were data-source ids; `/v1/databases/{id}/query` on 2022-06-28 only accepts
database ids → every board query 404'd). Tools taking a `database_id` argument accept **either**
id form: on failure they retry through the `/v1/data_sources/{id}` (2025-09-03) resolver.

| Tool | Approval | Description |
|---|---|---|
| `notion_search(query)` | None | Search pages and databases by title/content |
| `notion_get_page(page_id, max_lines)` | None | Read a page's **full content** — paginates ALL top-level blocks (not just the first 25) and **renders table rows**, so long pages (roadmaps, rate tables) are read end-to-end. Capped at `max_lines` (default 600) |
| `notion_get_database_schema(database_id)` | None | List a database's property names + types (and select/status option names) — use to learn valid field/option names before writing |
| `notion_query_database(database_id, filter, sorts, page_size)` | None | List / filter database rows in ONE call (e.g. all Work Items in a sprint). `database_id` accepts **either a database id or a data-source id** (a failed call retries through the `data_sources` resolver). `filter` is a Notion filter object/JSON string; a relation filter's `contains` accepts a page id or URL. Compact one-line-per-row output — `[ID]` + title + **`(page_id: <hex>)`** + select/status/number/date values. **Use that `page_id` to update/trash a row you found here** (never the display id like `EK-16`). **Relation properties are omitted** (a relation rendered as `Sprint=1 linked` was misread by models as the value "Sprint 1" — membership is implied by the filter; use `notion_get_page` for relation detail) |
| `notion_items_in_sprint(database_id="", sprint_id, sprint_name, status, exclude_status, sprint_property, status_property)` | None | **Sprint-scoped report helper** — list every work item in a sprint by the **Sprint relation**, with **automatic pagination** (all rows, not just the first page). `database_id` is **optional** — omit it and the tool uses the auto-discovered "Work Items" database (see board discovery note below); `sprint_name` (e.g. `"7"`) resolves via the Sprints DB with exact-title priority ("Sprint 7", never "Sprint 17"). The tool builds the nested relation filter for you, so a small model never has to construct it. `status` keeps only matching rows; `exclude_status="Done"` returns "what's left". Same compact `[ID] title (page_id: <hex>)` output as `notion_query_database`. Prefer this over `notion_query_database` for any "what is in / left in sprint X" question |
| `notion_create_page(parent_id, title, properties, content, parent_type, markdown_content)` | Staged | Create a database row (or child page). `properties` = dict of simple field values; `title` goes to the database's title property automatically. `markdown_content` = RICH body in Notion-flavored markdown (headings, lists, fenced code, tables, **bold**/`code`/links) — use it for technical/multi-section pages instead of plain `content` |
| `notion_update_page_props(page_id, properties)` | Staged | Update fields by simple value, e.g. `{"Status": "Done"}`. status / select / relation / date are coerced from the schema; pass `null` to clear a field |
| `notion_append_blocks(block_id, blocks, after_block_id)` | Staged | Append blocks. `blocks` may be a list of plain strings (each becomes a paragraph) or full block dicts; `after_block_id` controls insertion position |
| `notion_append_markdown(block_id, markdown)` | Staged | Append RICH content from Notion-flavored markdown (headings, lists, fenced code, tables, **bold**/`code`/links) — the rich counterpart to `notion_append_blocks`. Batches over the 100-block API cap automatically |
| `notion_update_content(page_id, old_str, new_str)` | Staged | **Reliable in-place SEARCH/REPLACE — the preferred way to "update / fix / change" existing text.** Finds the ONE block whose rendered markdown contains `old_str`, replaces `old_str`→`new_str`, patches it (recurses into nested sub-bullets). REJECTS zero or multiple matches, so it never edits the wrong block when blocks share a prefix across sections. No block id needed. (`*italic*` is not round-tripped; only `**bold**`/`code`/links.) |
| `notion_update_block(block_id, text, checked)` | Staged | Edit ONE block by id — rewrite its text (inline **bold**/`code`/links) or toggle a `to_do` checkbox. Get the `block_id` from `notion_get_page` (which now prints each `(block_id: <hex>)`). For editing existing prose prefer `notion_update_content` above (no id, no prefix ambiguity); use this only to target a specific block id |
| `notion_delete_block(block_id, restore)` | Staged | Trash an existing block (and its children); `restore=True` brings it back. Use to remove a stale line/section — prefer `notion_update_block` to replace text rather than delete |
| `notion_trash_page(page_id, restore)` | Staged | Trash a page (remove it from the board/database); `restore=True` brings it back. Trashing also drops the row from query results and from any relations |

**Migration runner** (`MIGRATIONS_ENABLED=true` required — review-gated)

Lets hive **apply** an Alembic migration as the **DB owner** via the mounted docker socket — only when
the task explicitly asks. Hive otherwise just writes migration files (it does not run them). Doubly
gated: the agent calls it only on explicit instruction, and `WRITE_REVIEW` stages it for human confirm
before it runs. It runs `alembic <direction> <revision>` **online** inside the service container (so DDL
is permitted and `op.bulk_insert` seed rows are applied — unlike offline `--sql`).

| Tool | Approval | Description |
|---|---|---|
| `run_migration(service, revision="head", direction="upgrade")` | Staged | Apply a migration for a configured service as the DB owner. `service` is a key in `MIGRATION_SERVICES`. Revision validated; owner password read from the db container at run time (never logged). |

Config (env): `MIGRATIONS_ENABLED`, `MIGRATION_DB_CONTAINER`, `MIGRATION_DB_OWNER`, `MIGRATION_DB_NAME`,
`MIGRATION_DB_HOST` (default `postgres`), `MIGRATION_DB_PORT` (default `5432`),
`MIGRATION_SERVICES` (`name:container,name:container`). Requires the host docker socket (already mounted).
**Security:** runs as the DB superuser — the review gate is the safeguard; the owner password is passed
to alembic via stdin (never in process args) and redacted from output.

---

## Setup

### Prerequisites

- Docker installed on your machine
- Tailscale installed (ZGX reaches your machine via Tailscale IP)

### Quick start

```bash
# Copy the compose file into your project
cp /path/to/agno-hive/hive-mcp/docker-compose.hive.yml .

# Pull and start
docker compose -f docker-compose.hive.yml up -d

# Verify it's healthy
docker ps --filter "name=hive-mcp"
```

### Using docker run directly

```bash
docker run -d \
  --name hive-mcp \
  --restart unless-stopped \
  -p 9003:9000 \
  -v "$(pwd):/project" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e PROJECT_ROOT=/project \
  -e WRITE_REVIEW=true \
  ghcr.io/abehera1992/hive-mcp:latest
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PROJECT_ROOT` | `/project` | Path inside the container to the project (matches volume mount target) |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `9000` | Port inside the container |
| `WRITE_REVIEW` | `true` | Stage all file writes and platform writes for human review |
| `WEB_SEARCH_ENABLED` | `false` | Enable `web_search` and `web_fetch` tools |
| `NOTION_API_KEY` | _(unset)_ | Notion integration token — activates all `notion_*` tools |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | _(unset)_ | Path to Google service account JSON — activates Google tools when implemented |
| `HIVE_DB_URL` | _(unset)_ | Read-only DSN — activates `db_schema` / `db_query` grounding tools |
| `HIVE_DB_MAX_ROWS` | `1000` | Max rows returned by `db_query` |
| `HIVE_DB_TIMEOUT_MS` | `5000` | Per-query `statement_timeout` |
| `HIVE_BASH_TOOL_ENABLED` | `true` | Master gate for `bash_session_start`/`bash_run`/`bash_job_status`/`bash_job_kill`/`bash_session_close` |
| `HIVE_BASH_MAX_OUTPUT_CHARS` | `20000` | Output cap per `bash_run` call and per background job's whole buffer |
| `HIVE_BASH_DEFAULT_TIMEOUT_SECONDS` | `120` | Default `bash_run` timeout when not specified |
| `HIVE_BASH_MAX_TIMEOUT_SECONDS` | `600` | Hard ceiling any caller-supplied timeout is clamped to (also a background job's max runtime) |
| `HIVE_BASH_MAX_SESSIONS` | `10` | Concurrency cap on live sessions |
| `HIVE_BASH_MAX_BACKGROUND_JOBS` | `5` | Concurrency cap on live background jobs |
| `HIVE_BASH_SESSION_TTL_SECONDS` | `1800` | Idle timeout for sessions and finished jobs |
| `HIVE_BASH_REAP_INTERVAL_SECONDS` | `60` | How often the background reaper sweeps for expired sessions/jobs |

---

## docker-compose.hive.yml reference

```yaml
services:
  hive-mcp:
    image: ghcr.io/abehera1992/hive-mcp:latest
    container_name: hive-mcp
    restart: unless-stopped
    ports:
      - "${HIVE_MCP_PORT:-9003}:9000"
    volumes:
      - ${PROJECT_PATH:-.}:/project
      - /var/run/docker.sock:/var/run/docker.sock
      - ${USERPROFILE:-.}/.gitconfig:/root/.gitconfig:ro
      - ${USERPROFILE:-.}/.ssh:/root/.ssh:ro
    environment:
      - PROJECT_ROOT=/project
      - MCP_PORT=9000
      - WRITE_REVIEW=${WRITE_REVIEW:-true}
      - WEB_SEARCH_ENABLED=${WEB_SEARCH_ENABLED:-false}
      - NOTION_API_KEY=${NOTION_API_KEY:-}
      - GOOGLE_SERVICE_ACCOUNT_JSON=${GOOGLE_SERVICE_ACCOUNT_JSON:-}
      - HIVE_DB_URL=${HIVE_DB_URL:-}   # read-only DSN → db_schema / db_query
      - HIVE_BASH_TOOL_ENABLED=${HIVE_BASH_TOOL_ENABLED:-true}   # bash_session_start / bash_run / bash_job_status / bash_job_kill
```

Env vars you can set in `.env` (same directory as the compose file) or in your shell:
- `PROJECT_PATH` — path to your project (default: current directory)
- `HIVE_MCP_PORT` — host port to expose (default: 9003)
- `WRITE_REVIEW` — `true` or `false`
- `WEB_SEARCH_ENABLED` — `true` to enable web tools
- `NOTION_API_KEY` — Notion internal integration token
- `HIVE_BASH_TOOL_ENABLED` — `false` to disable persistent bash sessions/background jobs (default: `true`)

---

## WRITE_REVIEW mode

When `WRITE_REVIEW=true` (the default), all writes — both file edits and external platform writes — are staged for human review.

### File writes

1. `apply_diff()` and `write_file()` write proposed content to `path.hive_proposed` instead of applying directly
2. The tool returns `review_pending: path — user will confirm/reject via CLI`
3. The **hive CLI** detects new `.hive_proposed` files, opens a VS Code diff tab (or shows inline terminal diff), and presents an arrow-key selector
4. The user confirms or rejects — the CLI applies or discards the file directly on the local filesystem
5. Agents **cannot** confirm or reject — `confirm_write`/`reject_write` are not registered as tools

`run_command` is also guarded: commands that write files (`>`, `>>`, `sed -i`, `tee`, `perl -i`, `truncate`, `dd of=`) are blocked. Agents must use `apply_diff` or `write_file` for all file changes. `bash_run` (both blocking and `background=True`) is guarded by the same blocklist.

### External platform writes (action staging)

1. Write tools (`notion_create_page`, `notion_append_blocks`, etc.) write a staging file to `.hive_pending_actions/<action_id>.json`
2. The tool returns `action_pending: platform/tool — summary` with the `action_id`
3. The agent **stops** — it must not call any other tool
4. The **hive CLI** detects new `.json` files in `.hive_pending_actions/`, shows the action summary, and presents an arrow-key selector
5. **confirm** → `POST /actions/confirm` to hive-mcp → the platform API call executes
6. **reject** → `POST /actions/reject` → staging file deleted, action discarded

HTTP endpoints for CLI (bypasses agent pipeline):
```
POST /actions/confirm   {"action_id": "<id>"}  → execute and return result
POST /actions/reject    {"action_id": "<id>"}  → discard staging file
```

---

## External Platform Integrations

### Notion

1. Go to [notion.so/my-integrations](https://notion.so/my-integrations), create an integration, copy the token (`ntn_...`)
2. Share each Notion page/database with the integration (Share → Invite → select integration name)
3. Add to `hive-mcp/.env`:
   ```
   NOTION_API_KEY=ntn_your_token_here
   ```
4. Restart hive-mcp:
   ```bash
   docker rm -f hive-mcp
   docker compose -f docker-compose.hive.yml up -d hive-mcp
   ```

### Adding a new platform

1. Create `tools/integrations/<platform>.py`
2. Implement `_execute(tool: str, args: dict) -> str` and call `register_executor("<platform>", _execute)` at module level
3. Add read tool functions (pass-through) and write tool functions (call `_stage_action()` when `WRITE_REVIEW=true`)
4. Add env var guard in `main.py`:
   ```python
   if config.PLATFORM_API_KEY:
       from tools.integrations.platform import tool_a, tool_b
       _INTEGRATION_TOOLS += [tool_a, tool_b]
   ```
5. Add config var to `config.py` and env var passthrough to `docker-compose.hive.yml`

---

## Semantic Bootstrap (`index_project`)

`index_project` walks the project directory and inserts chunked file content into LightRAG so agents can do semantic search.

```
index_project(
    project_id="EkamApp",
    lightrag_url="http://<zgx-tailscale-ip>:9002/mcp",
    glob_filter="**/*",   # optional
    force=False,          # True = reindex all files, even unchanged
)
```

- Python files: parsed with `ast` — module docstrings, functions, and classes become individual chunks
- All other files: split into 4000-char text windows
- SHA-256 checksums track which files changed since the last run (incremental)
- State stored in `/tmp/hive-index/{project_id}.json`

Trigger from the hive CLI:

```bash
hive --bootstrap --lightrag-url http://<zgx-tailscale-ip>:9002/mcp
hive --bootstrap --force   # full reindex
```

---

## Transport

hive-mcp uses **Streamable HTTP** (`/mcp` endpoint). ZGX connects to:

```
http://<your-tailscale-ip>:<HIVE_MCP_PORT>/mcp
```

The hive CLI auto-detects your Tailscale IP and constructs the URL — no manual configuration needed if Tailscale is installed.

---

## Updating the image

Edit files in `hive-mcp/`, commit, and push to `agno-hive` main. The CI workflow (`.github/workflows/hive-mcp.yml`) builds and pushes `ghcr.io/abehera1992/hive-mcp:latest` automatically.

Then pull and recreate the container:

```bash
docker compose -f docker-compose.hive.yml pull hive-mcp
docker rm -f hive-mcp
docker compose -f docker-compose.hive.yml up -d hive-mcp
```

> `docker restart` does **not** pick up a new image. `--force-recreate` fails with a name conflict. Use `docker rm -f` + `up -d`.

## Building the image locally

```bash
cd hive-mcp
docker build -t hive-mcp:local .
```
