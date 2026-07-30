"""Self-improving loop — records task outcomes and loads past context.

Success path  → task outcome inserted into an ISOLATED LightRAG "experience"
                namespace ({project_id}_experience), NOT the project's code
                namespace. This prevents past task Q&A (which can contain
                draft/incorrect specifics) from being retrieved by, and
                poisoning, code-grounding queries on the project namespace.
Failure path  → structured record in PostgreSQL failure_log table
Context load  → queries failure_log before each task, injected into coordinator instructions
"""
import asyncio
from datetime import datetime


# Suffix for the isolated experience-replay namespace. Task outcomes live here
# so code-grounding queries on `project_id` only ever see indexed source files.
EXPERIENCE_SUFFIX = "_experience"


def experience_namespace(project_id: str) -> str:
    return f"{project_id}{EXPERIENCE_SUFFIX}"


# ── Success ───────────────────────────────────────────────────────────────────

# Minimum result length — short/boilerplate outcomes don't have enough content
# for LightRAG entity extraction and land as failed doc_status rows.
_MIN_RESULT_LENGTH = 120


async def record_success(task: str, result: str, project_id: str) -> None:
    """Insert a successful task outcome into the isolated experience namespace."""
    try:
        # Skip indexing when the result is too short to yield useful entities.
        if len(result.strip()) < _MIN_RESULT_LENGTH:
            print(f"[feedback] skipping short outcome ({len(result)} chars) — not worth indexing")
            return
        from lightrag_mcp.rag import get_rag
        # Isolated namespace — never the project's code namespace (grounding poison).
        rag = get_rag(experience_namespace(project_id))
        await rag.initialize_storages()
        # Timestamp makes each submission unique so identical task/result pairs
        # don't collide on the same LightRAG content hash (which causes the
        # "Duplicate document detected" warning on every re-run).
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        text = (
            f"Past task outcome (SUCCESS) [{ts}]\n"
            f"Project: {project_id}\n"
            f"Task: {task}\n"
            f"Result:\n{result[:1500]}"
        )
        # Tag with a real file_path so these are identifiable (never "unknown_source").
        await rag.ainsert(text, file_paths="task_outcome")
    except Exception as exc:
        print(f"[feedback] record_success warning: {exc}")


# Track in-flight fire-and-forget feedback tasks so a graceful shutdown can await them
# (otherwise an outcome could be dropped if the process exits right after returning a result).
_bg_tasks: set[asyncio.Task] = set()


def record_success_bg(task: str, result: str, project_id: str) -> None:
    """Fire-and-forget record_success — schedules the experience-namespace indexing as a
    background task so the /run response is NOT blocked by post-run LightRAG extraction.
    The outcome is still recorded (same namespace, same data); it just no longer pads
    wall-clock. This is the AUTO post-run recording only — the explicit /feedback endpoint
    still awaits record_success directly. Tasks are tracked for drain_background_tasks()."""
    t = asyncio.create_task(record_success(task, result, project_id))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def drain_background_tasks(timeout: float = 30.0) -> None:
    """Await any in-flight background feedback tasks. Call on graceful shutdown so no
    outcome is dropped if the process exits right after returning a result."""
    if not _bg_tasks:
        return
    pending = list(_bg_tasks)
    print(f"[feedback] draining {len(pending)} background feedback task(s)…")
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[feedback] drain timed out with {len(_bg_tasks)} task(s) still pending")


# ── Failure ───────────────────────────────────────────────────────────────────

async def record_failure(
    task: str,
    error: str,
    project_id: str,
    agent: str = "unknown",
    rejected_output: str | None = None,
    corrected_output: str | None = None,
) -> None:
    """Write a failure record to the PostgreSQL failure_log table.

    `rejected_output` / `corrected_output` are optional and exist to make the row
    usable as a DPO/ORPO preference pair: (task, rejected_output, corrected_output).
    Runtime failures leave them None — only human `/feedback` supplies them.
    They are NOT truncated as aggressively as error_message: a training pair needs
    the full text on both sides to be usable.
    """
    try:
        import psycopg
        from config.config import config

        error_type = type(error).__name__ if not isinstance(error, str) else "RuntimeError"
        error_msg = str(error)[:500]

        async with await psycopg.AsyncConnection.connect(config.postgres_uri) as conn:
            await _ensure_table(conn)
            await conn.execute(
                """
                INSERT INTO failure_log
                    (project_id, task, error_type, error_message, agent,
                     rejected_output, corrected_output)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_id, task[:300], error_type, error_msg, agent,
                    (rejected_output or None), (corrected_output or None),
                ),
            )
            await conn.commit()
    except Exception as exc:
        print(f"[feedback] record_failure warning: {exc}")


# ── Context loader ────────────────────────────────────────────────────────────

async def load_failure_context(project_id: str, limit: int | None = None) -> str:
    """Return a formatted string of recent failures to inject into coordinator instructions.

    `limit` defaults to config.failure_context_limit (env AGNO_FAILURE_CONTEXT_LIMIT,
    default 10). Pass an explicit int to override per call.
    """
    try:
        import psycopg
        from config.config import config

        if limit is None:
            limit = config.failure_context_limit

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

        lines = ["── Past failures (USER FEEDBACK) — read every point before writing code ──"]
        for task_text, err_type, err_msg in failures:
            lines.append(f"  Task:  {task_text[:300]}")
            lines.append(f"  Correction: {err_msg[:800]}")
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
    # Preference-pair capture (training Phase 1). DPO/ORPO needs a
    # (prompt, rejected, chosen) triple; `task` is the prompt and `error_message`
    # is the human explanation, but the model's ACTUAL bad output was never stored
    # — without it a correction cannot be turned into a training pair.
    # Added as nullable columns so every existing row stays valid.
    await conn.execute(
        "ALTER TABLE failure_log ADD COLUMN IF NOT EXISTS rejected_output TEXT"
    )
    await conn.execute(
        "ALTER TABLE failure_log ADD COLUMN IF NOT EXISTS corrected_output TEXT"
    )
    await conn.commit()
