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
import re
from datetime import datetime

# Generic dev-speak that would make every task look related to every other task
# if treated as a relevance signal ("add", "file", "class", "correct"...). Kept
# deliberately short -- this only needs to strip words common enough to appear in
# nearly every task/correction pair regardless of topic; anything more specific
# than this list is exactly the kind of signal _significant_tokens wants to keep.
_STOPWORDS = {
    "the", "a", "an", "to", "for", "and", "or", "of", "in", "on", "with", "this",
    "that", "is", "are", "was", "were", "add", "added", "adding", "class", "file",
    "update", "updated", "create", "created", "new", "code", "change", "changed",
    "fix", "fixed", "task", "read", "check", "propose", "using", "used", "use",
    "here", "elsewhere", "correct", "rule", "existing", "not", "does", "real",
    "already", "value", "values", "name", "names", "content", "line", "call",
    "calls", "called", "make", "made", "need", "needs", "needed", "also",
}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{2,}")


def _significant_tokens(text: str) -> set[str]:
    """Path-like/module-like tokens worth matching a past failure against a new
    task on: filenames, module names, identifiers -- not generic English words.
    A token counts as significant if it contains a path separator, a dot
    (extension or module-ish), an underscore, or is simply long enough (>=6
    chars) to plausibly be a real identifier rather than common vocabulary this
    short stopword list happened to miss.

    Also adds the BASENAME of any path-like token (the part after the last "/")
    as its own entry -- a full path mentioned in one text ("API/inventory-
    service/router/vouchers_api.py") and a bare filename mentioned in another
    ("vouchers_api.py") clearly refer to the same file, but would never overlap
    as exact set members without this: the greedy token regex swallows the whole
    path into ONE token, distinct from the bare filename string. Confirmed live
    2026-08-06: this exact mismatch made a genuinely relevant failure (a wrong-
    file citation for vouchers_api.py) invisible to a task that named the file by
    its full repo-relative path."""
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(text.lower()):
        tok = m.group(0).strip(".-/")
        if not tok or tok in _STOPWORDS:
            continue
        if "/" in tok:
            basename = tok.rsplit("/", 1)[-1]
            if basename and basename not in _STOPWORDS:
                out.add(basename)
        if "/" in tok or "." in tok or "_" in tok or len(tok) >= 6:
            out.add(tok)
    return out


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


def _filter_relevant_failures(
    current_task: str,
    failures: list[tuple[str, str, str]],
    limit: int,
) -> list[tuple[str, str, str]]:
    """Rank `failures` (task, error_type, error_message rows, already ordered
    newest-first) by shared significant tokens with `current_task`, keep only
    ones with at least one shared token, and return the top `limit`.

    Ties broken by recency (the row's original position in `failures`) since
    the DB query that produced it is already newest-first. When `current_task`
    is empty, returns the first `limit` rows unfiltered -- the pre-existing
    recency-only behaviour, kept as the fallback when there's no task text to
    score against rather than silently returning nothing.

    Pulled out of load_failure_context as a pure function (no DB access) so the
    relevance logic itself -- the part that actually changed, and the part a
    regression would be silent and hard to notice in -- can be unit tested
    without mocking psycopg.
    """
    if not current_task:
        return failures[:limit]
    task_tokens = _significant_tokens(current_task)
    scored = []
    for i, (task_text, err_type, err_msg) in enumerate(failures):
        overlap = task_tokens & _significant_tokens(f"{task_text} {err_msg}")
        if overlap:
            scored.append((len(overlap), -i, task_text, err_type, err_msg))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [(t, et, em) for _, _, t, et, em in scored[:limit]]


# ── Context loader ────────────────────────────────────────────────────────────

async def load_failure_context(project_id: str, limit: int | None = None, current_task: str = "") -> str:
    """Return a formatted string of recent, RELEVANT failures to inject into
    coordinator instructions.

    Confirmed live 2026-08-06: this used to return the N most recent failures for
    the project with NO relevance filtering at all -- a day spent correcting one
    specific SCSS namespace bug on the parties module posted several /feedback
    corrections mentioning "statusBadge" and "parties.module.scss", and those
    corrections were then injected VERBATIM into a completely unrelated vouchers-
    module research task's coordinator instructions (labeled "read every point
    before writing code"). The coordinator, dutifully following that instruction,
    searched for "statusBadge" -- a term the vouchers task never mentioned -- found
    an unrelated real occurrence in a different module's stylesheet, and
    misattributed it to vouchers in its final answer.

    Scores each candidate failure's relevance against `current_task` by shared
    PATH-LIKE tokens (filenames, module names, identifiers) via
    _significant_tokens -- not generic English words, which would make every task
    look related to every other task. Only failures sharing at least one such
    token are injected; if none do, returns "" rather than falling back to
    "most recent regardless of topic", which is the exact behaviour that caused
    the incident above. When `current_task` is empty (no task text available to
    score against), falls back to the old recency-only behaviour instead of
    silently injecting nothing.

    `limit` defaults to config.failure_context_limit (env AGNO_FAILURE_CONTEXT_LIMIT,
    default 10). Pass an explicit int to override per call.
    """
    try:
        import psycopg
        from config.config import config

        if limit is None:
            limit = config.failure_context_limit

        # Pool wider than `limit` so relevance-ranking has something to rank --
        # fetching only `limit` rows up front (recency-first, like before) would
        # mean a genuinely relevant failure just outside the top N never gets a
        # chance to surface once irrelevant, merely-recent ones are filtered out.
        pool_size = max(limit * 5, 50)

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
                (project_id, pool_size),
            )
            failures = await rows.fetchall()

        if not failures:
            return ""

        failures = _filter_relevant_failures(current_task, failures, limit)
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
