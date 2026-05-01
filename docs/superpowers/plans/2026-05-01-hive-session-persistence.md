# Hive CLI — Persistent Chat Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-side persistent chat sessions to the hive CLI so follow-up prompts have full conversation context.

**Architecture:** Sessions are stored in PostgreSQL on ZGX (two new tables: `chat_sessions`, `session_messages`). Every `POST /run` accepts a `session_id`, loads the last 6 verbatim messages plus a compacted summary of older turns, injects them into the coordinator's instructions, and appends the new turn after the run. The CLI auto-resumes the last session in REPL mode; one-shot mode always creates a fresh session.

**Tech Stack:** Python 3.12, psycopg (async), FastAPI, httpx (for compaction Ollama call), pytest + pytest-asyncio, stdlib urllib (CLI — zero new dependencies).

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `config/config.py` | Modify | Add 4 new session config fields |
| `.env.example` | Modify | Document new env vars |
| `swarm/sessions.py` | **Create** | All session CRUD, compaction, TTL cleanup |
| `tests/test_sessions.py` | **Create** | Unit tests for sessions module |
| `api/models.py` | Modify | `SessionMeta`, updated `RunRequest`/`RunResponse`, session endpoint models |
| `swarm/team.py` | Modify | Accept `session_id`, inject context from `get_context()` |
| `api/server.py` | Modify | Update `/run`, add 4 session endpoints, startup cleanup task |
| `cli/hive` | Modify | Session flags, HTTP helpers, footer, REPL slash commands, auto-resume |

---

## Task 1: Config vars

**Files:**
- Modify: `config/config.py`
- Modify: `.env.example`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add 4 new fields to `Config` dataclass in `config/config.py`**

  Add after the `max_iterations` line:

  ```python
      # Session persistence
      session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "30"))
      session_window: int = int(os.getenv("AGNO_SESSION_WINDOW", "6"))
      compact_threshold: int = int(os.getenv("AGNO_COMPACT_THRESHOLD", "20"))
      session_cleanup_interval: int = int(os.getenv("SESSION_CLEANUP_INTERVAL", "3600"))
  ```

- [ ] **Step 2: Document new vars in `.env.example`**

  Add at the end of the file:

  ```
  # Session persistence
  SESSION_TTL_DAYS=30              # days before unpersisted sessions are deleted
  AGNO_SESSION_WINDOW=6            # verbatim messages injected per request
  AGNO_COMPACT_THRESHOLD=20        # total messages before compaction triggers
  SESSION_CLEANUP_INTERVAL=3600    # seconds between TTL cleanup sweeps
  ```

- [ ] **Step 3: Add session env vars to `clear_env` fixture in `tests/conftest.py`**

  Replace the `for var in (...)` list with:

  ```python
      for var in ("OLLAMA_HOST", "MCP_URL", "DB_URL", "PATTERNS_GLOB",
                  "LEADER_MODEL", "CODER_MODEL", "REVIEWER_MODEL",
                  "MEMORY_NAMESPACE", "STREAM", "MAX_ITERATIONS",
                  "SESSION_TTL_DAYS", "AGNO_SESSION_WINDOW",
                  "AGNO_COMPACT_THRESHOLD", "SESSION_CLEANUP_INTERVAL"):
          monkeypatch.delenv(var, raising=False)
  ```

- [ ] **Step 4: Write config tests**

  Add to `tests/test_config.py`:

  ```python
  def test_session_defaults():
      mod = _reload_config()
      cfg = mod.Config()
      assert cfg.session_ttl_days == 30
      assert cfg.session_window == 6
      assert cfg.compact_threshold == 20
      assert cfg.session_cleanup_interval == 3600


  def test_session_window_from_env(monkeypatch):
      monkeypatch.setenv("AGNO_SESSION_WINDOW", "12")
      mod = _reload_config()
      cfg = mod.Config()
      assert cfg.session_window == 12
  ```

- [ ] **Step 5: Run tests**

  ```
  pytest tests/test_config.py -v
  ```

  Expected: all tests pass (4 existing + 2 new).

- [ ] **Step 6: Commit**

  ```bash
  git add config/config.py .env.example tests/conftest.py tests/test_config.py
  git commit -m "feat: add session persistence config vars"
  ```

---

## Task 2: `swarm/sessions.py` — core CRUD

**Files:**
- Create: `swarm/sessions.py`

- [ ] **Step 1: Create `swarm/sessions.py` with full content**

  ```python
  """Session persistence — chat history stored in PostgreSQL on ZGX.

  Tables are auto-created on first use (same pattern as feedback.py).
  All functions fail silently so a PostgreSQL outage never blocks task runs.
  """
  import uuid
  from datetime import datetime, timedelta, timezone

  import psycopg

  from config.config import config


  # ── Schema ────────────────────────────────────────────────────────────────────

  async def _ensure_tables(conn) -> None:
      await conn.execute("""
          CREATE TABLE IF NOT EXISTS chat_sessions (
              id              UUID PRIMARY KEY,
              project_id      TEXT        NOT NULL,
              title           TEXT        NOT NULL,
              created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              expires_at      TIMESTAMPTZ,
              persist         BOOLEAN     NOT NULL DEFAULT FALSE,
              summary         TEXT,
              summary_through INT         NOT NULL DEFAULT 0
          )
      """)
      await conn.execute("""
          CREATE INDEX IF NOT EXISTS chat_sessions_project_idx
              ON chat_sessions (project_id, created_at DESC)
      """)
      await conn.execute("""
          CREATE TABLE IF NOT EXISTS session_messages (
              id          SERIAL PRIMARY KEY,
              session_id  UUID        NOT NULL
                              REFERENCES chat_sessions(id) ON DELETE CASCADE,
              role        TEXT        NOT NULL,
              content     TEXT        NOT NULL,
              created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
          )
      """)
      await conn.execute("""
          CREATE INDEX IF NOT EXISTS session_messages_session_idx
              ON session_messages (session_id, created_at ASC)
      """)
      await conn.commit()


  # ── Core CRUD ─────────────────────────────────────────────────────────────────

  async def create_session(
      project_id: str, title: str, persist: bool = False
  ) -> str:
      """Create a new session and return its UUID string."""
      session_id = str(uuid.uuid4())
      expires_at = (
          None
          if persist
          else datetime.now(timezone.utc) + timedelta(days=config.session_ttl_days)
      )
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              await _ensure_tables(conn)
              await conn.execute(
                  """
                  INSERT INTO chat_sessions (id, project_id, title, expires_at, persist)
                  VALUES (%s, %s, %s, %s, %s)
                  """,
                  (session_id, project_id, title[:80], expires_at, persist),
              )
              await conn.commit()
      except Exception as exc:
          print(f"[sessions] create_session warning: {exc}")
      return session_id


  async def append_message(session_id: str, role: str, content: str) -> None:
      """Append a message and bump updated_at on the session."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              await conn.execute(
                  "INSERT INTO session_messages (session_id, role, content)"
                  " VALUES (%s, %s, %s)",
                  (session_id, role, content),
              )
              await conn.execute(
                  "UPDATE chat_sessions SET updated_at = NOW() WHERE id = %s",
                  (session_id,),
              )
              await conn.commit()
      except Exception as exc:
          print(f"[sessions] append_message warning: {exc}")


  async def get_history(session_id: str, limit: int | None = None) -> list[dict]:
      """Return last `limit` messages ordered oldest-first."""
      if limit is None:
          limit = config.session_window
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              rows = await conn.execute(
                  """
                  SELECT role, content FROM (
                      SELECT role, content, created_at
                      FROM session_messages
                      WHERE session_id = %s
                      ORDER BY created_at DESC
                      LIMIT %s
                  ) sub ORDER BY created_at ASC
                  """,
                  (session_id, limit),
              )
              return [{"role": r[0], "content": r[1]} for r in await rows.fetchall()]
      except Exception as exc:
          print(f"[sessions] get_history warning: {exc}")
          return []


  async def get_session(session_id: str) -> dict | None:
      """Return session metadata + message count, or None if not found."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              await _ensure_tables(conn)
              rows = await conn.execute(
                  """
                  SELECT s.id, s.project_id, s.title, s.created_at, s.updated_at,
                         s.expires_at, s.persist, s.summary, s.summary_through,
                         COUNT(m.id) AS message_count
                  FROM chat_sessions s
                  LEFT JOIN session_messages m ON m.session_id = s.id
                  WHERE s.id = %s
                  GROUP BY s.id
                  """,
                  (session_id,),
              )
              row = await rows.fetchone()
              if not row:
                  return None
              return {
                  "id": str(row[0]),
                  "project_id": row[1],
                  "title": row[2],
                  "created_at": row[3],
                  "updated_at": row[4],
                  "expires_at": row[5],
                  "persist": row[6],
                  "summary": row[7],
                  "summary_through": row[8],
                  "message_count": row[9],
              }
      except Exception as exc:
          print(f"[sessions] get_session warning: {exc}")
          return None


  async def list_sessions(project_id: str, limit: int = 20) -> list[dict]:
      """Return session summaries for a project, most-recently-updated first."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              await _ensure_tables(conn)
              rows = await conn.execute(
                  """
                  SELECT s.id, s.title, s.created_at, s.updated_at,
                         s.expires_at, s.persist, COUNT(m.id) AS message_count
                  FROM chat_sessions s
                  LEFT JOIN session_messages m ON m.session_id = s.id
                  WHERE s.project_id = %s
                  GROUP BY s.id
                  ORDER BY s.updated_at DESC
                  LIMIT %s
                  """,
                  (project_id, limit),
              )
              return [
                  {
                      "id": str(r[0]),
                      "title": r[1],
                      "created_at": r[2],
                      "updated_at": r[3],
                      "expires_at": r[4],
                      "persist": r[5],
                      "message_count": r[6],
                  }
                  for r in await rows.fetchall()
              ]
      except Exception as exc:
          print(f"[sessions] list_sessions warning: {exc}")
          return []


  async def delete_session(session_id: str) -> bool:
      """Hard-delete a session and its messages. Returns True if a row was deleted."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              result = await conn.execute(
                  "DELETE FROM chat_sessions WHERE id = %s", (session_id,)
              )
              await conn.commit()
              return result.rowcount > 0
      except Exception as exc:
          print(f"[sessions] delete_session warning: {exc}")
          return False


  async def persist_session(session_id: str) -> bool:
      """Mark a session as permanent (clears expires_at). Returns True if updated."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              result = await conn.execute(
                  """
                  UPDATE chat_sessions
                  SET persist = TRUE, expires_at = NULL, updated_at = NOW()
                  WHERE id = %s
                  """,
                  (session_id,),
              )
              await conn.commit()
              return result.rowcount > 0
      except Exception as exc:
          print(f"[sessions] persist_session warning: {exc}")
          return False


  async def _cleanup_expired() -> int:
      """Delete expired non-persisted sessions. Returns count deleted."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              await _ensure_tables(conn)
              result = await conn.execute(
                  "DELETE FROM chat_sessions WHERE expires_at < NOW() AND persist = FALSE"
              )
              await conn.commit()
              return result.rowcount
      except Exception as exc:
          print(f"[sessions] cleanup warning: {exc}")
          return 0


  # ── Context for coordinator ───────────────────────────────────────────────────

  async def get_context(session_id: str) -> tuple[str, list[dict]]:
      """Return (summary_or_empty, recent_messages) for coordinator injection."""
      session = await get_session(session_id)
      if not session:
          return "", []
      messages = await get_history(session_id, limit=config.session_window)
      summary = session.get("summary") or ""
      return summary, messages


  # ── Compaction ────────────────────────────────────────────────────────────────

  async def compact_session(session_id: str) -> None:
      """Summarise older messages via llama3.1:8b and store in summary column.

      Triggered fire-and-forget from server.py when message count crosses
      config.compact_threshold. Failures are silent — next run retries.
      """
      try:
          session = await get_session(session_id)
          if not session:
              return

          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              rows = await conn.execute(
                  """
                  SELECT id, role, content FROM session_messages
                  WHERE session_id = %s ORDER BY created_at ASC
                  """,
                  (session_id,),
              )
              all_messages = [(r[0], r[1], r[2]) for r in await rows.fetchall()]

          window = config.session_window
          if len(all_messages) <= window:
              return

          to_compact = all_messages[:-window]
          last_id_covered = to_compact[-1][0]

          if session.get("summary_through", 0) >= last_id_covered:
              return  # Already compacted up to here

          history_text = "\n".join(
              f"[{role}] {content[:500]}" for _, role, content in to_compact
          )
          prompt = (
              "Summarise this conversation history concisely, preserving key "
              "decisions, file names, and outcomes. Be factual and brief.\n\n"
              + history_text
          )

          import httpx
          async with httpx.AsyncClient() as client:
              resp = await client.post(
                  f"{config.ollama_host}/api/generate",
                  json={"model": "llama3.1:8b", "prompt": prompt, "stream": False},
                  timeout=120,
              )
              resp.raise_for_status()
              summary = resp.json().get("response", "").strip()

          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              await conn.execute(
                  """
                  UPDATE chat_sessions
                  SET summary = %s, summary_through = %s, updated_at = NOW()
                  WHERE id = %s
                  """,
                  (summary, last_id_covered, session_id),
              )
              await conn.commit()
      except Exception as exc:
          print(f"[sessions] compact_session warning: {exc}")
  ```

- [ ] **Step 2: Commit the new file**

  ```bash
  git add swarm/sessions.py
  git commit -m "feat: add swarm/sessions.py — session CRUD, compaction, TTL cleanup"
  ```

---

## Task 3: Tests for `swarm/sessions.py`

**Files:**
- Create: `tests/test_sessions.py`

- [ ] **Step 1: Create `tests/test_sessions.py`**

  ```python
  """Unit tests for swarm/sessions.py — all psycopg I/O is mocked."""
  import pytest
  from unittest.mock import AsyncMock, patch, MagicMock


  # ── Mock helpers ──────────────────────────────────────────────────────────────

  def _make_cursor(rows=None, rowcount=0):
      cursor = AsyncMock()
      cursor.fetchall = AsyncMock(return_value=rows or [])
      cursor.fetchone = AsyncMock(return_value=None)
      cursor.rowcount = rowcount
      return cursor


  def _make_conn(cursor=None):
      if cursor is None:
          cursor = _make_cursor()
      conn = AsyncMock()
      conn.execute = AsyncMock(return_value=cursor)
      conn.commit = AsyncMock()
      conn.__aenter__ = AsyncMock(return_value=conn)
      conn.__aexit__ = AsyncMock(return_value=False)
      return conn


  def _patch_connect(conn):
      """Return context manager patching psycopg.AsyncConnection.connect."""
      async def _connect(*args, **kwargs):
          return conn
      return patch("swarm.sessions.psycopg.AsyncConnection.connect", side_effect=_connect)


  # ── create_session ────────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_create_session_returns_uuid():
      from swarm.sessions import create_session
      conn = _make_conn()
      with _patch_connect(conn):
          sid = await create_session("myproject", "test task", persist=False)
      assert len(sid) == 36
      assert sid.count("-") == 4


  @pytest.mark.asyncio
  async def test_create_session_persist_sets_no_expiry():
      from swarm.sessions import create_session
      conn = _make_conn()
      with _patch_connect(conn):
          await create_session("myproject", "persist task", persist=True)
      # Find the INSERT call and check expires_at is None
      calls = [str(c) for c in conn.execute.call_args_list]
      insert_call = next(c for c in conn.execute.call_args_list
                         if "INSERT INTO chat_sessions" in str(c))
      args = insert_call.args[1]  # (session_id, project_id, title, expires_at, persist)
      assert args[3] is None      # expires_at
      assert args[4] is True      # persist


  @pytest.mark.asyncio
  async def test_create_session_title_truncated():
      from swarm.sessions import create_session
      conn = _make_conn()
      long_title = "x" * 200
      with _patch_connect(conn):
          await create_session("myproject", long_title)
      insert_call = next(c for c in conn.execute.call_args_list
                         if "INSERT INTO chat_sessions" in str(c))
      title_stored = insert_call.args[1][2]
      assert len(title_stored) == 80


  # ── append_message ────────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_append_message_calls_insert_and_update():
      from swarm.sessions import append_message
      conn = _make_conn()
      with _patch_connect(conn):
          await append_message("session-uuid", "user", "hello")
      execute_calls = [str(c) for c in conn.execute.call_args_list]
      assert any("INSERT INTO session_messages" in c for c in execute_calls)
      assert any("UPDATE chat_sessions" in c for c in execute_calls)
      conn.commit.assert_called_once()


  # ── get_history ───────────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_get_history_returns_messages():
      from swarm.sessions import get_history
      cursor = _make_cursor(rows=[("user", "hi"), ("assistant", "hello")])
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          result = await get_history("session-uuid", limit=6)
      assert result == [
          {"role": "user", "content": "hi"},
          {"role": "assistant", "content": "hello"},
      ]


  @pytest.mark.asyncio
  async def test_get_history_returns_empty_on_error():
      from swarm.sessions import get_history
      async def _bad_connect(*args, **kwargs):
          raise RuntimeError("db down")
      with patch("swarm.sessions.psycopg.AsyncConnection.connect", side_effect=_bad_connect):
          result = await get_history("session-uuid")
      assert result == []


  # ── delete_session ────────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_delete_session_returns_true_when_deleted():
      from swarm.sessions import delete_session
      cursor = _make_cursor(rowcount=1)
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          result = await delete_session("session-uuid")
      assert result is True


  @pytest.mark.asyncio
  async def test_delete_session_returns_false_when_not_found():
      from swarm.sessions import delete_session
      cursor = _make_cursor(rowcount=0)
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          result = await delete_session("nonexistent")
      assert result is False


  # ── persist_session ───────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_persist_session_returns_true_when_updated():
      from swarm.sessions import persist_session
      cursor = _make_cursor(rowcount=1)
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          result = await persist_session("session-uuid")
      assert result is True
      update_call = next(c for c in conn.execute.call_args_list
                         if "UPDATE chat_sessions" in str(c))
      assert "persist = TRUE" in str(update_call)
      assert "expires_at = NULL" in str(update_call)


  # ── _cleanup_expired ──────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_cleanup_expired_returns_count():
      from swarm.sessions import _cleanup_expired
      cursor = _make_cursor(rowcount=3)
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          count = await _cleanup_expired()
      assert count == 3
      delete_call = next(c for c in conn.execute.call_args_list
                         if "DELETE FROM chat_sessions" in str(c))
      assert "expires_at < NOW()" in str(delete_call)


  # ── get_context ───────────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_get_context_returns_summary_and_messages():
      from swarm.sessions import get_context
      session_row = (
          "uuid", "proj", "title",
          None, None,          # created_at, updated_at
          None, False,         # expires_at, persist
          "Prior summary",     # summary
          5,                   # summary_through
          4,                   # message_count
      )
      msg_rows = [("user", "q1"), ("assistant", "a1")]

      call_count = 0

      def _make_dynamic_conn():
          nonlocal call_count
          call_count += 1
          if call_count == 1:
              # get_session call
              cursor = _make_cursor()
              cursor.fetchone = AsyncMock(return_value=session_row)
              return _make_conn(cursor)
          else:
              # get_history call
              cursor = _make_cursor(rows=msg_rows)
              return _make_conn(cursor)

      async def _connect(*args, **kwargs):
          return _make_dynamic_conn()

      with patch("swarm.sessions.psycopg.AsyncConnection.connect", side_effect=_connect):
          summary, messages = await get_context("session-uuid")

      assert summary == "Prior summary"
      assert messages == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]


  @pytest.mark.asyncio
  async def test_get_context_returns_empty_for_unknown_session():
      from swarm.sessions import get_context
      cursor = _make_cursor()
      cursor.fetchone = AsyncMock(return_value=None)
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          summary, messages = await get_context("nonexistent")
      assert summary == ""
      assert messages == []
  ```

- [ ] **Step 2: Run the tests**

  ```
  pytest tests/test_sessions.py -v
  ```

  Expected: 13 tests, all PASS.

- [ ] **Step 3: Commit**

  ```bash
  git add tests/test_sessions.py
  git commit -m "test: add unit tests for swarm/sessions.py"
  ```

---

## Task 4: `api/models.py` — session models

**Files:**
- Modify: `api/models.py`

- [ ] **Step 1: Replace `api/models.py` with full content**

  ```python
  from __future__ import annotations
  from datetime import datetime
  from pydantic import BaseModel


  class AgentSpec(BaseModel):
      name: str
      role: str
      model: str
      instructions: list[str]


  class RunRequest(BaseModel):
      task: str
      project_id: str = "default"
      team: str | None = None
      agents: list[AgentSpec] | None = None
      mcp_url: str | None = None
      session_id: str | None = None   # resume existing session
      persist: bool = False            # mark new session as permanent


  class SessionMeta(BaseModel):
      session_id: str
      turn: int         # message pairs completed (message_count // 2)
      context_size: int # verbatim messages injected into coordinator
      compacted: bool   # True if a summary was also injected
      persist: bool
      expires_at: str | None  # ISO 8601 or None when persist=True


  class RunResponse(BaseModel):
      result: str
      team: str
      agents_used: list[str]
      models_pulled: list[str]
      duration_seconds: float
      session: SessionMeta


  class PlanResponse(BaseModel):
      plan: str
      duration_seconds: float


  # ── Session endpoint models ────────────────────────────────────────────────────

  class SessionListItem(BaseModel):
      id: str
      title: str
      created_at: datetime
      updated_at: datetime
      expires_at: datetime | None
      persist: bool
      message_count: int


  class SessionMessage(BaseModel):
      role: str
      content: str
      created_at: datetime


  class SessionDetail(BaseModel):
      id: str
      project_id: str
      title: str
      created_at: datetime
      updated_at: datetime
      expires_at: datetime | None
      persist: bool
      summary: str | None
      message_count: int
      messages: list[SessionMessage]
  ```

- [ ] **Step 2: Run existing tests to verify no regressions**

  ```
  pytest tests/ -v
  ```

  Expected: all existing tests pass.

- [ ] **Step 3: Commit**

  ```bash
  git add api/models.py
  git commit -m "feat: add SessionMeta and session endpoint models to api/models.py"
  ```

---

## Task 5: `swarm/team.py` — inject session context

**Files:**
- Modify: `swarm/team.py`

- [ ] **Step 1: Add `session_id` parameter to `run_task_async` signature**

  In `swarm/team.py`, change the function signature from:

  ```python
  async def run_task_async(
      task: str,
      agent_specs: list | None = None,
      coordinator_model: str | None = None,
      mcp_url: str | None = None,
      project_id: str = "default",
  ) -> str:
  ```

  to:

  ```python
  async def run_task_async(
      task: str,
      agent_specs: list | None = None,
      coordinator_model: str | None = None,
      mcp_url: str | None = None,
      project_id: str = "default",
      session_id: str | None = None,
  ) -> str:
  ```

- [ ] **Step 2: Load session context in parallel with bootstrap and failure context**

  Replace the `asyncio.gather` block:

  ```python
      project_context, failure_context = await asyncio.gather(
          bootstrap(effective_mcp_url, _MCP_TIMEOUT, config.patterns_glob),
          load_failure_context(project_id),
      )
  ```

  with:

  ```python
      from swarm.sessions import get_context as get_session_context

      async def _load_session_context():
          if session_id:
              try:
                  return await get_session_context(session_id)
              except Exception as exc:
                  print(f"[team] session context warning: {exc}")
          return "", []

      project_context, failure_context, (session_summary, session_messages) = (
          await asyncio.gather(
              bootstrap(effective_mcp_url, _MCP_TIMEOUT, config.patterns_glob),
              load_failure_context(project_id),
              _load_session_context(),
          )
      )
  ```

- [ ] **Step 3: Inject session context into coordinator instructions**

  After the existing `if failure_context:` block, add:

  ```python
      if session_summary:
          instructions += [
              "",
              "── Session summary (older turns) ─────────────────────────────────",
              session_summary,
              "──────────────────────────────────────────────────────────────────",
          ]
      if session_messages:
          lines = ["── Recent messages ───────────────────────────────────────────────"]
          for msg in session_messages:
              lines.append(f"[{msg['role']}] {msg['content'][:800]}")
          lines.append("──────────────────────────────────────────────────────────────────")
          instructions += [""] + lines
  ```

- [ ] **Step 4: Run tests**

  ```
  pytest tests/ -v
  ```

  Expected: all tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add swarm/team.py
  git commit -m "feat: inject session context into coordinator instructions"
  ```

---

## Task 6: `api/server.py` — update `POST /run` with session handling

**Files:**
- Modify: `api/server.py`

- [ ] **Step 1: Add session imports at the top of `api/server.py`**

  After the existing imports, add:

  ```python
  from swarm.sessions import (
      create_session, append_message, get_session,
      persist_session as _persist_session,
      compact_session, _cleanup_expired,
  )
  ```

- [ ] **Step 2: Replace the `POST /run` handler**

  Replace the full `/run` function with:

  ```python
  @app.post("/run", response_model=RunResponse)
  async def run(request: RunRequest):
      from api.models import SessionMeta
      start = time.perf_counter()

      # Resolve team spec
      if request.agents:
          agent_specs = request.agents
          coordinator_model = config.leader_model
          team_name = request.team or "custom"
      elif request.team:
          agent_specs, coordinator_model = _load_team(request.team)
          team_name = request.team
      else:
          agent_specs, coordinator_model = _load_team("engineering")
          team_name = "engineering"

      all_models = list({coordinator_model} | {a.model for a in agent_specs})
      models_pulled = await ensure_models(all_models, config.ollama_host)

      mcp_url = request.mcp_url or config.mcp_url

      # Resolve session — create new one if none provided
      session_id = request.session_id
      if not session_id:
          session_id = await create_session(
              project_id=request.project_id,
              title=request.task,
              persist=request.persist,
          )
      elif request.persist:
          await _persist_session(session_id)

      # Load session context size before run (for footer metadata)
      from swarm.sessions import get_history, get_context
      _, prior_messages = await get_context(session_id)
      context_size = len(prior_messages)
      session_before = await get_session(session_id)
      has_summary = bool(session_before and session_before.get("summary"))

      result = await run_task_async(
          task=request.task,
          agent_specs=agent_specs,
          coordinator_model=coordinator_model,
          mcp_url=mcp_url,
          project_id=request.project_id,
          session_id=session_id,
      )

      # Append this turn to session
      await append_message(session_id, "user", request.task)
      await append_message(session_id, "assistant", result)

      # Refresh session metadata for response
      session_after = await get_session(session_id)
      message_count = session_after["message_count"] if session_after else 0
      turn = message_count // 2

      # Trigger compaction async if threshold crossed (fire-and-forget)
      if message_count >= config.compact_threshold:
          asyncio.create_task(compact_session(session_id))

      session_meta = SessionMeta(
          session_id=session_id,
          turn=turn,
          context_size=context_size,
          compacted=has_summary,
          persist=session_after["persist"] if session_after else request.persist,
          expires_at=(
              session_after["expires_at"].isoformat()
              if session_after and session_after.get("expires_at")
              else None
          ),
      )

      return RunResponse(
          result=result,
          team=team_name,
          agents_used=[a.name for a in agent_specs],
          models_pulled=models_pulled,
          duration_seconds=round(time.perf_counter() - start, 2),
          session=session_meta,
      )
  ```

- [ ] **Step 3: Run tests**

  ```
  pytest tests/ -v
  ```

  Expected: all tests pass.

- [ ] **Step 4: Commit**

  ```bash
  git add api/server.py
  git commit -m "feat: update POST /run to create and update sessions"
  ```

---

## Task 7: `api/server.py` — session REST endpoints + cleanup

**Files:**
- Modify: `api/server.py`

- [ ] **Step 1: Add `list_sessions` and `delete_session` imports**

  Update the `from swarm.sessions import (...)` block to include:

  ```python
  from swarm.sessions import (
      create_session, append_message, get_session,
      list_sessions as _list_sessions,
      delete_session as _delete_session,
      persist_session as _persist_session,
      compact_session, _cleanup_expired,
      get_context,
  )
  ```

- [ ] **Step 2: Add session list/detail/delete/persist endpoints**

  Add these four endpoints after the `/plan` endpoint:

  ```python
  @app.get("/sessions")
  async def list_sessions_endpoint(project_id: str = "default", limit: int = 20):
      from api.models import SessionListItem
      sessions = await _list_sessions(project_id, limit=limit)
      return {
          "sessions": [
              SessionListItem(
                  id=s["id"],
                  title=s["title"],
                  created_at=s["created_at"],
                  updated_at=s["updated_at"],
                  expires_at=s.get("expires_at"),
                  persist=s["persist"],
                  message_count=s["message_count"],
              )
              for s in sessions
          ]
      }


  @app.get("/sessions/{session_id}")
  async def get_session_endpoint(session_id: str):
      from api.models import SessionDetail, SessionMessage
      import psycopg
      from config.config import config as _config

      session = await get_session(session_id)
      if not session:
          raise HTTPException(status_code=404, detail="Session not found or expired")

      # Fetch all messages for detail view
      try:
          async with await psycopg.AsyncConnection.connect(_config.postgres_uri) as conn:
              rows = await conn.execute(
                  "SELECT role, content, created_at FROM session_messages"
                  " WHERE session_id = %s ORDER BY created_at ASC",
                  (session_id,),
              )
              messages = [
                  SessionMessage(role=r[0], content=r[1], created_at=r[2])
                  for r in await rows.fetchall()
              ]
      except Exception as exc:
          raise HTTPException(status_code=500, detail=str(exc))

      return SessionDetail(
          id=session["id"],
          project_id=session["project_id"],
          title=session["title"],
          created_at=session["created_at"],
          updated_at=session["updated_at"],
          expires_at=session.get("expires_at"),
          persist=session["persist"],
          summary=session.get("summary"),
          message_count=session["message_count"],
          messages=messages,
      )


  @app.delete("/sessions/{session_id}")
  async def delete_session_endpoint(session_id: str):
      deleted = await _delete_session(session_id)
      if not deleted:
          raise HTTPException(status_code=404, detail="Session not found")
      return {"deleted": session_id}


  @app.patch("/sessions/{session_id}/persist")
  async def persist_session_endpoint(session_id: str):
      updated = await _persist_session(session_id)
      if not updated:
          raise HTTPException(status_code=404, detail="Session not found")
      return {"persisted": session_id}
  ```

- [ ] **Step 3: Add background cleanup loop at startup**

  Add before the `@app.get("/health")` endpoint:

  ```python
  @app.on_event("startup")
  async def _start_cleanup_loop():
      asyncio.create_task(_session_cleanup_loop())


  async def _session_cleanup_loop():
      while True:
          await asyncio.sleep(config.session_cleanup_interval)
          count = await _cleanup_expired()
          if count:
              print(f"[sessions] cleaned up {count} expired session(s)")
  ```

- [ ] **Step 4: Add `import asyncio` to `api/server.py` if not already present**

  Check the top of `api/server.py`. If `import asyncio` is missing, add it after `import time`.

- [ ] **Step 5: Run tests**

  ```
  pytest tests/ -v
  ```

  Expected: all tests pass.

- [ ] **Step 6: Commit**

  ```bash
  git add api/server.py
  git commit -m "feat: add session REST endpoints and background TTL cleanup"
  ```

---

## Task 8: `cli/hive` — HTTP helpers, flags, updated footer

**Files:**
- Modify: `cli/hive`

- [ ] **Step 1: Replace `cli/hive` with full updated content**

  ```python
  #!/usr/bin/env python3
  """AGNOHive CLI — run the agent swarm from your project terminal.

  Installation (on any machine that can reach ZGX):
      cp cli/hive ~/.local/bin/hive
      chmod +x ~/.local/bin/hive

  Environment vars (add to ~/.bashrc or ~/.zshrc):
      export AGNO_HOST=http://<zgx-ip>:9001   # AGNOHive server address
      export AGNO_PROJECT=myproject            # override project auto-detect
      export AGNO_TEAM=engineering             # team to use (default)

  Usage:
      hive "task"                              # one-shot (new session each time)
      hive                                     # REPL (auto-resumes last session)
      hive --session <id>                      # resume a specific session
      hive --persist "task"                    # permanent session
      hive --list-sessions                     # list recent sessions and exit
      hive --review "task"                     # plan → approve → execute
      hive --project myapp "fix the bug"       # explicit project
  """
  import json
  import os
  import readline
  import subprocess
  import sys
  import urllib.error
  import urllib.request
  from pathlib import Path
  from urllib.parse import urlencode

  # ── Config ────────────────────────────────────────────────────────────────────

  AGNO_HOST    = os.getenv("AGNO_HOST",    "http://100.96.86.82:9001")
  AGNO_PROJECT = os.getenv("AGNO_PROJECT", "")
  AGNO_TEAM    = os.getenv("AGNO_TEAM",    "engineering")

  _LAST_SESSION_FILE = Path.home() / ".agno_last_session"

  # ── ANSI colours (disabled if not a TTY) ──────────────────────────────────────

  _COLOUR = sys.stdout.isatty()

  def _c(code: str, text: str) -> str:
      return f"\033[{code}m{text}\033[0m" if _COLOUR else text

  bold   = lambda t: _c("1",    t)
  dim    = lambda t: _c("2",    t)
  cyan   = lambda t: _c("36",   t)
  red    = lambda t: _c("31",   t)
  green  = lambda t: _c("32",   t)
  yellow = lambda t: _c("33",   t)

  # ── Helpers ───────────────────────────────────────────────────────────────────

  def detect_project() -> str:
      if AGNO_PROJECT:
          return AGNO_PROJECT
      try:
          r = subprocess.run(
              ["git", "remote", "get-url", "origin"],
              capture_output=True, text=True, timeout=3,
          )
          if r.returncode == 0:
              url = r.stdout.strip().rstrip("/")
              name = url.split("/")[-1].replace(".git", "")
              if name:
                  return name
      except Exception:
          pass
      return Path.cwd().name


  def load_last_session(project_id: str) -> str | None:
      """Return last session ID for this project from ~/.agno_last_session."""
      try:
          data = json.loads(_LAST_SESSION_FILE.read_text())
          if data.get("project_id") == project_id:
              return data.get("session_id")
      except Exception:
          pass
      return None


  def save_last_session(session_id: str, project_id: str) -> None:
      """Write last session ID to ~/.agno_last_session."""
      try:
          _LAST_SESSION_FILE.write_text(
              json.dumps({"session_id": session_id, "project_id": project_id})
          )
      except Exception:
          pass

  # ── HTTP helpers ──────────────────────────────────────────────────────────────

  def call_api(endpoint: str, payload: dict, timeout: int = 300) -> dict:
      data = json.dumps(payload).encode()
      req = urllib.request.Request(
          f"{AGNO_HOST}{endpoint}",
          data=data,
          headers={"Content-Type": "application/json"},
          method="POST",
      )
      with urllib.request.urlopen(req, timeout=timeout) as resp:
          return json.loads(resp.read())


  def call_get(endpoint: str, params: dict | None = None, timeout: int = 30) -> dict:
      url = f"{AGNO_HOST}{endpoint}"
      if params:
          url += "?" + urlencode(params)
      with urllib.request.urlopen(url, timeout=timeout) as resp:
          return json.loads(resp.read())


  def call_delete(endpoint: str, timeout: int = 30) -> dict:
      req = urllib.request.Request(f"{AGNO_HOST}{endpoint}", method="DELETE")
      with urllib.request.urlopen(req, timeout=timeout) as resp:
          return json.loads(resp.read())


  def call_patch(endpoint: str, timeout: int = 30) -> dict:
      req = urllib.request.Request(f"{AGNO_HOST}{endpoint}", method="PATCH")
      with urllib.request.urlopen(req, timeout=timeout) as resp:
          return json.loads(resp.read())


  def health_check() -> bool:
      try:
          with urllib.request.urlopen(f"{AGNO_HOST}/health", timeout=5) as r:
              return json.loads(r.read()).get("status") == "ok"
      except Exception:
          return False

  # ── Result display ────────────────────────────────────────────────────────────

  def _format_expiry(expires_at: str | None, persist: bool) -> str:
      if persist:
          return "[persistent]"
      if not expires_at:
          return ""
      try:
          from datetime import datetime
          exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
          return f"expires {exp.strftime('%Y-%m-%d')}"
      except Exception:
          return ""


  def _print_result(data: dict, show_resume: bool = False) -> None:
      result   = data.get("result", "")
      agents   = ", ".join(data.get("agents_used", []))
      duration = data.get("duration_seconds", 0)
      pulled   = data.get("models_pulled", [])
      session  = data.get("session", {})

      session_id   = session.get("session_id", "")
      turn         = session.get("turn", 0)
      context_size = session.get("context_size", 0)
      compacted    = session.get("compacted", False)
      persist      = session.get("persist", False)
      expires_at   = session.get("expires_at")

      print()
      print(result)
      print()

      sid_short   = session_id[:8] if session_id else ""
      context_str = f"summary + {context_size} msgs" if compacted else f"{context_size} msgs in context"
      expiry_str  = _format_expiry(expires_at, persist)

      parts = [f"── {agents} · {duration:.1f}s"]
      if sid_short:
          parts.append(f"session {sid_short}")
      if turn:
          parts.append(f"turn {turn}")
      if session_id:
          parts.append(context_str)
      if expiry_str:
          parts.append(expiry_str)
      if pulled:
          parts.append(f"pulled: {', '.join(pulled)}")

      print(dim("  ·  ".join(parts)))
      if show_resume and session_id:
          print(dim(f"  resume: hive --session {session_id}"))
      print()

  # ── Task runners ──────────────────────────────────────────────────────────────

  def run_task(
      task: str,
      project_id: str,
      session_id: str | None = None,
      persist: bool = False,
      show_resume: bool = False,
  ) -> str | None:
      """Run a task; return the session_id from the response, or None on error."""
      if _COLOUR:
          print(dim("  thinking..."), end="\r", flush=True)
      try:
          data = call_api("/run", {
              "task":       task,
              "project_id": project_id,
              "team":       AGNO_TEAM,
              "session_id": session_id,
              "persist":    persist,
          })
          if _COLOUR:
              print(" " * 20, end="\r")
          _print_result(data, show_resume=show_resume)
          return data.get("session", {}).get("session_id")
      except urllib.error.HTTPError as e:
          body = e.read().decode(errors="replace")
          try:
              msg = json.loads(body).get("detail", body)
          except Exception:
              msg = body
          print(red(f"  server error {e.code}: {msg}"))
      except urllib.error.URLError as e:
          print(red(f"  connection error: {e.reason}"))
          print(dim(f"  is AGNOHive running at {AGNO_HOST}?"))
      except Exception as e:
          print(red(f"  error: {e}"))
      return None


  def review_task(
      task: str,
      project_id: str,
      session_id: str | None = None,
      persist: bool = False,
  ) -> str | None:
      """HITL flow — plan → approve → execute. Returns session_id or None."""
      print(f"\n{bold('Planning...')} {dim('(ContextRouter → Researcher → Planner)')}\n")
      try:
          data = call_api("/plan", {"task": task, "project_id": project_id})
      except urllib.error.HTTPError as e:
          body = e.read().decode(errors="replace")
          try:
              msg = json.loads(body).get("detail", body)
          except Exception:
              msg = body
          print(red(f"  plan failed {e.code}: {msg}"))
          return None
      except Exception as e:
          print(red(f"  plan error: {e}"))
          return None

      plan     = data.get("plan", "")
      duration = data.get("duration_seconds", 0)

      print(f"{bold('─' * 60)}")
      print(f"{bold('Proposed Plan')}")
      print(f"{bold('─' * 60)}\n")
      print(plan)
      print(f"\n{dim(f'── planned in {duration:.1f}s')}\n")
      print(f"{bold('─' * 60)}\n")

      try:
          answer = input(f"{bold('Proceed with this plan?')} {dim('[Y/n]')} ").strip().lower()
      except (EOFError, KeyboardInterrupt):
          print(f"\n{yellow('  aborted.')}")
          return None

      if answer in ("n", "no"):
          print(f"{yellow('  plan rejected — nothing was executed.')}")
          return None

      print(f"\n{bold('Executing...')} {dim('(full engineering team)')}\n")
      return run_task(task, project_id, session_id=session_id, persist=persist)

  # ── REPL slash commands ───────────────────────────────────────────────────────

  def _cmd_sessions(project_id: str) -> None:
      try:
          data = call_get("/sessions", {"project_id": project_id, "limit": "20"})
          sessions = data.get("sessions", [])
          if not sessions:
              print(dim("  no sessions found for this project"))
              return
          print()
          for s in sessions:
              sid_short    = s["id"][:8]
              title        = s["title"][:52]
              count        = s.get("message_count", 0)
              persist_badge = " [persistent]" if s["persist"] else ""
              expires_str   = (
                  f"  expires {s['expires_at'][:10]}"
                  if s.get("expires_at") and not s["persist"]
                  else ""
              )
              print(
                  f"  {cyan(sid_short)}  {title}"
                  f"{dim(persist_badge)}"
                  f"  {dim(f'{count} msgs{expires_str}')}"
              )
          print()
      except Exception as e:
          print(red(f"  error listing sessions: {e}"))


  def _cmd_history(session_id: str | None) -> None:
      if not session_id:
          print(yellow("  no active session"))
          return
      try:
          data = call_get(f"/sessions/{session_id}")
          summary  = data.get("summary")
          messages = data.get("messages", [])
          if summary:
              print(f"\n{dim('── summary ──────────────────────────────────────')}")
              print(dim(summary))
          print(f"\n{dim('── messages ─────────────────────────────────────')}")
          for m in messages:
              role_str = bold(f"[{m['role']}]")
              print(f"{role_str} {m['content'][:300]}")
          print()
      except Exception as e:
          print(red(f"  error fetching history: {e}"))


  def _cmd_persist_current(session_id: str | None) -> None:
      if not session_id:
          print(yellow("  no active session"))
          return
      try:
          call_patch(f"/sessions/{session_id}/persist")
          print(green(f"  session {session_id[:8]} marked as persistent — it will never auto-delete"))
      except Exception as e:
          print(red(f"  error: {e}"))


  def _cmd_delete(args: str) -> None:
      sid = args.strip()
      if not sid:
          print(yellow("  usage: /delete <session-id>"))
          return
      try:
          call_delete(f"/sessions/{sid}")
          print(green(f"  session {sid[:8]} deleted"))
      except urllib.error.HTTPError as e:
          if e.code == 404:
              print(yellow(f"  session {sid[:8]} not found"))
          else:
              print(red(f"  error {e.code}: {e.read().decode()}"))
      except Exception as e:
          print(red(f"  error: {e}"))


  def _cmd_list_sessions_oneshot(project_id: str) -> None:
      """Tabular listing for --list-sessions flag."""
      try:
          data = call_get("/sessions", {"project_id": project_id, "limit": "20"})
          sessions = data.get("sessions", [])
          if not sessions:
              print("No sessions found.")
              return
          header = f"{'ID':<10}  {'Title':<52}  {'Msgs':>4}  Status"
          print(f"\n{header}")
          print("─" * len(header))
          for s in sessions:
              sid_short = s["id"][:8]
              title     = s["title"][:52]
              count     = s.get("message_count", 0)
              if s["persist"]:
                  status = "persistent"
              elif s.get("expires_at"):
                  status = f"expires {s['expires_at'][:10]}"
              else:
                  status = "—"
              print(f"{sid_short:<10}  {title:<52}  {count:>4}  {status}")
          print()
      except Exception as e:
          print(f"Error: {e}", file=sys.stderr)
          sys.exit(1)

  # ── Interactive REPL ──────────────────────────────────────────────────────────

  def repl(
      project_id: str,
      review: bool = False,
      initial_session_id: str | None = None,
      persist: bool = False,
  ) -> None:
      history_file = Path.home() / ".agno_history"
      try:
          readline.read_history_file(history_file)
      except FileNotFoundError:
          pass
      readline.set_history_length(500)

      # Resolve starting session
      session_id: str | None = initial_session_id
      is_explicit = initial_session_id is not None
      if session_id is None:
          session_id = load_last_session(project_id)
          is_resumed = session_id is not None
      else:
          is_resumed = True

      # Banner
      mode_label = cyan("review") if review else cyan(AGNO_TEAM)
      print(
          f"\n{bold('AGNOHive')}  "
          f"project {cyan(project_id)}  "
          f"mode {mode_label}  "
          f"{dim(AGNO_HOST)}"
      )
      if not health_check():
          print(yellow("  warning: AGNOHive is not reachable — tasks will fail"))

      if session_id and is_resumed and not is_explicit:
          print(dim(f"  resuming session {session_id[:8]}  (last used this project)"))
      elif session_id and is_explicit:
          print(dim(f"  session {session_id[:8]}"))
      else:
          print(dim("  new session will be created on first prompt"))

      if review:
          print(dim("  review mode: plan shown before every task  ·  prefix with ! to skip"))

      print(dim("  /new  /sessions  /history  /persist  /delete <id>  /exit\n"))

      try:
          while True:
              try:
                  task = input(bold("> ")).strip()
              except EOFError:
                  print()
                  break

              if not task:
                  continue

              # ── Slash commands ─────────────────────────────────────────────
              if task.startswith("/"):
                  parts = task.split(None, 1)
                  cmd   = parts[0].lower()
                  args  = parts[1] if len(parts) > 1 else ""

                  if cmd == "/exit":
                      break
                  elif cmd == "/new":
                      session_id = None
                      print(dim("  started new session (will be created on next prompt)"))
                  elif cmd == "/sessions":
                      _cmd_sessions(project_id)
                  elif cmd == "/history":
                      _cmd_history(session_id)
                  elif cmd == "/persist":
                      _cmd_persist_current(session_id)
                  elif cmd == "/delete":
                      _cmd_delete(args)
                  else:
                      print(dim(f"  unknown command: {cmd}  ·  /new /sessions /history /persist /delete /exit"))
                  continue

              # ── Run task ───────────────────────────────────────────────────
              skip_review = task.startswith("!") and review
              clean_task  = task[1:].strip() if skip_review else task

              if skip_review or not review:
                  returned_id = run_task(
                      clean_task, project_id,
                      session_id=session_id, persist=persist,
                  )
              else:
                  returned_id = review_task(
                      clean_task, project_id,
                      session_id=session_id, persist=persist,
                  )

              if returned_id:
                  session_id = returned_id

      except KeyboardInterrupt:
          print()
      finally:
          readline.write_history_file(history_file)
          if session_id:
              save_last_session(session_id, project_id)
              print(f"\n{dim('  session saved:  ' + session_id)}")
              print(dim(f"  resume later:   hive --session {session_id}"))

  # ── Entry point ───────────────────────────────────────────────────────────────

  def main() -> None:
      import argparse

      parser = argparse.ArgumentParser(
          prog="hive",
          description="AGNOHive CLI — run the agent swarm from your terminal",
          formatter_class=argparse.RawDescriptionHelpFormatter,
      )
      parser.add_argument("task",                 nargs="*", help="task to run (omit for REPL)")
      parser.add_argument("--project",  "-p",               help="project id (default: auto-detect)")
      parser.add_argument("--team",     "-t",               help=f"team (default: {AGNO_TEAM})")
      parser.add_argument("--host",     "-H",               help=f"AGNOHive host (default: {AGNO_HOST})")
      parser.add_argument("--review",   "-r",  action="store_true",
                          help="show plan and ask for approval before executing")
      parser.add_argument("--session",  "-s",               help="resume a specific session by ID")
      parser.add_argument("--persist",          action="store_true",
                          help="mark session as permanent (never auto-deleted)")
      parser.add_argument("--list-sessions",    action="store_true",
                          help="list recent sessions for this project and exit")

      args = parser.parse_args()

      global AGNO_HOST, AGNO_TEAM
      if args.host:
          AGNO_HOST = args.host
      if args.team:
          AGNO_TEAM = args.team

      project_id = args.project or detect_project()

      if args.list_sessions:
          _cmd_list_sessions_oneshot(project_id)
          return

      if args.task:
          task = " ".join(args.task)
          if args.review:
              review_task(task, project_id, session_id=args.session, persist=args.persist)
          else:
              run_task(
                  task, project_id,
                  session_id=args.session, persist=args.persist,
                  show_resume=True,
              )
      else:
          repl(
              project_id,
              review=args.review,
              initial_session_id=args.session,
              persist=args.persist,
          )


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add cli/hive
  git commit -m "feat: add session management to hive CLI — flags, REPL commands, informative footer"
  ```

---

## Task 9: Deploy and smoke test

**Files:** None changed — runtime verification only.

- [ ] **Step 1: Push to remote and pull on ZGX**

  ```bash
  # On Windows
  git push

  # On ZGX (via SSH or terminal)
  git -C ~/agno-hive pull
  ```

- [ ] **Step 2: Restart the AGNOHive server on ZGX**

  ```bash
  # On ZGX — kill existing server and restart
  pkill -f "python main.py --serve"
  cd ~/agno-hive && nohup python main.py --serve > agno.log 2>&1 &
  ```

- [ ] **Step 3: Verify health endpoint**

  ```bash
  curl http://100.96.86.82:9001/health
  ```

  Expected:
  ```json
  {"status": "ok", "mcp_url": "..."}
  ```

- [ ] **Step 4: Run one-shot and verify session ID appears in footer**

  From the EkamApp terminal:
  ```bash
  hive "what does the write_file function do?"
  ```

  Expected footer (example):
  ```
  ── Coder, Reviewer · 38.2s  ·  session a3f7c2d1  ·  turn 1  ·  0 msgs in context  ·  expires 2026-05-31
    resume: hive --session a3f7c2d1-8b3e-4f2a-...
  ```

- [ ] **Step 5: Resume the session and verify context is injected**

  Copy the full session ID from the footer and run:
  ```bash
  hive --session <full-session-id> "and what does confirm_write do?"
  ```

  Expected: the agent's response should reference `write_file` context from the previous turn. Footer should show `turn 2 · 2 msgs in context`.

- [ ] **Step 6: Test REPL auto-resume**

  ```bash
  hive   # enter REPL
  > what does reject_write do?
  > /exit
  ```

  Then re-enter the REPL:
  ```bash
  hive   # should show "resuming session <id>"
  > /history  # should show previous exchange
  > /exit
  ```

- [ ] **Step 7: Test /persist**

  ```bash
  hive
  > what is the MCP server port?
  > /persist   # should confirm "[persistent]"
  > /sessions  # should show [persistent] badge
  > /exit
  ```

- [ ] **Step 8: Test --list-sessions**

  ```bash
  hive --list-sessions
  ```

  Expected: tabular list of sessions with ID, title, message count, expiry or "persistent".

- [ ] **Step 9: Run full test suite**

  ```bash
  pytest tests/ -v
  ```

  Expected: all tests pass.

- [ ] **Step 10: Final commit**

  ```bash
  git add -A
  git commit -m "chore: verify session persistence smoke tests pass"
  ```

---

## Spec Coverage Check

| Spec requirement | Task |
|---|---|
| Sessions stored server-side in PostgreSQL | Task 2 |
| 30-day TTL on non-persisted sessions | Task 2 (`create_session`) |
| `persist` flag — no auto-delete | Task 2, Task 6 |
| Manual delete by session ID | Task 7 (`DELETE /sessions/{id}`) |
| Last 6 messages injected verbatim | Task 5 (`swarm/team.py`) |
| Compaction via `llama3.1:8b` at threshold | Task 2 (`compact_session`), Task 6 |
| Background TTL cleanup every hour | Task 7 (`_session_cleanup_loop`) |
| `POST /run` returns `session_id` always | Task 6 |
| `GET /sessions` list endpoint | Task 7 |
| `GET /sessions/{id}` detail endpoint | Task 7 |
| `DELETE /sessions/{id}` | Task 7 |
| `PATCH /sessions/{id}/persist` | Task 7 |
| `hive --session <id>` resume flag | Task 8 |
| `hive --persist` flag | Task 8 |
| `hive --list-sessions` flag | Task 8 |
| REPL auto-resumes last session | Task 8 (`repl()`) |
| `~/.agno_last_session` state file | Task 8 |
| `/new /sessions /history /persist /delete /exit` REPL commands | Task 8 |
| `/exit` shows session ID + resume command | Task 8 |
| Informative footer (agents · time · session · turn · context · expiry) | Task 8 (`_print_result`) |
| One-shot prints resume command | Task 8 (`show_resume=True`) |
| Config vars: `SESSION_TTL_DAYS`, `AGNO_SESSION_WINDOW`, `AGNO_COMPACT_THRESHOLD`, `SESSION_CLEANUP_INTERVAL` | Task 1 |
| Error handling: invalid session → new session + warn | Task 6 (404 handling) |
| Error handling: PostgreSQL down → silent fallback | Task 2 (try/except pattern) |
