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
                json={"model": "qwen3:8b", "prompt": prompt, "stream": False},
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
