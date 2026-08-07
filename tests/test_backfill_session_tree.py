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
