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
                        index_project (bootstrap into LightRAG)
                        web_search, web_fetch
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

| Tool | Approval | Description |
|---|---|---|
| `notion_search(query)` | None | Search pages and databases by title/content |
| `notion_get_page(page_id)` | None | Read a page's properties and top-level blocks |
| `notion_get_database_schema(database_id)` | None | List a database's property names + types (and select/status option names) — use to learn valid field/option names before writing |
| `notion_create_page(parent_id, title, properties, content, parent_type)` | Staged | Create a database row (or child page). `properties` = dict of simple field values; `title` goes to the database's title property automatically |
| `notion_update_page_props(page_id, properties)` | Staged | Update fields by simple value, e.g. `{"Status": "Done"}`. status / select / relation / date are coerced from the schema; pass `null` to clear a field |
| `notion_append_blocks(block_id, blocks, after_block_id)` | Staged | Append blocks. `blocks` may be a list of plain strings (each becomes a paragraph) or full block dicts; `after_block_id` controls insertion position |

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
```

Env vars you can set in `.env` (same directory as the compose file) or in your shell:
- `PROJECT_PATH` — path to your project (default: current directory)
- `HIVE_MCP_PORT` — host port to expose (default: 9003)
- `WRITE_REVIEW` — `true` or `false`
- `WEB_SEARCH_ENABLED` — `true` to enable web tools
- `NOTION_API_KEY` — Notion internal integration token

---

## WRITE_REVIEW mode

When `WRITE_REVIEW=true` (the default), all writes — both file edits and external platform writes — are staged for human review.

### File writes

1. `apply_diff()` and `write_file()` write proposed content to `path.hive_proposed` instead of applying directly
2. The tool returns `review_pending: path — user will confirm/reject via CLI`
3. The **hive CLI** detects new `.hive_proposed` files, opens a VS Code diff tab (or shows inline terminal diff), and presents an arrow-key selector
4. The user confirms or rejects — the CLI applies or discards the file directly on the local filesystem
5. Agents **cannot** confirm or reject — `confirm_write`/`reject_write` are not registered as tools

`run_command` is also guarded: commands that write files (`>`, `>>`, `sed -i`, `tee`, `perl -i`, `truncate`, `dd of=`) are blocked. Agents must use `apply_diff` or `write_file` for all file changes.

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
