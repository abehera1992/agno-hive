"""Unit tests for swarm/sessions.py — all psycopg I/O is mocked."""
import pytest
from unittest.mock import AsyncMock, patch


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
    """Patch psycopg.AsyncConnection.connect to return mock conn."""
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
            cursor = _make_cursor()
            cursor.fetchone = AsyncMock(return_value=session_row)
            return _make_conn(cursor)
        else:
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
