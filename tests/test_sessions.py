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
    """get_context now walks the tree via get_branch_history, not a flat scan --
    2 connections: get_session's own lookup, then get_branch_history's SINGLE
    connection, which reuses the same cursor for both its current_leaf_id lookup
    (fetchone) and its recursive CTE walk (fetchall, newest-first -- reversed by
    the function itself before returning)."""
    from swarm.sessions import get_context
    session_row = (
        "uuid", "proj", "title",
        None, None,          # created_at, updated_at
        None, False,         # expires_at, persist
        "Prior summary",     # summary
        5,                   # summary_through
        4,                   # message_count
    )
    leaf_row = (42,)
    branch_rows_newest_first = [("assistant", "a1"), ("user", "q1")]

    call_count = 0

    def _make_dynamic_conn():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            cursor = _make_cursor()
            cursor.fetchone = AsyncMock(return_value=session_row)
            return _make_conn(cursor)
        else:
            cursor = _make_cursor(rows=branch_rows_newest_first)
            cursor.fetchone = AsyncMock(return_value=leaf_row)
            return _make_conn(cursor)

    async def _connect(*args, **kwargs):
        return _make_dynamic_conn()

    with patch("swarm.sessions.psycopg.AsyncConnection.connect", side_effect=_connect):
        summary, messages = await get_context("session-uuid")

    assert summary == "Prior summary"
    assert messages == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]


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


@pytest.mark.asyncio
async def test_append_message_returns_none_on_error():
    from swarm.sessions import append_message
    async def _bad_connect(*args, **kwargs):
        raise RuntimeError("db down")
    with patch("swarm.sessions.psycopg.AsyncConnection.connect", side_effect=_bad_connect):
        result = await append_message("session-uuid", "user", "hi")
    assert result is None


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
async def test_get_branch_history_returns_empty_when_session_has_no_leaf():
    from swarm.sessions import get_branch_history
    cursor = _make_cursor()
    cursor.fetchone = AsyncMock(return_value=None)  # no current_leaf_id row
    conn = _make_conn(cursor)
    with _patch_connect(conn):
        result = await get_branch_history("session-uuid")
    assert result == []


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


@pytest.mark.asyncio
async def test_set_current_leaf_returns_false_when_session_not_found():
    from swarm.sessions import set_current_leaf
    cursor = _make_cursor(rowcount=0)
    conn = _make_conn(cursor)
    with _patch_connect(conn):
        result = await set_current_leaf("nonexistent", 17)
    assert result is False


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


@pytest.mark.asyncio
async def test_list_session_tree_returns_empty_on_error():
    from swarm.sessions import list_session_tree
    async def _bad_connect(*args, **kwargs):
        raise RuntimeError("db down")
    with patch("swarm.sessions.psycopg.AsyncConnection.connect", side_effect=_bad_connect):
        result = await list_session_tree("session-uuid")
    assert result == []


# ── fork_session ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fork_session_copies_the_branch_into_a_new_session():
    from swarm import sessions
    branch = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    with patch("swarm.sessions.get_branch_history", new=AsyncMock(return_value=branch)), \
         patch("swarm.sessions.create_session", new=AsyncMock(return_value="new-session-id")) as mock_create, \
         patch("swarm.sessions.append_message", new=AsyncMock(side_effect=[10, 11])) as mock_append:
        new_id = await sessions.fork_session("source-session-id", "ekam", "forked task")

    assert new_id == "new-session-id"
    mock_create.assert_awaited_once_with("ekam", "forked task", persist=False)
    assert mock_append.await_count == 2
    # second append's parent_message_id is the first append's returned id (10) -- chained, not orphaned
    second_call_kwargs = mock_append.await_args_list[1].kwargs
    assert second_call_kwargs["parent_message_id"] == 10


@pytest.mark.asyncio
async def test_fork_session_returns_none_when_source_has_no_messages():
    from swarm import sessions
    with patch("swarm.sessions.get_branch_history", new=AsyncMock(return_value=[])):
        result = await sessions.fork_session("source-session-id", "ekam", "forked task")
    assert result is None


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
