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
    # existed before this change (CREATE TABLE IF NOT EXISTS is a no-op there).
    await conn.execute("""
        ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS current_leaf_id INT
    """)
    await conn.execute("""
        ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS parent_message_id INT
            REFERENCES session_messages(id) ON DELETE SET NULL
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


async def append_message(
    session_id: str, role: str, content: str, parent_message_id: int | None = None
) -> int | None:
    """Append a message, chaining it onto the tree, and advance the session's
    current leaf. Returns the new message's id, or None on failure.

    If parent_message_id is omitted, chains onto the session's current
    current_leaf_id (NULL for a brand-new session, becoming a root message) --
    this reproduces the previous linear behavior exactly when branching is never
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
    a /tree picker's flat depth-indented display (not every branch's exact
    diagram shape -- an MVP, not a rendered tree)."""
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
    independent session -- distinct from in-place /tree branching, which stays in
    the same session and only diverges from a point. Returns the new session id,
    or None on failure."""
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
    """Return (summary_or_empty, recent_messages) for coordinator injection.
    Branch-aware: walks from the session's current_leaf_id, not a flat
    ORDER BY created_at scan -- see get_branch_history."""
    session = await get_session(session_id)
    if not session:
        return "", []
    messages = await get_branch_history(session_id, limit=config.session_window)
    summary = session.get("summary") or ""
    return summary, messages


async def save_handoff_summary(session_id: str, summary: str) -> None:
    """Store a chain-boundary handoff summary in the session's summary column.

    Called fire-and-forget after each successful run so the next chained call
    receives a compact structured digest instead of full message history.
    """
    try:
        async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
            await _ensure_tables(conn)
            await conn.execute(
                """
                UPDATE chat_sessions
                SET summary = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (summary, session_id),
            )
            await conn.commit()
    except Exception as exc:
        print(f"[sessions] save_handoff_summary warning: {exc}")


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
                json={"model": config.router_model, "prompt": prompt, "stream": False},
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
