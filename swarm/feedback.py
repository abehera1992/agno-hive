"""Self-improving loop — records task outcomes and loads past context.

Success path  → lightweight text inserted into LightRAG (agents can retrieve via memory_search)
Failure path  → structured record in PostgreSQL failure_log table
Context load  → queries failure_log before each task, injected into coordinator instructions
"""
import asyncio


# ── Success ───────────────────────────────────────────────────────────────────

async def record_success(task: str, result: str, project_id: str) -> None:
    """Insert a successful task outcome into LightRAG for future retrieval."""
    try:
        from lightrag_mcp.rag import get_rag
        rag = get_rag(project_id)
        text = (
            f"Past task outcome (SUCCESS)\n"
            f"Project: {project_id}\n"
            f"Task: {task}\n"
            f"Result:\n{result[:1500]}"
        )
        await rag.ainsert(text)
    except Exception as exc:
        print(f"[feedback] record_success warning: {exc}")


# ── Failure ───────────────────────────────────────────────────────────────────

async def record_failure(task: str, error: str, project_id: str, agent: str = "unknown") -> None:
    """Write a failure record to the PostgreSQL failure_log table."""
    try:
        import psycopg
        from config.config import config

        error_type = type(error).__name__ if not isinstance(error, str) else "RuntimeError"
        error_msg = str(error)[:500]

        async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
            await _ensure_table(conn)
            await conn.execute(
                """
                INSERT INTO failure_log (project_id, task, error_type, error_message, agent)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (project_id, task[:300], error_type, error_msg, agent),
            )
            await conn.commit()
    except Exception as exc:
        print(f"[feedback] record_failure warning: {exc}")


# ── Context loader ────────────────────────────────────────────────────────────

async def load_failure_context(project_id: str, limit: int = 3) -> str:
    """Return a formatted string of recent failures to inject into coordinator instructions."""
    try:
        import psycopg
        from config.config import config

        async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
            await _ensure_table(conn)
            rows = await conn.execute(
                """
                SELECT task, error_type, error_message
                FROM failure_log
                WHERE project_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (project_id, limit),
            )
            failures = await rows.fetchall()

        if not failures:
            return ""

        lines = ["── Past failures — avoid repeating these mistakes ──────────"]
        for task_text, err_type, err_msg in failures:
            lines.append(f"  Task:  {task_text[:120]}")
            lines.append(f"  Error: {err_type}: {err_msg[:120]}")
            lines.append("")
        return "\n".join(lines)
    except Exception as exc:
        print(f"[feedback] load_failure_context warning: {exc}")
        return ""


# ── Schema bootstrap ──────────────────────────────────────────────────────────

async def _ensure_table(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS failure_log (
            id          SERIAL PRIMARY KEY,
            project_id  TEXT        NOT NULL,
            task        TEXT        NOT NULL,
            error_type  TEXT        NOT NULL DEFAULT 'unknown',
            error_message TEXT      NOT NULL DEFAULT '',
            agent       TEXT        NOT NULL DEFAULT 'unknown',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS failure_log_project_idx ON failure_log (project_id, created_at DESC)"
    )
    await conn.commit()
