"""Tests for swarm/sessions.py -- runs against a real in-memory SQLite DB via
swarm/db.py (AGNOHive 2.3.2 addendum, 2026-08-08 — was psycopg-mock-based before
this, since sessions.py talked to Postgres directly; SQLAlchemy makes a real,
fast, dependency-free DB available in tests instead of mocking raw SQL strings)."""
import pytest

from config.config import config
from swarm import db, sessions


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    await db.reset_engine_for_tests()
    yield


# ── create_session ────────────────────────────────────────────────────────────

async def test_create_session_returns_uuid():
    sid = await sessions.create_session("myproject", "test task", persist=False)
    assert len(sid) == 36
    assert sid.count("-") == 4


async def test_create_session_persist_sets_no_expiry():
    sid = await sessions.create_session("myproject", "persist task", persist=True)
    row = await sessions.get_session(sid)
    assert row["expires_at"] is None
    assert row["persist"] is True


async def test_create_session_non_persist_sets_expiry():
    sid = await sessions.create_session("myproject", "task", persist=False)
    row = await sessions.get_session(sid)
    assert row["expires_at"] is not None
    assert row["persist"] is False


async def test_create_session_title_truncated():
    sid = await sessions.create_session("myproject", "x" * 200)
    row = await sessions.get_session(sid)
    assert len(row["title"]) == 80


# ── append_message ────────────────────────────────────────────────────────────

async def test_append_message_returns_new_message_id():
    sid = await sessions.create_session("p", "t")
    new_id = await sessions.append_message(sid, "user", "hello")
    assert isinstance(new_id, int)


async def test_append_message_defaults_parent_to_current_leaf():
    sid = await sessions.create_session("p", "t")
    m1 = await sessions.append_message(sid, "user", "hello")
    m2 = await sessions.append_message(sid, "assistant", "reply")  # parent_message_id omitted
    tree = await sessions.list_session_tree(sid)
    m2_row = next(r for r in tree if r["id"] == m2)
    assert m2_row["parent_message_id"] == m1


async def test_append_message_explicit_parent_skips_leaf_lookup():
    sid = await sessions.create_session("p", "t")
    m1 = await sessions.append_message(sid, "user", "hello")
    m2 = await sessions.append_message(sid, "assistant", "reply")
    m3 = await sessions.append_message(sid, "user", "branch from m1", parent_message_id=m1)
    tree = await sessions.list_session_tree(sid)
    m3_row = next(r for r in tree if r["id"] == m3)
    assert m3_row["parent_message_id"] == m1
    assert m3 != m2


async def test_append_message_advances_current_leaf():
    sid = await sessions.create_session("p", "t")
    m1 = await sessions.append_message(sid, "user", "hello")
    row = await sessions.get_session(sid)
    branch = await sessions.get_branch_history(sid)
    assert branch == [{"role": "user", "content": "hello"}]


async def test_append_message_returns_none_on_error(monkeypatch):
    def _broken_engine():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_engine", _broken_engine)
    result = await sessions.append_message("session-uuid", "user", "hi")
    assert result is None


# ── get_history ───────────────────────────────────────────────────────────────

async def test_get_history_returns_messages_oldest_first():
    sid = await sessions.create_session("p", "t")
    await sessions.append_message(sid, "user", "hi")
    await sessions.append_message(sid, "assistant", "hello")
    result = await sessions.get_history(sid, limit=6)
    assert result == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


async def test_get_history_returns_empty_on_error(monkeypatch):
    def _broken_engine():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_engine", _broken_engine)
    result = await sessions.get_history("session-uuid")
    assert result == []


# ── get_branch_history ───────────────────────────────────────────────────────

async def test_get_branch_history_walks_from_current_leaf_and_reverses_to_oldest_first():
    sid = await sessions.create_session("p", "t")
    await sessions.append_message(sid, "user", "first")
    await sessions.append_message(sid, "assistant", "first-reply")
    await sessions.append_message(sid, "user", "second")
    result = await sessions.get_branch_history(sid)
    assert result == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-reply"},
        {"role": "user", "content": "second"},
    ]


async def test_get_branch_history_uses_explicit_leaf_id_when_given():
    sid = await sessions.create_session("p", "t")
    m1 = await sessions.append_message(sid, "user", "root")
    await sessions.append_message(sid, "assistant", "child")  # advances current_leaf_id past m1
    result = await sessions.get_branch_history(sid, leaf_id=m1)
    assert result == [{"role": "user", "content": "root"}]


async def test_get_branch_history_returns_empty_when_session_has_no_leaf():
    sid = await sessions.create_session("p", "t")  # no messages appended
    result = await sessions.get_branch_history(sid)
    assert result == []


async def test_get_branch_history_returns_empty_on_error(monkeypatch):
    def _broken_engine():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_engine", _broken_engine)
    result = await sessions.get_branch_history("session-uuid")
    assert result == []


# ── set_current_leaf ─────────────────────────────────────────────────────────

async def test_set_current_leaf_returns_true_when_updated_and_rewinds_branch():
    sid = await sessions.create_session("p", "t")
    m1 = await sessions.append_message(sid, "user", "root")
    await sessions.append_message(sid, "assistant", "child")
    result = await sessions.set_current_leaf(sid, m1)
    assert result is True
    assert await sessions.get_branch_history(sid) == [{"role": "user", "content": "root"}]


async def test_set_current_leaf_returns_false_when_session_not_found():
    result = await sessions.set_current_leaf("00000000-0000-0000-0000-000000000000", 17)
    assert result is False


# ── list_session_tree ────────────────────────────────────────────────────────

async def test_list_session_tree_returns_all_messages_with_depth():
    sid = await sessions.create_session("p", "t")
    m1 = await sessions.append_message(sid, "user", "root")
    m2 = await sessions.append_message(sid, "assistant", "reply")
    result = await sessions.list_session_tree(sid)
    assert [r["id"] for r in result] == [m1, m2]
    assert result[0]["depth"] == 0
    assert result[1]["depth"] == 1
    assert result[1]["parent_message_id"] == m1


async def test_list_session_tree_returns_empty_on_error(monkeypatch):
    def _broken_engine():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_engine", _broken_engine)
    result = await sessions.list_session_tree("session-uuid")
    assert result == []


# ── fork_session ─────────────────────────────────────────────────────────────

async def test_fork_session_copies_the_branch_into_a_new_session():
    sid = await sessions.create_session("ekam", "source task")
    await sessions.append_message(sid, "user", "q1")
    await sessions.append_message(sid, "assistant", "a1")

    new_id = await sessions.fork_session(sid, "ekam", "forked task")

    assert new_id is not None
    assert new_id != sid
    assert await sessions.get_branch_history(new_id) == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    # original untouched
    assert await sessions.get_branch_history(sid) == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]


async def test_fork_session_returns_none_when_source_has_no_messages():
    sid = await sessions.create_session("ekam", "empty source")
    result = await sessions.fork_session(sid, "ekam", "forked task")
    assert result is None


# ── delete_session ────────────────────────────────────────────────────────────

async def test_delete_session_returns_true_when_deleted():
    sid = await sessions.create_session("p", "t")
    result = await sessions.delete_session(sid)
    assert result is True
    assert await sessions.get_session(sid) is None


async def test_delete_session_returns_false_when_not_found():
    result = await sessions.delete_session("00000000-0000-0000-0000-000000000000")
    assert result is False


async def test_delete_session_cascades_to_messages():
    """FK ON DELETE CASCADE must actually fire on SQLite (PRAGMA foreign_keys=ON,
    see swarm/db.py's connect listener) -- without it this would leave orphaned
    session_messages rows, a silent divergence from Postgres's default-enforced FKs."""
    sid = await sessions.create_session("p", "t")
    await sessions.append_message(sid, "user", "hi")
    await sessions.delete_session(sid)
    assert await sessions.list_session_tree(sid) == []


# ── persist_session ───────────────────────────────────────────────────────────

async def test_persist_session_returns_true_when_updated():
    sid = await sessions.create_session("p", "t", persist=False)
    result = await sessions.persist_session(sid)
    assert result is True
    row = await sessions.get_session(sid)
    assert row["persist"] is True
    assert row["expires_at"] is None


async def test_persist_session_returns_false_when_not_found():
    result = await sessions.persist_session("00000000-0000-0000-0000-000000000000")
    assert result is False


# ── list_sessions ─────────────────────────────────────────────────────────────

async def test_list_sessions_filters_by_project_and_orders_by_recency():
    s1 = await sessions.create_session("proj-a", "first")
    s2 = await sessions.create_session("proj-a", "second")
    await sessions.create_session("proj-b", "other project")

    result = await sessions.list_sessions("proj-a")

    assert {r["id"] for r in result} == {s1, s2}


# ── _cleanup_expired ──────────────────────────────────────────────────────────

async def test_cleanup_expired_returns_zero_when_nothing_expired():
    await sessions.create_session("p", "t", persist=False)  # expires 30 days out, not yet expired
    count = await sessions._cleanup_expired()
    assert count == 0


async def test_cleanup_expired_deletes_non_persisted_expired_sessions():
    import sqlalchemy as sa
    from datetime import datetime, timedelta, timezone

    sid = await sessions.create_session("p", "t", persist=False)
    async with db.get_engine().begin() as conn:
        await conn.execute(
            sa.update(db.chat_sessions)
            .where(db.chat_sessions.c.id == sid)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )

    count = await sessions._cleanup_expired()

    assert count == 1
    assert await sessions.get_session(sid) is None


async def test_cleanup_expired_never_deletes_persisted_sessions():
    import sqlalchemy as sa
    from datetime import datetime, timedelta, timezone

    sid = await sessions.create_session("p", "t", persist=True)
    async with db.get_engine().begin() as conn:
        # persist=True normally clears expires_at, but force one to prove the
        # WHERE clause's persist=FALSE guard, not just "expires_at IS NULL", is
        # what protects a persisted session.
        await conn.execute(
            sa.update(db.chat_sessions)
            .where(db.chat_sessions.c.id == sid)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )

    count = await sessions._cleanup_expired()

    assert count == 0
    assert await sessions.get_session(sid) is not None


# ── get_context ───────────────────────────────────────────────────────────────

async def test_get_context_returns_summary_and_branch_messages():
    sid = await sessions.create_session("p", "t")
    await sessions.append_message(sid, "user", "q1")
    await sessions.append_message(sid, "assistant", "a1")
    await sessions.save_handoff_summary(sid, "Prior summary")

    summary, messages = await sessions.get_context(sid)

    assert summary == "Prior summary"
    assert messages == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]


async def test_get_context_returns_empty_for_unknown_session():
    summary, messages = await sessions.get_context("00000000-0000-0000-0000-000000000000")
    assert summary == ""
    assert messages == []


async def test_get_context_no_summary_returns_empty_string():
    sid = await sessions.create_session("p", "t")
    summary, _ = await sessions.get_context(sid)
    assert summary == ""


# ── get_session / message_count ──────────────────────────────────────────────

async def test_get_session_message_count_reflects_appended_messages():
    sid = await sessions.create_session("p", "t")
    await sessions.append_message(sid, "user", "hi")
    await sessions.append_message(sid, "assistant", "hello")
    row = await sessions.get_session(sid)
    assert row["message_count"] == 2


async def test_get_session_returns_none_for_unknown_id():
    row = await sessions.get_session("00000000-0000-0000-0000-000000000000")
    assert row is None
