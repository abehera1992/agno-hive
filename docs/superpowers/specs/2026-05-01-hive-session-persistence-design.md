# Hive CLI — Persistent Chat Sessions Design

**Date:** 2026-05-01  
**Status:** Approved  
**Scope:** AGNOHive (`agno-hive`) — server + CLI

---

## Problem

Each `hive` invocation is a stateless `POST /run` — the coordinator has no memory of previous prompts in the same work thread. Follow-up messages like "go ahead" or "can you also add type hints?" produce confused, context-free responses because the agents never saw the prior exchange.

---

## Goals

- Every REPL session has a persistent, resumable chat history stored server-side
- Sessions expire automatically after 30 days unless marked as persistent
- Persistent sessions are deleted only by explicit user action (passing session ID)
- Resuming a session injects the last 10 messages into the coordinator as conversation context
- One-shot invocations (`hive "task"`) always start a fresh session
- REPL auto-resumes the last session for the current project

---

## Non-Goals

- Streaming history (server-sent events, websockets) — out of scope
- Session sharing between users — single-user tool
- History summarisation — sliding window of last 10 turns is sufficient
- Client-side history caching — server is the single source of truth

---

## Architecture

### Storage: PostgreSQL on ZGX

Two new tables in the existing AGNOHive PostgreSQL database. Auto-created on first server start via `_ensure_tables()` in `swarm/sessions.py`, matching the pattern established by `swarm/feedback.py`.

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  TEXT        NOT NULL,
    title       TEXT        NOT NULL,        -- first 80 chars of first user message
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ,                 -- NULL when persist = true
    persist     BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS chat_sessions_project_idx
    ON chat_sessions (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS session_messages (
    id          SERIAL PRIMARY KEY,
    session_id  UUID        NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role        TEXT        NOT NULL,        -- "user" | "assistant"
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS session_messages_session_idx
    ON session_messages (session_id, created_at ASC);
```

`ON DELETE CASCADE` ensures messages are removed when a session is deleted — no orphaned rows.

### TTL Enforcement

An `asyncio` background task launched at server startup runs every hour:

```sql
DELETE FROM chat_sessions WHERE expires_at < NOW() AND persist = FALSE;
```

Cascades automatically to `session_messages`. No new dependencies — uses the existing `asyncio` event loop.

Default TTL: **30 days** from session creation. Configurable via `SESSION_TTL_DAYS` env var.

---

## Server-Side Changes

### New module: `swarm/sessions.py`

Public interface:

| Function | Description |
|---|---|
| `create_session(project_id, title, persist) -> str` | Create session, return UUID string |
| `append_message(session_id, role, content)` | Add one turn to session |
| `get_history(session_id, limit=10) -> list[dict]` | Return last N turns as `[{role, content}]` |
| `list_sessions(project_id, limit=20) -> list[dict]` | Return session summaries |
| `delete_session(session_id)` | Hard delete (works on persisted sessions) |
| `persist_session(session_id)` | Set persist=true, expires_at=NULL |
| `get_session(session_id) -> dict \| None` | Return session metadata |
| `_ensure_tables(conn)` | Create tables + indexes if absent |
| `_cleanup_expired()` | Delete expired sessions (called by background task) |

### Updated `POST /run`

`RunRequest` gains two new optional fields:

```python
session_id: str | None = None   # resume existing session
persist:    bool       = False  # mark new session as permanent
```

`RunResponse` gains one new field:

```python
session_id: str   # always returned — new or existing
```

**Request flow:**

1. If `session_id` provided → load last 10 turns via `get_history()`
2. If no `session_id` → call `create_session()` to get a new UUID
3. Inject history into coordinator instructions (see Context Injection below)
4. Run the task
5. `append_message(session_id, "user", task)` + `append_message(session_id, "assistant", result)`
6. Return `session_id` in response

### New REST Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/sessions?project_id=X&limit=20` | List sessions for a project |
| `GET` | `/sessions/{id}` | Full session with all messages |
| `DELETE` | `/sessions/{id}` | Hard delete (including persisted) |
| `PATCH` | `/sessions/{id}/persist` | Mark session as permanent |

### Context Injection Format

Injected into the coordinator's `instructions` list after the existing project context and failure context blocks:

```
── Conversation history (last 10 turns) ─────────────────────
[user] add a docstring to write_file in mcp-server/tools/write.py
[assistant] The docstring has been updated. Here's what changed: ...

[user] now add type hints to the same function
[assistant] Type hints added. The signature is now: ...
─────────────────────────────────────────────────────────────
```

History window: last 10 **messages** total (ordered oldest-first for injection), configurable via `AGNO_SESSION_WINDOW` env var (default: `10`). A value of 10 means 5 user + 5 assistant messages in a balanced session.

---

## CLI Changes

### New Flags

```bash
hive --session <id>      # resume a specific session by UUID
hive --persist           # mark this session as permanent on creation
hive --list-sessions     # print recent sessions for this project and exit
```

### Local State File

`~/.agno_last_session` — small JSON file written by the REPL after each session:

```json
{"session_id": "a3f7c2d1-...", "project_id": "EkamApp"}
```

Only updated by REPL mode. One-shot mode never touches this file.

### REPL Behaviour

**On start:**
- Load `~/.agno_last_session`
- If exists and `project_id` matches current project → auto-resume that session
- Show banner:
  ```
  AGNOHive  project EkamApp  mode engineering  http://...
    resuming session a3f7c2d1  (started 2h ago · 4 turns)
    /new to start fresh · /exit to quit and save session id
  ```
- If no saved session → create a new one silently, show:
  ```
  AGNOHive  project EkamApp  mode engineering  http://...
    new session a3f7c2d1  (expires in 30 days)
    /persist to keep forever · /exit to quit
  ```

**REPL slash commands:**

| Command | Action |
|---|---|
| `/new` | Create a fresh session, update `~/.agno_last_session` |
| `/sessions` | List recent sessions for this project |
| `/history` | Print turns in the current session |
| `/persist` | Mark current session as permanent |
| `/delete <id>` | Delete a session by ID |
| `/exit` | Print session ID prominently, save to `~/.agno_last_session`, quit |

**On `/exit`:**
```
  session saved: a3f7c2d1-8b3e-4f2a-9c1d-...
  resume later: hive --session a3f7c2d1-8b3e-4f2a-9c1d-...
```

### One-Shot Mode

`hive "task"` always creates a new ephemeral session. Does NOT update `~/.agno_last_session`. Prints session ID at the bottom in dim text so the user can resume manually if they want to follow up.

```
  ── Coder, Reviewer · 42.3s · session: a3f7c2d1
```

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `SESSION_TTL_DAYS` | `30` | Days before an unpersisted session expires |
| `AGNO_SESSION_WINDOW` | `10` | Max turns of history injected per request |
| `SESSION_CLEANUP_INTERVAL` | `3600` | Seconds between TTL cleanup sweeps |

All added to `config/config.py` and `.env.example`.

---

## Error Handling

- **Invalid session ID on resume:** Server returns 404. CLI warns the user, creates a new session, and continues.
- **PostgreSQL unreachable:** Session operations fail silently (same pattern as `feedback.py`). The task still runs — session just won't be persisted.
- **History injection failure:** Falls back to running without history — never blocks the task.
- **Expired session resumed:** Server returns 404 (expired sessions are deleted). CLI treats same as invalid ID.

---

## Files Changed

| File | Change |
|---|---|
| `swarm/sessions.py` | New — all session CRUD + TTL cleanup |
| `swarm/team.py` | Inject history into coordinator instructions |
| `api/models.py` | Add `session_id`, `persist` to `RunRequest`; `session_id` to `RunResponse`; new session models |
| `api/server.py` | New session endpoints + startup cleanup task |
| `config/config.py` | Add `session_ttl_days`, `session_window`, `session_cleanup_interval` |
| `.env.example` | Document new env vars |
| `cli/hive` | Session flags, REPL slash commands, local state file, banner |

---

## Out of Scope (Future)

- `hive --delete-session <id>` as a standalone one-shot flag (covered by `/delete` in REPL for now)
- Session export to JSON
- Cross-project session search
- Session tagging / labelling
