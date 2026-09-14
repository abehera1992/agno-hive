"""Phase F -- project-owned memory promotion (swarm/execution_store.py's
promote_claim/promote_session_claims, wired into api/server.py's /feedback
endpoint on its rating=="good" branch ONLY).

Same layering as the prior phases' own suites: execution_store's own
promotion primitives exercised directly against a real (in-memory SQLite)
database, plus source-level guards proving the wiring at api/server.py is
exactly what this module's docstring claims (one call site, fail-open,
gated on rating=="good", never touching LightRAG/Qdrant/AGE).
"""
import asyncio
import types
import uuid

import pytest
import sqlalchemy as sa

from config.config import config
from swarm import db
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


async def _team_with_persisted_run(session_id: str, run_id: str = "run-1") -> types.SimpleNamespace:
    team = types.SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    return team


async def _existing_evidence_id(team) -> str:
    from swarm.team import _make_tool_interception_hook

    hook = _make_tool_interception_hook()

    async def fake_tool(**kwargs):
        return "backend has 26956 characters"

    async with db.get_engine().begin() as conn:
        before = {r["evidence_id"]
                  for r in (await conn.execute(sa.select(db.evidence))).mappings().all()}
    await hook("get_file_content", fake_tool, {"path": "a.py"}, team=team)
    async with db.get_engine().begin() as conn:
        after = (await conn.execute(sa.select(db.evidence))).mappings().all()
    new_ids = [r["evidence_id"] for r in after if r["evidence_id"] not in before]
    return new_ids[0]


async def _claim_row(claim_id: str):
    async with db.get_engine().begin() as conn:
        return (await conn.execute(
            sa.select(db.claims).where(db.claims.c.claim_id == claim_id)
        )).mappings().first()


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


async def _promotion_rows():
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(db.project_memory_promotions))).mappings().all()


# ── 1-3. validated Claim -> promotion, with provenance -----------------------

@pytest.mark.asyncio
async def test_validated_claim_is_promoted():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    claim_id = await execution_store.persist_claim(rc, "backend has 26956 chars", "supported")
    claim = await _claim_row(claim_id)

    promotion_id = await execution_store.promote_claim(
        "p", claim, "backend has 26956 characters", "abc123hash",
        validated_by=execution_store.VALIDATED_BY_DETERMINISTIC_PLUS_FEEDBACK)

    assert promotion_id is not None
    uuid.UUID(promotion_id)  # genuine UUID
    rows = await _promotion_rows()
    assert len(rows) == 1
    assert rows[0]["claim_id"] == claim_id
    assert rows[0]["run_id"] == rc.run_id
    assert rows[0]["project_id"] == "p"
    assert rows[0]["statement"] == "backend has 26956 chars"
    assert rows[0]["claim_status"] == "supported"


@pytest.mark.asyncio
async def test_promotion_preserves_provenance_run_and_execution_ids():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    root = rc.root_execution_id
    claim_id = await execution_store.persist_claim(rc, "stmt", "supported")
    claim = await _claim_row(claim_id)

    await execution_store.promote_claim(
        "p", claim, None, None, validated_by=execution_store.VALIDATED_BY_DETERMINISTIC_PLUS_FEEDBACK)

    rows = await _promotion_rows()
    assert rows[0]["execution_id"] == root
    assert rows[0]["validated_by"] == execution_store.VALIDATED_BY_DETERMINISTIC_PLUS_FEEDBACK


# ── 4. unvalidated Claim -> no promotion -------------------------------------

@pytest.mark.asyncio
async def test_unvalidated_claim_is_refused():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    claim_id = await execution_store.persist_claim(rc, "stmt", "contradicted")
    claim = await _claim_row(claim_id)

    with pytest.raises(ValueError):
        await execution_store.promote_claim(
            "p", claim, None, None, validated_by="anything")
    assert await _table_row_count(db.project_memory_promotions) == 0


@pytest.mark.asyncio
async def test_promote_session_claims_only_promotes_supported_claims():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_claim(rc, "supported one", "supported")
    await execution_store.persist_claim(rc, "contradicted one", "contradicted")
    await execution_store.persist_claim(rc, "unverifiable one", "unverifiable")
    await execution_store.persist_claim(rc, "partial one", "partially_supported")

    promoted = await execution_store.promote_session_claims(sid, "p")

    assert len(promoted) == 1
    rows = await _promotion_rows()
    assert rows[0]["statement"] == "supported one"


# ── 5. runtime success alone -> no automatic promotion -----------------------

@pytest.mark.asyncio
async def test_supported_claim_alone_does_not_promote_without_feedback():
    """A Claim reaching status=='supported' is Phase E's own deterministic
    reconciliation -- an ordinary, automatic part of run execution. Phase F
    promotion NEVER fires on its own; promote_session_claims must be called
    explicitly (i.e. from /feedback), which this test deliberately never
    does."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "stmt", "supported")

    assert await _table_row_count(db.project_memory_promotions) == 0


# ── 6-7. Idempotency ----------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_promotion_is_idempotent():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "stmt", "supported")

    first = await execution_store.promote_session_claims(sid, "p")
    second = await execution_store.promote_session_claims(sid, "p")

    assert first == second
    assert await _table_row_count(db.project_memory_promotions) == 1


@pytest.mark.asyncio
async def test_repeated_feedback_does_not_duplicate_project_knowledge():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    claim_id = await execution_store.persist_claim(team._run_context, "stmt", "supported")
    claim = await _claim_row(claim_id)

    id1 = await execution_store.promote_claim(
        "p", claim, None, None, validated_by="x")
    id2 = await execution_store.promote_claim(
        "p", claim, None, None, validated_by="x")

    assert id1 == id2
    assert await _table_row_count(db.project_memory_promotions) == 1


# ── 8. Evidence snapshot/hash preservation ------------------------------------

@pytest.mark.asyncio
async def test_evidence_snapshot_and_hash_are_preserved_exactly():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)
    async with db.get_engine().begin() as conn:
        ev = (await conn.execute(
            sa.select(db.evidence).where(db.evidence.c.evidence_id == evidence_id)
        )).mappings().first()

    claim_id = await execution_store.persist_claim(
        team._run_context, "backend has 26956 characters", "supported",
        evidence_ids=[evidence_id])

    promoted = await execution_store.promote_session_claims(team._run_context.session_id, "p")
    assert len(promoted) == 1
    rows = await _promotion_rows()
    assert rows[0]["evidence_snapshot"] == ev["content"]
    assert rows[0]["evidence_hash"] == ev["content_hash"]


@pytest.mark.asyncio
async def test_promotion_with_no_linked_evidence_has_null_snapshot():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "stmt", "supported")  # no evidence_ids

    await execution_store.promote_session_claims(sid, "p")

    rows = await _promotion_rows()
    assert rows[0]["evidence_snapshot"] is None
    assert rows[0]["evidence_hash"] is None


# ── 9. Session deletion does not delete promoted memory -----------------------

@pytest.mark.asyncio
async def test_session_deletion_does_not_delete_promoted_memory():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)
    await execution_store.persist_claim(
        team._run_context, "backend has 26956 characters", "supported",
        evidence_ids=[evidence_id])
    await execution_store.promote_session_claims(sid, "p")
    assert await _table_row_count(db.project_memory_promotions) == 1

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        await conn.execute(db.chat_sessions.delete().where(db.chat_sessions.c.id == sid))

    # The whole session-owned tree is gone...
    assert await _table_row_count(db.runs) == 0
    assert await _table_row_count(db.claims) == 0
    assert await _table_row_count(db.evidence) == 0
    # ...but the promoted memory survives, snapshot intact.
    rows = await _promotion_rows()
    assert len(rows) == 1
    assert rows[0]["evidence_snapshot"] == "backend has 26956 characters"
    assert rows[0]["statement"] == "backend has 26956 characters"


# ── 10. Contradictory/revised promotion behavior ------------------------------

@pytest.mark.asyncio
async def test_contradicting_promotions_both_survive_as_separate_rows():
    """Phase F is append-only: a later, contradicting validated Claim about
    the same subject never overwrites an earlier promoted one. Reconciling
    between two promoted rows that disagree is explicitly NOT solved here --
    see this module's own docstring -- only that neither is silently lost or
    replaced."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "there are 5 missing files", "supported")
    await execution_store.persist_claim(team._run_context, "there are 6 missing files", "supported")

    promoted = await execution_store.promote_session_claims(sid, "p")

    assert len(promoted) == 2
    rows = await _promotion_rows()
    assert {r["statement"] for r in rows} == {
        "there are 5 missing files", "there are 6 missing files"}


# ── 11. Fail-open ---------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_session_claims_lookup_failure_is_fail_open(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "stmt", "supported")

    def _boom_engine():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom_engine)
    result = await execution_store.promote_session_claims(sid, "p")
    assert result == []  # swallowed, not raised


@pytest.mark.asyncio
async def test_promote_claim_insert_failure_is_fail_open_via_guard(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    claim_id = await execution_store.persist_claim(team._run_context, "stmt", "supported")
    claim = await _claim_row(claim_id)

    def _boom_engine():
        raise RuntimeError("db is down")

    # promote_claim's OWN try/except wraps the whole transaction -- this proves
    # that internal handling works even without guard() as a backstop.
    monkeypatch.setattr(execution_store, "get_engine", _boom_engine)
    direct = await execution_store.promote_claim("p", claim, None, None, validated_by="x")
    assert direct is None

    # And guard() at the call site is still the last-resort layer regardless.
    guarded = await execution_store.guard(execution_store.promote_claim(
        "p", claim, None, None, validated_by="x"))
    assert guarded is None


# ── 12. No unintended changes to existing memory retrieval/write behavior ----

def test_promotion_never_references_lightrag_or_task_outcome_queue():
    import inspect
    source = inspect.getsource(execution_store.promote_claim) + \
        inspect.getsource(execution_store.promote_session_claims)
    forbidden = ("lightrag", "qdrant", "record_success", "task_outcome_queue",
                 "_queue_outcome")
    for term in forbidden:
        assert term not in source.lower()


def test_feedback_endpoint_still_calls_queue_outcome_unchanged():
    """The pre-existing LightRAG-backed success path (_queue_outcome) must
    still fire exactly as before -- Phase F is purely additive alongside it,
    never a replacement."""
    import inspect
    import api.server as server_mod
    source = inspect.getsource(server_mod.feedback)
    assert "await _queue_outcome(" in source


def test_feedback_endpoint_promotion_is_gated_on_rating_good_and_fail_open():
    import inspect
    import api.server as server_mod
    source = inspect.getsource(server_mod.feedback)
    assert "execution_store.guard(execution_store.promote_session_claims(" in source
    # The promotion call must be reachable only from the else (non-"bad") branch --
    # i.e. it appears strictly after the `if request.rating == "bad":` block's own
    # return, in the same source text used above.
    bad_branch_end = source.index("return FeedbackResponse(")
    promote_call_pos = source.index("promote_session_claims(")
    assert promote_call_pos > bad_branch_end
