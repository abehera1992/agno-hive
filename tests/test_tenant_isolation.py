"""Phase J -- enterprise multi-tenancy, isolation & authorization.

Forensics found this codebase has NO authentication system anywhere (every
endpoint in api/server.py is unauthenticated, by documented design, over a
private Tailscale network -- see that file's own /admin/model-routes
comment). "Authorization" here is therefore NOT identity verification (that
would require inventing an auth system, explicitly out of this phase's
scope) -- it is OWNERSHIP verification: never trusting a client-supplied
project_id in isolation, always cross-checking it against the ACTUAL
project_id a referenced resource (session_id, run_id, claim) belongs to,
derived from the database, not from the request.

Two concrete, exploitable gaps were found and are covered here:
  1. execution_store.promote_session_claims(session_id, project_id) never
     verified session_id belonged to project_id -- a caller could promote
     ANY project's validated claims into ANY other project's durable
     memory by supplying a mismatched pair.
  2. Every session-scoped endpoint (GET/DELETE/PATCH /sessions/{id}, /tree,
     /branch, /fork) identified its target by session_id ALONE -- any
     caller holding (or guessing) any session_id could read, delete,
     mutate, or exfiltrate (via /fork) ANY project's session.

execution_store.rehydrate_run also gained an OPTIONAL project_id
parameter (defense in depth for a function not yet exposed over HTTP).

Same layering as prior phases: functions/endpoint-handlers exercised
directly against a real (in-memory SQLite) database, plus source-level
guards.
"""
import asyncio
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


# ── 1-2. Cross-project promotion (the core Phase J finding) -----------------

@pytest.mark.asyncio
async def test_feedback_cannot_promote_another_projects_claim():
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "a validated fact", "supported")

    promoted = await execution_store.promote_session_claims(sid, "project-b")  # forged pair

    assert promoted == []
    assert await _table_row_count(db.project_memory_promotions) == 0


@pytest.mark.asyncio
async def test_promotion_still_works_for_the_matching_project():
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "a validated fact", "supported")

    promoted = await execution_store.promote_session_claims(sid, "project-a")

    assert len(promoted) == 1
    assert await _table_row_count(db.project_memory_promotions) == 1


@pytest.mark.asyncio
async def test_promotion_against_a_nonexistent_session_id_is_refused():
    result = await execution_store.promote_session_claims(
        "no-such-session-id", "project-a")
    assert result == []


@pytest.mark.asyncio
async def test_cross_project_promotion_is_logged_as_a_refusal(capsys):
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_claim(team._run_context, "fact", "supported")

    await execution_store.promote_session_claims(sid, "project-b")

    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "project-b" in out


# ── 3-5. rehydrate_run project_id enforcement --------------------------------

@pytest.mark.asyncio
async def test_rehydrate_run_matching_project_id_still_resumable():
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    result = await execution_store.rehydrate_run("run-1", project_id="project-a")

    assert result["resumable"] is True


@pytest.mark.asyncio
async def test_rehydrate_run_mismatched_project_id_fails_closed():
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    result = await execution_store.rehydrate_run("run-1", project_id="project-b")

    assert result["resumable"] is False
    assert "authorization" in result["reason"]


@pytest.mark.asyncio
async def test_rehydrate_run_without_project_id_preserves_old_behavior():
    """Backward compatibility: Phase H's existing callers/tests never pass
    project_id -- omitting it must behave EXACTLY as before this phase."""
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    await execution_store.persist_checkpoint(team._run_context, run_status="running")

    result = await execution_store.rehydrate_run("run-1")  # no project_id at all

    assert result["resumable"] is True


# ── 6. ensure_project / resolve_tenant_for_project ---------------------------

@pytest.mark.asyncio
async def test_ensure_project_registers_a_new_project():
    await execution_store.ensure_project("proj-x", tenant_id="tenant-1")

    async with db.get_engine().begin() as conn:
        row = (await conn.execute(
            sa.select(db.projects).where(db.projects.c.id == "proj-x")
        )).mappings().first()
    assert row["tenant_id"] == "tenant-1"


@pytest.mark.asyncio
async def test_ensure_project_is_idempotent_and_never_overwrites_tenant():
    await execution_store.ensure_project("proj-x", tenant_id="tenant-1")
    await execution_store.ensure_project("proj-x", tenant_id=None)  # a later, ignorant caller
    await execution_store.ensure_project("proj-x", tenant_id="tenant-2")  # a conflicting caller

    tenant = await execution_store.resolve_tenant_for_project("proj-x")
    assert tenant == "tenant-1"  # first registration wins, never silently reassigned
    assert await _table_row_count(db.projects) == 1


@pytest.mark.asyncio
async def test_resolve_tenant_for_unregistered_project_is_none():
    assert await execution_store.resolve_tenant_for_project("never-registered") is None


@pytest.mark.asyncio
async def test_ensure_project_failure_is_fail_open(monkeypatch):
    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "get_engine", _boom)
    await execution_store.ensure_project("proj-x")  # must not raise


# ── 7. _authorize_session_access (the HTTP-layer boundary) -------------------

@pytest.mark.asyncio
async def test_authorize_session_access_allows_a_matching_project():
    import api.server as server_mod

    sid = await _create_session("project-a")
    session = await server_mod._authorize_session_access(sid, "project-a")
    assert session["id"] == sid


@pytest.mark.asyncio
async def test_authorize_session_access_rejects_a_mismatched_project():
    import api.server as server_mod

    sid = await _create_session("project-a")
    with pytest.raises(HTTPException) as exc_info:
        await server_mod._authorize_session_access(sid, "project-b")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_authorize_session_access_rejects_a_nonexistent_session():
    import api.server as server_mod

    with pytest.raises(HTTPException) as exc_info:
        await server_mod._authorize_session_access("no-such-session", "project-a")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_authorize_session_access_without_project_id_is_backward_compatible():
    import api.server as server_mod

    sid = await _create_session("project-a")
    # No project_id supplied -- preserves the pre-Phase-J open behavior.
    session = await server_mod._authorize_session_access(sid, None)
    assert session["id"] == sid


# ── 8. Cross-tenant read/write/delete via the actual endpoint handlers -------

@pytest.mark.asyncio
async def test_get_session_endpoint_rejects_cross_project_access():
    import api.server as server_mod

    sid = await _create_session("project-a")
    with pytest.raises(HTTPException) as exc_info:
        await server_mod.get_session_endpoint(sid, project_id="project-b")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_endpoint_rejects_cross_project_delete():
    import api.server as server_mod

    sid = await _create_session("project-a")
    with pytest.raises(HTTPException) as exc_info:
        await server_mod.delete_session_endpoint(sid, project_id="project-b")
    assert exc_info.value.status_code == 404
    # The session must survive the rejected attempt.
    assert await _table_row_count(db.chat_sessions) == 1


@pytest.mark.asyncio
async def test_delete_session_endpoint_still_works_for_the_owning_project():
    import api.server as server_mod

    sid = await _create_session("project-a")
    result = await server_mod.delete_session_endpoint(sid, project_id="project-a")
    assert result == {"deleted": sid}
    assert await _table_row_count(db.chat_sessions) == 0


@pytest.mark.asyncio
async def test_delete_session_endpoint_without_project_id_is_backward_compatible():
    import api.server as server_mod

    sid = await _create_session("project-a")
    result = await server_mod.delete_session_endpoint(sid, project_id=None)
    assert result == {"deleted": sid}


@pytest.mark.asyncio
async def test_persist_session_endpoint_rejects_cross_project_access():
    import api.server as server_mod

    sid = await _create_session("project-a")
    with pytest.raises(HTTPException) as exc_info:
        await server_mod.persist_session_endpoint(sid, project_id="project-b")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_forged_session_id_cannot_bypass_authorization():
    """A caller supplying a structurally-valid but entirely fabricated
    session_id gets the same closed failure as a real cross-project one --
    never a different code path, never information about which case it was."""
    import api.server as server_mod

    forged_but_well_formed = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await server_mod.get_session_endpoint(forged_but_well_formed, project_id="project-a")
    assert exc_info.value.status_code == 404


# ── 9. /fork cross-project exfiltration ---------------------------------

@pytest.mark.asyncio
async def test_fork_session_endpoint_rejects_cross_project_fork():
    import api.server as server_mod
    from api.models import ForkRequest
    from swarm.sessions import append_message

    sid = await _create_session("project-a")
    await append_message(sid, "user", "sensitive project-a content", parent_message_id=None)

    with pytest.raises(HTTPException) as exc_info:
        await server_mod.fork_session_endpoint(
            sid, ForkRequest(title="stolen", project_id="project-b"))
    assert exc_info.value.status_code == 404
    # No new session was created under project-b.
    assert await _table_row_count(db.chat_sessions) == 1


@pytest.mark.asyncio
async def test_fork_session_endpoint_still_works_within_the_same_project():
    import api.server as server_mod
    from api.models import ForkRequest
    from swarm.sessions import append_message

    sid = await _create_session("project-a")
    await append_message(sid, "user", "content", parent_message_id=None)

    result = await server_mod.fork_session_endpoint(
        sid, ForkRequest(title="a legit fork", project_id="project-a"))

    assert "session_id" in result
    assert await _table_row_count(db.chat_sessions) == 2


# ── 10. Cleanup isolation (fairness, not a leak) ------------------------

@pytest.mark.asyncio
async def test_cleanup_is_fair_across_projects_not_a_cross_tenant_leak():
    from swarm.sessions import _cleanup_expired
    from datetime import datetime, timedelta, timezone

    sid_a_expired = await _create_session("project-a")
    sid_b_expired = await _create_session("project-b")
    sid_b_alive = await _create_session("project-b")

    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(db.chat_sessions.update()
                            .where(db.chat_sessions.c.id == sid_a_expired)
                            .values(expires_at=past))
        await conn.execute(db.chat_sessions.update()
                            .where(db.chat_sessions.c.id == sid_b_expired)
                            .values(expires_at=past))
        await conn.execute(db.chat_sessions.update()
                            .where(db.chat_sessions.c.id == sid_b_alive)
                            .values(expires_at=future))

    deleted = await _cleanup_expired()

    assert deleted == 2  # both expired sessions, regardless of project
    remaining = await _table_row_count(db.chat_sessions)
    assert remaining == 1  # only project-b's still-alive session survives


# ── 11-12. Migration / existing single-project behavior ------------------

@pytest.mark.asyncio
async def test_projects_table_exists_after_migration_and_is_empty_by_default():
    assert await _table_row_count(db.projects) == 0  # no backfill -- purely additive


@pytest.mark.asyncio
async def test_existing_chat_sessions_are_untouched_by_the_migration():
    """The migration must be purely additive -- a session created under the
    pre-Phase-J convention (a bare project_id string, no `projects` row)
    keeps working exactly as before."""
    sid = await _create_session("some-legacy-project-id-never-registered")
    import api.server as server_mod

    session = await server_mod._authorize_session_access(sid, None)
    assert session["project_id"] == "some-legacy-project-id-never-registered"


def test_no_foreign_key_from_chat_sessions_to_projects():
    """Deliberate: a hard FK here would make upgrading an existing
    installation with unregistered project_id values destructive."""
    fk_targets = {fk.target_fullname for fk in db.chat_sessions.foreign_keys}
    assert not any("projects" in t for t in fk_targets)


# ── source-level guards -------------------------------------------------

def test_authorization_failures_never_fall_through_to_fail_open():
    import inspect
    src = inspect.getsource(execution_store.promote_session_claims)
    # The ownership check must `return []` immediately on mismatch, never
    # continue into the promotion loop below it.
    assert "if owner_project_id != project_id:" in src
    assert src.index("if owner_project_id != project_id:") < src.index("run_ids = (await conn.execute(")


def test_rehydrate_run_authorization_check_precedes_resumability_logic():
    import inspect
    src = inspect.getsource(execution_store.rehydrate_run)
    assert src.index("if project_id is not None:") < src.index("checkpoint = (await conn.execute(")
