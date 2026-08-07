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
