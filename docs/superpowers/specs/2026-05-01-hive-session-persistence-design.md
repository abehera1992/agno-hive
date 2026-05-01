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
- Full history summarisation on every turn — compaction only triggers at a message threshold
- Client-side history caching — server is the single source of truth

---

## Architecture

### Storage: PostgreSQL on ZGX

Two new tables in the existing AGNOHive PostgreSQL database. Auto-created on first server start via `_ensure_tables()` in `swarm/sessions.py`, matching the pattern established by `swarm/feedback.py`.

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      TEXT        NOT NULL,
    title           TEXT        NOT NULL,        -- first 80 chars of first user message
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,                 -- NULL when persist = true
    persist         BOOLEAN     NOT NULL DEFAULT FALSE,
    summary         TEXT,                        -- compacted summary of older messages
    summary_through INT         NOT NULL DEFAULT 0  -- message id up to which summary covers
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
| `get_history(session_id, limit=10) -> list[dict]` | Return last N verbatim messages as `[{role, content}]` |
| `get_context(session_id) -> tuple[str, list[dict]]` | Return `(summary_or_empty, recent_messages)` for injection |
| `compact_session(session_id)` | Summarise older messages via `llama3.1:8b`, store in `summary` column |
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

1. If `session_id` provided → load context via `get_context()` (summary + last 6 verbatim messages)
2. If no `session_id` → call `create_session()` to get a new UUID
3. Inject context into coordinator instructions (see Context Injection below)
4. Run the task
5. `append_message(session_id, "user", task)` + `append_message(session_id, "assistant", result)`
6. If total message count crosses compaction threshold → trigger `compact_session()` asynchronously (non-blocking)
7. Return `session_id` in response

### New REST Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/sessions?project_id=X&limit=20` | List sessions for a project |
| `GET` | `/sessions/{id}` | Full session with all messages |
| `DELETE` | `/sessions/{id}` | Hard delete (including persisted) |
| `PATCH` | `/sessions/{id}/persist` | Mark session as permanent |

### Context Injection Format

Injected into the coordinator's `instructions` list after the existing project context and failure context blocks. Two-layer format — compacted summary (if exists) followed by verbatim recent messages:

```
── Session summary (turns 1–20) ──────────────────────────────
The user is building session persistence for the hive CLI.
Key decisions: PostgreSQL server-side storage, 30-day TTL,
persist flag for permanent sessions, sliding window context.
──────────────────────────────────────────────────────────────
── Recent messages (last 6) ──────────────────────────────────
[user] add a docstring to write_file in mcp-server/tools/write.py
[assistant] The docstring has been updated. Here's what changed: ...

[user] now add type hints to the same function
[assistant] Type hints added. The signature is now: ...
──────────────────────────────────────────────────────────────
```

If no compaction has occurred yet (session is short), only the recent messages block is injected — no summary header.

**Verbatim window:** last 6 messages (configurable via `AGNO_SESSION_WINDOW`, default `6`).

### Compaction

Compaction triggers when total message count exceeds `AGNO_COMPACT_THRESHOLD` (default: `20`) and the existing summary does not yet cover all messages before the verbatim window.

**Compaction flow:**
1. Load all messages older than the verbatim window
2. Send to `llama3.1:8b` (already in the engineering team roster — no new model pull) with a system prompt: `"Summarise this conversation history concisely, preserving key decisions, file names, and outcomes. Be factual and brief."`
3. Store the summary in `chat_sessions.summary` and update `summary_through` to the last message ID covered
4. Future runs inject this summary + the fresh verbatim window

Compaction runs as a fire-and-forget `asyncio.create_task` after the response is returned — it never blocks the current turn. If it fails, the next turn retries.

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

### Result Footer

Every response (both REPL and one-shot) prints an informative footer line:

**REPL (ongoing session):**
```
── Coder, Reviewer · 42.3s  ·  session a3f7c2d1  ·  turn 4  ·  6 msgs in context  ·  expires 2026-05-31
```

**REPL (persisted session):**
```
── Coder, Reviewer · 42.3s  ·  session a3f7c2d1  ·  turn 4  ·  summary + 6 msgs  ·  [persistent]
```

**One-shot:**
```
── Coder, Reviewer · 42.3s  ·  session a3f7c2d1  ·  expires 2026-05-31  ·  resume: hive --session a3f7c2d1
```

Fields shown: agents used · duration · session ID (short 8-char prefix) · turn number (REPL only) · context window used · expiry or persistent badge. All rendered in dim colour.

### One-Shot Mode

`hive "task"` always creates a new ephemeral session. Does NOT update `~/.agno_last_session`. The footer includes the session ID and a ready-to-paste resume command so the user can continue in REPL mode if they want to follow up.

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `SESSION_TTL_DAYS` | `30` | Days before an unpersisted session expires |
| `AGNO_SESSION_WINDOW` | `6` | Verbatim messages injected per request |
| `AGNO_COMPACT_THRESHOLD` | `20` | Total messages before compaction triggers |
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
| `swarm/sessions.py` | New — session CRUD, compaction, TTL cleanup |
| `swarm/team.py` | Inject `get_context()` result into coordinator instructions |
| `api/models.py` | Add `session_id`, `persist` to `RunRequest`; `session_id` to `RunResponse`; new session models |
| `api/server.py` | New session endpoints + startup cleanup task |
| `config/config.py` | Add `session_ttl_days`, `session_window`, `compact_threshold`, `session_cleanup_interval` |
| `.env.example` | Document new env vars |
| `cli/hive` | Session flags, REPL slash commands, local state file, informative footer, banner |

---

## Out of Scope (Future)

- `hive --delete-session <id>` as a standalone one-shot flag (covered by `/delete` in REPL for now)
- Session export to JSON
- Cross-project session search
- Session tagging / labelling
- Manual compaction trigger (`/compact` REPL command)
- Compaction model override (currently hardcoded to `llama3.1:8b`)
