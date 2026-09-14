"""Phase E -- durable Claim + ClaimEvidence persistence and deterministic
reconciliation (swarm/execution_store.py's persist_claim/reconcile_claim,
wired into swarm/team.py's _reconcile_completeness_claim_with_comparison via
the new _persist_completeness_claim helper, with _computed_comparison
instrumented to give that reconciliation a REAL durable evidence_id to link
against -- it previously called compare_enumerations through a bespoke MCP
session, bypassing _tool_interception_hook and therefore Phase D's ToolCall/
Evidence persistence entirely).

Same layering as test_execution_store.py (Phase C) and
test_tool_evidence_persistence.py (Phase D):

  * execution_store's own Claim primitives (reconcile_claim, persist_claim),
    exercised directly against a real (in-memory SQLite) database.
  * the actual team.py integration points (_computed_comparison,
    _reconcile_completeness_claim_with_comparison), driven the same way
    tests/test_comparison_reconciliation.py already does (a bare `_Team`
    stand-in, or a lightweight fake carrying a real RunContext) -- asserting
    on the DURABLE claims/claim_evidence rows those calls produce, not just
    on the returned content/retry behavior test_comparison_reconciliation.py
    already covers.

Every DB-touching helper is `async def` and every test is
`@pytest.mark.asyncio async def`, except the migration fixture (see
test_migrations.py's documented reasoning for why Alembic's sync command API
cannot run inside pytest-asyncio's own event loop).
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
import swarm.team as team_mod
from swarm.team import _computed_comparison, _reconcile_completeness_claim_with_comparison
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


async def _table_row_count(table) -> int:
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()


async def _claim_rows():
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(db.claims))).mappings().all()


async def _claim_evidence_rows():
    async with db.get_engine().begin() as conn:
        return (await conn.execute(sa.select(db.claim_evidence))).mappings().all()


async def _evidence_row(evidence_id: str):
    async with db.get_engine().begin() as conn:
        return (await conn.execute(
            sa.select(db.evidence).where(db.evidence.c.evidence_id == evidence_id)
        )).mappings().first()


async def _team_with_persisted_run(session_id: str, run_id: str = "run-1") -> types.SimpleNamespace:
    team = types.SimpleNamespace()
    rc = RunContext(session_id, run_id)
    rc.start_execution("Coordinator", "coordinator", parent_execution_id=None)
    team._run_context = rc
    await execution_store.persist_run_started(rc)
    return team


async def _existing_evidence_id(team) -> str:
    """A real, already-persisted evidence_id to link claims to -- created via
    the same ToolCall/Evidence path Phase D wired, not fabricated. Returns
    the NEWLY created row's id (set-difference before/after), so calling
    this twice for the same team yields two distinct ids rather than always
    the same unordered "first" row."""
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
    assert len(new_ids) == 1
    return new_ids[0]


# ── 1-8. reconcile_claim: forensic cases A-F ---------------------------------

def test_case_a_supported_exact_match():
    assert execution_store.reconcile_claim({"129"}, {"129"}) == "supported"


def test_case_b_unsupported_disjoint_values_is_contradicted():
    # Evidence establishes X; claim says Y.
    assert execution_store.reconcile_claim({"Y"}, {"X"}) == "contradicted"


def test_case_c_wrong_subset_is_partially_supported_not_fully_supported():
    # Evidence establishes {A,B,C,D,E,F}; claim says missing {A,B,C,E,F} only
    # (i.e. the claim's own "missing" set omits D) -- modeled here as the
    # claim's asserted set overlapping, but not equaling, the evidence set.
    evidence = {"A", "B", "C", "D", "E", "F"}
    claimed = {"A", "B", "C", "E", "F"}
    verdict = execution_store.reconcile_claim(claimed, evidence)
    assert verdict == "partially_supported"
    assert verdict != "supported"


def test_case_d_wrong_target_is_contradicted_not_silently_validated():
    # Evidence belongs to target A; claim refers to target B.
    evidence_for_a = {"target-a-value"}
    claim_about_b = {"target-b-value"}
    assert execution_store.reconcile_claim(claim_about_b, evidence_for_a) == "contradicted"


def test_case_e_field_relabeling_is_not_accepted_solely_because_value_matches():
    # Evidence: field_A = value_1.  Claim: field_B = value_1.
    # reconcile_claim compares whatever strings the CALLER passes -- a caller
    # that (correctly) scopes the comparison to "field=value" pairs, not bare
    # values, gets "contradicted" because the two full pairs differ, even
    # though the bare value is identical.
    evidence = {"field_A=value_1"}
    claim = {"field_B=value_1"}
    assert execution_store.reconcile_claim(claim, evidence) == "contradicted"


def test_case_f_unverifiable_when_no_evidence_to_compare():
    assert execution_store.reconcile_claim({"anything"}, set()) == "unverifiable"


def test_case_f_unverifiable_when_claim_is_empty():
    assert execution_store.reconcile_claim(set(), {"anything"}) == "unverifiable"


def test_reconcile_claim_never_returns_supported_for_partial_overlap():
    for _ in range(20):
        verdict = execution_store.reconcile_claim({"a", "b"}, {"b", "c"})
        assert verdict != "supported"


def test_reconcile_claim_accepts_a_bare_string_as_a_single_element_set():
    assert execution_store.reconcile_claim("x", {"x"}) == "supported"


# ── 9-16. persist_claim -------------------------------------------------

@pytest.mark.asyncio
async def test_persist_claim_mints_a_genuine_uuid():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context

    claim_id = await execution_store.persist_claim(rc, "backend has 26956 chars", "supported")

    assert claim_id is not None
    uuid.UUID(claim_id)  # raises if not well-formed


@pytest.mark.asyncio
async def test_persist_claim_references_the_correct_run():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid, run_id="run-xyz")
    rc = team._run_context

    claim_id = await execution_store.persist_claim(rc, "stmt", "supported")

    rows = await _claim_rows()
    assert rows[0]["claim_id"] == claim_id
    assert rows[0]["run_id"] == "run-xyz"


@pytest.mark.asyncio
async def test_persist_claim_references_the_current_execution():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    root = rc.root_execution_id

    await execution_store.persist_claim(rc, "stmt", "supported")

    rows = await _claim_rows()
    assert rows[0]["execution_id"] == root


@pytest.mark.asyncio
async def test_persist_claim_rejects_an_invalid_status():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    with pytest.raises(ValueError):
        await execution_store.persist_claim(team._run_context, "stmt", "definitely_not_a_real_status")
    assert await _table_row_count(db.claims) == 0


@pytest.mark.asyncio
async def test_claim_evidence_links_the_correct_claim_to_the_correct_evidence():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)

    claim_id = await execution_store.persist_claim(
        team._run_context, "backend has 26956 characters", "supported",
        evidence_ids=[evidence_id])

    links = await _claim_evidence_rows()
    assert len(links) == 1
    assert links[0]["claim_id"] == claim_id
    assert links[0]["evidence_id"] == evidence_id


@pytest.mark.asyncio
async def test_existing_evidence_is_reused_not_duplicated():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)
    assert await _table_row_count(db.evidence) == 1

    await execution_store.persist_claim(
        team._run_context, "claim one", "supported", evidence_ids=[evidence_id])
    await execution_store.persist_claim(
        team._run_context, "claim two", "contradicted", evidence_ids=[evidence_id])

    assert await _table_row_count(db.evidence) == 1  # still exactly one row
    assert await _table_row_count(db.claims) == 2
    assert await _table_row_count(db.claim_evidence) == 2


@pytest.mark.asyncio
async def test_exact_evidence_is_unchanged_after_claim_creation():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)
    before = await _evidence_row(evidence_id)

    await execution_store.persist_claim(
        team._run_context, "a claim that might be wrong", "contradicted",
        evidence_ids=[evidence_id])

    after = await _evidence_row(evidence_id)
    assert after["content"] == before["content"]
    assert after["content_hash"] == before["content_hash"]
    assert after["success"] == before["success"]


@pytest.mark.asyncio
async def test_multiple_evidence_rows_can_support_one_claim():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    ev1 = await _existing_evidence_id(team)
    ev2 = await _existing_evidence_id(team)
    assert ev1 != ev2

    claim_id = await execution_store.persist_claim(
        team._run_context, "stmt", "supported", evidence_ids=[ev1, ev2])

    links = await _claim_evidence_rows()
    assert {r["evidence_id"] for r in links if r["claim_id"] == claim_id} == {ev1, ev2}


@pytest.mark.asyncio
async def test_multiple_claims_can_reference_the_same_evidence():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)

    c1 = await execution_store.persist_claim(
        team._run_context, "claim A", "supported", evidence_ids=[evidence_id])
    c2 = await execution_store.persist_claim(
        team._run_context, "claim B", "contradicted", evidence_ids=[evidence_id])

    links = await _claim_evidence_rows()
    assert {r["claim_id"] for r in links} == {c1, c2}


# ── 17-18. Fail-open / real-failure distinction ------------------------------

@pytest.mark.asyncio
async def test_persist_claim_failure_is_fail_open_via_guard(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)

    async def _boom(*a, **k):
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "persist_claim", _boom)
    result = await execution_store.guard(execution_store.persist_claim(
        team._run_context, "stmt", "supported"))
    assert result is None  # swallowed, not raised


@pytest.mark.asyncio
async def test_persist_claim_programmer_error_is_not_swallowed_by_its_own_validation():
    """An invalid status is a caller bug, not an I/O failure -- persist_claim
    itself raises ValueError (see its own docstring); only guard() at a
    runtime call site is responsible for turning that into fail-open
    behavior, and this test proves persist_claim does NOT quietly convert
    the error into None on its own."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    with pytest.raises(ValueError):
        await execution_store.persist_claim(team._run_context, "stmt", "bogus")


# ── 19. Retry claims attach to the correct Execution -------------------------

@pytest.mark.asyncio
async def test_retry_claim_attaches_to_the_retry_execution_not_root():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    rc = team._run_context
    root = rc.root_execution_id
    retry_id = rc.start_execution("Coordinator", "coordinator", parent_execution_id=root)
    await execution_store.persist_execution_created(rc.executions[retry_id])

    claim_id = await execution_store.persist_claim(rc, "retry-time claim", "supported")

    rows = await _claim_rows()
    row = next(r for r in rows if r["claim_id"] == claim_id)
    assert row["execution_id"] == retry_id
    assert row["execution_id"] != root


# ── 20-21. Session cascade ----------------------------------------------

@pytest.mark.asyncio
async def test_session_deletion_cascades_to_claims_and_claim_evidence():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)
    await execution_store.persist_claim(
        team._run_context, "stmt", "supported", evidence_ids=[evidence_id])
    assert await _table_row_count(db.claims) == 1
    assert await _table_row_count(db.claim_evidence) == 1

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        await conn.execute(db.chat_sessions.delete().where(db.chat_sessions.c.id == sid))

    assert await _table_row_count(db.claims) == 0
    assert await _table_row_count(db.claim_evidence) == 0
    # Evidence itself is session-owned too, via tool_calls -> executions ->
    # runs -> chat_sessions -- also removed, not orphaned.
    assert await _table_row_count(db.evidence) == 0


@pytest.mark.asyncio
async def test_deleting_evidence_cascades_to_claim_evidence_but_not_the_claim():
    """claims.execution_id is SET NULL on execution delete (a claim's
    supporting execution is incidental context, per swarm/db.py's own
    comment) -- but claim_evidence rows for a deleted Evidence row are
    CASCADE-deleted, since a provenance link to Evidence that no longer
    exists cannot be kept."""
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    evidence_id = await _existing_evidence_id(team)
    claim_id = await execution_store.persist_claim(
        team._run_context, "stmt", "supported", evidence_ids=[evidence_id])

    async with db.get_engine().begin() as conn:
        await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        await conn.execute(db.evidence.delete().where(db.evidence.c.evidence_id == evidence_id))

    assert await _table_row_count(db.claim_evidence) == 0
    remaining = await _claim_rows()
    assert [r["claim_id"] for r in remaining] == [claim_id]  # the Claim itself survives


# ── 22-24. team.py integration: the wired completeness-claim boundary -------

NO_GAP_RAW_TEXT = (
    "compare_enumerations — a.py  vs  b.ts\n"
    "TOTALS: left 2, right 2, matched 2, left-only 0, right-only 0."
)
GAP_RAW_TEXT = (
    "compare_enumerations — a.py  vs  b.ts\n"
    "LEFT ONLY — defined on the left with no match on the right (6):\n"
    "  POST /register\n"
    "TOTALS: left 13, right 16, matched 7, left-only 6, right-only 9."
)
NO_GAPS_CLAIM_CONTENT = (
    "### Endpoints:\n- POST /register\n\n### Gap Analysis:\n"
    "There are no missing or mismatched items."
)
NAMED_GAPS_CONTENT = (
    "### Gap Analysis:\n6 endpoints have no corresponding hook: /register."
)


class _FakeSession:
    def __init__(self, text):
        self._text = text

    async def call_tool(self, name, args):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=self._text)])


class _FakeHiveMcpTools:
    def __init__(self, text):
        self._text = text

    async def get_session_for_run(self):
        return _FakeSession(self._text)


ENUMERATIONS = {"a": {"path": "a.py", "count": 5}, "b": {"path": "b.ts", "count": 5}}
TWO_SIDED_TASK = "list every endpoint in a.py, then list every hook in b.ts"


@pytest.mark.asyncio
async def test_computed_comparison_persists_a_real_tool_call_and_evidence_row():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)

    cmp_note = await _computed_comparison(
        TWO_SIDED_TASK, ENUMERATIONS, "http://fake", _FakeHiveMcpTools(NO_GAP_RAW_TEXT),
        "", team=team)

    assert "TOTALS: left 2, right 2, matched 2, left-only 0, right-only 0." in cmp_note
    assert await _table_row_count(db.tool_calls) == 1
    assert await _table_row_count(db.evidence) == 1
    assert getattr(team, "_last_comparison_evidence_id", None) is not None


@pytest.mark.asyncio
async def test_supported_completeness_claim_is_persisted_with_a_zero_gap():
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    team._read_state = {"enumerations": ENUMERATIONS}

    cmp_note = await _computed_comparison(
        TWO_SIDED_TASK, ENUMERATIONS, "http://fake", _FakeHiveMcpTools(NO_GAP_RAW_TEXT),
        NO_GAPS_CLAIM_CONTENT, team=team)
    content, result, reconciled = await _reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, TWO_SIDED_TASK, team, [None], None, None, cmp_note, False)

    assert reconciled is False              # existing control flow: nothing to reconcile
    assert content == NO_GAPS_CLAIM_CONTENT  # unchanged, exactly as before Phase E
    rows = await _claim_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "supported"
    assert "no missing or mismatched items" in rows[0]["statement"]
    links = await _claim_evidence_rows()
    assert len(links) == 1  # linked to the real compare_enumerations Evidence


@pytest.mark.asyncio
async def test_contradicted_completeness_claim_is_persisted_with_a_real_gap(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    team._read_state = {"enumerations": ENUMERATIONS}

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return NAMED_GAPS_CONTENT, types.SimpleNamespace(messages=[])

    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    cmp_note = await _computed_comparison(
        TWO_SIDED_TASK, ENUMERATIONS, "http://fake", _FakeHiveMcpTools(GAP_RAW_TEXT),
        NO_GAPS_CLAIM_CONTENT, team=team)
    content, result, reconciled = await _reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, TWO_SIDED_TASK, team, [None], None, None, cmp_note, False)

    assert reconciled is True  # existing retry/adopt behavior, unchanged by Phase E
    rows = await _claim_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "contradicted"
    assert "no missing or mismatched items" in rows[0]["statement"]
    links = await _claim_evidence_rows()
    assert len(links) == 1


@pytest.mark.asyncio
async def test_claim_persistence_failure_does_not_change_reconciliation_behavior(monkeypatch):
    sid = await _create_session()
    team = await _team_with_persisted_run(sid)
    team._read_state = {"enumerations": ENUMERATIONS}

    async def _boom(*a, **k):
        raise RuntimeError("db is down")

    monkeypatch.setattr(execution_store, "persist_claim", _boom)

    cmp_note = await _computed_comparison(
        TWO_SIDED_TASK, ENUMERATIONS, "http://fake", _FakeHiveMcpTools(NO_GAP_RAW_TEXT),
        NO_GAPS_CLAIM_CONTENT, team=team)
    content, result, reconciled = await _reconcile_completeness_claim_with_comparison(
        NO_GAPS_CLAIM_CONTENT, TWO_SIDED_TASK, team, [None], None, None, cmp_note, False)

    assert reconciled is False
    assert content == NO_GAPS_CLAIM_CONTENT  # unaffected by the persistence failure
    assert await _table_row_count(db.claims) == 0  # nothing landed, and nothing crashed


# ── 25. No project-memory / Phase F leakage ----------------------------------

def test_execution_store_never_references_project_memory_promotion():
    """Checks for actual call/reference SHAPES, not the bare words -- this
    module's own docstrings legitimately name LightRAG/Qdrant/AGE/
    record_success/task_outcome_queue in prose explaining what Phase E does
    NOT do, which a bare substring check would itself trip on."""
    import inspect
    source = inspect.getsource(execution_store)
    forbidden = (
        "lightrag_query(", "lightrag.insert", "import lightrag",
        "qdrant_client", "from qdrant", "import qdrant",
        "record_success(", "task_outcome_queue.insert", "index_project(",
        "memory_store(", "memory_search(",
    )
    for term in forbidden:
        assert term not in source, f"Phase E must not reference {term!r}"


def test_persist_completeness_claim_never_references_project_memory_promotion():
    import inspect
    source = inspect.getsource(team_mod._persist_completeness_claim)
    for term in ("lightrag", "qdrant", "record_success", "task_outcome_queue"):
        assert term not in source.lower()


def test_persist_claim_and_reconcile_claim_never_generate_run_or_execution_ids():
    """Phase E must not mint run_id/execution_id -- only claim_id. Source-level
    guard: the only uuid4() mint site in persist_claim is for claim_id."""
    import inspect
    source = inspect.getsource(execution_store.persist_claim)
    assert source.count("_uuid.uuid4()") == 1


def test_reconciliation_call_sites_use_guard():
    import inspect
    source = inspect.getsource(team_mod._persist_completeness_claim)
    assert "execution_store.guard(execution_store.persist_claim(" in source


# ── existing-behavior regression: unchanged comparison-reconciliation tests --
# (full re-run lives in tests/test_comparison_reconciliation.py; not duplicated
# here beyond the two Phase-E-aware integration tests above.)
