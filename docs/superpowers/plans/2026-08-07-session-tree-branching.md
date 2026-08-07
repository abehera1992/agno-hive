# Session Tree Branching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pi-style tree-based conversation branching (`parentId`/leaf-navigation/`--fork`) to agno-hive's session store, so a user can rewind to an earlier point when the agent goes down a hallucination rabbit hole, resubmit, and get a new sibling branch instead of losing the original.

**Architecture:** Add `parent_message_id` (nullable FK on `session_messages`) and `current_leaf_id` (nullable FK on `chat_sessions`) to the existing linear Postgres schema — additive, nullable, backward-compatible columns, not a rewrite. `append_message` auto-chains each new message onto the session's current leaf and advances the leaf pointer, so when branching is never invoked the system behaves byte-for-byte identically to today (a straight-line chain, just now with explicit edges). `get_context` (used to build the LLM prompt) switches from a flat `ORDER BY created_at` query to a recursive-CTE walk from `current_leaf_id` to root. New CLI commands `/tree` (interactive picker) and `/branch <id>` (direct) rewind the leaf to an earlier message's parent for edit-and-resubmit — mirroring pi's confirmed UX. `--fork <session>` copies the walked current branch into a brand-new, independent session.

**Tech Stack:** Python 3.12, psycopg (async), PostgreSQL recursive CTEs, pytest + pytest-asyncio (mocked psycopg, matching `tests/test_sessions.py`'s existing pattern), FastAPI, stdlib `urllib`/`argparse` (CLI).

## Global Constraints

- Every schema change is additive and nullable — no existing column is dropped, renamed, or made `NOT NULL`. Existing sessions must continue to work exactly as before until the backfill script runs, and identically to today afterward if branching is never used.
- `get_history` (the existing flat-linear query) is left in place, unchanged, still tested — only `get_context`'s internals are rewired to call the new `get_branch_history` instead. `get_history` becomes uncalled-by-`get_context` but remains a valid, independently correct function (e.g. for a future "show every branch" admin view) — not deleted, to avoid unnecessary scope/risk.
- **Explicitly out of scope, documented not silently ignored:** `compact_session`'s summarization (`swarm/sessions.py:277-338`) still operates on ALL messages ever appended to a session in creation-time order, regardless of which branch they're on. Once branching is actually used, compaction may summarize abandoned-branch content alongside the active branch. This is a known interaction this plan does not resolve — flagged here rather than silently producing surprising behavior later.
- **Explicitly out of scope:** `api/server.py`'s `get_session_endpoint` (`/sessions/{id}`, used by the `/history` CLI command) keeps its own inline SQL showing ALL messages ever appended (every branch merged, creation-time order) — not rewired to be branch-aware in this plan. `/history` after this plan shows "everything," not "the current branch." This is a display-only limitation, not a correctness issue (the LLM's actual context via `get_context` IS branch-scoped) — noted as a follow-up, not bundled in here to keep this plan's blast radius contained to the load-bearing path.
- `/tree`'s display is an MVP: a flat, depth-indented list computed by walking every message from every root, not a rendered ASCII tree diagram. A fancier renderer is a separate future enhancement.
- **Cross-plan dependency:** Task 5 (CLI commands) needs the `load_cli_hive` pytest fixture. If the *SSE Tool-Call Event Pipe* plan (`2026-08-07-sse-tool-call-event-pipe.md`) has already been executed, that fixture already exists in `tests/conftest.py` — skip re-adding it. If this plan is executed first/standalone, Task 5 Step 1 adds it here instead.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `swarm/sessions.py` | Modify | Schema (2 new columns, 1 new index), `append_message` chaining, new `get_branch_history`/`set_current_leaf`/`list_session_tree`/`fork_session`, `get_context` rewire |
| `tests/test_sessions.py` | Modify | Tests for all new/changed functions above, in the existing mock style |
| `scripts/backfill_session_tree.py` | Create | One-off script chaining every pre-existing session into a straight-line tree |
| `tests/test_backfill_session_tree.py` | Create | Unit test for the backfill script's SQL, mocked psycopg |
| `api/models.py` | Modify | `SessionListItem`/new response models for tree/branch endpoints |
| `api/server.py` | Modify | New `GET /sessions/{id}/tree`, `POST /sessions/{id}/branch`, `POST /sessions/{id}/fork` endpoints |
| `tests/test_server_tree_endpoints.py` | Create | Unit tests for the 3 new endpoint handler functions |
| `cli/hive` | Modify | `/tree`, `/branch <id>` REPL commands; `--fork <session>` CLI flag |
| `tests/conftest.py` | Modify (if not already done by the SSE plan) | `load_cli_hive` fixture |
| `tests/test_cli_hive_tree.py` | Create | Unit tests for the new CLI commands |

---

## Task 1: Schema + core tree-walk functions in `swarm/sessions.py`

**Files:**
- Modify: `swarm/sessions.py:16-48` (`_ensure_tables`), `:79-94` (`append_message`), `:243-250` (`get_context`)
- Test: `tests/test_sessions.py`

**Interfaces:**
- Produces:
  - `append_message(session_id: str, role: str, content: str, parent_message_id: int | None = None) -> int` — now RETURNS the new message's integer id (previously returned `None`). If `parent_message_id` is omitted, chains onto the session's current `current_leaf_id` (preserving today's linear behavior when branching is never used). Always advances `chat_sessions.current_leaf_id` to the new message's id after insert.
  - `get_branch_history(session_id: str, leaf_id: int | None = None, limit: int | None = None) -> list[dict]` — walks from `leaf_id` (defaults to the session's `current_leaf_id`) to root via `parent_message_id`, returns the `limit` most recent messages (default `config.session_window`, same default as `get_history`), oldest-first, same `[{"role":..., "content":...}]` shape as `get_history`.
  - `set_current_leaf(session_id: str, message_id: int) -> bool` — updates `chat_sessions.current_leaf_id`. Returns `True` if a row was updated.
  - `list_session_tree(session_id: str) -> list[dict]` — every message in the session with `id`, `parent_message_id`, `role`, `content`, `created_at`, `depth` (0 = root), ordered `created_at ASC`.
- Consumes: nothing new from outside this module.

- [ ] **Step 1: Write the failing tests**

  Add to `tests/test_sessions.py` (reuse the existing `_make_cursor`/`_make_conn`/`_patch_connect` helpers already defined at the top of that file — do not redefine them):

  ```python
  # ── append_message (tree-aware) ────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_append_message_returns_new_message_id():
      from swarm.sessions import append_message
      cursor = _make_cursor()
      cursor.fetchone = AsyncMock(return_value=(42,))
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          new_id = await append_message("session-uuid", "user", "hello")
      assert new_id == 42


  @pytest.mark.asyncio
  async def test_append_message_defaults_parent_to_current_leaf():
      from swarm.sessions import append_message
      cursor = _make_cursor()
      cursor.fetchone = AsyncMock(side_effect=[(7,), (99,)])  # leaf lookup, then INSERT...RETURNING id
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          await append_message("session-uuid", "assistant", "reply")
      insert_call = next(c for c in conn.execute.call_args_list
                         if "INSERT INTO session_messages" in str(c))
      assert insert_call.args[1][3] == 7  # parent_message_id positional arg


  @pytest.mark.asyncio
  async def test_append_message_explicit_parent_skips_leaf_lookup():
      from swarm.sessions import append_message
      cursor = _make_cursor()
      cursor.fetchone = AsyncMock(return_value=(55,))
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          await append_message("session-uuid", "user", "hi", parent_message_id=3)
      insert_call = next(c for c in conn.execute.call_args_list
                         if "INSERT INTO session_messages" in str(c))
      assert insert_call.args[1][3] == 3


  @pytest.mark.asyncio
  async def test_append_message_advances_current_leaf():
      from swarm.sessions import append_message
      cursor = _make_cursor()
      cursor.fetchone = AsyncMock(return_value=(101,))
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          await append_message("session-uuid", "user", "hi", parent_message_id=None)
      leaf_update = next(c for c in conn.execute.call_args_list
                         if "current_leaf_id" in str(c) and "UPDATE chat_sessions" in str(c))
      assert 101 in leaf_update.args[1]


  # ── get_branch_history ───────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_get_branch_history_walks_from_current_leaf_and_reverses_to_oldest_first():
      from swarm.sessions import get_branch_history
      # Recursive CTE returns newest-first (depth 0 = leaf); function must reverse it.
      rows = [("user", "second"), ("assistant", "first-reply"), ("user", "first")]
      cursor = _make_cursor(rows=rows)
      cursor.fetchone = AsyncMock(return_value=(5,))  # current_leaf_id lookup
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          result = await get_branch_history("session-uuid")
      assert result == [
          {"role": "user", "content": "first"},
          {"role": "assistant", "content": "first-reply"},
          {"role": "user", "content": "second"},
      ]


  @pytest.mark.asyncio
  async def test_get_branch_history_uses_explicit_leaf_id_when_given():
      from swarm.sessions import get_branch_history
      cursor = _make_cursor(rows=[("user", "x")])
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          await get_branch_history("session-uuid", leaf_id=42)
      walk_call = next(c for c in conn.execute.call_args_list if "WITH RECURSIVE" in str(c))
      assert walk_call.args[1]["leaf_id"] == 42


  @pytest.mark.asyncio
  async def test_get_branch_history_returns_empty_on_error():
      from swarm.sessions import get_branch_history
      async def _bad_connect(*args, **kwargs):
          raise RuntimeError("db down")
      with patch("swarm.sessions.psycopg.AsyncConnection.connect", side_effect=_bad_connect):
          result = await get_branch_history("session-uuid")
      assert result == []


  # ── set_current_leaf ─────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_set_current_leaf_returns_true_when_updated():
      from swarm.sessions import set_current_leaf
      cursor = _make_cursor(rowcount=1)
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          result = await set_current_leaf("session-uuid", 17)
      assert result is True
      update_call = next(c for c in conn.execute.call_args_list
                         if "UPDATE chat_sessions" in str(c) and "current_leaf_id" in str(c))
      assert 17 in update_call.args[1]


  # ── list_session_tree ────────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_list_session_tree_returns_all_messages_with_depth():
      from swarm.sessions import list_session_tree
      rows = [(1, None, "user", "root", "2026-01-01", 0), (2, 1, "assistant", "reply", "2026-01-01", 1)]
      cursor = _make_cursor(rows=rows)
      conn = _make_conn(cursor)
      with _patch_connect(conn):
          result = await list_session_tree("session-uuid")
      assert result == [
          {"id": 1, "parent_message_id": None, "role": "user", "content": "root", "created_at": "2026-01-01", "depth": 0},
          {"id": 2, "parent_message_id": 1, "role": "assistant", "content": "reply", "created_at": "2026-01-01", "depth": 1},
      ]


  # ── get_context (rewired) ────────────────────────────────────────────────────

  @pytest.mark.asyncio
  async def test_get_context_now_calls_get_branch_history_not_get_history():
      from swarm import sessions
      session_row = ("uuid", "proj", "title", None, None, None, False, "summary", 0, 4)
      cursor = _make_cursor()
      cursor.fetchone = AsyncMock(return_value=session_row)
      conn = _make_conn(cursor)
      with _patch_connect(conn), \
           patch("swarm.sessions.get_branch_history", new=AsyncMock(return_value=[{"role": "user", "content": "hi"}])) as mocked, \
           patch("swarm.sessions.get_history", new=AsyncMock(return_value=[])) as unused:
          summary, messages = await sessions.get_context("session-uuid")
      mocked.assert_awaited_once()
      unused.assert_not_awaited()
      assert messages == [{"role": "user", "content": "hi"}]
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `python -m pytest tests/test_sessions.py -v -k "append_message_returns or branch_history or current_leaf or session_tree or now_calls"`
  Expected: FAIL — `append_message` doesn't return a value, `get_branch_history`/`set_current_leaf`/`list_session_tree` don't exist, `get_context` still calls `get_history`.

- [ ] **Step 3: Update the schema in `_ensure_tables`**

  Replace `swarm/sessions.py:16-48` with:

  ```python
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
              summary_through INT         NOT NULL DEFAULT 0,
              current_leaf_id INT
          )
      """)
      await conn.execute("""
          CREATE INDEX IF NOT EXISTS chat_sessions_project_idx
              ON chat_sessions (project_id, created_at DESC)
      """)
      await conn.execute("""
          CREATE TABLE IF NOT EXISTS session_messages (
              id                SERIAL PRIMARY KEY,
              session_id        UUID        NOT NULL
                                    REFERENCES chat_sessions(id) ON DELETE CASCADE,
              role              TEXT        NOT NULL,
              content           TEXT        NOT NULL,
              created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              parent_message_id INT         REFERENCES session_messages(id) ON DELETE SET NULL
          )
      """)
      await conn.execute("""
          CREATE INDEX IF NOT EXISTS session_messages_session_idx
              ON session_messages (session_id, created_at ASC)
      """)
      await conn.execute("""
          CREATE INDEX IF NOT EXISTS session_messages_parent_idx
              ON session_messages (parent_message_id)
      """)
      # Additive columns for pre-existing deployments where the tables above already
      # existed before this plan (CREATE TABLE IF NOT EXISTS is a no-op there).
      await conn.execute("""
          ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS current_leaf_id INT
      """)
      await conn.execute("""
          ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS parent_message_id INT
              REFERENCES session_messages(id) ON DELETE SET NULL
      """)
      await conn.commit()
  ```

  (`ADD COLUMN IF NOT EXISTS` is safe both on a genuinely fresh database — where the `CREATE TABLE` above already includes the column, so the `ALTER TABLE` is a no-op — and on an existing production database that already has the old schema.)

- [ ] **Step 4: Rewrite `append_message`**

  Replace `swarm/sessions.py:79-94` with:

  ```python
  async def append_message(
      session_id: str, role: str, content: str, parent_message_id: int | None = None
  ) -> int | None:
      """Append a message, chaining it onto the tree, and advance the session's
      current leaf. Returns the new message's id, or None on failure.

      If parent_message_id is omitted, chains onto the session's current
      current_leaf_id (NULL for a brand-new session, becoming a root message) --
      this reproduces today's linear behavior exactly when branching is never
      invoked, since the leaf always trails the most recent append.
      """
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              if parent_message_id is None:
                  leaf_row = await conn.execute(
                      "SELECT current_leaf_id FROM chat_sessions WHERE id = %s", (session_id,)
                  )
                  row = await leaf_row.fetchone()
                  parent_message_id = row[0] if row else None

              insert_result = await conn.execute(
                  "INSERT INTO session_messages (session_id, role, content, parent_message_id)"
                  " VALUES (%s, %s, %s, %s) RETURNING id",
                  (session_id, role, content, parent_message_id),
              )
              new_row = await insert_result.fetchone()
              new_id = new_row[0] if new_row else None

              await conn.execute(
                  "UPDATE chat_sessions SET updated_at = NOW(), current_leaf_id = %s WHERE id = %s",
                  (new_id, session_id),
              )
              await conn.commit()
              return new_id
      except Exception as exc:
          print(f"[sessions] append_message warning: {exc}")
          return None
  ```

- [ ] **Step 5: Add `get_branch_history`, `set_current_leaf`, `list_session_tree`**

  Add these functions after `get_history` (which ends at `swarm/sessions.py:118`), leaving `get_history` itself completely unchanged:

  ```python
  async def get_branch_history(
      session_id: str, leaf_id: int | None = None, limit: int | None = None
  ) -> list[dict]:
      """Walk from `leaf_id` (default: the session's current_leaf_id) to the root
      via parent_message_id, returning the `limit` most recent messages oldest-first
      -- same external shape as get_history, tree-aware instead of flat."""
      if limit is None:
          limit = config.session_window
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              if leaf_id is None:
                  leaf_row = await conn.execute(
                      "SELECT current_leaf_id FROM chat_sessions WHERE id = %s", (session_id,)
                  )
                  row = await leaf_row.fetchone()
                  leaf_id = row[0] if row else None
                  if leaf_id is None:
                      return []

              rows = await conn.execute(
                  """
                  WITH RECURSIVE branch AS (
                      SELECT id, role, content, parent_message_id, 0 AS depth
                      FROM session_messages WHERE id = %(leaf_id)s
                      UNION ALL
                      SELECT sm.id, sm.role, sm.content, sm.parent_message_id, b.depth + 1
                      FROM session_messages sm
                      JOIN branch b ON sm.id = b.parent_message_id
                  )
                  SELECT role, content FROM branch ORDER BY depth ASC LIMIT %(limit)s
                  """,
                  {"leaf_id": leaf_id, "limit": limit},
              )
              newest_first = [{"role": r[0], "content": r[1]} for r in await rows.fetchall()]
              return list(reversed(newest_first))
      except Exception as exc:
          print(f"[sessions] get_branch_history warning: {exc}")
          return []


  async def set_current_leaf(session_id: str, message_id: int) -> bool:
      """Rewind/advance the session's active branch tip. Used by /branch to rewind
      to an earlier message's parent before the user resubmits a sibling branch."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              result = await conn.execute(
                  "UPDATE chat_sessions SET current_leaf_id = %s, updated_at = NOW() WHERE id = %s",
                  (message_id, session_id),
              )
              await conn.commit()
              return result.rowcount > 0
      except Exception as exc:
          print(f"[sessions] set_current_leaf warning: {exc}")
          return False


  async def list_session_tree(session_id: str) -> list[dict]:
      """Every message in the session, depth-from-root computed server-side, for
      the /tree picker's flat depth-indented display (not every branch's exact
      shape -- see the plan's Global Constraints for this MVP's scope)."""
      try:
          async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
              rows = await conn.execute(
                  """
                  WITH RECURSIVE tree AS (
                      SELECT id, parent_message_id, role, content, created_at, 0 AS depth
                      FROM session_messages
                      WHERE session_id = %(session_id)s AND parent_message_id IS NULL
                      UNION ALL
                      SELECT sm.id, sm.parent_message_id, sm.role, sm.content, sm.created_at, t.depth + 1
                      FROM session_messages sm
                      JOIN tree t ON sm.parent_message_id = t.id
                      WHERE sm.session_id = %(session_id)s
                  )
                  SELECT id, parent_message_id, role, content, created_at, depth
                  FROM tree ORDER BY created_at ASC
                  """,
                  {"session_id": session_id},
              )
              return [
                  {
                      "id": r[0], "parent_message_id": r[1], "role": r[2],
                      "content": r[3], "created_at": r[4], "depth": r[5],
                  }
                  for r in await rows.fetchall()
              ]
      except Exception as exc:
          print(f"[sessions] list_session_tree warning: {exc}")
          return []


  async def fork_session(source_session_id: str, project_id: str, title: str) -> str | None:
      """Copy the source session's CURRENT branch (leaf to root) into a brand-new,
      independent session -- mirrors pi's --fork (new file) vs /tree (same file,
      diverging paths) distinction. Returns the new session id, or None on failure."""
      try:
          branch = await get_branch_history(source_session_id, limit=10_000)  # effectively "whole branch"
          if not branch:
              return None
          new_session_id = await create_session(project_id, title, persist=False)
          parent_id: int | None = None
          for msg in branch:  # oldest-first, matching get_branch_history's contract
              parent_id = await append_message(
                  new_session_id, msg["role"], msg["content"], parent_message_id=parent_id
              )
          return new_session_id
      except Exception as exc:
          print(f"[sessions] fork_session warning: {exc}")
          return None
  ```

- [ ] **Step 6: Rewire `get_context`**

  Replace the body of `get_context` at `swarm/sessions.py:243-250`:

  ```python
  async def get_context(session_id: str) -> tuple[str, list[dict]]:
      """Return (summary_or_empty, recent_messages) for coordinator injection.
      Branch-aware since the Session Tree Branching plan: walks from the
      session's current_leaf_id, not a flat ORDER BY created_at scan."""
      session = await get_session(session_id)
      if not session:
          return "", []
      messages = await get_branch_history(session_id, limit=config.session_window)
      summary = session.get("summary") or ""
      return summary, messages
  ```

- [ ] **Step 7: Run tests to verify they pass**

  Run: `python -m pytest tests/test_sessions.py -v`
  Expected: PASS (all previous tests + new ones, none broken)

- [ ] **Step 8: Run the full suite**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads`
  Expected: PASS (no `-k "not test_sessions"` deselect needed anymore now that `test_sessions.py` is part of the plan under test)

- [ ] **Step 9: Commit**

  ```bash
  git add swarm/sessions.py tests/test_sessions.py
  git commit -m "feat(sessions): add tree branching -- parent_message_id, current_leaf_id

  append_message now chains onto and advances the session's current leaf by
  default, reproducing today's linear behavior exactly when branching is
  never invoked. New get_branch_history/set_current_leaf/list_session_tree/
  fork_session. get_context rewired to be branch-aware; get_history left
  unchanged and still tested for a future flat/all-branches view."
  ```

---

## Task 2: Backfill script for pre-existing sessions

**Files:**
- Create: `scripts/backfill_session_tree.py`
- Test: `tests/test_backfill_session_tree.py`

**Interfaces:**
- Produces: `backfill(dry_run: bool = True) -> int` — importable as `from scripts.backfill_session_tree import backfill`. Returns the number of sessions updated. `dry_run=True` (the default, and the CLI's default) prints what WOULD change without writing.
- Consumes: `swarm.sessions.config.postgres_uri` (same connection config as the rest of the module).

- [ ] **Step 1: Write the failing test**

  Create `tests/test_backfill_session_tree.py`:

  ```python
  """Unit test for the one-off session-tree backfill script. Mocked psycopg,
  same pattern as tests/test_sessions.py."""
  import pytest
  from unittest.mock import AsyncMock, patch


  @pytest.mark.asyncio
  async def test_backfill_dry_run_does_not_commit():
      from scripts.backfill_session_tree import backfill
      cursor = AsyncMock()
      cursor.fetchall = AsyncMock(return_value=[("session-1",), ("session-2",)])
      conn = AsyncMock()
      conn.execute = AsyncMock(return_value=cursor)
      conn.commit = AsyncMock()
      conn.__aenter__ = AsyncMock(return_value=conn)
      conn.__aexit__ = AsyncMock(return_value=False)

      async def _connect(*args, **kwargs):
          return conn

      with patch("scripts.backfill_session_tree.psycopg.AsyncConnection.connect", side_effect=_connect):
          count = await backfill(dry_run=True)

      assert count == 2
      conn.commit.assert_not_called()


  @pytest.mark.asyncio
  async def test_backfill_live_run_commits_and_uses_lag_window_function():
      from scripts.backfill_session_tree import backfill
      cursor = AsyncMock()
      cursor.fetchall = AsyncMock(return_value=[("session-1",)])
      conn = AsyncMock()
      conn.execute = AsyncMock(return_value=cursor)
      conn.commit = AsyncMock()
      conn.__aenter__ = AsyncMock(return_value=conn)
      conn.__aexit__ = AsyncMock(return_value=False)

      async def _connect(*args, **kwargs):
          return conn

      with patch("scripts.backfill_session_tree.psycopg.AsyncConnection.connect", side_effect=_connect):
          await backfill(dry_run=False)

      conn.commit.assert_called()
      update_calls = [str(c) for c in conn.execute.call_args_list]
      assert any("LAG(id) OVER" in c for c in update_calls)
      assert any("current_leaf_id" in c for c in update_calls)
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run: `python -m pytest tests/test_backfill_session_tree.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.backfill_session_tree'`

- [ ] **Step 3: Create `scripts/backfill_session_tree.py`**

  ```python
  """One-off backfill: chain every pre-existing session's messages into a
  straight-line tree (parent_message_id) and set each session's current_leaf_id
  to its most recent message. Non-breaking -- every existing session already
  behaves as a straight-line chain by created_at order; this just makes that
  chain explicit so the new tree-walk queries (get_branch_history, etc.) see
  the exact same history they'd get from the old flat ORDER BY query.

  Usage:
      python -m scripts.backfill_session_tree            # dry run (default)
      python -m scripts.backfill_session_tree --live      # actually writes
  """
  import asyncio
  import sys

  import psycopg

  from config.config import config


  async def backfill(dry_run: bool = True) -> int:
      """Returns the number of sessions processed (dry_run) or updated (live)."""
      async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
          rows = await conn.execute("SELECT id FROM chat_sessions")
          session_ids = [r[0] for r in await rows.fetchall()]

          if dry_run:
              print(f"[backfill] DRY RUN: would chain {len(session_ids)} session(s)")
              return len(session_ids)

          await conn.execute(
              """
              WITH ordered AS (
                  SELECT id, session_id,
                         LAG(id) OVER (PARTITION BY session_id ORDER BY created_at) AS prev_id
                  FROM session_messages
              )
              UPDATE session_messages sm SET parent_message_id = o.prev_id
              FROM ordered o WHERE sm.id = o.id AND sm.parent_message_id IS NULL
              """
          )
          await conn.execute(
              """
              UPDATE chat_sessions cs
              SET current_leaf_id = (
                  SELECT id FROM session_messages
                  WHERE session_id = cs.id ORDER BY created_at DESC LIMIT 1
              )
              WHERE cs.current_leaf_id IS NULL
              """
          )
          await conn.commit()
          print(f"[backfill] chained {len(session_ids)} session(s)")
          return len(session_ids)


  if __name__ == "__main__":
      asyncio.run(backfill(dry_run="--live" not in sys.argv))
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run: `python -m pytest tests/test_backfill_session_tree.py -v`
  Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/backfill_session_tree.py tests/test_backfill_session_tree.py
  git commit -m "feat(scripts): add session-tree backfill for pre-existing sessions

  Dry-run by default; --live writes. Chains every session's messages via
  LAG() OVER (PARTITION BY session_id ORDER BY created_at) and sets
  current_leaf_id to each session's most recent message -- restores the
  'tip = most recent' invariant get_branch_history/append_message depend on."
  ```

  **Do not run this against the production ZGX database as part of this plan's execution** — that is a deployment step for a human to run deliberately after the code above is deployed, not something to automate here (see the plan's overall Verification section).

---

## Task 3: New tree/branch/fork endpoints in `api/server.py`

**Files:**
- Modify: `api/models.py` (new response models)
- Modify: `api/server.py` (3 new endpoints, near the existing `/sessions/*` routes at `api/server.py:475-544`)
- Test: `tests/test_server_tree_endpoints.py`

**Interfaces:**
- Consumes: `swarm.sessions.list_session_tree`, `set_current_leaf`, `fork_session` (Task 1).
- Produces: `GET /sessions/{id}/tree`, `POST /sessions/{id}/branch` (body: `{"message_id": int}`), `POST /sessions/{id}/fork` (body: `{"title": str}`, returns `{"session_id": str}`).

- [ ] **Step 1: Write the failing tests**

  Create `tests/test_server_tree_endpoints.py`:

  ```python
  """Unit tests for the 3 new tree/branch/fork endpoint handlers. Each handler
  function is called directly (not via a FastAPI TestClient -- this repo has
  no existing TestClient usage; mocking the swarm.sessions calls it delegates
  to is consistent with how the rest of this file's session endpoints work)."""
  import pytest
  from unittest.mock import AsyncMock, patch

  from fastapi import HTTPException


  @pytest.mark.asyncio
  async def test_tree_endpoint_returns_messages():
      from api.server import get_session_tree_endpoint
      fake_tree = [{"id": 1, "parent_message_id": None, "role": "user", "content": "hi", "created_at": None, "depth": 0}]
      with patch("api.server.list_session_tree", new=AsyncMock(return_value=fake_tree)):
          result = await get_session_tree_endpoint("session-uuid")
      assert result["messages"] == fake_tree


  @pytest.mark.asyncio
  async def test_branch_endpoint_sets_leaf_to_parent_of_selected_message():
      from api.server import branch_session_endpoint
      from api.models import BranchRequest
      fake_tree = [
          {"id": 1, "parent_message_id": None, "role": "user", "content": "root", "created_at": None, "depth": 0},
          {"id": 2, "parent_message_id": 1, "role": "assistant", "content": "reply", "created_at": None, "depth": 1},
      ]
      with patch("api.server.list_session_tree", new=AsyncMock(return_value=fake_tree)), \
           patch("api.server.set_current_leaf", new=AsyncMock(return_value=True)) as mock_set:
          result = await branch_session_endpoint("session-uuid", BranchRequest(message_id=2))
      mock_set.assert_awaited_once_with("session-uuid", 1)  # rewinds to message 2's PARENT (id 1)
      assert result["new_leaf_id"] == 1
      assert result["editable_content"] == "reply"


  @pytest.mark.asyncio
  async def test_branch_endpoint_404s_on_unknown_message_id():
      from api.server import branch_session_endpoint
      from api.models import BranchRequest
      with patch("api.server.list_session_tree", new=AsyncMock(return_value=[])):
          with pytest.raises(HTTPException) as exc_info:
              await branch_session_endpoint("session-uuid", BranchRequest(message_id=999))
      assert exc_info.value.status_code == 404


  @pytest.mark.asyncio
  async def test_fork_endpoint_returns_new_session_id():
      from api.server import fork_session_endpoint
      from api.models import ForkRequest
      with patch("api.server.fork_session", new=AsyncMock(return_value="new-session-uuid")):
          result = await fork_session_endpoint("session-uuid", ForkRequest(title="forked task", project_id="ekam"))
      assert result["session_id"] == "new-session-uuid"


  @pytest.mark.asyncio
  async def test_fork_endpoint_404s_when_source_session_has_no_messages():
      from api.server import fork_session_endpoint
      from api.models import ForkRequest
      with patch("api.server.fork_session", new=AsyncMock(return_value=None)):
          with pytest.raises(HTTPException) as exc_info:
              await fork_session_endpoint("session-uuid", ForkRequest(title="x", project_id="ekam"))
      assert exc_info.value.status_code == 404
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `python -m pytest tests/test_server_tree_endpoints.py -v`
  Expected: FAIL with `ImportError` (endpoints and request models don't exist yet)

- [ ] **Step 3: Add `BranchRequest`/`ForkRequest` to `api/models.py`**

  Add near the other session-related models in `api/models.py`:

  ```python
  class BranchRequest(BaseModel):
      message_id: int


  class ForkRequest(BaseModel):
      title: str
      project_id: str
  ```

  (This file already imports `BaseModel` from `pydantic` for its other request models — no new import needed.)

- [ ] **Step 4: Add the 3 endpoints to `api/server.py`**

  Add the import at `api/server.py:16-23`'s existing `from swarm.sessions import (...)` block — extend it to also pull in the 3 new functions:

  ```python
  from swarm.sessions import (
      create_session, append_message, get_session,
      list_sessions as _list_sessions,
      delete_session as _delete_session,
      persist_session as _persist_session,
      compact_session, _cleanup_expired,
      get_context, list_session_tree, set_current_leaf, fork_session,
  )
  ```

  Add near the existing `/sessions/*` routes (after `persist_session_endpoint`, which ends around `api/server.py:544`):

  ```python
  @app.get("/sessions/{session_id}/tree")
  async def get_session_tree_endpoint(session_id: str):
      messages = await list_session_tree(session_id)
      return {"messages": messages}


  @app.post("/sessions/{session_id}/branch")
  async def branch_session_endpoint(session_id: str, request: "BranchRequest"):
      from api.models import BranchRequest as _BranchRequest  # local import matches this file's existing lazy-import style
      messages = await list_session_tree(session_id)
      target = next((m for m in messages if m["id"] == request.message_id), None)
      if target is None:
          raise HTTPException(status_code=404, detail="message not found in this session")
      new_leaf_id = target["parent_message_id"]  # rewind to the SELECTED message's PARENT
      await set_current_leaf(session_id, new_leaf_id)
      return {"new_leaf_id": new_leaf_id, "editable_content": target["content"]}


  @app.post("/sessions/{session_id}/fork")
  async def fork_session_endpoint(session_id: str, request: "ForkRequest"):
      from api.models import ForkRequest as _ForkRequest
      new_session_id = await fork_session(session_id, request.project_id, request.title)
      if new_session_id is None:
          raise HTTPException(status_code=404, detail="source session has no messages to fork")
      return {"session_id": new_session_id}
  ```

  Then replace the two placeholder string type hints with real imports — add to `api/server.py:9`'s existing `from api.models import (...)` line:

  ```python
  from api.models import AgentSpec, RunRequest, RunResponse, PlanResponse, ScanRequest, ScanResponse, FeedbackRequest, FeedbackResponse, BranchRequest, ForkRequest
  ```

  and change the two endpoint signatures above from `request: "BranchRequest"` / `request: "ForkRequest"` (quoted placeholders used only to write the step in order) to the real unquoted types `request: BranchRequest` / `request: ForkRequest`, and delete the two local `from api.models import ... as _...` lines added above (they were only needed if the top-level import wasn't updated first — since it now is, remove them to avoid an unused import).

- [ ] **Step 5: Run tests to verify they pass**

  Run: `python -m pytest tests/test_server_tree_endpoints.py -v`
  Expected: PASS (5 tests)

- [ ] **Step 6: Run the full suite**

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads`
  Expected: PASS

- [ ] **Step 7: Commit**

  ```bash
  git add api/models.py api/server.py tests/test_server_tree_endpoints.py
  git commit -m "feat(server): add GET /sessions/{id}/tree, POST .../branch, POST .../fork"
  ```

---

## Task 4: `/tree`, `/branch <id>` REPL commands and `--fork` CLI flag

**Files:**
- Modify: `tests/conftest.py` (add `load_cli_hive` fixture — **skip this step if the SSE Tool-Call Event Pipe plan already added it**)
- Modify: `cli/hive:1503` (REPL command-list banner line), `cli/hive:1516-1579` (slash-command dispatch), `cli/hive:1638-1721` (argparse + early-exit dispatch in `main()`)
- Test: `tests/test_cli_hive_tree.py`

**Interfaces:**
- Consumes: `GET /sessions/{id}/tree`, `POST /sessions/{id}/branch`, `POST /sessions/{id}/fork` (Task 3) via the existing `call_get`/`call_api` helpers (`cli/hive:634-651`).
- Produces: `_cmd_tree(session_id) -> str | None` (returns editable content to prefill the next prompt, or `None`), `_cmd_branch(session_id, message_id) -> str | None` (same contract, direct id path), `_cmd_fork_oneshot(session_id, title, project_id) -> None` (prints the new session id and exits, mirroring `_cmd_list_sessions_oneshot`'s style).

- [ ] **Step 1 (conditional): Add `load_cli_hive` fixture to `tests/conftest.py`**

  Skip this step entirely if `tests/conftest.py` already has a `load_cli_hive` fixture (added by the SSE Tool-Call Event Pipe plan). Otherwise add:

  ```python
  import importlib.util
  import sys
  from pathlib import Path


  @pytest.fixture
  def load_cli_hive():
      """Dynamically load cli/hive (no .py extension, not a package) as a fresh
      module object each time it's called, so tests can inspect/monkeypatch its
      module-level functions without polluting sys.modules across tests."""
      def _load():
          path = Path(__file__).resolve().parent.parent / "cli" / "hive"
          spec = importlib.util.spec_from_file_location("cli_hive_under_test", path)
          module = importlib.util.module_from_spec(spec)
          sys.modules["cli_hive_under_test"] = module
          spec.loader.exec_module(module)
          return module
      return _load
  ```

- [ ] **Step 2: Write the failing tests**

  Create `tests/test_cli_hive_tree.py`:

  ```python
  """Unit tests for cli/hive's /tree, /branch, --fork commands."""


  def test_cmd_branch_calls_the_branch_endpoint_and_returns_editable_content(load_cli_hive, monkeypatch):
      hive = load_cli_hive()
      calls = []

      def _fake_call_api(endpoint, payload, timeout=300):
          calls.append((endpoint, payload))
          return {"new_leaf_id": 1, "editable_content": "earlier message text"}

      monkeypatch.setattr(hive, "call_api", _fake_call_api)
      result = hive._cmd_branch("session-uuid", 5)

      assert calls == [("/sessions/session-uuid/branch", {"message_id": 5})]
      assert result == "earlier message text"


  def test_cmd_branch_prints_a_warning_on_404_and_returns_none(load_cli_hive, monkeypatch, capsys):
      import urllib.error
      hive = load_cli_hive()

      def _fake_call_api(endpoint, payload, timeout=300):
          raise urllib.error.HTTPError(endpoint, 404, "not found", None, None)

      monkeypatch.setattr(hive, "call_api", _fake_call_api)
      result = hive._cmd_branch("session-uuid", 999)

      assert result is None
      assert "not found" in capsys.readouterr().out.lower()


  def test_cmd_tree_lists_messages_and_lets_user_pick(load_cli_hive, monkeypatch):
      hive = load_cli_hive()
      fake_tree = {
          "messages": [
              {"id": 1, "parent_message_id": None, "role": "user", "content": "root message here", "created_at": None, "depth": 0},
              {"id": 2, "parent_message_id": 1, "role": "assistant", "content": "reply message here", "created_at": None, "depth": 1},
          ]
      }
      monkeypatch.setattr(hive, "call_get", lambda endpoint, params=None, timeout=30: fake_tree)
      monkeypatch.setattr(hive, "_arrow_select", lambda options, hints=None: 0)  # picks the first entry
      monkeypatch.setattr(hive, "call_api", lambda endpoint, payload, timeout=300: {"new_leaf_id": None, "editable_content": "root message here"})

      result = hive._cmd_tree("session-uuid")

      assert result == "root message here"


  def test_cmd_tree_returns_none_when_cancelled(load_cli_hive, monkeypatch):
      hive = load_cli_hive()
      fake_tree = {"messages": [{"id": 1, "parent_message_id": None, "role": "user", "content": "x", "created_at": None, "depth": 0}]}
      monkeypatch.setattr(hive, "call_get", lambda endpoint, params=None, timeout=30: fake_tree)
      monkeypatch.setattr(hive, "_arrow_select", lambda options, hints=None: -1)  # Esc/cancel

      result = hive._cmd_tree("session-uuid")

      assert result is None


  def test_cmd_fork_oneshot_prints_new_session_id(load_cli_hive, monkeypatch, capsys):
      hive = load_cli_hive()
      monkeypatch.setattr(hive, "call_api", lambda endpoint, payload, timeout=300: {"session_id": "new-uuid-123"})

      hive._cmd_fork_oneshot("session-uuid", "forked title", "ekam")

      assert "new-uuid-123" in capsys.readouterr().out
  ```

- [ ] **Step 3: Run tests to verify they fail**

  Run: `python -m pytest tests/test_cli_hive_tree.py -v`
  Expected: FAIL with `AttributeError` (none of `_cmd_branch`/`_cmd_tree`/`_cmd_fork_oneshot` exist yet)

- [ ] **Step 4: Add the 3 command functions to `cli/hive`**

  Add near the other `_cmd_*` functions (e.g. right after `_cmd_delete`, which ends at `cli/hive:1116`):

  ```python
  def _cmd_branch(session_id: str | None, message_id: int) -> str | None:
      """Rewind the session's leaf to `message_id`'s parent; return that message's
      content so the caller can prefill it in the input buffer for edit-and-resubmit."""
      if not session_id:
          print(yellow("  no active session"))
          return None
      try:
          data = call_api(f"/sessions/{session_id}/branch", {"message_id": message_id})
          return data.get("editable_content")
      except urllib.error.HTTPError as e:
          if e.code == 404:
              print(yellow(f"  message {message_id} not found in this session"))
          else:
              print(red(f"  error {e.code}: {e.read().decode()}"))
          return None
      except Exception as e:
          print(red(f"  error: {e}"))
          return None


  def _cmd_tree(session_id: str | None) -> str | None:
      """Interactive branch-point picker (MVP: a flat depth-indented list, not a
      rendered diagram -- see the plan's Global Constraints). Returns the picked
      message's content for edit-and-resubmit, or None if cancelled/empty."""
      if not session_id:
          print(yellow("  no active session"))
          return None
      try:
          data = call_get(f"/sessions/{session_id}/tree")
      except Exception as e:
          print(red(f"  error fetching tree: {e}"))
          return None

      messages = data.get("messages", [])
      if not messages:
          print(dim("  no messages in this session yet"))
          return None

      options = [f"{'  ' * m['depth']}[{m['id']}] {m['content'][:60]}" for m in messages]
      hints   = [m["role"] for m in messages]
      idx = _arrow_select(options, hints)
      if idx < 0:
          return None
      return _cmd_branch(session_id, messages[idx]["id"])


  def _cmd_fork_oneshot(session_id: str, title: str, project_id: str) -> None:
      """--fork <session>: copy the session's current branch into a new,
      independent session and print its id (mirrors --list-sessions' one-shot style)."""
      try:
          data = call_api(f"/sessions/{session_id}/fork", {"title": title, "project_id": project_id})
          new_id = data.get("session_id", "")
          print(green(f"  forked: {new_id}"))
          print(dim(f"  resume: hive --session {new_id}"))
      except urllib.error.HTTPError as e:
          if e.code == 404:
              print(yellow(f"  session {session_id[:8]} has no messages to fork"))
          else:
              print(red(f"  error {e.code}: {e.read().decode()}"))
      except Exception as e:
          print(red(f"  error: {e}"))
  ```

- [ ] **Step 5: Run tests to verify they pass**

  Run: `python -m pytest tests/test_cli_hive_tree.py -v`
  Expected: PASS (5 tests)

- [ ] **Step 6: Wire `/tree` and `/branch` into the REPL slash-command dispatch**

  In `repl()`'s slash-command `if/elif` chain (`cli/hive:1516-1579`), add two new branches — insert them right after the existing `elif cmd == "/history":` branch (`cli/hive:1529-1530`):

  ```python
              elif cmd == "/history":
                  _cmd_history(session_id)
              elif cmd == "/tree":
                  editable = _cmd_tree(session_id)
                  if editable is not None:
                      task = editable  # falls through to "Run task" below with this prefilled
                      # (handled by NOT calling `continue` here -- see Step 7's note)
              elif cmd == "/branch":
                  if not args.strip().isdigit():
                      print(dim("  usage: /branch <message-id>"))
                      continue
                  editable = _cmd_branch(session_id, int(args.strip()))
                  if editable is not None:
                      task = editable
  ```

  **This requires one structural change**: every other slash-command branch ends the loop iteration with `continue` (`cli/hive:1579`, the single `continue` after the whole `if/elif` chain). `/tree` and `/branch` are different — on success they must fall through to the existing "Run task" block below (`cli/hive:1598-1614`) using the picked message's content as the new `task`, not skip it. Change the single trailing `continue` at `cli/hive:1579` to be conditional:

  ```python
              else:
                  print(dim(f"  unknown command: {cmd}  ·  /new /sessions /history /persist /delete /diff /cleanup /plan /review /mcp /confirm /reject /tree /branch /exit"))
              if cmd not in ("/tree", "/branch") or task == parts[0]:
                  # parts[0] is the original slash command itself; task is only
                  # reassigned to editable content on a successful /tree or /branch,
                  # so `task == parts[0]` means "cancelled or nothing to resubmit"
                  continue
  ```

  Also update the REPL banner's command list at `cli/hive:1503`:

  ```python
      print(dim("  /new  /sessions  /history  /persist  /delete <id>  /delete-all  /diff  /cleanup  /plan  /review  /mcp  /tree  /branch <id>  /exit  ·  Ctrl+C to interrupt\n"))
  ```

- [ ] **Step 7: Wire `--fork` into `main()`'s argument parsing**

  Add a new argument next to `--session` at `cli/hive:1644`:

  ```python
      parser.add_argument("--fork",     metavar="SESSION_ID",
                          help="copy a session's current branch into a new, independent session and exit")
  ```

  Add a new early-exit branch mirroring `--list-sessions`'s pattern (`cli/hive:1715-1717`), inserted right after it:

  ```python
      if args.list_sessions:
          _cmd_list_sessions_oneshot(project_id)
          return

      if args.fork:
          title = " ".join(args.task) if args.task else f"forked from {args.fork[:8]}"
          _cmd_fork_oneshot(args.fork, title, project_id)
          return
  ```

- [ ] **Step 8: Run the full CLI test suite plus the full repo suite**

  Run: `python -m pytest tests/test_cli_hive_tree.py tests/test_cli_hive_tool_events.py -v` (the second file only exists if the SSE plan ran first — omit it from this command if not)
  Expected: PASS

  Run: `python -m pytest tests/ -q --deselect tests/test_bootstrap.py::test_load_from_session_discovers_patterns --deselect tests/test_bootstrap.py::test_load_from_session_skips_failed_file_reads`
  Expected: PASS

- [ ] **Step 9: Commit**

  ```bash
  git add cli/hive tests/test_cli_hive_tree.py tests/conftest.py
  git commit -m "feat(cli): add /tree, /branch <id> REPL commands and --fork flag

  /tree is an MVP flat depth-indented picker (not a rendered diagram) built
  on the existing _arrow_select raw-terminal infra. Both /tree and /branch
  rewind the session leaf to the picked message's PARENT and prefill its
  content for edit-and-resubmit, matching pi's confirmed UX. --fork copies
  the current branch into a new, independent session."
  ```

---

## Verification (manual, after deployment)

1. Deploy: `git push`, then on ZGX: `cd ~/agno-hive && git pull && systemctl --user restart agno-api.service` (this restarts the API with the new schema-ensuring code, which runs `_ensure_tables`'s additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on next connection — no separate migration step needed for the schema itself).
2. Run the backfill against the real ZGX database as a deliberate, separate step (not automated by this plan): `ssh` or `mcp__zgx-workstation__execute_command` into the agno-hive environment, `python -m scripts.backfill_session_tree` (dry run first, confirm the printed count looks right), then `python -m scripts.backfill_session_tree --live`.
3. `hive` (REPL) → send a task → `/tree` → confirm the picker shows the conversation so far → pick the first user message → confirm the prompt is prefilled with that message's text → edit it slightly and press Enter → confirm a NEW reply comes back (a sibling branch), and the ORIGINAL reply is still recoverable via `/tree` (not deleted).
4. `hive --fork <session-id>` → confirm a new session id prints → `hive --session <new-id>` → `/history` → confirm the forked session's history matches the original's branch at fork time.
