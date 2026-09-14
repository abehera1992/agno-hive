"""Phase G -- project memory retrieval & context rehydration
(swarm/feedback.load_project_memory_context, wired into swarm/team.py's
run_task_async/run_task_stream at the SAME asyncio.gather/coordinator-
instructions boundary load_failure_context/load_success_context already
use).

Same layering as the prior phases' own suites: the retrieval function
exercised directly against a real (in-memory SQLite) database, plus
source-level guards proving the wiring is exactly what this phase's own
scope requires (read-only, no auto-promotion, no mutation of
project_memory_promotions, labeled distinctly from fresh Evidence).
"""
import asyncio
import types
import uuid

import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db, feedback
from swarm.execution_context import RunContext
from swarm.migrations import run_upgrade
import swarm.execution_store as execution_store


@pytest.fixture(autouse=True)
def _fresh_migrated_db(monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "postgres_uri", "")
    asyncio.run(db.reset_engine_for_tests())
    run_upgrade("head")   # plain sync call -- see test_migrations.py's own docstring
    yield


async def _create_session() -> str:
    sid = str(uuid.uuid4())
    async with db.get_engine().begin() as conn:
        await conn.execute(db.chat_sessions.insert().values(
            id=sid, project_id="p", title="t", persist=False))
    return sid


async def _promote_one(project_id: str, statement: str, session_id: str | None = None) -> str:
    """A minimal supported-Claim -> promoted-memory pipeline, reusing the
    exact Phase E/F primitives (no parallel reimplementation). Reusing an
    existing `session_id` re-runs promote_session_claims over that WHOLE
    session (idempotently re-confirming any already-promoted claim from an
    earlier call alongside the new one) -- callers that need an exact count
    should query project_memory_promotions directly rather than trust this
    helper's return value in that case."""
    sid = session_id or await _create_session()
    team = types.SimpleNamespace()
    rc = RunContext(sid, f"run-{uuid.uuid4().hex[:8]}")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    await execution_store.persist_claim(rc, statement, "supported")
    promoted = await execution_store.promote_session_claims(sid, project_id)
    assert len(promoted) >= 1
    return sid


# ── 1-2. project-scoping ------------------------------------------------

@pytest.mark.asyncio
async def test_relevant_project_memory_is_retrieved():
    await _promote_one("p", "the vouchers_api.py router has 6 endpoints")

    ctx = await feedback.load_project_memory_context(
        "p", current_task="describe vouchers_api.py")

    assert "vouchers_api.py" in ctx


@pytest.mark.asyncio
async def test_wrong_project_memory_is_excluded():
    await _promote_one("project-a", "the vouchers_api.py router has 6 endpoints")

    ctx = await feedback.load_project_memory_context(
        "project-b", current_task="describe vouchers_api.py")

    assert ctx == ""


# ── 3. only promoted memory is consumed --------------------------------

@pytest.mark.asyncio
async def test_only_promoted_claims_are_consumed_not_merely_supported_ones():
    sid = await _create_session()
    team = types.SimpleNamespace()
    rc = RunContext(sid, "run-1")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    await execution_store.persist_claim(rc, "never fed to /feedback", "supported")
    # Deliberately never call promote_session_claims.

    ctx = await feedback.load_project_memory_context(
        "p", current_task="fed to feedback")

    assert ctx == ""
    assert await _row_count(db.project_memory_promotions) == 0


async def _row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


# ── 4. deterministic / bounded retrieval --------------------------------

@pytest.mark.asyncio
async def test_retrieval_is_deterministic_across_repeated_calls():
    await _promote_one("p", "fact one about vouchers_api.py")
    await _promote_one("p", "fact two about vouchers_api.py")

    ctx1 = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")
    ctx2 = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")

    assert ctx1 == ctx2


@pytest.mark.asyncio
async def test_retrieval_is_bounded_by_limit():
    for i in range(8):
        await _promote_one("p", f"fact number {i} about vouchers_api.py endpoints")

    ctx = await feedback.load_project_memory_context("p", limit=3, current_task="vouchers_api.py")

    assert ctx.count("[promotion=") == 3


# ── 5-6. provenance + labeling -----------------------------------------

@pytest.mark.asyncio
async def test_provenance_is_preserved_in_the_injected_text():
    sid = await _create_session()
    team = types.SimpleNamespace()
    rc = RunContext(sid, "run-prov")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    claim_id = await execution_store.persist_claim(
        rc, "backend has 26956 chars for vouchers_api.py", "supported")
    await execution_store.promote_session_claims(sid, "p")

    async with db.get_engine().begin() as conn:
        promo = (await conn.execute(sa.select(db.project_memory_promotions))).mappings().first()

    ctx = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")

    assert f"promotion={promo['id']}" in ctx
    assert f"claim={claim_id}" in ctx
    assert f"run={rc.run_id}" in ctx
    assert "evidence_hash=" in ctx
    assert "validated_by=" in ctx


def test_memory_is_explicitly_labeled_as_prior_validated_knowledge():
    import inspect
    src = inspect.getsource(feedback.load_project_memory_context)
    assert "PROJECT MEMORY / PRIOR VALIDATED KNOWLEDGE" in src


def test_memory_is_never_represented_as_current_evidence():
    import inspect
    src = inspect.getsource(feedback.load_project_memory_context)
    assert "CURRENT TOOL EVIDENCE" in src
    assert "CURRENT FILE CONTENT" in src
    assert "CURRENT DATABASE STATE" in src


# ── 7. trust ordering is stated in the injected text ---------------------

@pytest.mark.asyncio
async def test_fresh_evidence_precedence_is_stated_in_the_context_block():
    await _promote_one("p", "vouchers_api.py has 6 endpoints")

    ctx = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")

    assert "fresh evidence wins" in ctx.lower()


# ── 8. contradictory current evidence does not mutate memory ------------

@pytest.mark.asyncio
async def test_contradicting_promotion_does_not_mutate_the_earlier_one():
    sid = await _promote_one("p", "vouchers_api.py has 5 endpoints")
    async with db.get_engine().begin() as conn:
        first = (await conn.execute(sa.select(db.project_memory_promotions))).mappings().first()

    # A later run's OWN validated claim disagrees -- promoted as a SEPARATE row,
    # never an update to the first.
    await _promote_one("p", "vouchers_api.py has 6 endpoints", session_id=sid)

    rows = await _row_count(db.project_memory_promotions)
    assert rows == 2
    async with db.get_engine().begin() as conn:
        unchanged = (await conn.execute(
            sa.select(db.project_memory_promotions)
            .where(db.project_memory_promotions.c.id == first["id"])
        )).mappings().first()
    assert unchanged["statement"] == first["statement"]  # byte-identical, untouched


def test_project_memory_promotions_is_never_updated_or_deleted_anywhere():
    """Append-only by construction across the whole codebase -- a contradiction
    is represented as a NEW row (Phase F), never an UPDATE/DELETE of an
    existing one."""
    import inspect
    import api.server as server_mod
    import swarm.execution_store as es_mod
    import swarm.feedback as fb_mod
    import swarm.team as team_mod

    for mod in (server_mod, es_mod, fb_mod, team_mod):
        src = inspect.getsource(mod)
        assert "project_memory_promotions.update(" not in src
        assert "project_memory_promotions.delete(" not in src


# ── 9. duplicates are never injected twice -------------------------------

@pytest.mark.asyncio
async def test_the_same_memory_is_never_injected_twice_in_one_block():
    await _promote_one("p", "vouchers_api.py has 6 endpoints")

    ctx = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")

    async with db.get_engine().begin() as conn:
        promo = (await conn.execute(sa.select(db.project_memory_promotions))).mappings().first()
    assert ctx.count(f"promotion={promo['id']}") == 1


# ── 10. empty memory path -------------------------------------------------

@pytest.mark.asyncio
async def test_empty_memory_path_returns_empty_string():
    ctx = await feedback.load_project_memory_context("p", current_task="anything at all")
    assert ctx == ""


# ── 11. retrieval failure is fail-open -----------------------------------

@pytest.mark.asyncio
async def test_retrieval_failure_is_fail_open(monkeypatch):
    await _promote_one("p", "vouchers_api.py has 6 endpoints")

    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(db, "get_engine", _boom)
    ctx = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")
    assert ctx == ""  # indistinguishable from "no memory", never raised


# ── 12. oversized memory is bounded/truncated safely ----------------------

@pytest.mark.asyncio
async def test_oversized_statement_is_truncated():
    long_statement = "vouchers_api.py " + ("x" * 1000)
    await _promote_one("p", long_statement)

    ctx = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")

    assert len(long_statement) > feedback._PROJECT_MEMORY_STATEMENT_CHARS
    assert long_statement not in ctx        # not injected verbatim
    assert "…" in ctx                        # truncation marker present
    # Bounded: no single line carries the full 1000+ char statement.
    assert all(len(line) < 500 for line in ctx.splitlines())


@pytest.mark.asyncio
async def test_evidence_snapshot_body_is_never_injected_only_its_hash():
    sid = await _create_session()
    from swarm.team import _make_tool_interception_hook

    team = types.SimpleNamespace()
    rc = RunContext(sid, "run-snap")
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "THE-FULL-LARGE-SNAPSHOT-BODY-THAT-MUST-NEVER-BE-INJECTED"

    async with db.get_engine().begin() as conn:
        before = {r["evidence_id"]
                  for r in (await conn.execute(sa.select(db.evidence))).mappings().all()}
    await hook("get_file_content", fake_tool, {"path": "vouchers_api.py"}, team=team)
    async with db.get_engine().begin() as conn:
        after = (await conn.execute(sa.select(db.evidence))).mappings().all()
    evidence_id = [r["evidence_id"] for r in after if r["evidence_id"] not in before][0]

    await execution_store.persist_claim(
        rc, "vouchers_api.py contains the snapshot body", "supported",
        evidence_ids=[evidence_id])
    await execution_store.promote_session_claims(sid, "p")

    ctx = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")

    assert "THE-FULL-LARGE-SNAPSHOT-BODY-THAT-MUST-NEVER-BE-INJECTED" not in ctx
    assert "evidence_hash=" in ctx


# ── 13. no unintended change to no-memory behavior -------------------------

@pytest.mark.asyncio
async def test_no_memory_yet_behaves_exactly_like_load_failure_context_with_nothing():
    ctx_memory = await feedback.load_project_memory_context("p", current_task="anything")
    ctx_failure = await feedback.load_failure_context("p", current_task="anything")
    ctx_success = await feedback.load_success_context("p", current_task="anything")
    assert ctx_memory == ctx_failure == ctx_success == ""


# ── 14. session deletion does not remove or read-break promoted memory -----

@pytest.mark.asyncio
async def test_session_deletion_does_not_remove_or_break_retrieval():
    sid = await _promote_one("p", "vouchers_api.py has 6 endpoints")

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        await conn.execute(db.chat_sessions.delete().where(db.chat_sessions.c.id == sid))

    ctx = await feedback.load_project_memory_context("p", current_task="vouchers_api.py")
    assert "vouchers_api.py" in ctx


# ── source-level guards: no auto-promotion, correct wiring -----------------

def test_retrieval_never_calls_promotion_functions():
    import inspect
    src = inspect.getsource(feedback.load_project_memory_context)
    for forbidden in ("promote_claim(", "promote_session_claims(", "persist_claim(",
                      ".insert(", ".update(", ".delete("):
        assert forbidden not in src


def test_team_py_wires_project_memory_at_both_run_entry_points():
    import inspect
    import swarm.team as team_mod
    source = inspect.getsource(team_mod)
    assert source.count("load_project_memory_context(project_id, current_task=task)") == 2
