"""Phase C/D/E -- durable persistence for Run, Execution, ToolCall, Evidence,
Claim and ClaimEvidence.

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
task_outcome_queue call, appears anywhere in this module; see this module's
own test suite for an explicit regression guard on that boundary. A
validated Claim is the END of Phase E's responsibility, not the beginning of
Phase F's.

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
from typing import Any, Awaitable, TypeVar

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
