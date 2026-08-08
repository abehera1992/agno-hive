"""Session persistence — chat history stored via SQLAlchemy (swarm/db.py), SQLite
by default or Postgres/anything else per config.database_url (AGNOHive 2.3.2
addendum, 2026-08-08 — was raw psycopg/Postgres-only before this).

Tables are auto-created on first use (same pattern as feedback.py).
All functions fail silently so a DB outage never blocks task runs.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, literal, select, update

from config.config import config
from swarm import db

chat_sessions = db.chat_sessions
session_messages = db.session_messages


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
        await db.ensure_schema()
        async with db.get_engine().begin() as conn:
            await conn.execute(
                chat_sessions.insert().values(
                    id=session_id, project_id=project_id, title=title[:80],
                    expires_at=expires_at, persist=persist,
                )
            )
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
        async with db.get_engine().begin() as conn:
            if parent_message_id is None:
                row = (
                    await conn.execute(
                        select(chat_sessions.c.current_leaf_id).where(chat_sessions.c.id == session_id)
                    )
                ).first()
                parent_message_id = row[0] if row else None

            result = await conn.execute(
                session_messages.insert().values(
                    session_id=session_id, role=role, content=content,
                    parent_message_id=parent_message_id,
                )
            )
            new_id = result.inserted_primary_key[0] if result.inserted_primary_key else None

            await conn.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == session_id)
                .values(updated_at=func.now(), current_leaf_id=new_id)
            )
            return new_id
    except Exception as exc:
        print(f"[sessions] append_message warning: {exc}")
        return None


async def get_history(session_id: str, limit: int | None = None) -> list[dict]:
    """Return last `limit` messages ordered oldest-first."""
    if limit is None:
        limit = config.session_window
    try:
        async with db.get_engine().begin() as conn:
            # id is a secondary sort key, not just created_at -- SQLite's
            # CURRENT_TIMESTAMP (what func.now() compiles to there) only has
            # second resolution, so two messages appended within the same second
            # would otherwise sort in an unstable/arbitrary order. id (an
            # autoincrement PK) always reflects true insertion order as a tiebreak,
            # on both SQLite and Postgres.
            sub = (
                select(
                    session_messages.c.id, session_messages.c.role,
                    session_messages.c.content, session_messages.c.created_at,
                )
                .where(session_messages.c.session_id == session_id)
                .order_by(session_messages.c.created_at.desc(), session_messages.c.id.desc())
                .limit(limit)
                .subquery()
            )
            stmt = select(sub.c.role, sub.c.content).order_by(sub.c.created_at.asc(), sub.c.id.asc())
            rows = (await conn.execute(stmt)).mappings().all()
            return [{"role": r["role"], "content": r["content"]} for r in rows]
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
        async with db.get_engine().begin() as conn:
            if leaf_id is None:
                row = (
                    await conn.execute(
                        select(chat_sessions.c.current_leaf_id).where(chat_sessions.c.id == session_id)
                    )
                ).first()
                leaf_id = row[0] if row else None
                if leaf_id is None:
                    return []

            sm = session_messages
            base = (
                select(sm.c.id, sm.c.role, sm.c.content, sm.c.parent_message_id, literal(0).label("depth"))
                .where(sm.c.id == leaf_id)
                .cte(name="branch", recursive=True)
            )
            sm2 = sm.alias("sm2")
            recursive = select(
                sm2.c.id, sm2.c.role, sm2.c.content, sm2.c.parent_message_id, (base.c.depth + 1).label("depth"),
            ).select_from(sm2.join(base, sm2.c.id == base.c.parent_message_id))
            branch_cte = base.union_all(recursive)

            stmt = select(branch_cte.c.role, branch_cte.c.content).order_by(branch_cte.c.depth.asc()).limit(limit)
            rows = (await conn.execute(stmt)).mappings().all()
            newest_first = [{"role": r["role"], "content": r["content"]} for r in rows]
            return list(reversed(newest_first))
    except Exception as exc:
        print(f"[sessions] get_branch_history warning: {exc}")
        return []


async def set_current_leaf(session_id: str, message_id: int) -> bool:
    """Rewind/advance the session's active branch tip. Used by /branch to rewind
    to an earlier message's parent before the user resubmits a sibling branch."""
    try:
        async with db.get_engine().begin() as conn:
            result = await conn.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == session_id)
                .values(current_leaf_id=message_id, updated_at=func.now())
            )
            return result.rowcount > 0
    except Exception as exc:
        print(f"[sessions] set_current_leaf warning: {exc}")
        return False


async def list_session_tree(session_id: str) -> list[dict]:
    """Every message in the session, depth-from-root computed server-side, for
    a /tree picker's flat depth-indented display (not every branch's exact
    diagram shape -- an MVP, not a rendered tree)."""
    try:
        async with db.get_engine().begin() as conn:
            sm = session_messages
            base = (
                select(
                    sm.c.id, sm.c.parent_message_id, sm.c.role, sm.c.content, sm.c.created_at,
                    literal(0).label("depth"),
                )
                .where(sm.c.session_id == session_id, sm.c.parent_message_id.is_(None))
                .cte(name="tree", recursive=True)
            )
            sm2 = sm.alias("sm2")
            recursive = (
                select(
                    sm2.c.id, sm2.c.parent_message_id, sm2.c.role, sm2.c.content, sm2.c.created_at,
                    (base.c.depth + 1).label("depth"),
                )
                .select_from(sm2.join(base, sm2.c.parent_message_id == base.c.id))
                .where(sm2.c.session_id == session_id)
            )
            tree_cte = base.union_all(recursive)

            # id as a secondary sort key -- see get_history's comment on why
            # created_at alone is not a reliable tiebreak on SQLite.
            stmt = select(
                tree_cte.c.id, tree_cte.c.parent_message_id, tree_cte.c.role,
                tree_cte.c.content, tree_cte.c.created_at, tree_cte.c.depth,
            ).order_by(tree_cte.c.created_at.asc(), tree_cte.c.id.asc())
            rows = (await conn.execute(stmt)).mappings().all()
            return [
                {
                    "id": r["id"], "parent_message_id": r["parent_message_id"], "role": r["role"],
                    "content": r["content"], "created_at": r["created_at"], "depth": r["depth"],
                }
                for r in rows
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
        await db.ensure_schema()
        async with db.get_engine().begin() as conn:
            cs, sm = chat_sessions, session_messages
            stmt = (
                select(
                    cs.c.id, cs.c.project_id, cs.c.title, cs.c.created_at, cs.c.updated_at,
                    cs.c.expires_at, cs.c.persist, cs.c.summary, cs.c.summary_through,
                    func.count(sm.c.id).label("message_count"),
                )
                .select_from(cs.outerjoin(sm, sm.c.session_id == cs.c.id))
                .where(cs.c.id == session_id)
                .group_by(
                    cs.c.id, cs.c.project_id, cs.c.title, cs.c.created_at, cs.c.updated_at,
                    cs.c.expires_at, cs.c.persist, cs.c.summary, cs.c.summary_through,
                )
            )
            row = (await conn.execute(stmt)).mappings().first()
            if not row:
                return None
            return {
                "id": str(row["id"]),
                "project_id": row["project_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
                "persist": row["persist"],
                "summary": row["summary"],
                "summary_through": row["summary_through"],
                "message_count": row["message_count"],
            }
    except Exception as exc:
        print(f"[sessions] get_session warning: {exc}")
        return None


async def list_sessions(project_id: str, limit: int = 20) -> list[dict]:
    """Return session summaries for a project, most-recently-updated first."""
    try:
        await db.ensure_schema()
        async with db.get_engine().begin() as conn:
            cs, sm = chat_sessions, session_messages
            stmt = (
                select(
                    cs.c.id, cs.c.title, cs.c.created_at, cs.c.updated_at,
                    cs.c.expires_at, cs.c.persist,
                    func.count(sm.c.id).label("message_count"),
                )
                .select_from(cs.outerjoin(sm, sm.c.session_id == cs.c.id))
                .where(cs.c.project_id == project_id)
                .group_by(cs.c.id, cs.c.title, cs.c.created_at, cs.c.updated_at, cs.c.expires_at, cs.c.persist)
                .order_by(cs.c.updated_at.desc())
                .limit(limit)
            )
            rows = (await conn.execute(stmt)).mappings().all()
            return [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "expires_at": r["expires_at"],
                    "persist": r["persist"],
                    "message_count": r["message_count"],
                }
                for r in rows
            ]
    except Exception as exc:
        print(f"[sessions] list_sessions warning: {exc}")
        return []


async def delete_session(session_id: str) -> bool:
    """Hard-delete a session and its messages. Returns True if a row was deleted."""
    try:
        async with db.get_engine().begin() as conn:
            result = await conn.execute(delete(chat_sessions).where(chat_sessions.c.id == session_id))
            return result.rowcount > 0
    except Exception as exc:
        print(f"[sessions] delete_session warning: {exc}")
        return False


async def persist_session(session_id: str) -> bool:
    """Mark a session as permanent (clears expires_at). Returns True if updated."""
    try:
        async with db.get_engine().begin() as conn:
            result = await conn.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == session_id)
                .values(persist=True, expires_at=None, updated_at=func.now())
            )
            return result.rowcount > 0
    except Exception as exc:
        print(f"[sessions] persist_session warning: {exc}")
        return False


async def _cleanup_expired() -> int:
    """Delete expired non-persisted sessions. Returns count deleted."""
    try:
        await db.ensure_schema()
        async with db.get_engine().begin() as conn:
            result = await conn.execute(
                delete(chat_sessions).where(chat_sessions.c.expires_at < func.now(), chat_sessions.c.persist.is_(False))
            )
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
        await db.ensure_schema()
        async with db.get_engine().begin() as conn:
            await conn.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == session_id)
                .values(summary=summary, updated_at=func.now())
            )
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

        async with db.get_engine().begin() as conn:
            stmt = (
                select(session_messages.c.id, session_messages.c.role, session_messages.c.content)
                .where(session_messages.c.session_id == session_id)
                .order_by(session_messages.c.created_at.asc())
            )
            all_messages = [(r[0], r[1], r[2]) for r in (await conn.execute(stmt)).all()]

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

        async with db.get_engine().begin() as conn:
            await conn.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == session_id)
                .values(summary=summary, summary_through=last_id_covered, updated_at=func.now())
            )
    except Exception as exc:
        print(f"[sessions] compact_session warning: {exc}")
