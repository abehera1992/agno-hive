"""Phase O -- operational observability & incident reconstruction.

Forensics (this phase) audited every existing observability surface an
operator has after a worker process is gone: runs/executions/tool_calls/
evidence/claims/claim_evidence (Phase B-E), checkpoints (Phase H),
project_memory_promotions (Phase F), check_storage_integrity/list_stale_runs
(Phase I/L), run_ownership_state (Phase K), swarm.team._evidence_fidelity_report
(Phase N -- a standalone simulated-input trace never called with a real
RunContext by anything durable, so it is out of scope for reconstruction),
and swarm/phase0.py's JSONL telemetry.

swarm.execution_store.reconstruct_run is what got built: a single, read-only,
deterministic aggregation of the EXISTING Phase A-N tables into one incident
timeline, reusing (never duplicating) rehydrate_run (Phase H) and
run_ownership_state (Phase K). No new schema/migration this phase -- every
column reconstruct_run reads already exists.

Phase 0's own JSONL telemetry was deliberately excluded (see
reconstruct_run's own module-level comment): it is explicitly ephemeral/
best-effort, and its own run_id has no column anywhere correlating it with
the durable run_id this function is keyed on -- exactly the kind of
"ephemeral log" this phase's core invariant says reconstruction must not
depend on.
"""
import asyncio
import types
import uuid
from datetime import datetime, timedelta, timezone

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


async def _create_session(project_id: str = "p") -> str:
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


async def _complete_run(rc: RunContext, status: str = "ok") -> None:
    rc.finish_execution(rc.root_execution_id, status=status)
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status=status)


async def _add_tool_call(rc: RunContext, tool_name: str = "get_file_content",
                          content: str = "some content", success: bool = True,
                          error: str | None = None) -> tuple[str, str]:
    """Persists one full ToolCall+Evidence pair and returns
    (durable_tool_call_id, evidence_id)."""
    tc = rc.start_tool_call(tool_name, {"path": "a.py"})
    durable_id = await execution_store.persist_tool_call_created(tc)
    ev = rc.finish_tool_call(tc, content=content, success=success, error=error)
    evidence_id = await execution_store.persist_tool_call_completed(durable_id, tc, ev)
    return durable_id, evidence_id


# ── 1. Run does not exist -> None -------------------------------------------

@pytest.mark.asyncio
async def test_unknown_run_id_returns_none():
    result = await execution_store.reconstruct_run("nonexistent-run")
    assert result is None


# ── 2-3. Complete reconstruction covers every layer -------------------------

@pytest.mark.asyncio
async def test_complete_run_reconstruction_covers_every_layer():
    sid = await _create_session("project-a")
    team = await _team_with_persisted_run(sid)
    rc = team._run_context

    _, evidence_id = await _add_tool_call(rc, content="vouchers_api.py has 6 endpoints")
    claim_id = await execution_store.persist_claim(
        rc, "vouchers_api.py has 6 endpoints", "supported", evidence_ids=[evidence_id])
    await execution_store.persist_checkpoint(rc, run_status="running")
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    assert report["authorized"] is True
    assert report["run_id"] == "run-1"
    assert report["run"]["status"] == "ok"
    assert report["project_id"] == "project-a"
    assert len(report["executions"]) == 1
    assert len(report["tool_calls"]) == 1
    assert len(report["evidence"]) == 1
    assert len(report["claims"]) == 1
    assert report["claims"][0]["claim_id"] == claim_id
    assert len(report["checkpoints"]) == 1
    assert report["ownership"] is not None
    assert report["rehydration"] is not None
    assert report["known_limitations"]  # always present, never silently empty


@pytest.mark.asyncio
async def test_run_with_no_tool_calls_claims_or_checkpoints_still_reconstructs():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await _complete_run(team._run_context, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    assert report["authorized"] is True
    assert report["executions"] != []
    assert report["tool_calls"] == []
    assert report["evidence"] == []
    assert report["claims"] == []
    assert report["checkpoints"] == []


# ── 4. Execution ordering is deterministic -----------------------------------

@pytest.mark.asyncio
async def test_executions_are_ordered_by_started_at_ascending():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    child = rc.start_execution("researcher", "delegation", parent_execution_id=rc.root_execution_id)
    await execution_store.persist_execution_created(rc.executions[child])
    rc.finish_execution(child, status="ok")
    await execution_store.persist_execution_completed(rc.executions[child])
    await _complete_run(rc, status="ok")

    # SQLite's func.now() has second-level resolution, so two inserts in the
    # same test can legitimately tie -- set explicit, unambiguous timestamps
    # (matching a realistic delegation starting strictly after its root) so
    # this test exercises ordering itself, not id-tiebreak behavior.
    root_ts = datetime.now(timezone.utc) - timedelta(seconds=5)
    child_ts = datetime.now(timezone.utc)
    async with db.get_engine().begin() as conn:
        await conn.execute(db.executions.update()
                            .where(db.executions.c.execution_id == rc.root_execution_id)
                            .values(started_at=root_ts))
        await conn.execute(db.executions.update()
                            .where(db.executions.c.execution_id == child)
                            .values(started_at=child_ts))

    report = await execution_store.reconstruct_run("run-1")

    ids_in_order = [e["execution_id"] for e in report["executions"]]
    assert ids_in_order == [rc.root_execution_id, child]


# ── 5. ToolCall/Evidence provenance is preserved -----------------------------

@pytest.mark.asyncio
async def test_tool_call_and_evidence_provenance_links_back_to_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    durable_id, evidence_id = await _add_tool_call(rc, content="200")
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    tc = report["tool_calls"][0]
    ev = report["evidence"][0]
    assert tc["execution_id"] == rc.root_execution_id
    assert str(ev["tool_call_id"]) == str(tc["tool_call_id"]) == durable_id
    assert str(ev["evidence_id"]) == evidence_id


# ── 6. Evidence content omitted by default, present when opted in ----------

@pytest.mark.asyncio
async def test_evidence_content_omitted_by_default():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await _add_tool_call(rc, content="sensitive tool output")
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    assert "content" not in report["evidence"][0]
    assert report["evidence"][0]["content_hash"]  # metadata still present


@pytest.mark.asyncio
async def test_evidence_content_included_when_opted_in():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await _add_tool_call(rc, content="sensitive tool output")
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1", include_evidence_content=True)

    assert report["evidence"][0]["content"] == "sensitive tool output"


# ── 7. Claim/Evidence linkage is exposed -------------------------------------

@pytest.mark.asyncio
async def test_claim_evidence_linkage_is_reported():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    _, evidence_id = await _add_tool_call(rc, content="6 endpoints")
    await execution_store.persist_claim(rc, "6 endpoints", "supported",
                                         evidence_ids=[evidence_id])
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    assert report["claims"][0]["evidence_ids"] == [evidence_id]


# ── 8. Checkpoint history with integrity validation --------------------------

@pytest.mark.asyncio
async def test_checkpoint_history_reports_hash_validity():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_checkpoint(rc, run_status="running")
    await execution_store.persist_checkpoint(rc, run_status="running")

    report = await execution_store.reconstruct_run("run-1")

    seqs = [cp["sequence"] for cp in report["checkpoints"]]
    assert seqs == sorted(seqs)
    assert all(cp["hash_valid"] for cp in report["checkpoints"])


@pytest.mark.asyncio
async def test_tampered_checkpoint_is_flagged_invalid_and_classified():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_checkpoint(rc, run_status="running")
    async with db.get_engine().begin() as conn:
        await conn.execute(db.checkpoints.update()
                            .where(db.checkpoints.c.run_id == "run-1")
                            .values(state_hash="tampered-hash"))

    report = await execution_store.reconstruct_run("run-1")

    assert report["checkpoints"][0]["hash_valid"] is False
    categories = {f["category"] for f in report["failure_classification"]}
    assert "checkpoint_invalidity" in categories


# ── 9. Ownership visibility ---------------------------------------------------

@pytest.mark.asyncio
async def test_ownership_state_is_visible_in_the_report():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.acquire_run_ownership("run-1", "worker-A")

    report = await execution_store.reconstruct_run("run-1")

    assert report["ownership"]["owner_worker_id"] == "worker-A"
    assert report["ownership"]["owned"] is True


@pytest.mark.asyncio
async def test_stale_ownership_is_classified():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    await execution_store.acquire_run_ownership("run-1", "worker-A", lease_seconds=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    async with db.get_engine().begin() as conn:
        await conn.execute(db.runs.update().where(db.runs.c.run_id == "run-1")
                            .values(owner_lease_expires_at=past))

    report = await execution_store.reconstruct_run("run-1")

    categories = {f["category"] for f in report["failure_classification"]}
    assert "stale_ownership" in categories


# ── 10. Failure classification: tool/execution/run failure + cancellation ---

@pytest.mark.asyncio
async def test_tool_failure_is_classified():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await _add_tool_call(rc, content=None, success=False, error="ValueError: boom")
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    findings = [f for f in report["failure_classification"] if f["category"] == "tool_failure"]
    assert len(findings) == 1
    assert findings[0]["subject_type"] == "tool_call"


@pytest.mark.asyncio
async def test_cancellation_is_distinguished_from_plain_tool_failure():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await _add_tool_call(rc, content=None, success=False,
                          error="CancelledError: ")
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    categories = {f["category"] for f in report["failure_classification"]}
    assert "cancellation" in categories
    assert "tool_failure" not in categories


@pytest.mark.asyncio
async def test_run_failure_is_classified():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    rc.finish_execution(rc.root_execution_id, status="failed", error="boom")
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status="failed", error="boom")

    report = await execution_store.reconstruct_run("run-1")

    categories = [f["category"] for f in report["failure_classification"]]
    assert "execution_failure" in categories
    assert "run_failure" in categories


@pytest.mark.asyncio
async def test_unsupported_claim_is_classified():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await execution_store.persist_claim(rc, "wrong fact", "contradicted")
    await _complete_run(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    categories = {f["category"] for f in report["failure_classification"]}
    assert "unsupported_contradicted_claim" in categories


@pytest.mark.asyncio
async def test_never_claims_an_llm_root_cause():
    """Explicit prompt requirement: never claim an LLM root cause the durable
    evidence doesn't support. Verify no classification category or limitation
    text implies WHY a model behaved a certain way."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await _add_tool_call(rc, content=None, success=False, error="boom")
    rc.finish_execution(rc.root_execution_id, status="failed", error="boom")
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    await execution_store.persist_run_completed(rc, status="failed", error="boom")

    report = await execution_store.reconstruct_run("run-1")

    categories = {f["category"] for f in report["failure_classification"]}
    assert "model_uncertainty" not in categories
    assert "llm_root_cause" not in categories
    limitations_text = " ".join(report["known_limitations"])
    assert "cannot be inferred from durable state" in limitations_text


# ── 11. Cross-project diagnostic denial (fail-closed) ------------------------

@pytest.mark.asyncio
async def test_cross_project_reconstruction_is_denied():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)

    report = await execution_store.reconstruct_run("run-1", project_id="project-b")

    assert report["authorized"] is False
    assert "reason" in report
    assert "executions" not in report
    assert "evidence" not in report


@pytest.mark.asyncio
async def test_same_project_reconstruction_is_authorized():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)

    report = await execution_store.reconstruct_run("run-1", project_id="project-a")

    assert report["authorized"] is True


@pytest.mark.asyncio
async def test_no_project_id_supplied_skips_authorization_check():
    sid = await _create_session("project-a")
    await _team_with_persisted_run(sid)

    report = await execution_store.reconstruct_run("run-1")

    assert report["authorized"] is True


# ── 12. Read-only behavior ----------------------------------------------------

@pytest.mark.asyncio
async def test_reconstruction_never_mutates_any_row():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await _add_tool_call(rc, content="x")
    await execution_store.persist_checkpoint(rc, run_status="running")

    before = {
        "runs": await execution_store.check_storage_integrity(),
    }
    async with db.get_engine().begin() as conn:
        run_row_before = dict((await conn.execute(
            sa.select(db.runs).where(db.runs.c.run_id == "run-1"))).mappings().first())

    await execution_store.reconstruct_run("run-1")
    await execution_store.reconstruct_run("run-1", include_evidence_content=True)

    async with db.get_engine().begin() as conn:
        run_row_after = dict((await conn.execute(
            sa.select(db.runs).where(db.runs.c.run_id == "run-1"))).mappings().first())
    assert run_row_before == run_row_after


@pytest.mark.asyncio
async def test_read_only_source_level_no_write_calls():
    import inspect
    src = inspect.getsource(execution_store.reconstruct_run)
    for forbidden in (".insert(", ".update(", ".delete("):
        assert forbidden not in src


# ── 13. Missing/partial durable records handled gracefully -------------------

@pytest.mark.asyncio
async def test_run_still_in_progress_has_no_checkpoints_or_completion_and_still_reconstructs():
    sid = await _create_session()
    await _team_with_persisted_run(sid)  # never completed

    report = await execution_store.reconstruct_run("run-1")

    assert report["authorized"] is True
    assert report["run"]["status"] == "running"
    assert report["run"]["completed_at"] is None
    assert report["checkpoints"] == []


# ── 14. Deterministic / idempotent across repeated calls ---------------------

@pytest.mark.asyncio
async def test_repeated_reconstruction_is_idempotent():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    await _add_tool_call(rc, content="a")
    await _add_tool_call(rc, content="b")
    await execution_store.persist_checkpoint(rc, run_status="running")
    await _complete_run(rc, status="ok")

    first = await execution_store.reconstruct_run("run-1")
    second = await execution_store.reconstruct_run("run-1")

    assert [e["execution_id"] for e in first["executions"]] == \
           [e["execution_id"] for e in second["executions"]]
    assert [str(t["tool_call_id"]) for t in first["tool_calls"]] == \
           [str(t["tool_call_id"]) for t in second["tool_calls"]]
    assert first["failure_classification"] == second["failure_classification"]


# ── 15. Retry lineage: attempt_number is visible per execution --------------

@pytest.mark.asyncio
async def test_retry_lineage_visible_via_attempt_number():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    rc.finish_execution(rc.root_execution_id, status="failed", error="e1")
    await execution_store.persist_execution_completed(rc.executions[rc.root_execution_id])
    retry_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    rc.executions[retry_id].attempt_number = 2
    await execution_store.persist_execution_created(rc.executions[retry_id])
    rc.finish_execution(retry_id, status="ok")
    await execution_store.persist_execution_completed(rc.executions[retry_id])
    await execution_store.persist_run_completed(rc, status="ok")

    report = await execution_store.reconstruct_run("run-1")

    attempts = sorted(e["attempt_number"] for e in report["executions"])
    assert attempts == [1, 2]


# ── 16. DB failure never raises, never returns a falsely-complete report ----

@pytest.mark.asyncio
async def test_db_failure_mid_reconstruction_returns_error_not_raise(monkeypatch):
    sid = await _create_session()
    await _team_with_persisted_run(sid)

    def _boom(*a, **kw):
        raise RuntimeError("engine unreachable")
    monkeypatch.setattr(execution_store, "get_engine", _boom)

    result = await execution_store.reconstruct_run("run-1")

    assert "error" in result
    assert "authorized" not in result
