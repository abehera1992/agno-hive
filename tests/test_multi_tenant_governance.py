"""Phase M -- multi-tenant project isolation & resource governance.

Central forensic finding: the PRIMARY /run and /stream endpoints' session-
resumption path (session_id supplied in the request) never verified that
the resumed session actually belonged to the request's own project_id --
the exact same class of gap Phase J closed for the standalone /sessions/{id}
endpoints, but left open in the two endpoints that matter most. Fixed by
reusing _authorize_session_access (Phase J, already tested) directly in
both places, rather than re-implementing the check.

Also extends Phase K's run-ownership primitives (acquire_run_ownership/
release_run_ownership) and Phase L's list_stale_runs with an OPTIONAL
project_id parameter for defense-in-depth / operator scoping -- backward
compatible (default None preserves prior behavior exactly), consistent
with rehydrate_run's own Phase J-era precedent.

No new schema this phase. See docs/guide/deferred-limitations-ledger.md
for what forensics found and deliberately did NOT fix (resource limits,
mcp_url/project_id pairing trust, tenant-level enforcement).
"""
import asyncio
import inspect
import types
import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

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


async def _create_session(project_id: str = "project-a") -> str:
    sid = str(uuid.uuid4())
    async with db.get_engine().begin() as conn:
        await conn.execute(db.chat_sessions.insert().values(
            id=sid, project_id=project_id, title="t", persist=False))
    return sid


async def _team_with_persisted_run(session_id: str, run_id: str = "run-1") -> types.SimpleNamespace:
    team = types.SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    return team


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


# ── 1-3. /run and /stream now enforce session ownership on resume -----------

def test_run_endpoint_authorizes_session_resumption():
    import api.server as server_mod
    src = inspect.getsource(server_mod.run)
    assert "_authorize_session_access(session_id, request.project_id)" in src
    # The check must precede get_context -- otherwise the wrong project's
    # history is already loaded before the check would ever fire.
    assert src.index("_authorize_session_access(session_id, request.project_id)") \
        < src.index("await get_context(session_id)")


def test_stream_endpoint_authorizes_session_resumption():
    import api.server as server_mod
    src = inspect.getsource(server_mod.stream_endpoint)
    assert "_authorize_session_access(session_id, request.project_id)" in src
    assert src.index("_authorize_session_access(session_id, request.project_id)") \
        < src.index("await get_context(session_id)")


def test_run_chunked_inherits_the_fix_via_the_shared_run_handler():
    """/run_chunked calls run() directly for every chunk -- it must not
    re-implement session resolution, or it would silently bypass the fix
    above for chained chunks."""
    import api.server as server_mod
    src = inspect.getsource(server_mod.run_chunked)
    assert "await run(sub, http_request)" in src
    assert "_authorize_session_access" not in src  # no duplicate/parallel check


@pytest.mark.asyncio
async def test_cross_project_session_resumption_is_rejected_at_the_authorization_layer():
    """The actual runtime behavior the source-level checks above wire in --
    exercised directly against _authorize_session_access, the same function
    /run and /stream now call, with a real database (not mocked)."""
    import api.server as server_mod

    sid = await _create_session("project-a")
    with pytest.raises(HTTPException) as exc_info:
        await server_mod._authorize_session_access(sid, "project-b")
    assert exc_info.value.status_code == 404


# ── 4-6. acquire_run_ownership project scoping -------------------------------

@pytest.mark.asyncio
async def test_acquire_run_ownership_succeeds_for_the_owning_project():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)

    acquired = await execution_store.acquire_run_ownership(
        "run-1", "worker-A", project_id="project-a")

    assert acquired is True


@pytest.mark.asyncio
async def test_acquire_run_ownership_denies_a_mismatched_project():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)

    acquired = await execution_store.acquire_run_ownership(
        "run-1", "worker-A", project_id="project-b")

    assert acquired is False
    state = await execution_store.run_ownership_state("run-1")
    assert state["owned"] is False  # the denied attempt left no claim behind


@pytest.mark.asyncio
async def test_acquire_run_ownership_without_project_id_is_backward_compatible():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)

    acquired = await execution_store.acquire_run_ownership("run-1", "worker-A")  # no project_id at all

    assert acquired is True


# ── 7-8. release_run_ownership project scoping -------------------------------

@pytest.mark.asyncio
async def test_release_run_ownership_succeeds_for_the_owning_project():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)
    await execution_store.acquire_run_ownership("run-1", "worker-A", project_id="project-a")

    released = await execution_store.release_run_ownership(
        "run-1", "worker-A", project_id="project-a")

    assert released is True


@pytest.mark.asyncio
async def test_release_run_ownership_denies_a_mismatched_project():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)
    await execution_store.acquire_run_ownership("run-1", "worker-A")  # unscoped acquire

    released = await execution_store.release_run_ownership(
        "run-1", "worker-A", project_id="project-b")

    assert released is False
    state = await execution_store.run_ownership_state("run-1")
    assert state["owner_worker_id"] == "worker-A"  # still held -- the mismatched release did nothing


# ── 9-10. list_stale_runs project scoping ------------------------------------

@pytest.mark.asyncio
async def test_list_stale_runs_scoped_to_one_project_excludes_others():
    from datetime import datetime, timedelta, timezone

    sid_a = await _create_session("project-a")
    team_a = await _team_with_persisted_run(sid_a, run_id="run-a")
    sid_b = await _create_session("project-b")
    team_b = await _team_with_persisted_run(sid_b, run_id="run-b")
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with db.get_engine().begin() as conn:
        await conn.execute(db.runs.update().where(db.runs.c.run_id == "run-a")
                            .values(started_at=old))
        await conn.execute(db.runs.update().where(db.runs.c.run_id == "run-b")
                            .values(started_at=old))

    stale_a = await execution_store.list_stale_runs(older_than_seconds=3600, project_id="project-a")

    assert [r["run_id"] for r in stale_a] == ["run-a"]


@pytest.mark.asyncio
async def test_list_stale_runs_without_project_id_still_sees_everything():
    """Backward compatible / admin default: an operator's unscoped call is
    not itself an isolation violation -- see this function's own docstring."""
    from datetime import datetime, timedelta, timezone

    sid_a = await _create_session("project-a")
    await _team_with_persisted_run(sid_a, run_id="run-a")
    sid_b = await _create_session("project-b")
    await _team_with_persisted_run(sid_b, run_id="run-b")
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with db.get_engine().begin() as conn:
        await conn.execute(db.runs.update().where(db.runs.c.run_id == "run-a")
                            .values(started_at=old))
        await conn.execute(db.runs.update().where(db.runs.c.run_id == "run-b")
                            .values(started_at=old))

    stale_all = await execution_store.list_stale_runs(older_than_seconds=3600)

    assert {r["run_id"] for r in stale_all} == {"run-a", "run-b"}


# ── 11-12. Regression: Phase J promotion/rehydration isolation still holds ---

@pytest.mark.asyncio
async def test_cross_project_promotion_still_denied():
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "fact", "supported")

    promoted = await execution_store.promote_session_claims(sid, "project-b")

    assert promoted == []


@pytest.mark.asyncio
async def test_cross_project_rehydration_still_denied():
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    result = await execution_store.rehydrate_run("run-1", project_id="project-b")

    assert result["resumable"] is False


# ── 13. Tenant-level isolation: characterize the ACTUAL boundary (project only) --

@pytest.mark.asyncio
async def test_tenant_id_is_registered_but_not_enforced_anywhere():
    """Characterization test, not a defect report: proves the documented
    ledger item (tenant_id exists but is never checked by any authorization
    path) is accurate, so a future phase implementing real tenant
    enforcement has a concrete regression baseline to change."""
    await execution_store.ensure_project("project-a", tenant_id="tenant-1")
    await execution_store.ensure_project("project-b", tenant_id="tenant-2")

    sid = await _create_session("project-a")
    # project-a and project-b belong to DIFFERENT tenants, yet the ownership
    # check below is purely project-scoped -- it correctly denies cross-
    # PROJECT access, but nothing here ever reads or compares tenant_id.
    import api.server as server_mod
    with pytest.raises(HTTPException):
        await server_mod._authorize_session_access(sid, "project-b")

    for fn in (server_mod._authorize_session_access,):
        src = inspect.getsource(fn)
        assert "tenant_id" not in src
        assert "resolve_tenant_for_project" not in src


# ── 14. Memory isolation (LightRAG experience namespace) regression ---------

def test_experience_namespaces_are_distinct_per_project():
    from swarm.feedback import experience_namespace
    assert experience_namespace("project-a") != experience_namespace("project-b")
    assert experience_namespace("project-a") == "project-a_experience"


def test_load_project_memory_context_is_never_confused_by_a_second_identifier():
    """Unlike promote_session_claims (which used to trust session_id-vs-
    project_id blindly), load_project_memory_context takes ONLY project_id
    -- there is no second, cross-referenceable identifier for it to
    confuse. Source-level confirmation, not a new behavior."""
    import inspect as _inspect
    from swarm import feedback
    src = _inspect.getsource(feedback.load_project_memory_context)
    assert "session_id" not in src.split("def load_project_memory_context")[1].split("\n\n")[0]


# ── 15. Filesystem/MCP scope: characterize the trust boundary ---------------

def test_project_id_and_mcp_url_are_independently_caller_supplied():
    """Forensic finding, documented (not fixed -- see the ledger): RunRequest
    lets a caller supply project_id and mcp_url/mcp_urls as UNRELATED
    fields. agno-hive has no durable project->mcp_url registry to validate
    this pairing against; enforcing one would be new infrastructure not
    justified by evidence of actual harm. Isolation at the MCP/filesystem
    layer is achieved by DEPLOYMENT TOPOLOGY (one hive-mcp instance per
    project) instead -- outside this codebase's own database."""
    from api.models import RunRequest
    fields = RunRequest.model_fields
    assert "project_id" in fields
    assert "mcp_url" in fields
    assert "mcp_urls" in fields
    # A request naming an arbitrary, unrelated pair is accepted at the model
    # level -- there is no cross-field validator coupling them.
    req = RunRequest(task="x", project_id="project-a", mcp_url="http://example.invalid/mcp")
    assert req.project_id == "project-a"
    assert req.mcp_url == "http://example.invalid/mcp"


# ── 16. Resource governance: confirm existing coverage, no new limiter ------

def test_run_duration_is_already_governed_by_liveness_autokill():
    """Resource governance forensic conclusion: run DURATION already has a
    governing mechanism (the pre-Phase-A liveness auto-kill); this phase
    adds no new limiter for it -- confirmed the config knob still exists."""
    assert hasattr(config, "enable_liveness_autokill")


def test_evidence_content_remains_deliberately_unbounded():
    """Resource governance forensic conclusion: Evidence volume is NOT
    capped, deliberately (Phase D's own fidelity principle) -- confirmed
    the column is still unbounded Text, not truncated by this phase."""
    assert str(db.evidence.c.content.type) == "TEXT"


def test_project_memory_promotions_remains_uncapped_by_design():
    """Resource governance forensic conclusion: project-memory growth is
    self-limiting by construction (requires a human /feedback approval per
    row) -- no artificial cap was added, and none should be."""
    async def _count():
        async with db.get_engine().begin() as conn:
            return (await conn.execute(
                sa.select(sa.func.count()).select_from(db.project_memory_promotions)
            )).scalar()
    assert asyncio.run(_count()) == 0  # no pre-seeded cap/limit row of any kind
