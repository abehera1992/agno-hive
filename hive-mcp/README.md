# hive-mcp

A generic, platform-agnostic Docker MCP server that gives AGNOHive control over your local machine — file system, shell, Docker, and git — over Streamable HTTP via Tailscale.

## Why it exists

Project-specific MCP servers expose tools for reading and working within an app (routes, schemas, patterns). They deliberately don't expose raw filesystem or shell access. `hive-mcp` fills that gap: it gives agents surgical file editing, shell commands, Docker inspection, git operations, and project bootstrapping — regardless of what project you're working on.

## Architecture

```
ZGX (AGNOHive)
  │
  ├── project MCP  →  read context, memory, app-specific workflows
  └── hive-mcp     →  apply_diff, write_file, run_shell, run_docker, git_*
                        index_project (bootstrap into LightRAG)
```

Agents choose which MCP to use based on operation type. The coordinator instructions make the split explicit.

## Tools

### File reading (read-only)
| Tool | Description |
|---|---|
| `get_project_context()` | Full project overview — directory tree + key files |
| `get_file_content(path)` | Read a file |
| `find_files(pattern)` | Glob search |
| `search_files(pattern, glob)` | Regex content search |
| `list_directory(path)` | List a directory |

### File writing (WRITE_REVIEW-aware)
| Tool | Description |
|---|---|
| `apply_diff(path, old_string, new_string)` | Surgical replacement — use for ALL edits to existing files |
| `write_file(path, content)` | Create a new file |
| `run_command(cmd)` | Read-only shell (tests, linters) — blocked for writes when `WRITE_REVIEW=true` |

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
| `index_project(project_id, lightrag_url, glob_filter, force)` | Walk project, chunk files, insert into LightRAG for semantic search |

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
  -p 9000:9000 \
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
| `WRITE_REVIEW` | `true` | Stage all file writes as `.hive_proposed` for human review |

---

## docker-compose.hive.yml reference

```yaml
services:
  hive-mcp:
    image: ghcr.io/abehera1992/hive-mcp:latest
    container_name: hive-mcp
    restart: unless-stopped
    ports:
      - "${HIVE_MCP_PORT:-9000}:9000"
    volumes:
      - ${PROJECT_PATH:-.}:/project            # your project files
      - /var/run/docker.sock:/var/run/docker.sock  # host Docker access
      - ${HOME}/.gitconfig:/root/.gitconfig:ro
      - ${HOME}/.ssh:/root/.ssh:ro
    environment:
      - PROJECT_ROOT=/project
      - MCP_PORT=9000
      - WRITE_REVIEW=${WRITE_REVIEW:-true}
```

Env vars you can set before running:
- `PROJECT_PATH` — path to your project (default: current directory)
- `HIVE_MCP_PORT` — host port to expose (default: 9000)
- `WRITE_REVIEW` — `true` or `false`

---

## WRITE_REVIEW mode

When `WRITE_REVIEW=true` (the default):

1. `apply_diff()` and `write_file()` write proposed content to `path.hive_proposed` instead of applying directly
2. The tool returns `review_pending: path — user will confirm/reject via CLI`
3. The **hive CLI** detects new `.hive_proposed` files, shows a diff, and presents an arrow-key selector
4. The user confirms or rejects — the CLI applies or discards the file directly on the local filesystem
5. Agents **cannot** confirm or reject — `confirm_write`/`reject_write` are not registered as tools

`run_command` is also guarded: commands that write files (`>`, `>>`, `sed -i`, `tee`, `perl -i`, `truncate`, `dd of=`) are blocked. Agents must use `apply_diff` or `write_file` for all file changes.

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

## Building the image locally

```bash
cd hive-mcp
docker build -t hive-mcp:local .
```

The GHCR image is rebuilt automatically on every push to `main` that changes `hive-mcp/**` via GitHub Actions.

```
ghcr.io/abehera1992/hive-mcp:latest
```
