"""Phase C/D/E/F -- durable persistence for Run, Execution, ToolCall,
Evidence, Claim, ClaimEvidence and (Phase F) project-owned memory promotion.

LLM messages remain a view of execution state; this module is what makes the
underlying execution state itself durable, in the Phase B schema
(swarm/db.py's `runs`/`executions`/`tool_calls`/`evidence`/`claims`/
`claim_evidence` tables), mirroring Phase A's in-memory runtime identity
(swarm/execution_context.py's RunContext/ExecutionRecord/ToolCallRecord/
EvidenceRecord) without changing anything about how that identity is
computed.

Phase E adds Claim/ClaimEvidence persistence plus one deterministic
reconciliation primitive (reconcile_claim). It does NOT add project-memory
promotion of any kind -- no LightRAG/Qdrant/AGE write, no record_success/
task_outcome_queue call, appears anywhere in the Phase E functions in this
module; a validated Claim was the END of Phase E's responsibility.

Phase F adds exactly that promotion, but into a NEW, project-owned table
(swarm/db.py's project_memory_promotions) that is deliberately SEPARATE from
LightRAG/Qdrant/AGE's existing experience-namespace memory (swarm/feedback.py's
record_success/_queue_outcome/task_outcome_queue) -- Phase F does not read,
write, or otherwise touch that system at all; the two memory paths run side
by side, fed by the same human trigger, never merged.

Phase F's promotion boundary is deliberately narrower than "a Claim is
supported": promote_session_claims is called from exactly one place --
api/server.py's /feedback endpoint, only on its rating=="good" branch (the
one existing, explicit, human-driven approval signal already established in
this codebase; see swarm/feedback.py's own _queue_outcome docstring for the
2026-08-29 incident that is the reason ordinary run completion or a
deterministic "supported" verdict ALONE must never be enough: a fabrication
no guard caught once became the top retrieved exemplar for its own question,
purely because "no guard objected" was mistaken for "this was validated").
So Phase F requires BOTH signals together: a Claim already deterministically
verdicted "supported" by Phase E's reconciliation (evidence-backed, not mere
absence of complaint), AND a human's explicit /feedback approval of the
session that produced it. Neither signal alone triggers a promotion.

Fail-open by design, and this is the one thing every function here must never
violate: a persistence failure is logged and swallowed, never raised,
because runtime execution (delegation, retries, tool calls, the final answer)
must continue identically whether or not the database is reachable. This
mirrors the exact try/except-and-print convention already used throughout
swarm/sessions.py and swarm/feedback.py for the same reason -- not a new
failure-handling idiom, the established one.

Run/Execution identity is never re-derived or re-generated here: every
run_id/execution_id written to the database is read verbatim from the
RunContext/ExecutionRecord objects Phase A already created.

ToolCall/Evidence identity is DIFFERENT, deliberately (Phase D): Phase A's
ToolCallRecord.tool_call_id is execution_context.new_id() -- the same
12-hex-char uuid4().hex[:12] scheme as run_id/execution_id, not a
well-formed UUID -- but tool_calls.tool_call_id/evidence.evidence_id are
genuine sa.Uuid columns (Phase C deliberately did NOT weaken those to Text
the way it did for runs.run_id/executions.execution_id, since Phase C never
wrote to them). Phase D's instructions are explicit that this must be
resolved by minting real UUIDs here, not by repeating Phase C's column-type
fix or by changing execution_context.py's identity scheme. So
persist_tool_call_created below mints a fresh, genuine `uuid.uuid4()` for
the durable row and hands it back to the caller -- a second, DB-only id
that exists alongside (never replacing) ToolCallRecord.tool_call_id.
"""
from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, TypeVar

import sqlalchemy as sa
from sqlalchemy.sql import func

from swarm import db
from swarm.db import get_engine
from swarm.execution_context import EvidenceRecord, ExecutionRecord, RunContext, ToolCallRecord

T = TypeVar("T")


async def guard(awaitable: Awaitable[T]) -> T | None:
    """Defense-in-depth wrapper every swarm/team.py call site uses around
    every function in this module. Each persist_* function below already has
    its own internal try/except (the realistic failure mode: a DB connection
    error, an IntegrityError, etc.) -- this is the LAST-RESORT layer that
    guarantees nothing from this module can ever propagate into team.py's own
    control flow even if that inner handling has a bug, is bypassed by a
    future refactor, or (as one of this module's own tests demonstrated) a
    caller replaces a persist_* function with something that doesn't have
    the same protection. Usage: `await execution_store.guard(execution_store.
    persist_run_started(run_context))` -- the call itself only builds a
    coroutine object; nothing runs until `guard` awaits it here, inside the
    try.

    Returns the awaited value on success, or None if it raised (Phase D's
    persist_tool_call_created returns a durable id its caller needs to pass
    to persist_tool_call_completed -- None here means exactly what
    persist_tool_call_created's own None return means: "no durable row
    exists", handled identically either way).
    """
    try:
        return await awaitable
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] unexpected failure escaped a persist_* "
              f"call -- swallowed here as the last line of defense: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


def _canonicalize_result(result: Any) -> str:
    """The EXACT text of a tool result, canonicalized so identical results
    always produce identical bytes (content_hash's whole contract depends on
    this). Never a raw `str(obj)` repr for anything that isn't already text
    or reliably serializable:

      * a bare str -> stored verbatim.
      * an MCP-style wrapper (e.g. CallToolResult, shaped with a
        .content/.text/.data/.output/.result attribute) -> the wrapped text,
        unwrapped the same way swarm/team.py's _result_text does. NOT a call
        to _result_text itself -- that function exists for a different,
        deliberately lossy purpose (phase0 telemetry/prompt text, whose own
        docstring calls its str(result) fallback "the last resort... a
        repr"), and importing it here would be circular (swarm/team.py
        imports this module). The two are independently implemented but
        must agree on what "the tool's real text" is, hence the mirrored
        logic.
      * a JSON-serializable value (dict/list/tuple/int/float/bool) that
        isn't already covered above -> canonical JSON (sort_keys=True), so
        key order never perturbs the hash.
      * anything else (e.g. the async generator delegate_task_to_member can
        return when "the run was handed back to be iterated") -> an
        explicit, deterministic marker naming the type. str() on a generator
        embeds its memory address, which would silently break "same result
        -> same hash" -- this marker is reproducible (same type -> same
        marker) instead.
    """
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    for attr in ("content", "text", "data", "output", "result"):
        val = getattr(result, attr, None)
        if isinstance(val, str):
            return val
        if isinstance(val, (list, tuple)):
            parts = [b if isinstance(b, str) else getattr(b, "text", None) for b in val]
            parts = [p for p in parts if isinstance(p, str)]
            if parts:
                return "\n".join(parts)
    if isinstance(result, (dict, list, tuple, int, float, bool)):
        try:
            return json.dumps(result, sort_keys=True, ensure_ascii=False)
        except TypeError:
            pass
    return f"<non-text result: {type(result).__name__}>"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def persist_run_started(
    run_context: RunContext,
    *,
    team_name: str | None = None,
    run_type: str | None = None,
    task_preview: str | None = None,
) -> None:
    """INSERT the `runs` row AND its root coordinator `executions` row in ONE
    transaction, so a persisted execution can never reference a run that does
    not exist, and a persisted run can never be missing its root execution
    because the second insert failed independently -- either both land or
    neither does.

    Call once, immediately after RunContext.start_execution() has created the
    root execution in memory (see swarm/team.py's run_task_async/
    run_task_stream) -- reads run_context.root_execution_id/executions for
    the row to persist rather than taking those fields as separate arguments,
    so the durable row can never drift from what the in-memory object says.

    Silently skipped (not an error) when run_context.session_id is None --
    runs.session_id is NOT NULL with a real FK to chat_sessions.id, and a
    caller with no session (e.g. main.py's bare CLI one-shot path, which
    calls run_task_async with no session_id at all) has nothing for a durable
    Run to attach to. This is an expected, legitimate case, not a failure.
    """
    root_id = run_context.root_execution_id
    root = run_context.executions.get(root_id) if root_id else None
    if root is None:
        print(f"[execution_store] persist_run_started: no root execution on "
              f"run_id={run_context.run_id!r} -- skipping", flush=True)
        return
    if run_context.session_id is None:
        return
    try:
        async with get_engine().begin() as conn:
            await conn.execute(db.runs.insert().values(
                run_id=run_context.run_id,
                session_id=run_context.session_id,
                run_type=run_type,
                team_name=team_name,
                task_preview=task_preview,
                status="running",
            ))
            await conn.execute(db.executions.insert().values(
                execution_id=root.execution_id,
                run_id=root.run_id,
                parent_execution_id=root.parent_execution_id,
                agent_name=root.agent_name,
                execution_type=root.execution_type,
                attempt_number=root.attempt_number,
                status=root.status,
            ))
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_run_started failed "
              f"(run_id={run_context.run_id!r}): {type(exc).__name__}: {exc}",
              flush=True)


async def persist_run_completed(
    run_context: RunContext, status: str, error: str | None = None,
) -> None:
    """UPDATE the `runs` row's status/error/completed_at. Called exactly once,
    at the true end of run_task_async/run_task_stream (their outermost
    finally, which runs regardless of which return/yield path was taken or
    whether an exception propagated) -- deliberately NOT tied to the root
    execution's own completion, since retries can still be running (as new
    sibling Executions under the same Run) after the root execution's own
    attempt has already finished.

    A no-op (not an error) if persist_run_started() never ran or failed for
    this run_id -- the UPDATE simply affects zero rows, which is the correct,
    silent behavior: this function does not fabricate a Run row that was
    never created.
    """
    if run_context.session_id is None:
        return
    try:
        async with get_engine().begin() as conn:
            await conn.execute(
                db.runs.update()
                .where(db.runs.c.run_id == run_context.run_id)
                .values(status=status, error_message=error, completed_at=func.now())
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_run_completed failed "
              f"(run_id={run_context.run_id!r}): {type(exc).__name__}: {exc}",
              flush=True)


async def persist_execution_created(record: ExecutionRecord) -> None:
    """INSERT one `executions` row from an already-created in-memory
    ExecutionRecord -- called for every delegation and every retry, right
    after RunContext.start_execution() creates it (swarm/team.py's
    _tool_interception_hook and _stream_team_run). Never called for the root
    execution, which is created together with its Run by
    persist_run_started() so the two can share one transaction.

    A no-op (not raised) if the parent run/execution row does not exist in
    the database -- the INSERT's own FK constraint rejects it, caught and
    logged like any other persistence failure; the in-memory execution and
    the rest of the run are completely unaffected.
    """
    try:
        async with get_engine().begin() as conn:
            await conn.execute(db.executions.insert().values(
                execution_id=record.execution_id,
                run_id=record.run_id,
                parent_execution_id=record.parent_execution_id,
                agent_name=record.agent_name,
                execution_type=record.execution_type,
                attempt_number=record.attempt_number,
                status=record.status,
            ))
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_execution_created failed "
              f"(execution_id={record.execution_id!r}, run_id={record.run_id!r}): "
              f"{type(exc).__name__}: {exc}", flush=True)


async def persist_execution_completed(record: ExecutionRecord) -> None:
    """UPDATE the `executions` row's status/error/completed_at from an
    already-finished in-memory ExecutionRecord (i.e. called AFTER
    RunContext.finish_execution() has updated it) -- for the root execution
    (in run_task_async's/run_task_stream's own finally blocks), delegations,
    and retries alike. A no-op if the row was never created (the same
    fail-open reasoning as persist_execution_created)."""
    try:
        async with get_engine().begin() as conn:
            await conn.execute(
                db.executions.update()
                .where(db.executions.c.execution_id == record.execution_id)
                .values(status=record.status, error_message=record.error,
                        completed_at=func.now())
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_execution_completed failed "
              f"(execution_id={record.execution_id!r}): {type(exc).__name__}: {exc}",
              flush=True)


async def persist_tool_call_created(tool_call: ToolCallRecord) -> str | None:
    """INSERT one `tool_calls` row in status "running", called right after
    RunContext.start_tool_call() creates the in-memory ToolCallRecord
    (swarm/team.py's _tool_interception_hook) -- before the real tool
    function has even been awaited, so a tool call that never returns
    (hangs, or is cancelled) still has a durable row rather than no record
    at all.

    Returns the freshly minted, genuine UUID4 string used as this row's
    tool_call_id -- see this module's own docstring for why that is a NEW
    id, not ToolCallRecord.tool_call_id verbatim. Callers must carry this
    return value forward to persist_tool_call_completed(); None means no
    durable row exists (either there was no current execution to attribute
    the call to, or the INSERT failed) and the caller must skip completion
    accordingly.
    """
    if tool_call.execution_id is None:
        print(f"[execution_store] persist_tool_call_created: no current "
              f"execution for tool_name={tool_call.tool_name!r} -- "
              f"skipping", flush=True)
        return None
    durable_id = str(_uuid.uuid4())
    try:
        async with get_engine().begin() as conn:
            await conn.execute(db.tool_calls.insert().values(
                tool_call_id=durable_id,
                execution_id=tool_call.execution_id,
                tool_name=tool_call.tool_name,
                arguments=tool_call.arguments,
                status="running",
            ))
        return durable_id
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_tool_call_created failed "
              f"(tool_name={tool_call.tool_name!r}, "
              f"execution_id={tool_call.execution_id!r}): "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


async def persist_tool_call_completed(
    durable_tool_call_id: str | None,
    tool_call: ToolCallRecord,
    evidence: EvidenceRecord,
) -> str | None:
    """UPDATE the `tool_calls` row's status/error/completed_at AND INSERT its
    `evidence` row in ONE transaction, so a completed-but-evidence-less
    ToolCall or an Evidence row pointing at a still-"running" ToolCall can
    never happen -- either both land together or neither does (same
    orphan-avoidance reasoning as persist_run_started's Run+root-Execution
    transaction).

    A no-op (returns None) if durable_tool_call_id is None --
    persist_tool_call_created either failed or was skipped, so there is no
    row to complete and no valid tool_calls.tool_call_id for
    evidence.tool_call_id's FK to reference.

    `evidence.content` is canonicalized (see _canonicalize_result) before
    being stored and hashed -- content/content_hash are always computed
    from the SAME canonicalized string, so "same result -> same hash" and
    "different result -> different hash" both hold by construction.

    Returns the freshly minted evidence_id on success, or None on failure/
    no-op -- Phase E's persist_claim needs this id to link a Claim to the
    Evidence it was reconciled against (via claim_evidence), and this is the
    only place that id is minted, so it must be surfaced rather than
    discarded. Existing Phase D callers that never used a return value are
    unaffected.
    """
    if durable_tool_call_id is None:
        return None
    content_text = _canonicalize_result(evidence.content)
    evidence_id = str(_uuid.uuid4())
    try:
        async with get_engine().begin() as conn:
            await conn.execute(
                db.tool_calls.update()
                .where(db.tool_calls.c.tool_call_id == durable_tool_call_id)
                .values(status=tool_call.status, error_message=tool_call.error,
                        completed_at=func.now())
            )
            await conn.execute(db.evidence.insert().values(
                evidence_id=evidence_id,
                tool_call_id=durable_tool_call_id,
                content=content_text,
                content_hash=_content_hash(content_text),
                success=evidence.success,
                error_message=evidence.error,
            ))
        return evidence_id
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_tool_call_completed failed "
              f"(tool_call_id={durable_tool_call_id!r}): "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


# ============================================================================
# Phase E -- Claim + ClaimEvidence persistence, and one deterministic
# reconciliation primitive.
# ============================================================================

# The full, closed vocabulary Phase E's schema/persistence layer accepts.
# Only "supported" and "contradicted" are actually produced by the one wired
# reconciliation boundary this phase instruments
# (swarm/team.py's _reconcile_completeness_claim_with_comparison); the other
# three are real, tested states of the reconcile_claim() primitive below,
# available to a later phase's reconciliation boundary without inventing a
# new ontology when it needs them. Deliberately NOT including a separate
# "unsupported": for a set-based comparison, a claim that is not fully
# supported is either partially supported (nonempty overlap with evidence),
# fully contradicted (disjoint from non-empty evidence), or unverifiable (no
# evidence to compare against) -- a fourth undifferentiated "unsupported"
# bucket would just be contradicted+unverifiable relabeled.
VALID_CLAIM_STATUSES = frozenset(
    {"supported", "contradicted", "partially_supported", "unverifiable"})


def reconcile_claim(claimed: set[str] | str, evidence_values: set[str]) -> str:
    """Deterministic verdict for a claim expressed as a set of discrete,
    exact values (filenames, field values, counts rendered as strings, etc.)
    against the set of values Evidence actually establishes for the SAME
    thing. This is a SET-COMPARISON primitive, not a natural-language claim
    parser -- callers reduce a claim/evidence pair to comparable sets
    themselves (e.g. compare_enumerations' left-only/right-only filenames,
    or a single field=value pair as a one-element set).

    Returns exactly one of VALID_CLAIM_STATUSES's four values:

      "unverifiable"          -- claimed or evidence_values is empty:
                                 nothing to compare, so nothing is asserted
                                 either way. Never returned as "supported".
      "supported"             -- claimed == evidence_values, exactly.
      "contradicted"          -- claimed and evidence_values share NOTHING
                                 (disjoint), and evidence_values is
                                 non-empty: evidence positively rules out
                                 everything the claim asserts.
      "partially_supported"   -- some but not all overlap -- neither a full
                                 match nor a full contradiction.

    Never returns "supported" merely because a claim's WORDING resembles the
    evidence, and never collapses a partial or contradicted claim into
    "supported" -- see this module's own test suite for the field-relabeling
    case this guards against: comparing VALUES only, never claimed field
    NAMES, is a deliberate, narrow contract. A caller that passes
    {"field_B=value_1"} as `claimed` and {"field_A=value_1"} as
    `evidence_values` gets "contradicted", not "supported" -- the two
    strings are literally different, and this function does no field-name-
    aware matching of any kind.
    """
    claimed_set = claimed if isinstance(claimed, set) else {claimed}
    if not claimed_set or not evidence_values:
        return "unverifiable"
    if claimed_set == evidence_values:
        return "supported"
    if not (claimed_set & evidence_values):
        return "contradicted"
    return "partially_supported"


async def persist_claim(
    run_context: RunContext,
    statement: str,
    status: str,
    *,
    evidence_ids: list[str] | None = None,
) -> str | None:
    """INSERT one `claims` row, plus one `claim_evidence` row per id in
    `evidence_ids`, in ONE transaction -- so a ClaimEvidence row can never
    reference a Claim that failed to insert, matching persist_run_started's
    and persist_tool_call_completed's own orphan-avoidance reasoning.

    claim_id is a freshly minted, genuine UUID4 string -- the identical
    identity discipline Phase D established for tool_call_id/evidence_id
    (see this module's own docstring): NOT execution_context.new_id()'s
    12-hex-char runtime scheme, and NOT re-derived from `statement` or any
    other prose.

    run_id is read verbatim from run_context (never re-derived).
    execution_id is whatever execution is CURRENT on run_context at call
    time (None if none is open) -- claims.execution_id is SET NULL on
    delete (see swarm/db.py's own comment: a claim's supporting execution is
    incidental context, not what makes the claim exist), so this is
    correctly optional.

    `evidence_ids` must already exist as real evidence.evidence_id values
    (e.g. returned by persist_tool_call_completed) -- this function NEVER
    creates or duplicates an Evidence row, only links to rows that already
    exist; the FK on claim_evidence.evidence_id enforces this at the
    database level too. A no-op session (run_context.session_id is None,
    the same convention persist_run_started uses) skips persistence
    entirely -- there is no session-owned Run for the claim to attach to.

    status must be one of VALID_CLAIM_STATUSES -- passing anything else is a
    caller bug, not a database-reachability failure, so this raises
    ValueError immediately rather than being swallowed by the fail-open
    try/except below (which exists for I/O failures, not for programming
    errors).
    """
    if status not in VALID_CLAIM_STATUSES:
        raise ValueError(f"persist_claim: invalid status {status!r}, "
                          f"must be one of {sorted(VALID_CLAIM_STATUSES)}")
    if run_context.session_id is None:
        return None
    claim_id = str(_uuid.uuid4())
    try:
        async with get_engine().begin() as conn:
            await conn.execute(db.claims.insert().values(
                claim_id=claim_id,
                run_id=run_context.run_id,
                execution_id=run_context.current_execution_id,
                statement=statement,
                status=status,
            ))
            for evidence_id in (evidence_ids or []):
                await conn.execute(db.claim_evidence.insert().values(
                    claim_id=claim_id, evidence_id=evidence_id,
                ))
        return claim_id
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_claim failed "
              f"(run_id={run_context.run_id!r}): {type(exc).__name__}: {exc}",
              flush=True)
        return None


# ============================================================================
# Phase F -- project-owned memory promotion.
# ============================================================================

# The fixed, short descriptor stamped on every promotion this module writes --
# see this module's own docstring for why BOTH halves are required together.
VALIDATED_BY_DETERMINISTIC_PLUS_FEEDBACK = (
    "phase_e_deterministic_reconciliation+feedback_rating_good")


async def promote_claim(
    project_id: str,
    claim_row,
    evidence_snapshot: str | None,
    evidence_hash: str | None,
    validated_by: str,
    feedback_notes: str | None = None,
) -> str | None:
    """INSERT one `project_memory_promotions` row for an already-validated
    Claim -- or, if this exact (project_id, claim_id) pair was already
    promoted, return the EXISTING promotion's id unchanged (idempotent: a
    repeated /feedback call or a second good rating on the same session can
    never create a duplicate project memory, and never re-promotes or
    overwrites the first one -- see this table's own UniqueConstraint in
    swarm/db.py).

    `claim_row` is a mapping with at least claim_id/run_id/execution_id/
    statement/status (e.g. a row from `claims`, exactly what
    promote_session_claims below reads and passes through). Its status MUST
    be "supported" -- promoting a contradicted, partially_supported, or
    unverifiable claim would durably record something that was never
    actually validated, so this raises ValueError immediately (a caller
    bug, not an I/O failure) rather than silently downgrading it, the same
    convention persist_claim uses for its own status argument.

    `evidence_snapshot`/`evidence_hash` are copied verbatim into the new row
    -- never re-read from `evidence` later, and never used to look the
    Evidence row up again; this table has no FK to it (see swarm/db.py's own
    comment on this table for why: it must survive session deletion, and
    the Evidence row it snapshots does not).

    Phase I concurrency fix: the pre-check SELECT above and the INSERT
    below are two separate statements, so two genuinely concurrent
    /feedback calls for the same (project_id, claim_id) -- a real, plausible
    trigger (a double-click, a client retry-on-timeout that actually
    landed) -- can both pass the SELECT before either INSERTs. The SECOND
    INSERT then hits project_memory_promotions' own UniqueConstraint and
    raises sqlalchemy.exc.IntegrityError. Caught specifically (not by the
    generic except below, which exists for genuine I/O failures) and
    treated as the SAME idempotent outcome the pre-check SELECT would have
    produced if it had run a moment later: re-query and return the row that
    won the race, rather than reporting None (which would read as "this
    promotion failed" when it did not -- it succeeded, just via the other
    caller).
    """
    if claim_row["status"] != "supported":
        raise ValueError(
            f"promote_claim: refusing to promote a claim with "
            f"status={claim_row['status']!r} -- only 'supported' claims may "
            f"become project memory")
    promotion_id = str(_uuid.uuid4())
    try:
        async with get_engine().begin() as conn:
            existing = (await conn.execute(
                sa.select(db.project_memory_promotions.c.id)
                .where(db.project_memory_promotions.c.project_id == project_id,
                       db.project_memory_promotions.c.claim_id == claim_row["claim_id"])
            )).scalar()
            if existing is not None:
                return existing
            await conn.execute(db.project_memory_promotions.insert().values(
                id=promotion_id,
                project_id=project_id,
                claim_id=claim_row["claim_id"],
                run_id=claim_row.get("run_id"),
                execution_id=claim_row.get("execution_id"),
                statement=claim_row["statement"],
                claim_status=claim_row["status"],
                evidence_snapshot=evidence_snapshot,
                evidence_hash=evidence_hash,
                validated_by=validated_by,
                feedback_notes=feedback_notes,
            ))
        return promotion_id
    except sa.exc.IntegrityError:
        # Lost the race -- another concurrent call already inserted the SAME
        # (project_id, claim_id) between our SELECT and our INSERT. The
        # `async with` block above has already rolled back on its own (the
        # exception propagated out of it), so this is a FRESH
        # connection/transaction, not a reuse of the failed one. Look up the
        # row the winner just created and return ITS id -- the correct,
        # idempotent outcome, not a failure.
        try:
            async with get_engine().begin() as conn:
                winner = (await conn.execute(
                    sa.select(db.project_memory_promotions.c.id)
                    .where(db.project_memory_promotions.c.project_id == project_id,
                           db.project_memory_promotions.c.claim_id == claim_row["claim_id"])
                )).scalar()
            print(f"[execution_store] promote_claim: lost a concurrent "
                  f"promotion race for claim_id={claim_row['claim_id']!r} -- "
                  f"returning the winner's id instead of a duplicate", flush=True)
            return winner
        except Exception as exc:  # noqa: BLE001
            print(f"[execution_store] promote_claim: race-recovery lookup "
                  f"failed (project_id={project_id!r}, "
                  f"claim_id={claim_row.get('claim_id')!r}): "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return None
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] promote_claim failed "
              f"(project_id={project_id!r}, "
              f"claim_id={claim_row.get('claim_id')!r}): "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


async def promote_session_claims(
    session_id: str,
    project_id: str,
    *,
    feedback_notes: str | None = None,
) -> list[str]:
    """THE Phase F promotion boundary -- called from exactly one place,
    api/server.py's /feedback endpoint, only on its rating=="good" branch.
    See this module's own docstring for why that is the safest existing
    explicit-validation boundary in this codebase, and why a "supported"
    Claim by itself (an automatic, deterministic part of ordinary run
    execution) is not sufficient on its own.

    Finds every "supported" Claim belonging to any Run under `session_id`
    that has not already been promoted for `project_id` (idempotent via
    promote_claim), snapshots the content/content_hash of ONE linked
    Evidence row for each (a claim need not have linked evidence -- e.g.
    none was available to link at reconciliation time -- in which case both
    snapshot fields are None), and promotes each one.

    Fail-open, matching every other function in this module: a lookup or
    insert failure here is logged and an empty (or partial) list is
    returned, never raised -- callers use execution_store.guard(...) around
    this call for the same last-resort protection every other call site
    gets. Returns an empty list, not an error, for the ordinary "nothing to
    promote" case (no session-owned run found, or no supported claim
    found) -- this is the ordinary, common outcome for the vast majority of
    /feedback calls that name no session_id or reference a run with no
    validated completeness claim, not a failure.

    Deliberately reads the database here: this runs entirely AFTER the
    triggering run has already finished and its answer already returned
    (via a separate, later /feedback POST) -- never during tool execution,
    so it does not violate Phase D/E's write-only-during-execution
    boundary, which is scoped to the live run, not to post-hoc human
    review.

    Phase J: `project_id` is never trusted blindly -- the FIRST thing this
    function does is verify session_id's OWN chat_sessions.project_id
    matches the supplied project_id, and fails CLOSED (returns [],
    promotes nothing) on any mismatch or on a session_id that does not
    exist. See the inline comment at that check for the concrete
    cross-project promotion this closes.
    """
    try:
        async with get_engine().begin() as conn:
            # Phase J authorization boundary: verify session_id ACTUALLY
            # belongs to the caller-supplied project_id BEFORE reading or
            # promoting anything under it. Before this check,
            # promote_session_claims trusted project_id blindly -- a caller
            # could pass session_id from Project A alongside project_id
            # "B" and this function would happily promote Project A's
            # validated claims into Project B's durable memory. This is an
            # AUTHORIZATION decision, not a persistence failure: on
            # mismatch (or a session_id that does not exist at all) this
            # FAILS CLOSED -- returns [] immediately, promotes nothing --
            # never falls through to "promote anyway", unlike this
            # function's own fail-OPEN handling of genuine I/O failures
            # below.
            owner_project_id = (await conn.execute(
                sa.select(db.chat_sessions.c.project_id)
                .where(db.chat_sessions.c.id == session_id)
            )).scalar()
            if owner_project_id is None:
                return []
            if owner_project_id != project_id:
                print(f"[execution_store] promote_session_claims: REFUSED -- "
                      f"session_id={session_id!r} belongs to project "
                      f"{owner_project_id!r}, not the supplied "
                      f"{project_id!r}; promoting nothing", flush=True)
                return []
            run_ids = (await conn.execute(
                sa.select(db.runs.c.run_id).where(db.runs.c.session_id == session_id)
            )).scalars().all()
            if not run_ids:
                return []
            claim_rows = (await conn.execute(
                sa.select(db.claims)
                .where(db.claims.c.run_id.in_(run_ids), db.claims.c.status == "supported")
            )).mappings().all()
            evidence_by_claim: dict[str, tuple[str | None, str | None]] = {}
            for claim in claim_rows:
                ev = (await conn.execute(
                    sa.select(db.evidence.c.content, db.evidence.c.content_hash)
                    .select_from(db.claim_evidence.join(
                        db.evidence,
                        db.claim_evidence.c.evidence_id == db.evidence.c.evidence_id))
                    .where(db.claim_evidence.c.claim_id == claim["claim_id"])
                    .limit(1)
                )).first()
                evidence_by_claim[claim["claim_id"]] = (ev[0], ev[1]) if ev else (None, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] promote_session_claims lookup failed "
              f"(session_id={session_id!r}): {type(exc).__name__}: {exc}", flush=True)
        return []

    promoted: list[str] = []
    for claim in claim_rows:
        snapshot, content_hash = evidence_by_claim[claim["claim_id"]]
        promotion_id = await promote_claim(
            project_id, claim, snapshot, content_hash,
            validated_by=VALIDATED_BY_DETERMINISTIC_PLUS_FEEDBACK,
            feedback_notes=feedback_notes)
        if promotion_id is not None:
            promoted.append(promotion_id)
    return promoted


# ============================================================================
# Phase H -- checkpoint + rehydration.
# ============================================================================

# Bumped only if this row's SHAPE changes incompatibly (a column added/
# removed/repurposed) -- never for ordinary new data. rehydrate_run refuses
# to interpret a checkpoint whose schema_version it does not recognize,
# rather than guessing at a shape it was not written to understand.
CHECKPOINT_SCHEMA_VERSION = 1

def _checkpoint_state_hash(run_id: str, sequence: int,
                            last_execution_id: str | None, run_status: str) -> str:
    """A tamper/corruption checksum over THIS ROW's own other columns --
    deliberately NOT a fingerprint of the run's live execution/tool_call
    state (which changes constantly and legitimately as a run progresses;
    hashing it here would make every checkpoint "stale" the instant
    anything else happens). rehydrate_run recomputes this from the
    checkpoint row it just read and compares -- a mismatch means the ROW
    ITSELF is internally inconsistent (hand-edited, corrupted on disk),
    not that the world has moved on since it was written.
    """
    canonical = (f"{run_id}:{sequence}:{last_execution_id or ''}:"
                 f"{run_status}:{CHECKPOINT_SCHEMA_VERSION}")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def persist_checkpoint(run_context: RunContext, run_status: str) -> str | None:
    """INSERT one `checkpoints` row for run_context.run_id at the NEXT
    sequence number for that run. Safe to call repeatedly for the same run
    -- each call computes and inserts the next sequence, an append-only
    ledger like claims/claim_evidence, never an update to a prior row.

    Called from exactly 4 places (swarm/team.py's run_task_async and
    run_task_stream, at the SAME points those functions already call
    persist_run_started/persist_run_completed): once right after a run
    starts (run_status="running", establishing the baseline -- nothing has
    completed yet) and once in the outermost finally after the run reaches
    a terminal status ("ok"/"failed"). Both are boundaries already reliably
    observable IN-PROCESS by ordinary control flow -- unlike a SIGKILL
    (api/server.py's `_run_worker_subprocess` kills the whole worker
    process outright on disconnect/liveness-timeout; see that function's
    own docstring), which no in-process code can ever observe or react to.
    This is exactly why Phase H does not attempt "checkpoint on interrupt"
    as its own event: the only run states this function can ever durably
    record are "just started" and "reached a terminal outcome cleanly" --
    a run that was SIGKILLed leaves no checkpoint newer than its last
    "running" one, which is precisely the signal rehydrate_run's own
    resumability judgment relies on (a run stuck at runs.status=="running"
    forever, because nothing ever got the chance to persist_run_completed).

    last_execution_id is always run_context.root_execution_id -- the one
    execution guaranteed to exist and be identifiable at BOTH call sites
    (at start, it is the only execution; at the end, delegation/retry
    executions may have come and gone, but the root is the stable anchor
    for "which run/attempt-tree this checkpoint belongs to"). Rehydration
    does not rely on this pointer alone for resumability -- it always
    re-reads the full, current executions/tool_calls state for the run
    directly (see rehydrate_run).

    A no-op (returns None) when run_context.session_id is None, the same
    convention persist_run_started uses -- a run with no session has no
    durable Run row for this checkpoint to reference either.
    """
    if run_context.session_id is None:
        return None
    checkpoint_id = str(_uuid.uuid4())
    last_execution_id = run_context.root_execution_id
    try:
        async with get_engine().begin() as conn:
            current_max = (await conn.execute(
                sa.select(sa.func.max(db.checkpoints.c.sequence))
                .where(db.checkpoints.c.run_id == run_context.run_id)
            )).scalar()
            sequence = (current_max or 0) + 1
            state_hash = _checkpoint_state_hash(
                run_context.run_id, sequence, last_execution_id, run_status)
            await conn.execute(db.checkpoints.insert().values(
                id=checkpoint_id,
                run_id=run_context.run_id,
                sequence=sequence,
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                last_execution_id=last_execution_id,
                run_status=run_status,
                state_hash=state_hash,
            ))
        return checkpoint_id
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] persist_checkpoint failed "
              f"(run_id={run_context.run_id!r}): {type(exc).__name__}: {exc}",
              flush=True)
        return None


def _unresumable(run_id: str, reason: str, checkpoint=None, session_id: str | None = None) -> dict:
    return {
        "run_id": run_id,
        "resumable": False,
        "reason": reason,
        "checkpoint_id": checkpoint["id"] if checkpoint is not None else None,
        "checkpoint_sequence": checkpoint["sequence"] if checkpoint is not None else None,
        # checkpoint.last_execution_id IS the root execution id by construction
        # (persist_checkpoint always sets it to run_context.root_execution_id).
        "root_execution_id": checkpoint["last_execution_id"] if checkpoint is not None else None,
        "attempt_number": None,
        "session_id": session_id,
    }


async def rehydrate_run(run_id: str, project_id: str | None = None) -> dict | None:
    """Locate the latest checkpoint for `run_id`, validate its integrity and
    schema_version, and judge whether the run can be safely continued.

    Returns None if the run has never been checkpointed at all (nothing to
    rehydrate -- indistinguishable from "this run_id doesn't exist" from
    this function's point of view; a caller should treat either case as
    "start a fresh run").

    Otherwise returns a dict with these keys, always:
        run_id, resumable (bool), reason (str, always explains the verdict),
        checkpoint_id, checkpoint_sequence, root_execution_id,
        attempt_number, session_id.

    `resumable` is True ONLY when ALL of the following hold:
      1. the latest checkpoint's schema_version matches
         CHECKPOINT_SCHEMA_VERSION exactly.
      2. the checkpoint's own state_hash is internally consistent (not
         corrupted/tampered) -- see _checkpoint_state_hash.
      3. runs.status for this run is still "running" -- a run that already
         reached "ok"/"failed" is DONE; there is nothing to resume, only a
         new run to start.
      4. NOT ONE ToolCall belonging to any Execution under this run is in a
         non-terminal "running" state. This is deliberately absolute, per
         this phase's own explicit instruction: a non-terminal ToolCall's
         real-world effect is unknown (it may have already written a file,
         run a command, applied a diff -- or not), and this function has no
         way to know which tools are safe to re-issue and which are not.
         Rather than guess, ANY non-terminal ToolCall makes the WHOLE run
         non-resumable -- "when uncertain, mark the execution state
         requiring manual/new-run recovery" is implemented literally here,
         not approximated.
      5. NOT ONE Execution belonging to this run is itself still "running"
         (the same ambiguity, one level up -- covers a run killed between a
         tool call finishing and its owning Execution being marked
         complete).

    When resumable, `root_execution_id`/`attempt_number` describe the run's
    root coordinator Execution and how many attempts it has already made
    (Phase A's attempt_number, already tracking exactly this) -- REFERENCES
    for a caller to use when starting a genuinely NEW run that is aware of
    this history, never something this function itself acts on. Phase H
    does not launch, drive, or continue any execution itself: it only ever
    answers "is this safe, and if so, here is the context" -- see this
    module's own docstring for why (no job queue/worker, no distributed
    locking -- both explicitly out of this phase's scope).

    Fail-open: any exception during lookup returns a `resumable: False` dict
    naming the failure as the reason, never raised and never mistaken for
    "yes, safe to resume."

    Phase J: `project_id` is OPTIONAL (default None, preserving Phase H's
    original behavior exactly for existing callers) -- but when a caller
    DOES supply it, it is enforced STRICTLY and fails CLOSED: this run's
    OWN session's chat_sessions.project_id must match, or `resumable` is
    unconditionally False regardless of the run's actual technical
    resumability (see checkpoint_id/checkpoint_sequence in that case are
    still populated for audit purposes, but `resumable` never is). This is
    an authorization decision, not a persistence failure -- it is never
    converted into fail-open ("resumable anyway") behavior. Without a
    project_id argument, any caller holding any run_id gets full
    resumability details for that run regardless of which project it
    belongs to -- acceptable only because, as of this phase, nothing in
    api/server.py exposes rehydrate_run to an HTTP caller at all (it is an
    internal function only); a future endpoint that DOES expose it MUST
    supply project_id.
    """
    try:
        async with get_engine().begin() as conn:
            run_row = (await conn.execute(
                sa.select(db.runs).where(db.runs.c.run_id == run_id)
            )).mappings().first()
            if run_row is None:
                return None
            if project_id is not None:
                owner_project_id = (await conn.execute(
                    sa.select(db.chat_sessions.c.project_id)
                    .where(db.chat_sessions.c.id == run_row["session_id"])
                )).scalar()
                if owner_project_id != project_id:
                    print(f"[execution_store] rehydrate_run: REFUSED -- "
                          f"run_id={run_id!r} belongs to project "
                          f"{owner_project_id!r}, not the supplied "
                          f"{project_id!r}", flush=True)
                    return _unresumable(
                        run_id,
                        "authorization failed: this run does not belong to "
                        "the supplied project_id",
                        session_id=run_row["session_id"])
            checkpoint = (await conn.execute(
                sa.select(db.checkpoints)
                .where(db.checkpoints.c.run_id == run_id)
                .order_by(db.checkpoints.c.sequence.desc())
                .limit(1)
            )).mappings().first()
            if checkpoint is None:
                return None

            if checkpoint["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
                return _unresumable(
                    run_id,
                    f"unrecognized checkpoint schema_version "
                    f"{checkpoint['schema_version']!r} (this code expects "
                    f"{CHECKPOINT_SCHEMA_VERSION!r})",
                    checkpoint, session_id=run_row["session_id"])
            expected_hash = _checkpoint_state_hash(
                checkpoint["run_id"], checkpoint["sequence"],
                checkpoint["last_execution_id"], checkpoint["run_status"])
            if checkpoint["state_hash"] != expected_hash:
                return _unresumable(
                    run_id,
                    "checkpoint integrity hash mismatch -- this row may be "
                    "corrupted or was modified outside persist_checkpoint",
                    checkpoint, session_id=run_row["session_id"])
            if run_row["status"] != "running":
                # Anything other than literally "running" -- "ok", "failed", or
                # any future/unrecognized status value -- is treated as NOT
                # resumable by default, never accidentally as resumable.
                return _unresumable(
                    run_id,
                    f"run already reached status {run_row['status']!r} -- "
                    f"nothing to resume, start a new run instead",
                    checkpoint, session_id=run_row["session_id"])

            root = (await conn.execute(
                sa.select(db.executions)
                .where(db.executions.c.run_id == run_id,
                       db.executions.c.parent_execution_id.is_(None))
            )).mappings().first()
            executions = (await conn.execute(
                sa.select(db.executions).where(db.executions.c.run_id == run_id)
            )).mappings().all()
            execution_ids = [e["execution_id"] for e in executions]
            # A "coordinator"-type execution (root, or a retry sibling) being
            # "running" is EXPECTED and NORMAL for any run this function ever
            # sees resumable=True for -- it IS the in-progress turn being
            # rehydrated, not evidence of anything ambiguous (the root only
            # ever finishes at the run's own terminal checkpoint, which the
            # runs.status!="running" check above already gates on). A
            # "delegation"-type execution left "running", by contrast, means
            # _tool_interception_hook's own finally block (which always
            # closes a delegation exactly once, including on cancellation --
            # see Phase A finding B) never got the chance to run at all: the
            # process died mid-delegation. THAT is the genuinely ambiguous
            # case this check exists to catch.
            running_delegations = [e for e in executions
                                    if e["execution_type"] == "delegation" and e["status"] == "running"]
            if running_delegations:
                return _unresumable(
                    run_id,
                    f"{len(running_delegations)} delegation execution(s) are still "
                    f"in a non-terminal 'running' state -- ambiguous, treated as "
                    f"unresumable",
                    checkpoint, session_id=run_row["session_id"])
            non_terminal_tool_calls = []
            if execution_ids:
                non_terminal_tool_calls = (await conn.execute(
                    sa.select(db.tool_calls.c.tool_call_id)
                    .where(db.tool_calls.c.execution_id.in_(execution_ids),
                           db.tool_calls.c.status == "running")
                )).scalars().all()
            if non_terminal_tool_calls:
                return _unresumable(
                    run_id,
                    f"{len(non_terminal_tool_calls)} tool call(s) are still "
                    f"in a non-terminal 'running' state -- their real-world "
                    f"effect is unknown, and blindly replaying a tool call "
                    f"that may already have taken effect (write_file, "
                    f"apply_diff, run_command, ...) risks a duplicate side "
                    f"effect; this run requires manual inspection or a new "
                    f"run instead",
                    checkpoint, session_id=run_row["session_id"])

            return {
                "run_id": run_id,
                "resumable": True,
                "reason": "run status is 'running' with no non-terminal executions "
                          "or tool calls as of the latest checkpoint -- safe to "
                          "start a NEW run using this context",
                "checkpoint_id": checkpoint["id"],
                "checkpoint_sequence": checkpoint["sequence"],
                "root_execution_id": root["execution_id"] if root is not None else None,
                "attempt_number": root["attempt_number"] if root is not None else None,
                "session_id": run_row["session_id"],
            }
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] rehydrate_run failed "
              f"(run_id={run_id!r}): {type(exc).__name__}: {exc}", flush=True)
        return _unresumable(
            run_id, f"rehydration lookup failed: {type(exc).__name__}: {exc}")


# ============================================================================
# Phase I -- operational integrity diagnostics (read-only, never repairs).
# ============================================================================

async def check_storage_integrity() -> dict:
    """A read-only, deterministic operational snapshot of the durable
    execution/evidence backbone -- COUNTS worth an operator's attention,
    never a repair. Nothing in this function writes to any table; see this
    module's own module docstring and every persist_*/promote_*/
    rehydrate_run function above for where actual writes happen.

    True referential orphans (an Evidence row with no matching ToolCall, a
    ToolCall with no matching Execution, etc.) are already structurally
    impossible when the database enforces its own FKs -- see swarm/db.py's
    `_enable_sqlite_fk` (SQLite does not enforce FOREIGN KEY by default;
    this engine turns it on for every connection) and Postgres's
    unconditional enforcement. So this reports the class of problem FK
    enforcement CANNOT catch: rows LOGICALLY stuck in a non-terminal state
    with no live process left to finish them (the same condition
    rehydrate_run judges per-run, aggregated here across the whole
    database for a human to notice), checkpoint rows whose own
    self-consistency hash no longer matches what they claim (see
    _checkpoint_state_hash), rows whose status/completed_at pair
    contradicts itself (Phase L: e.g. status="ok" with completed_at still
    NULL, or status="running" with completed_at already set -- neither is
    possible through this codebase's own write paths, which always set
    both together, so a row like this means external tampering or a
    genuine code bug, not ordinary operation), promoted memories that
    violate promote_claim's own enforced invariant (Phase L: claim_status
    != "supported" -- the ONLY way such a row can exist, since promote_
    claim raises ValueError rather than ever writing one), and duplicate
    checkpoint sequences (Phase L: defensive -- the UniqueConstraint on
    (run_id, sequence) already makes this structurally impossible when
    enforced, counted here anyway as a second, independent signal rather
    than trusting the constraint alone). None of these are ever repaired
    automatically -- detection only, per this phase's own instruction that
    repair must be demonstrably safe before it is automatic, and nothing
    here has established that.

    Fail-open, matching every other function in this module: a lookup
    failure returns a dict with "error" set and every count as None,
    never raised -- a diagnostic that cannot itself be checked without a
    healthy database is not useful, but it also must never be allowed to
    crash whatever is asking.
    """
    result: dict = {
        "stuck_tool_calls": None, "stuck_executions": None, "stuck_runs": None,
        "checkpoints_checked": None, "checkpoint_hash_mismatches": None,
        "impossible_lifecycle_states": None, "invalid_promotions": None,
        "duplicate_checkpoint_sequences": None,
        "error": None,
    }
    try:
        async with get_engine().begin() as conn:
            result["stuck_tool_calls"] = (await conn.execute(
                sa.select(sa.func.count()).select_from(db.tool_calls)
                .where(db.tool_calls.c.status == "running")
            )).scalar()
            result["stuck_executions"] = (await conn.execute(
                sa.select(sa.func.count()).select_from(db.executions)
                .where(db.executions.c.status == "running")
            )).scalar()
            result["stuck_runs"] = (await conn.execute(
                sa.select(sa.func.count()).select_from(db.runs)
                .where(db.runs.c.status == "running")
            )).scalar()
            checkpoint_rows = (await conn.execute(
                sa.select(db.checkpoints)
            )).mappings().all()

            impossible = 0
            for table, terminal in (
                (db.runs, ("ok", "failed")),
                (db.executions, ("ok", "failed")),
                (db.tool_calls, ("ok", "error")),
            ):
                impossible += (await conn.execute(
                    sa.select(sa.func.count()).select_from(table)
                    .where(sa.or_(
                        sa.and_(table.c.status.in_(terminal), table.c.completed_at.is_(None)),
                        sa.and_(table.c.status == "running", table.c.completed_at.is_not(None)),
                    ))
                )).scalar()
            result["impossible_lifecycle_states"] = impossible

            result["invalid_promotions"] = (await conn.execute(
                sa.select(sa.func.count()).select_from(db.project_memory_promotions)
                .where(db.project_memory_promotions.c.claim_status != "supported")
            )).scalar()
        result["checkpoints_checked"] = len(checkpoint_rows)
        result["checkpoint_hash_mismatches"] = sum(
            1 for c in checkpoint_rows
            if c["state_hash"] != _checkpoint_state_hash(
                c["run_id"], c["sequence"], c["last_execution_id"], c["run_status"])
        )
        seen_sequences: dict[tuple[str, int], int] = {}
        for c in checkpoint_rows:
            key = (c["run_id"], c["sequence"])
            seen_sequences[key] = seen_sequences.get(key, 0) + 1
        result["duplicate_checkpoint_sequences"] = sum(
            1 for count in seen_sequences.values() if count > 1)
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] check_storage_integrity failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


async def list_stale_runs(older_than_seconds: int = 3600) -> list[dict]:
    """Read-only: which specific runs are still status=="running" with no
    durably-observed forward progress (their own most recent checkpoint,
    or their own started_at if none exists yet) in at least
    `older_than_seconds`. The per-run companion to check_storage_
    integrity's aggregate `stuck_runs` COUNT -- this is the listing an
    operator actually needs to decide what to do about each one.

    DETECTION ONLY. Never resumes, drives, cancels, or mutates anything --
    see rehydrate_run for the per-run judgment ("is this specific one
    actually safe to continue from") an operator would consult NEXT, for
    each run_id this function surfaces; this function's only job is
    surfacing candidates, not judging or acting on them. Consistent with
    this phase's own explicit instruction not to introduce automatic
    resume merely because rehydrate_run exists.

    Fail-open: returns [] on any lookup failure (indistinguishable from
    "no stale runs found" -- both are the ordinary, harmless case from a
    caller's point of view; the failure itself is still logged).
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        async with get_engine().begin() as conn:
            running_runs = (await conn.execute(
                sa.select(db.runs).where(db.runs.c.status == "running")
            )).mappings().all()
            stale: list[dict] = []
            for run in running_runs:
                latest_checkpoint_at = (await conn.execute(
                    sa.select(db.checkpoints.c.created_at)
                    .where(db.checkpoints.c.run_id == run["run_id"])
                    .order_by(db.checkpoints.c.sequence.desc())
                    .limit(1)
                )).scalar()
                last_activity = latest_checkpoint_at or run["started_at"]
                # SQLite hands DateTime(timezone=True) columns back NAIVE on
                # read (it has no real timezone-aware storage type; Postgres
                # does not have this problem) -- every value this codebase
                # ever writes to these columns is UTC (func.now() server-
                # side, or datetime.now(timezone.utc) client-side), so a
                # naive value read back is safely assumed UTC rather than
                # left to raise "can't compare offset-naive and
                # offset-aware datetimes" against `cutoff` below.
                if last_activity is not None and last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=timezone.utc)
                if last_activity is not None and last_activity < cutoff:
                    stale.append({
                        "run_id": run["run_id"],
                        "session_id": run["session_id"],
                        "started_at": run["started_at"],
                        "last_checkpoint_at": latest_checkpoint_at,
                        "owner_worker_id": run["owner_worker_id"],
                    })
        return stale
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] list_stale_runs failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return []


# ============================================================================
# Phase J -- project/tenant ownership registry (read/write on `projects`,
# the normalized parent Phase J's authorization checks above reference).
# ============================================================================

async def ensure_project(project_id: str, tenant_id: str | None = None) -> None:
    """Idempotent upsert: registers `project_id` in the `projects` table if
    it has no row yet, so every project this codebase has ever touched
    (via the pre-existing bare project_id STRING convention -- chat_sessions/
    failure_log/task_outcome_queue/project_memory_promotions all predate
    this table) eventually gets a normalized anchor row without requiring a
    backfill migration.

    Never overwrites an existing row's tenant_id with None -- a project
    already registered with a real tenant_id must not be silently
    reset to "no tenant" by a later caller that simply didn't know it.
    Passing a tenant_id for an already-registered project with a
    DIFFERENT existing tenant_id is a caller/config error, not something
    this function resolves; it leaves the existing row untouched either
    way (last-write-wins tenant reassignment is not implemented -- not
    justified by anything found in this phase's own forensics, which
    found no code path that ever needs to REASSIGN a project's tenant).

    Fail-open, matching every other function in this module: a lookup or
    insert failure here is logged and swallowed, never raised -- this is
    ordinary persistence, not an authorization decision (compare
    promote_session_claims/rehydrate_run above, which fail CLOSED because
    THEY gate access to data; this function only ever adds a row that
    makes future ownership lookups possible, never removes or narrows
    access to anything).
    """
    try:
        async with get_engine().begin() as conn:
            existing = (await conn.execute(
                sa.select(db.projects.c.id).where(db.projects.c.id == project_id)
            )).scalar()
            if existing is not None:
                return
            await conn.execute(db.projects.insert().values(
                id=project_id, tenant_id=tenant_id))
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] ensure_project failed "
              f"(project_id={project_id!r}): {type(exc).__name__}: {exc}",
              flush=True)


async def resolve_tenant_for_project(project_id: str) -> str | None:
    """The registered tenant_id for `project_id`, or None if the project has
    no row (never registered via ensure_project) or is registered with no
    tenant. Read-only; fail-open (returns None on any lookup failure,
    exactly as if the project were simply unregistered -- indistinguishable
    on purpose, since neither case licenses any different behavior from a
    caller of this function)."""
    try:
        async with get_engine().begin() as conn:
            return (await conn.execute(
                sa.select(db.projects.c.tenant_id).where(db.projects.c.id == project_id)
            )).scalar()
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] resolve_tenant_for_project failed "
              f"(project_id={project_id!r}): {type(exc).__name__}: {exc}",
              flush=True)
        return None


# ============================================================================
# Phase K -- run ownership/exclusivity (database-backed, no queue/broker).
# ============================================================================

# Forensic finding this whole section exists to defend against, not to
# retrofit into a currently-broken path: as of Phase K, run_id is ALWAYS
# freshly minted (execution_context.new_id(), never supplied by a caller)
# by exactly ONE process -- run_task_async/run_task_stream mint it, and
# _run_worker_subprocess (api/server.py) spawns exactly one dedicated
# ephemeral worker process per /run, /run_chunked chunk, or /stream call,
# each with its own fresh run_id. No code path today lets a caller hand an
# EXISTING run_id back in to be "resumed" by a second worker --
# rehydrate_run (Phase H) is deliberately read-only and never launches,
# drives, or continues execution itself (verified again this phase: still
# true, still tested). So the specific race this section is required to
# prevent -- "two workers advance the same run_id concurrently" -- is not
# reachable through any existing code path, and run_task_async/
# run_task_stream do NOT call acquire_run_ownership below at all; wiring
# it into them would add locking overhead and complexity to a flow that
# structurally cannot race today, contradicting this phase's own "do not
# solve hypothetical problems" instruction.
#
# What IS built here is the PRIMITIVE the invariant asks for, available
# now and tested now, for the day a caller DOES take a rehydrate_run
# verdict and launch a continuation (a future phase's concern -- Phase K
# explicitly forbids redesigning rehydration/the agent runtime to wire
# this in prematurely). A single atomic UPDATE...WHERE against two new
# nullable columns on `runs` (owner_worker_id, owner_lease_expires_at) --
# not a new table, not an in-process lock, not a queue/broker/scheduler:
# mutual exclusion over ONE row's advancement needs none of that
# infrastructure. The UPDATE's own WHERE clause is atomic at the database
# engine level identically on SQLite and PostgreSQL (no dialect-specific
# locking clause like SELECT...FOR UPDATE SKIP LOCKED), which is also
# exactly why SQLite's test behavior stays fully deterministic.

# Ownership acquisition/release are AUTHORIZATION-shaped decisions (do I,
# specifically, currently hold exclusive advancement rights over this
# run?), not ordinary persistence -- so unlike almost everything else in
# this module, a DB failure here FAILS SAFE (returns False -- "you do NOT
# have confirmed ownership, do not proceed as though you do"), never
# fail-open ("assume you own it"). This mirrors Phase J's own established
# distinction: authorization fails closed, persistence fails open;
# ownership acquisition is the former, not the latter.

DEFAULT_RUN_OWNERSHIP_LEASE_SECONDS = 300


async def acquire_run_ownership(
    run_id: str, worker_id: str, lease_seconds: int = DEFAULT_RUN_OWNERSHIP_LEASE_SECONDS,
) -> bool:
    """Atomically claim (or renew) exclusive advancement rights over
    `run_id` for `worker_id`. Returns True iff THIS call's UPDATE matched
    and changed the row -- i.e. this worker now holds the lease -- False
    otherwise (another worker holds a live lease, or run_id does not
    exist).

    Succeeds when the row is currently UNOWNED (owner_worker_id IS NULL),
    already owned by THIS SAME worker_id (re-entrant -- a worker renewing
    its own lease before it expires is not a race with itself), or the
    existing lease has EXPIRED (owner_lease_expires_at is in the past --
    stale-owner recovery: a worker that died without releasing does not
    block the run forever, only until its lease runs out). One statement,
    one round trip: the WHERE clause's own conditions ARE the atomicity --
    no separate SELECT-then-UPDATE, so there is no window between
    "checked" and "claimed" for a second caller to land in.

    `lease_seconds` bounds how long a claim survives without being
    renewed -- a crashed worker's ownership expires on its own; nothing
    else needs to detect or clean up after it. Logged with run_id/
    worker_id/outcome for observability (see this module's own
    docstring's "minimal structured information" list) -- the log is
    diagnostic only, never authoritative (the row itself is).
    """
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    try:
        async with get_engine().begin() as conn:
            result = await conn.execute(
                db.runs.update()
                .where(db.runs.c.run_id == run_id)
                .where(sa.or_(
                    db.runs.c.owner_worker_id.is_(None),
                    db.runs.c.owner_worker_id == worker_id,
                    db.runs.c.owner_lease_expires_at < func.now(),
                ))
                .values(owner_worker_id=worker_id, owner_lease_expires_at=new_expiry)
            )
        acquired = result.rowcount == 1
        print(f"[execution_store] acquire_run_ownership: run_id={run_id!r} "
              f"worker_id={worker_id!r} -> {'ACQUIRED' if acquired else 'DENIED'}",
              flush=True)
        return acquired
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] acquire_run_ownership failed -- FAILING SAFE "
              f"(treated as NOT acquired) (run_id={run_id!r}, "
              f"worker_id={worker_id!r}): {type(exc).__name__}: {exc}", flush=True)
        return False


async def release_run_ownership(run_id: str, worker_id: str) -> bool:
    """Clear ownership of `run_id`, ONLY if `worker_id` is still the
    current owner -- a stale/expired former owner releasing late can never
    clear a DIFFERENT, newer worker's live claim (the WHERE clause makes
    that structurally impossible, not just unlikely). Returns True iff
    this call's UPDATE matched a row (this worker really did hold and just
    released it); False if it did not own the run (already released,
    lease already expired and reclaimed by someone else, or run_id does
    not exist) -- a safe, informational no-op, never an error.

    A failure to release is not dangerous: the lease still has its own
    `lease_seconds` bound and expires on its own (see
    acquire_run_ownership) -- so this fails safe by simply returning False
    and logging, rather than raising.
    """
    try:
        async with get_engine().begin() as conn:
            result = await conn.execute(
                db.runs.update()
                .where(db.runs.c.run_id == run_id, db.runs.c.owner_worker_id == worker_id)
                .values(owner_worker_id=None, owner_lease_expires_at=None)
            )
        released = result.rowcount == 1
        print(f"[execution_store] release_run_ownership: run_id={run_id!r} "
              f"worker_id={worker_id!r} -> {'RELEASED' if released else 'NOT_OWNER'}",
              flush=True)
        return released
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] release_run_ownership failed "
              f"(run_id={run_id!r}, worker_id={worker_id!r}): "
              f"{type(exc).__name__}: {exc}", flush=True)
        return False


async def run_ownership_state(run_id: str) -> dict | None:
    """Read-only observability snapshot of run_id's current ownership --
    NEVER used by acquire_run_ownership/release_run_ownership themselves
    to make a decision (their own atomic UPDATE...WHERE is the sole
    authority; this is a diagnostic view for a human/log, not a second
    source of truth to race against the first). Returns None if run_id
    does not exist; otherwise a dict with owner_worker_id (None means
    unowned), owner_lease_expires_at, and `owned` (True only while a
    lease is present -- this function does NOT evaluate expiry itself,
    since "expired" is a judgment acquire_run_ownership's own WHERE
    clause makes atomically against the database's current time, not
    something worth recomputing approximately here with this process's
    own clock).
    """
    try:
        async with get_engine().begin() as conn:
            row = (await conn.execute(
                sa.select(db.runs.c.owner_worker_id, db.runs.c.owner_lease_expires_at)
                .where(db.runs.c.run_id == run_id)
            )).mappings().first()
        if row is None:
            return None
        return {
            "run_id": run_id,
            "owner_worker_id": row["owner_worker_id"],
            "owner_lease_expires_at": row["owner_lease_expires_at"],
            "owned": row["owner_worker_id"] is not None,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[execution_store] run_ownership_state failed "
              f"(run_id={run_id!r}): {type(exc).__name__}: {exc}", flush=True)
        return None
