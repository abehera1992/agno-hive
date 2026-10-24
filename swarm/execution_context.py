"""Phase A -- runtime identity for the durable execution/evidence backbone.

Session -> Run -> Execution -> ToolCall -> Evidence. This module ONLY
establishes those identities and a structured (non-lossy) in-memory record of
what happened during one run. It does no I/O, touches no database, and
persists nothing -- that is Phase B/D's job. Phase A exists so a later phase
can persist these identities and this exact evidence without having to
reconstruct them from prose after the fact.

Scoping, deliberately: one RunContext instance per run_task_async/
run_task_stream invocation, attached to the `team` object the same way
team._phase0/team._tool_evidence/team._context_pack already are (see
swarm/team.py's _build_team callers) -- never a global or thread-local. The
object is garbage-collected with the team/run that created it.

Canonical run_id: Phase0Run.run_id when Phase0 telemetry happens to be
enabled for this run (PHASE0_TELEMETRY=1), so the two identities are the
SAME string whenever both exist. Phase0 is off by default, though, and a Run
identity must exist unconditionally -- so when there is no Phase0Run, a
fresh id is minted here using the identical scheme (uuid4, hex, 12 chars).
This is not a second, competing identifier: it is the same scheme, used only
as the fallback source for the one canonical value.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_id() -> str:
    """Same shape as Phase0Run.run_id (swarm/phase0.py) -- uuid4, hex, 12
    chars -- so execution/tool-call ids look and behave like the run id they
    nest under, deliberately not a different scheme."""
    return uuid.uuid4().hex[:12]


@dataclass
class ExecutionRecord:
    execution_id: str
    run_id: str
    parent_execution_id: str | None
    agent_name: str
    execution_type: str            # "coordinator" | "delegation"
    attempt_number: int
    started_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    status: str = "running"        # "running" | "ok" | "failed"
    error: str | None = None


@dataclass
class ToolCallRecord:
    tool_call_id: str
    execution_id: str | None
    run_id: str
    tool_name: str
    arguments: Any
    started_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    status: str = "running"        # "running" | "ok" | "error"
    error: str | None = None


@dataclass
class EvidenceRecord:
    """Immutable observation captured at the tool-call boundary. `content` is
    the EXACT object the tool call returned -- never truncated, previewed, or
    otherwise transformed. This is a separate, additional structure from
    team._tool_evidence (the existing capped/preview-truncated LLM-context
    cache, which this module does not touch, replace, or read).

    `agent_name` (T24): the RAW agent identity captured directly at the
    tool-call boundary by whoever calls finish_tool_call -- e.g. the real
    agno `agent` object's own `.name`, the same source team.py's
    phase0.note_tool_call already trusts for per-member read-budget
    tracking. Optional and defaulted to None so every pre-existing caller
    (none of which passes it) is unaffected. Exists because
    execution_id-based attribution (ExecutionRecord.agent_name, looked up
    via _fidelity_agent_name in team.py) silently collapses to the root
    Coordinator execution whenever no child Execution was ever pushed for
    the call that produced this evidence -- true for every
    delegate_structured_task-based delegation, since only the agno-native
    delegate_task_to_member name creates one (see _tool_interception_hook's
    `_is_delegation` check). This field is a second, independent identity
    channel for the SAME evidence item, populated from data already
    available at capture time rather than reconstructed afterward from
    execution-stack state that may never have been pushed."""
    tool_call_id: str
    execution_id: str | None
    run_id: str
    tool_name: str
    arguments: Any
    content: Any
    success: bool
    error: str | None
    started_at: float
    completed_at: float
    agent_name: str | None = None


class RunContext:
    """One instance per run_task_async/run_task_stream invocation.

    Not a global: constructed fresh per call, attached to that call's own
    `team` object, and reachable only through it -- exactly the propagation
    pattern team._phase0/team._session_summary/team._context_pack already use
    (see swarm/team.py's _build_team callers). Every hook that needs identity
    already receives `team` as a parameter, so nothing here requires a new
    thread-local, contextvar, or module-level mutable state.
    """

    def __init__(self, session_id: str | None, run_id: str):
        self.session_id = session_id
        self.run_id = run_id
        self.root_execution_id: str | None = None
        self.executions: dict[str, ExecutionRecord] = {}
        self.tool_calls: list[ToolCallRecord] = []
        self.evidence: list[EvidenceRecord] = []
        self._stack: list[str] = []
        # (parent_execution_id, agent_name) -> attempts made so far. Scoped to
        # this one run, so attempt numbering never leaks across runs.
        self._attempt_counts: dict[tuple[str | None, str], int] = {}

    @property
    def current_execution_id(self) -> str | None:
        return self._stack[-1] if self._stack else None

    def start_execution(self, agent_name: str, execution_type: str,
                         parent_execution_id: str | None) -> str:
        """Creates and pushes a new execution, returning its id.

        attempt_number distinguishes retries of the SAME logical slot. For a
        delegation, that slot is (this specific parent, this specific member)
        -- a different parent or a different member starts its own counter at
        1. For the coordinator itself, every retry in a run is a re-attempt of
        the ONE top-level answer regardless of whether it is keyed by its own
        parent=None (the root) or parent=root_execution_id (every retry after
        it) -- both must share one counter so the root is attempt 1 and each
        retry is 2, 3, ... rather than each independently restarting at 1.
        """
        key = (agent_name, "__coordinator__") if execution_type == "coordinator" \
            else (parent_execution_id, agent_name)
        self._attempt_counts[key] = self._attempt_counts.get(key, 0) + 1
        execution_id = new_id()
        self.executions[execution_id] = ExecutionRecord(
            execution_id=execution_id,
            run_id=self.run_id,
            parent_execution_id=parent_execution_id,
            agent_name=agent_name,
            execution_type=execution_type,
            attempt_number=self._attempt_counts[key],
        )
        self._stack.append(execution_id)
        if execution_type == "coordinator" and parent_execution_id is None:
            self.root_execution_id = execution_id
        return execution_id

    def finish_execution(self, execution_id: str, status: str,
                          error: str | None = None) -> None:
        """Idempotent: a second call on an already-finished execution is a
        no-op except for the (harmless) status overwrite -- callers only ever
        call this once per execution in practice, but nothing here assumes
        that to stay safe."""
        rec = self.executions.get(execution_id)
        if rec is not None:
            rec.completed_at = time.monotonic()
            rec.status = status
            rec.error = error
        if self._stack and self._stack[-1] == execution_id:
            self._stack.pop()

    def start_tool_call(self, tool_name: str, arguments: Any) -> ToolCallRecord:
        """Uses whatever execution is CURRENT at the moment this is called --
        callers that also start a child execution for this same call (e.g. a
        delegation) must call this FIRST, before pushing the child, so the
        tool call itself is attributed to the calling execution, not the one
        it is about to create."""
        rec = ToolCallRecord(
            tool_call_id=new_id(),
            execution_id=self.current_execution_id,
            run_id=self.run_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        self.tool_calls.append(rec)
        return rec

    def finish_tool_call(self, tool_call: ToolCallRecord, content: Any,
                          success: bool, error: str | None,
                          agent_name: str | None = None) -> EvidenceRecord:
        """Records the EXACT `content` handed in -- no truncation, no
        preview, no summarisation. Returns the Evidence record; also appended
        to self.evidence.

        `agent_name` (T24, optional, defaults to None): see EvidenceRecord's
        own docstring -- a direct identity passed in by the caller, who may
        have a more reliable source (the real agent object) than this
        execution's own ExecutionRecord.agent_name. Never required: every
        pre-existing caller that omits it keeps the exact prior behavior."""
        tool_call.completed_at = time.monotonic()
        tool_call.status = "ok" if success else "error"
        tool_call.error = error
        ev = EvidenceRecord(
            tool_call_id=tool_call.tool_call_id,
            execution_id=tool_call.execution_id,
            run_id=tool_call.run_id,
            tool_name=tool_call.tool_name,
            arguments=tool_call.arguments,
            content=content,
            success=success,
            error=error,
            started_at=tool_call.started_at,
            completed_at=tool_call.completed_at,
            agent_name=agent_name,
        )
        self.evidence.append(ev)
        return ev


@dataclass(frozen=True)
class ExecutionProfile:
    """Phase B (2026-10-24) -- a named, typed bundle of execution budgets for
    one run, resolved once at run start and never re-derived mid-run.

    Centrally defined here, next to RunContext, rather than scattered as
    literals across team.py/api/server.py/EkamApp's caller -- the same
    "one authoritative source" principle RunContext already applies to
    identity, now applied to time/tool budgets. A profile only RESOLVES
    values; it does not enforce anything itself -- see each field's own
    consumer for where it is actually read (team._execution_deadline is
    consumed by _repo_lightrag_query's own deadline parameter and, where
    wired, by tool_call_limit resolution; the client_timeout_hint_s field
    is consumed by the CALLER (api/server.py's request handling and
    EkamApp's agno_run wrapper), not by anything inside this process,
    since this process has no way to enforce how long ITS OWN caller
    waits.

    server_deadline_s=None means "unbounded" -- the standard profile's
    value, deliberately preserving today's existing, unchanged behavior
    for every caller that does not opt into a named profile.
    """
    name: str
    client_timeout_hint_s: float | None
    server_deadline_s: float | None
    tool_call_limit_bonus: int = 0
    lightrag_prefetch_budget_s: float | None = None
    completion_reserve_s: float | None = None


# Three profiles, per the mission's own minimum set. Values are an initial
# operating point to validate, not a guarantee (explicit per the mission's
# own wording for the exhaustive profile) -- each is env-overridable so a
# live measurement can retune without a code change.
import os as _os  # local alias -- this module otherwise has no os dependency

_EXECUTION_PROFILES: dict[str, ExecutionProfile] = {
    "standard": ExecutionProfile(
        name="standard", client_timeout_hint_s=None, server_deadline_s=None,
    ),
    "extended": ExecutionProfile(
        name="extended",
        client_timeout_hint_s=float(_os.getenv("EXEC_PROFILE_EXTENDED_CLIENT_TIMEOUT_S", "2700")),
        server_deadline_s=float(_os.getenv("EXEC_PROFILE_EXTENDED_DEADLINE_S", "2400")),
        tool_call_limit_bonus=int(_os.getenv("EXEC_PROFILE_EXTENDED_TOOL_BONUS", "15")),
        lightrag_prefetch_budget_s=60.0,
        completion_reserve_s=180.0,
    ),
    "exhaustive": ExecutionProfile(
        name="exhaustive",
        # Mission's own explicit initial value -- "3600 seconds... configurable
        # rather than hard-coded... an initial operating value to validate".
        client_timeout_hint_s=float(_os.getenv("EXEC_PROFILE_EXHAUSTIVE_CLIENT_TIMEOUT_S", "3600")),
        server_deadline_s=float(_os.getenv("EXEC_PROFILE_EXHAUSTIVE_DEADLINE_S", "3300")),
        tool_call_limit_bonus=int(_os.getenv("EXEC_PROFILE_EXHAUSTIVE_TOOL_BONUS", "40")),
        lightrag_prefetch_budget_s=90.0,
        # Reserve kept well under (deadline - client_timeout margin): the
        # server must finish synthesizing and return BEFORE the client's own
        # ceiling, not merely before its own deadline.
        completion_reserve_s=300.0,
    ),
}


def resolve_profile(name: str | None) -> ExecutionProfile:
    """Resolve a profile name to its ExecutionProfile, defaulting to
    "standard" for None, an empty string, or any name this process does not
    recognize -- never raises, since an unrecognized profile name from an
    older/mismatched caller must fail safe to today's existing unbounded
    behavior, not crash the run.
    """
    if not name:
        return _EXECUTION_PROFILES["standard"]
    profile = _EXECUTION_PROFILES.get(name.strip().lower())
    if profile is None:
        print(f"[execution_context] unrecognized execution_profile {name!r} -- "
              f"falling back to 'standard'", flush=True)
        return _EXECUTION_PROFILES["standard"]
    return profile
