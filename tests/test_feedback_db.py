"""Tests for swarm/feedback.py's DB-touching functions (record_failure,
load_failure_context) -- runs against a real in-memory SQLite DB via swarm/db.py
(AGNOHive 2.3.2 addendum, 2026-08-08 — was raw psycopg/Postgres-only before this).
_filter_relevant_failures / _significant_tokens are pure functions, covered
separately in tests/test_feedback_relevance.py and untouched by this migration."""
import pytest

from config.config import config
from swarm import db, feedback


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    await db.reset_engine_for_tests()
    yield


async def test_record_failure_then_load_failure_context_round_trips():
    await feedback.record_failure("fix vouchers_api.py bug", "wrong field name", "proj1", agent="Coder")

    ctx = await feedback.load_failure_context("proj1", current_task="fix the vouchers_api.py endpoint")

    assert "vouchers_api.py" in ctx
    assert "wrong field name" in ctx


async def test_load_failure_context_filters_out_irrelevant_failures():
    await feedback.record_failure("fix vouchers_api.py bug", "wrong field name", "proj1", agent="Coder")
    await feedback.record_failure("unrelated statusBadge scss fix", "wrong namespace", "proj1", agent="Coder")

    ctx = await feedback.load_failure_context("proj1", current_task="fix the vouchers_api.py endpoint")

    assert "vouchers_api.py" in ctx
    assert "statusBadge" not in ctx


async def test_load_failure_context_scoped_to_project():
    await feedback.record_failure("proj-a specific bug", "error a", "proj-a")
    await feedback.record_failure("proj-b specific bug", "error b", "proj-b")

    ctx = await feedback.load_failure_context("proj-a", current_task="proj-a specific bug")

    assert "error a" in ctx
    assert "error b" not in ctx


async def test_load_failure_context_returns_empty_string_for_unknown_project():
    ctx = await feedback.load_failure_context("nonexistent-project", current_task="anything")
    assert ctx == ""


async def test_record_failure_stores_preference_pair_fields():
    """rejected_output/corrected_output exist to make the row usable as a DPO/ORPO
    training pair -- verify they actually persist, not just accepted silently."""
    import sqlalchemy as sa

    await feedback.record_failure(
        "some task", "some error", "proj1", agent="Coder",
        rejected_output="bad code here", corrected_output="good code here",
    )

    async with db.get_engine().begin() as conn:
        row = (await conn.execute(sa.select(db.failure_log))).mappings().first()

    assert row["rejected_output"] == "bad code here"
    assert row["corrected_output"] == "good code here"


async def test_record_failure_never_raises_on_db_outage(monkeypatch):
    def _broken_engine():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_engine", _broken_engine)
    await feedback.record_failure("task", "error", "proj1")  # must not raise


async def test_load_failure_context_returns_empty_on_db_outage(monkeypatch):
    def _broken_engine():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_engine", _broken_engine)
    result = await feedback.load_failure_context("proj1", current_task="anything")
    assert result == ""
