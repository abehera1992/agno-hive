"""Application-owned acceptance boundary (Phase 1.14c/d).

Four independent layers of truth, never conflated:
  1. Workflow execution state      (recorded, never promotes anything)
  2. Agent attempt state           (terminal gate)
  3. Tool execution evidence       (per-invocation outcome + evidence kind)
  4. AgnoHive acceptance state     (ACCEPTED_COMPLETE / ACCEPTED_PARTIAL / UNRESOLVED / BLOCKED / NOT_ACCEPTED)

The existing claim-level assessor (swarm.team._assess_member_result) is reused unchanged in
spirit and stays visible as a diagnostic verdict; this module can only LOWER its verdict (or
refuse to accept), never raise it or manufacture evidence.

No persistence here: attempts/generations live in memory (Phase 1.15 owns the durable ledger).
This module must stay import-light: swarm.team imports it at module load.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import itertools
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

# ---------------------------------------------------------------------------- identity plumbing
# Set by whoever runs an attempt; read by swarm.team's evidence recorder so evidence recorded under
# one attempt id can never be consumed by another (D4).
ATTEMPT_ID: contextvars.ContextVar = contextvars.ContextVar("hive_attempt_id", default=None)

KEY = "researcher"

SUCCESS, FAILED, REFUSED, CANCELLED, TIMED_OUT, UNKNOWN = (
    "SUCCESS", "FAILED", "REFUSED", "CANCELLED", "TIMED_OUT", "UNKNOWN")
OUTCOMES = (SUCCESS, FAILED, REFUSED, CANCELLED, TIMED_OUT, UNKNOWN)

ACCEPTED_COMPLETE, ACCEPTED_PARTIAL, UNRESOLVED, BLOCKED, NOT_ACCEPTED = (
    "ACCEPTED_COMPLETE", "ACCEPTED_PARTIAL", "UNRESOLVED", "BLOCKED", "NOT_ACCEPTED")

# Precedence (which is more restrictive), NOT a confidence scale. A gate may only move a verdict
# to a more restrictive state.
_RESTRICTION = {"COMPLETE": 0, "PARTIAL": 1, "UNRESOLVED": 2, "BLOCKED": 3}


class ToolRefusal(str):
    """A tool-call response that the APPLICATION produced to deny execution (read ceiling, batch cap,
    repeated-failure collapse, duplicate-read stub...). Behaves exactly like the str it wraps for Agno
    and the model; carries a structured marker so accounting never depends on matching message text (D2)."""
    kind: str = "refusal"

    def __new__(cls, text, kind: str = "refusal"):
        obj = super().__new__(cls, text)
        obj.kind = kind
        return obj


# ---------------------------------------------------------------------------- evidence classification
READ_TOOLS = {"get_file_content", "get_files_batch"}
SEARCH_TOOLS = {"search_files", "search_files_batch", "find_files", "count_matches", "lightrag_query"}
DIR_TOOLS = {"list_directory", "list_directory_tree"}
STATE_TOOLS = {"update_session_state", "load_skill", "forward_member_answer", "request_clarification"}

_NUMBERED = re.compile(r"^\s*\d+\t", re.M)
_BATCH_HEADER = re.compile(r"^=== (.+?) ===$", re.M)
_FAILURE_PREFIXES = ("File not found", "ERROR:", "Error:", "Error ", "error:", "Traceback", "Tool error",
                     "No such file", "Permission denied", "PermissionError")
# Fallback ONLY (structured ToolRefusal is primary). Source lines in swarm/team.py.
_REFUSAL_TEXT_FALLBACK = ("REPORT NOW:", "STOP READING AND ANSWER", "REFUSED:", "REPEATED FAILURE COLLAPSED",
                          "Already returned this exact", "STOP calling ")


def norm_path(p) -> str:
    s = str(p or "").strip().strip("`'\"").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/").lower()


def sha(s) -> str:
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()[:12]


@dataclass
class FileResult:
    path: str
    status: str          # READ | SKELETON | TRUNCATED | EMPTY | ERROR | NO_SOURCE_CONTENT | MISSING
    reason: str = ""


def classify_file_body(body: str) -> tuple:
    """(status, reason) for ONE file's returned text (a get_file_content result or one batch section)."""
    b = body or ""
    if not b.strip():
        return "EMPTY", "no content returned"
    s = b.lstrip()
    if s.startswith(_FAILURE_PREFIXES):
        return "ERROR", s.splitlines()[0][:160]
    if "STRUCTURAL SKELETON below" in b[:600] or "too large to return whole" in b[:300]:
        return "SKELETON", "oversized file reduced to a declaration skeleton (bodies elided)"
    if not _NUMBERED.search(b[:6000]):
        return "NO_SOURCE_CONTENT", "result has no line-numbered source content"
    if "... TRUNCATED at" in b[-400:]:
        return "TRUNCATED", "result cut off by the output cap"
    return "READ", ""


def parse_files_batch(text: str, requested: list) -> list:
    """Parse hive-mcp get_files_batch output: sections '=== <path> ===\\n<get_file_content text | ERROR: ...>'
    joined by a blank line, the whole clamped by one output cap (which can cut the LAST section).
    Anything not parseable yields NO_SOURCE_CONTENT / MISSING — never a fabricated read."""
    text = text or ""
    heads = list(_BATCH_HEADER.finditer(text))
    out, seen = [], set()
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        path, body = h.group(1).strip(), text[h.end():end].strip("\n")
        st, why = classify_file_body(body)
        if st == "READ" and i == len(heads) - 1 and "... TRUNCATED at" in text[-400:]:
            st, why = "TRUNCATED", "last section cut off by the batch output cap"
        out.append(FileResult(path, st, why)); seen.add(norm_path(path))
    for r in requested or []:
        if norm_path(r) not in seen:
            out.append(FileResult(str(r), "MISSING", "no section for this requested path in the batch result"))
    return out


@dataclass
class ToolOutcome:
    seq: int
    tool: str
    args: dict
    outcome: str = UNKNOWN
    reason: str = ""
    reached_tool: Optional[bool] = None   # innermost hook ran => the real tool entrypoint was invoked
    refusal_kind: str = ""
    refusal_detected_by: str = ""         # "structured" | "text-fallback"
    evidence_kind: str = "none"           # source_read | search | directory | comparison | state | none
    paths_read: list = field(default_factory=list)
    files: list = field(default_factory=list)   # per-file results for get_files_batch / single read
    cached: bool = False
    result_len: Optional[int] = None
    result_sha: Optional[str] = None
    result_head: str = ""
    tool_call_id: Optional[str] = None
    attempt_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    origin: str = "live-hook"
    result_text: Optional[str] = field(default=None, repr=False)


def evidence_of(tool: str, args, text: str):
    """(evidence_kind, paths_read, per_file_results) for a call whose tool ACTUALLY RAN and returned."""
    a = args if isinstance(args, dict) else {}
    if tool in STATE_TOOLS:
        return "state", [], []
    if tool in READ_TOOLS:
        if tool == "get_file_content":
            p = a.get("relative_path") or a.get("path") or ""
            st, why = classify_file_body(text)
            fr = [FileResult(str(p), st, why)]
        else:
            req = a.get("paths") or a.get("relative_paths") or []
            req = [req] if isinstance(req, str) else list(req)
            fr = parse_files_batch(text, req)
        paths = [norm_path(f.path) for f in fr if f.status == "READ"]
        return ("source_read" if paths else "none"), paths, fr
    if tool in SEARCH_TOOLS:
        return "search", [], []
    if tool in DIR_TOOLS:
        return "directory", [], []
    if tool == "compare_enumerations":
        return "comparison", [], []
    return "none", [], []


def classify_return(c: ToolOutcome, out, out_text: str, prior_success_shas: set) -> None:
    """Classify a call that RETURNED normally. Structural signals first (ToolRefusal type; whether the
    real tool ran); message text is consulted only as a labelled fallback to NAME a refusal."""
    c.result_len = len(out_text or ""); c.result_sha = sha(out_text)
    c.result_head = (out_text or "")[:160]; c.result_text = out_text
    if isinstance(out, ToolRefusal):
        c.outcome, c.refusal_kind, c.refusal_detected_by = REFUSED, out.kind, "structured"
        c.reason = (out_text or "")[:200]
        return
    if c.reached_tool is False:
        for m in _REFUSAL_TEXT_FALLBACK:
            if (out_text or "").lstrip().startswith(m):
                c.outcome, c.refusal_kind, c.refusal_detected_by = REFUSED, "text-marker:" + m.strip(), "text-fallback"
                c.reason = (out_text or "")[:200]
                return
        if c.result_sha in prior_success_shas:   # hook re-served an earlier genuine result (read cache)
            c.outcome, c.cached = SUCCESS, True
            c.evidence_kind, c.paths_read, c.files = evidence_of(c.tool, c.args, out_text)
            return
        c.outcome, c.reason = UNKNOWN, "hook-generated response; tool did not execute; unrecognised"
        return
    if (out_text or "").lstrip().startswith(_FAILURE_PREFIXES) and c.tool not in READ_TOOLS:
        c.outcome, c.reason = FAILED, (out_text or "")[:200]
        return
    c.evidence_kind, c.paths_read, c.files = evidence_of(c.tool, c.args, out_text)
    if c.tool in READ_TOOLS and c.files and all(f.status in ("ERROR", "MISSING") for f in c.files):
        c.outcome, c.reason = FAILED, "; ".join(f"{f.path}: {f.reason}" for f in c.files)[:200]
        return
    c.outcome = SUCCESS


_CALL: contextvars.ContextVar = contextvars.ContextVar("hive_tool_call", default=None)
# Phase 1.29d Track 1 (second root cause, found live after the first propagation fix):
# the TRUE outermost tool_hooks entry is whichever hook actually receives Agno's own
# self.arguments object as its own `args` parameter (same object, not a copy -- confirmed
# by reading Agno 2.5.17's tools/function.py directly: the hook chain's first call passes
# self.arguments itself, since a non-empty dict is truthy). acceptance_outcome_outer below
# is installed as [o_hook] + <everything else, including swarm/team.py's budget hook> +
# [i_hook] by swarm/eng_workflow.py -- so IT, not the budget hook, is the true outermost.
# A hook anywhere else in the chain receives a freshly repacked kwargs dict at every hop
# (Agno's `next_func(**kwargs)` forwarding always creates a new dict), so mutating that
# dict in place only ever affects what the NEXT hook down sees -- never what
# execute_entrypoint_async actually uses, since it reads self.arguments directly and
# ignores its own `args` parameter entirely. Live evidence: a get_file_content call on
# hive.md was correctly adapted to max_output_chars=10,104 by the budget hook (mid-chain),
# but reached the real producer with no cap at all and returned the full 15,055-char
# render -- the budget hook's own object was never self.arguments.
# This contextvar lets any hook in the chain reach the SAME object the outermost hook
# holds, so a downstream hook can mutate the real self.arguments directly instead of its
# own (possibly different) `args` parameter.
_RAW_ARGS: contextvars.ContextVar = contextvars.ContextVar("hive_raw_args", default=None)


class OutcomeRecorder:
    """Two hooks per attempt. `outer` (first in tool_hooks) records identity at call start and a final outcome
    independent of normal return; `inner` (last in tool_hooks) runs only if the real tool entrypoint is reached,
    which proves the tool executed rather than a hook answering in its place. No monkeypatching of tools."""

    def __init__(self, attempt_id: str, tool_deadline: float = None, on_call=None):
        self.attempt_id = attempt_id
        self.on_call = on_call          # Phase 1.17: async persistence callback, awaited once per finished call
        self.persist_error = None       # set if the callback raised: the attempt's evidence is then NOT durable
        self.calls: list = []
        self._seq = itertools.count(1)
        self.deadline = tool_deadline
        self._succ_shas: set = set()

    async def _emit(self, c) -> None:
        if self.on_call is None:
            return
        try:
            await asyncio.shield(self.on_call(c))
        except BaseException as e:  # noqa: BLE001 -- never alters the tool result; flags the attempt instead
            self.persist_error = repr(e)[:200]

    def hooks(self):
        me = self

        async def acceptance_outcome_outer(function_name, function, args, agent=None, run_context=None):
            c = ToolOutcome(seq=next(me._seq), tool=function_name, args=dict(args or {}), reached_tool=False,
                            attempt_id=me.attempt_id, agent_run_id=getattr(run_context, "run_id", None))
            me.calls.append(c)
            tok = _CALL.set(c)
            tok_a = ATTEMPT_ID.set(me.attempt_id)
            # Phase 1.29d: this `args` IS Agno's own self.arguments object (same reference,
            # not a copy) because this hook is always installed outermost. Publish it so a
            # hook further down the chain -- which only ever sees a freshly repacked kwargs
            # dict at its own position, never this object -- can mutate THIS one directly
            # when it needs the real producer call to see an adaptation.
            tok_r = _RAW_ARGS.set(args)
            try:
                coro = function(**args)
                out = await (asyncio.wait_for(coro, me.deadline) if me.deadline else coro)
            except asyncio.CancelledError:
                c.outcome, c.reason = CANCELLED, "CancelledError"
                raise
            except (asyncio.TimeoutError, TimeoutError) as e:
                c.outcome, c.reason = TIMED_OUT, f"{type(e).__name__} (deadline={me.deadline})"
                raise
            except BaseException as e:  # noqa: BLE001 -- recorded, then re-raised unchanged
                c.outcome, c.reason = FAILED, repr(e)[:300]
                raise
            finally:
                _CALL.reset(tok)
                ATTEMPT_ID.reset(tok_a)
                _RAW_ARGS.reset(tok_r)
                if c.outcome != UNKNOWN:          # exception paths: outcome already final
                    await me._emit(c)
            text = out if isinstance(out, str) else _text_of(out)
            classify_return(c, out, text, me._succ_shas)
            if c.outcome == SUCCESS and c.result_sha:
                me._succ_shas.add(c.result_sha)
            await me._emit(c)
            return out

        async def acceptance_outcome_inner(function_name, function, args):
            c = _CALL.get()
            if c is not None:
                c.reached_tool = True
            return await function(**args)

        return acceptance_outcome_outer, acceptance_outcome_inner


def _text_of(result) -> str:
    if isinstance(result, str):
        return result
    for attr in ("content", "text", "data", "output", "result"):
        v = getattr(result, attr, None)
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            parts = [b if isinstance(b, str) else getattr(b, "text", None) for b in v]
            parts = [p for p in parts if isinstance(p, str)]
            if parts:
                return "\n".join(parts)
    return str(result)


def count_outcomes(outcomes) -> dict:
    allc = {k: 0 for k in OUTCOMES}
    inv = {k: 0 for k in OUTCOMES}
    for o in outcomes:
        allc[o.outcome] += 1
        if o.tool not in STATE_TOOLS:
            inv[o.outcome] += 1
    reads = sorted({p for o in outcomes if o.outcome == SUCCESS and o.evidence_kind == "source_read" for p in o.paths_read})
    return {"all": allc, "investigation": inv, "source_files_read": reads}


# ---------------------------------------------------------------------------- required-evidence policy
@dataclass
class Requirement:
    id: str
    description: str
    any_of_paths: list


@dataclass
class Link:
    """R5: a relationship between two required files, evidenced in the SOURCE TEXT this attempt read
    (not in the model's prose), and named in the report."""
    id: str
    from_req: str
    to_req: str
    from_regex: str
    to_regex: str
    report_tokens: list
    description: str = ""


@dataclass
class EvidencePolicy:
    name: str
    requirements: list
    links: list = field(default_factory=list)
    # Phase 1.23: content-level coverage obligations, evaluated by enum_requirement_result() and
    # folded into gate_evaluate()'s satisfied/missing alongside `requirements` -- see EnumRequirement.
    enum_requirements: list = field(default_factory=list)
    # Phase 1.34/1.35: two-sided comparison obligations, evaluated by cross_reference_result() --
    # see CrossReferenceRequirement. Deliberately a separate list, not folded into
    # enum_requirements: an EnumRequirement's evidence check proves an item EXISTS (pattern found
    # in both the answer and the source read); a cross-reference item proves a LEFT item has NO
    # matching RIGHT item, which needs the inverted evidence check that function performs --
    # reusing enum_requirement_result() unchanged for this would always fail, since "no matching
    # counterpart" language never appears in source code for its in-evidence check to find.
    # Carries NO project-specific content itself -- see CrossReferenceRequirement's own docstring.
    cross_reference_requirements: list = field(default_factory=list)


_FORM = "auth/register/business/businessregistrationform.tsx"
T11_DOC_UPLOAD_POLICY = EvidencePolicy(
    name="t11-doc-upload-chain",
    requirements=[
        Requirement("R1_frontend_business_registration_form", "frontend business-registration form (upload initiation)", [_FORM]),
        Requirement("R2_storage_upload_handler", "storage-service upload endpoint and handler", ["api/storage-service/router/storage_api.py"]),
        Requirement("R3_business_document_registration", "business-service document registration", ["api/business-service/router/business_api.py"]),
        Requirement("R4_business_admin_seller_verification", "business-admin seller verification", ["api/business-service/router/business_admin_api.py"]),
    ],
    links=[
        # Phase 1.26 (Track B): report_tokens retains only the handler-function name,
        # not the literal route fragment. Verified first, not assumed: AST inspection of
        # the real EkamApp source (not textual proximity) confirmed each to_regex match
        # is the SOLE decorator on the SOLE function matching it in its named file --
        # router.post("/upload", ...) at storage_api.py:32 is the only decorator on
        # upload_file (storage_api.py:33); router.post("/{business_id}/documents", ...)
        # at business_api.py:483 is the only decorator on register_documents
        # (business_api.py:484); router.post("/verify/{business_id}") at
        # business_admin_api.py:84 is the only decorator on verify_seller
        # (business_admin_api.py:85). Each raw-text to_regex pattern also matches
        # exactly once in its file, so there is no second candidate to confuse it with.
        # from_regex/to_regex (the actual source-backed relationship check) and
        # any_of_paths (the fixed, policy-named files) are UNCHANGED below -- this only
        # retires a report-text requirement that was redundant with to_regex given that
        # verified uniqueness, after live battery evidence (Phase 1.25 T11 recovery)
        # showed the model consistently names the real handler function it read but
        # never transcribes the raw internal URL fragment verbatim in its own prose.
        Link("L1_frontend_to_storage", "R1_frontend_business_registration_form", "R2_storage_upload_handler",
             r"/api/storageservice/upload", r'@router\.post\(\s*"/upload"', ["upload_file"],
             "form posts to the upload URL; storage router exposes POST /upload"),
        Link("L2_frontend_to_registration", "R1_frontend_business_registration_form", "R3_business_document_registration",
             r"/api/businessservice/business/[^`'\"]*/documents", r'@router\.post\(\s*"/\{business_id\}/documents"',
             ["register_documents"], "form posts file refs to /documents; business router registers them"),
        Link("L3_registration_to_verification", "R3_business_document_registration", "R4_business_admin_seller_verification",
             r"BusinessDocument\(", r'@router\.post\(\s*"/verify/\{business_id\}"', ["verify_seller"],
             "registration creates BusinessDocument rows; admin router verifies the seller by business_id"),
    ])


def _read_sources(outcomes) -> dict:
    """normalized path -> concatenated source text, from this attempt's own SUCCESS source_read calls only."""
    src: dict = {}
    for o in outcomes:
        if o.outcome == SUCCESS and o.evidence_kind == "source_read":
            for p in o.paths_read:
                src[p] = src.get(p, "") + "\n" + _section_for(o, p)
    return src


def _section_for(o, p) -> str:
    if o.tool == "get_file_content":
        return o.result_text or ""
    t = o.result_text or ""
    heads = list(_BATCH_HEADER.finditer(t))
    for i, h in enumerate(heads):
        if norm_path(h.group(1)) == p:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(t)
            return t[h.end():end]
    return ""


# ---------------------------------------------------------------------------- Phase 1.23: deterministic coverage
@dataclass
class EnumRequirement:
    """A coverage obligation satisfied only when a specific named item appears in the attempt's OWN
    final answer text AND is traceable to source text this attempt actually read (Track B property 3:
    a claim alone never satisfies an obligation). `items` is a list of (item_id, pattern) pairs; pattern
    is matched case-insensitively as a regex against both the answer and the scoped source text.
    `source_paths` mirrors Requirement.any_of_paths (ANY_SOURCE for "any file this attempt read").
    An item whose membership cannot be reliably decided by substring/regex matching (e.g. a word that is
    also the task's own generic topic noun) should use pattern=None and is reported NOT_EVALUATED rather
    than guessed -- this stays an accuracy/framing question for the existing assessor, never silently
    scored as satisfied."""
    id: str
    description: str
    items: list
    source_paths: list
    mandatory: bool = True


def enum_requirement_result(req: "EnumRequirement", srcs: dict, text: str) -> dict:
    report = text or ""
    if list(req.source_paths) == [ANY_SOURCE]:
        scoped_src, scope_read = "\n".join(srcs.values()), bool(srcs)
    else:
        parts = [t for p, t in srcs.items() if any(p.endswith(norm_path(c)) for c in req.source_paths)]
        scoped_src, scope_read = "\n".join(parts), bool(parts)
    items_out = []
    for item_id, pattern in req.items:
        if pattern is None:
            items_out.append({"item_id": item_id, "status": "NOT_EVALUATED",
                              "why": "no reliable deterministic pattern for this item (see EnumRequirement docstring)"})
            continue
        in_answer = bool(re.search(pattern, report, re.I))
        if not in_answer:
            items_out.append({"item_id": item_id, "status": "MISSING", "in_answer": False, "in_evidence": None})
            continue
        in_evidence = scope_read and bool(re.search(pattern, scoped_src, re.I))
        items_out.append({"item_id": item_id, "status": "SATISFIED" if in_evidence else "INSUFFICIENT_EVIDENCE",
                          "in_answer": True, "in_evidence": in_evidence})
    evaluated = [i for i in items_out if i["status"] != "NOT_EVALUATED"]
    if not evaluated:
        overall = "NOT_EVALUATED"
    elif any(i["status"] == "MISSING" for i in evaluated):
        overall = "MISSING"
    elif any(i["status"] == "INSUFFICIENT_EVIDENCE" for i in evaluated):
        overall = "INSUFFICIENT_EVIDENCE"
    else:
        overall = "SATISFIED"
    return {"id": req.id, "description": req.description, "mandatory": req.mandatory, "status": overall,
            "items": items_out}


@dataclass
class CrossReferenceRequirement:
    """Phase 1.35: project-agnostic cross-layer comparison primitive. Replaces Phase 1.34's
    GapDisclosureRequirement, which hardcoded one task's own item names, a frontend file path,
    and voucher-specific regex patterns directly in this GENERIC acceptance module -- a real
    EkamApp-specific instance (T13_GAP_DISCLOSURE_REQ) was removed entirely (see
    docs/reports/phase-1.35-report.md); this class itself carries NO project-specific content.
    Every item name, pattern, and file path below is supplied by the CALLER as task-provided
    policy data -- the same separation EnumRequirement/EvidencePolicy already establish for
    ordinary presence checks, now extended to a two-sided comparison.

    Computes, DETERMINISTICALLY from evidence (never from the model's own claims about what
    corresponds to what), which `left_items` are confirmed present (found in the answer AND
    backed by an actual read of `left_source_paths`) but have no `right_items` entry ALSO
    confirmed present that `match()` pairs it with -- the unmatched set -- then checks the
    final answer explicitly discloses each one, grounded in a genuine read of
    `right_source_paths` (never merely inferred from the left item's own presence, and never
    merely true by omission).

    `left_items`/`right_items`: lists of (item_id, presence_pattern) pairs -- the SAME shape
    EnumRequirement.items already uses, by design: an existing EnumRequirement's own `.items`
    and `.source_paths` can be passed here directly rather than duplicated as a second,
    separately-maintained copy of the same list (the duplication Phase 1.34 fell into when its
    gap-disclosure item list repeated the same 4 operation names EP_endpoints already declared).
    A pattern of `None` for an item (EnumRequirement's own "cannot be reliably discriminated by
    substring matching" escape hatch) makes that item NOT_EVALUATED here too -- never guessed.

    `match(left_item_id, right_item_id) -> bool`: task-supplied correspondence rule. Default
    `None` uses exact case-insensitive item_id equality. A task needing semantic correspondence
    (e.g. "create_X" on one side, "useCreateXMutation" on the other) supplies its own function
    -- still deterministic application logic, never a model call -- OR relies on a Researcher-
    supplied semantic mapping at RUNTIME (Phase 1.36, see cross_reference_result()'s own
    docstring): the two are complementary, not alternatives -- `match` is for a correspondence
    the TASK's own policy author already knows in advance; a runtime mapping is for one the
    Researcher discovers while investigating, which this class cannot know ahead of time.

    Per-item status (Phase 1.36, widened from Phase 1.35's SATISFIED/MISSING/UNCERTAIN to the
    five categories task-provided comparisons need to distinguish): MATCHED (a confirmed
    right-side counterpart exists, by `match()` or a grounded runtime mapping), UNMATCHED (right
    side read, no counterpart found -- must be disclosed), AMBIGUOUS (more than one plausible
    right-side counterpart, or a runtime mapping conflicting with the deterministic finding --
    NEVER silently resolved in either direction), UNCERTAIN (the right side's own evidence was
    never read; matching cannot be determined -- Track B: "absent from inspected evidence" is
    preserved distinct from "proven not to exist", never collapsed into a guess), and
    INSUFFICIENT_EVIDENCE (a runtime mapping was supplied but its claimed target was never
    actually confirmed present -- the Researcher's own claim, unverified). The requirement's
    OVERALL status additionally distinguishes UNPERFORMED (the left side's own evidence was
    never read at all -- the comparison never had an input to run against, so it must never be
    allowed to default to a vacuous SATISFIED merely because there was nothing to iterate)."""
    id: str
    description: str
    left_items: list
    left_source_paths: list
    right_items: list
    right_source_paths: list
    match: object = None   # Callable[[str, str], bool] | None
    mandatory: bool = True


def _alt_pattern(*alts) -> str:
    """Generic regex-alternation helper (join with `|`) -- used by any EnumRequirement or
    CrossReferenceRequirement item needing several equivalent phrasings for one item. Carries
    no project-specific content itself; renamed from the pre-Phase-1.35 `_voucher_pat` (same
    body, misleading name -- see docs/reports/phase-1.35-report.md's audit)."""
    return "|".join(alts)


# Phase 1.34/1.35: generic vocabulary for "this item is confirmed absent on the other side" --
# no project-specific terms, and deliberately NOUN-AGNOSTIC (caught live during this phase's own
# scratch validation with synthetic, non-EkamApp data: an earlier draft required a specific noun
# after "no matching" -- frontend/client/counterpart/hook -- so a genuinely generic disclosure
# like "no matching gadget handler" failed to match; a domain-specific noun list is exactly the
# kind of residual non-genericity this phase exists to remove). Matches the STRUCTURE of a
# disclosure ("no matching ___", "X has no ___", "missing a ___") without requiring any
# particular noun to follow. A false negative here (a real disclosure the regex fails to
# recognize) is the costlier failure mode, since it would wrongly fail an answer that DID
# disclose the gap in different words.
_UNMATCHED_DISCLOSURE_TERMS = (
    r"no\s+matching\b|no\s+corresponding\b|no\s+counterpart\b|"
    r"has\s+no\b|does(?:n'?t|\s+not)\s+have\b|"
    r"missing(?:\s+a)?\b|"
    r"not\s+(?:yet\s+)?(?:implemented|covered|built|exposed|present|matched)\b|"
    r"\buncovered\b|\bunmatched\b|\bunexposed\b|"
    r"lacks?\b|\bgap\b|without\s+a\b"
)


def _proximity_disclosure_pat(item_pattern: str) -> str:
    """Bidirectional proximity match, pure regex (never model-based): an item's own presence
    pattern and a disclosure term within 400 chars of each other, in EITHER order -- a model
    might write "X has no matching counterpart" or "the following have no counterpart: ...X...",
    and both are genuine disclosures. Uses [\\s\\S] (any character INCLUDING newlines), never
    [^\\n] -- a real live run (Phase 1.34's own validation, before this generalization) disclosed
    a gap as a markdown header followed by each item on its own bullet line; [^\\n] cannot cross
    even one of the several newlines between the header and a later bullet, so a line-restricted
    version of this pattern wrongly scored that exact, genuine disclosure as MISSING. 400 chars
    (measured against that real disclosure: the header-to-furthest-item span was ~230-250 chars)
    is generous enough to span a short header+list or paragraph without reaching into an
    unrelated section."""
    return rf"(?:{item_pattern})[\s\S]{{0,400}}(?:{_UNMATCHED_DISCLOSURE_TERMS})|(?:{_UNMATCHED_DISCLOSURE_TERMS})[\s\S]{{0,400}}(?:{item_pattern})"


def _presence(pattern, report: str, scoped_src: str, scope_read: bool):
    """Shared by both sides of a CrossReferenceRequirement -- the SAME in_answer/in_evidence
    check enum_requirement_result() already performs per item, extracted here so
    cross_reference_result() does not duplicate that logic. Returns None (NOT_EVALUATED) for a
    pattern of None, exactly as EnumRequirement's own escape hatch already does."""
    if pattern is None:
        return None
    if not re.search(pattern, report, re.I):
        return False
    return scope_read and bool(re.search(pattern, scoped_src, re.I))


def cross_reference_result(req: "CrossReferenceRequirement", srcs: dict, text: str,
                            semantic_mappings: dict | None = None) -> dict:
    """See CrossReferenceRequirement's own docstring. Deterministic stages: (1) presence on each
    side via _presence(), reusing enum_requirement_result()'s own check; (2) matching -- pure
    application logic, req.match() or exact item_id equality, PLUS any runtime semantic mapping
    the Researcher supplied (Phase 1.36); (3) disclosure -- for each UNMATCHED left item, was the
    gap actually stated, reusing the item's OWN presence pattern so synonyms already declared for
    presence-checking are honored for disclosure-checking too.

    `semantic_mappings` (Phase 1.36): {left_item_id: {"maps_to": right_item_id, "evidence": str}},
    sourced from the Researcher's OWN `update_session_state` call during the attempt (see
    docs/reports/phase-1.36-report.md for the exact convention and why this reuses an existing,
    already-granted tool rather than adding a new one) -- never a model call made BY this
    function; by the time this runs, the mapping is already-recorded data, evaluated the same
    deterministic way as everything else here. A left item with a semantic mapping is MATCHED
    only if the claimed right_item_id is itself confirmed present (grounded, not merely claimed);
    otherwise INSUFFICIENT_EVIDENCE -- a researcher's own unverified correspondence claim is
    never trusted at face value. A mapping whose claimed target DIFFERS from what the
    deterministic match() independently found is AMBIGUOUS (two different answers exist; never
    silently pick one)."""
    report = text or ""
    semantic_mappings = semantic_mappings or {}

    def scoped(paths):
        if list(paths) == [ANY_SOURCE]:
            return "\n".join(srcs.values()), bool(srcs)
        parts = [t for p, t in srcs.items() if any(p.endswith(norm_path(c)) for c in paths)]
        return "\n".join(parts), bool(parts)

    l_src, l_read = scoped(req.left_source_paths)
    r_src, r_read = scoped(req.right_source_paths)
    match_fn = req.match or (lambda a, b: a.lower() == b.lower())

    left_present = {iid: _presence(pat, report, l_src, l_read) for iid, pat in req.left_items}
    right_present = {iid: _presence(pat, report, r_src, r_read) for iid, pat in req.right_items}

    if not l_read:
        # the comparison's own LEFT input was never read at all -- there is nothing to iterate,
        # which must never be allowed to default to a vacuous SATISFIED. Phase 1.35's own gap:
        # an empty items_out (because the loop below never even starts) fell through to
        # "else: overall = SATISFIED". UNPERFORMED is neither "SATISFIED" nor "UNCERTAIN", so
        # gate_evaluate()'s existing fold logic already treats it as blocking for a mandatory
        # requirement with no changes to that function needed.
        return {"id": req.id, "description": req.description, "mandatory": req.mandatory,
                "status": "UNPERFORMED", "items": [],
                "reason": "left-side evidence was never read; this comparison never had an input to run"}

    items_out = []
    for l_id, l_pattern in req.left_items:
        if not left_present.get(l_id):
            continue   # not confirmed present on the left side (or NOT_EVALUATED) -- nothing to compare yet
        det_matches = [r_id for r_id, _ in req.right_items
                       if r_read and right_present.get(r_id) and match_fn(l_id, r_id)] if r_read else []
        mapping = semantic_mappings.get(l_id)
        if mapping:
            claimed = mapping.get("maps_to")
            claimed_confirmed = r_read and bool(right_present.get(claimed))
            if det_matches and claimed not in det_matches:
                items_out.append({"item_id": l_id, "status": "AMBIGUOUS",
                                  "deterministic_matches": det_matches, "semantic_mapping": mapping,
                                  "reason": "the Researcher-supplied mapping conflicts with the "
                                            "deterministic match -- never silently resolved"})
                continue
            if not claimed_confirmed:
                items_out.append({"item_id": l_id, "status": "INSUFFICIENT_EVIDENCE",
                                  "semantic_mapping": mapping,
                                  "reason": "the mapping's claimed target was never confirmed "
                                            "present on the right side -- an unverified claim"})
                continue
            items_out.append({"item_id": l_id, "status": "MATCHED", "via": "semantic_mapping",
                              "matched_to": claimed, "evidence": mapping.get("evidence")})
            continue
        if not r_read:
            items_out.append({"item_id": l_id, "status": "UNCERTAIN",
                              "reason": "right-side evidence was never read; matching cannot be determined"})
            continue
        if len(det_matches) > 1:
            items_out.append({"item_id": l_id, "status": "AMBIGUOUS", "deterministic_matches": det_matches,
                              "reason": "more than one right-side item matched deterministically -- "
                                        "never silently picking one"})
            continue
        if det_matches:
            items_out.append({"item_id": l_id, "status": "MATCHED", "via": "deterministic", "matched_to": det_matches[0]})
            continue
        disclosed = bool(re.search(_proximity_disclosure_pat(l_pattern), report, re.I))
        items_out.append({"item_id": l_id, "status": "UNMATCHED", "disclosed": disclosed})

    blocking_statuses = ("UNMATCHED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE")
    blocking = [i for i in items_out if i["status"] in blocking_statuses
                and not (i["status"] == "UNMATCHED" and i.get("disclosed"))]
    uncertain = [i for i in items_out if i["status"] == "UNCERTAIN"]
    if blocking:
        # priority order so the overall status names the most structurally significant problem
        # first when several kinds co-occur -- an unresolved ambiguity is a sharper failure than
        # an undisclosed gap, which is sharper than an unverified mapping claim.
        for pri in ("AMBIGUOUS", "UNMATCHED", "INSUFFICIENT_EVIDENCE"):
            if any(i["status"] == pri for i in blocking):
                overall = pri
                break
    elif uncertain and not any(i["status"] in ("MATCHED",) for i in items_out):
        overall = "UNCERTAIN"
    else:
        overall = "SATISFIED"
    return {"id": req.id, "description": req.description, "mandatory": req.mandatory, "status": overall,
            "items": items_out}


def build_policy_from_spec(spec: dict) -> "EvidencePolicy":
    """Phase 1.36: the project-agnostic deserializer that lets a CALLER supply its own
    EvidencePolicy as plain, JSON-safe data over the wire (RunRequest.evidence_policy, see
    docs/reports/phase-1.36-report.md) -- instead of a new project requiring an AgnoHive source
    commit (a new module-level Python constant in THIS file) to register its own acceptance
    requirements. Contains no project-specific content itself; every id/pattern/path below comes
    from `spec`.

    Schema (all keys optional, default empty):
      {"name": str,
       "requirements": [{"id", "description", "any_of_paths": [str,...], "mandatory": bool}],
       "enum_requirements": [{"id", "description",
                              "items": [[item_id, pattern_or_null], ...], "source_paths": [...],
                              "mandatory": bool}],
       "cross_reference_requirements": [{"id", "description",
                                         "left_items": [[item_id, pattern_or_null], ...],
                                         "left_source_paths": [...],
                                         "right_items": [[item_id, pattern_or_null], ...],
                                         "right_source_paths": [...], "mandatory": bool}]}

    Deliberately does NOT support `links` (T11's own relationship-chain semantics -- a narrower,
    task-specific feature, not part of this generic surface) or a custom `match` callable for
    CrossReferenceRequirement (a Python function cannot cross a JSON wire safely) -- a supplied
    cross-reference requirement always uses the default exact case-insensitive item_id match.
    Semantic correspondence between differently-named items is handled at RUNTIME instead, via
    the Researcher's own `update_session_state` mapping (see cross_reference_result()'s own
    docstring) -- a deliberately different, complementary mechanism from this static spec, not a
    gap in it: this spec is for what a policy author already knows when they write the task;
    the runtime mapping is for what the Researcher discovers while investigating, which the
    policy author cannot know in advance. A malformed entry (missing a required key) raises
    ValueError immediately -- never silently dropped or guessed, since a policy that silently
    checks less than the caller asked for is worse than one that fails loudly at construction."""
    def _req(e):
        return Requirement(e["id"], e.get("description", ""), list(e["any_of_paths"]), bool(e.get("mandatory", True)))

    def _enum(e):
        return EnumRequirement(e["id"], e.get("description", ""),
                               [tuple(i) if i[1] is not None else (i[0], None) for i in e["items"]],
                               list(e["source_paths"]), bool(e.get("mandatory", True)))

    def _xref(e):
        return CrossReferenceRequirement(
            e["id"], e.get("description", ""),
            [tuple(i) for i in e["left_items"]], list(e["left_source_paths"]),
            [tuple(i) for i in e["right_items"]], list(e["right_source_paths"]),
            match=None, mandatory=bool(e.get("mandatory", True)))

    spec = spec or {}
    return EvidencePolicy(
        name=spec.get("name") or "supplied-policy",
        requirements=[_req(e) for e in spec.get("requirements", [])],
        enum_requirements=[_enum(e) for e in spec.get("enum_requirements", [])],
        cross_reference_requirements=[_xref(e) for e in spec.get("cross_reference_requirements", [])])


T13_ENDPOINTS_REQ = EnumRequirement(
    "EP_endpoints", "all 9 voucher endpoints named, backed by vouchers_api.py", [
        ("list_vouchers", _alt_pattern(r"list_vouchers\b", r"\blist\s+vouchers\b", r"get\s*/vouchers\b(?!/)")),
        ("get_voucher", _alt_pattern(r"get_voucher\b", r"\bget\s+voucher\b(?!s)", r"/vouchers/\{?\s*voucher_id")),
        ("create_voucher", _alt_pattern(r"create_voucher\b", r"\bcreate\s+voucher\b(?!s)", r"post\s*/vouchers\b(?!/)")),
        ("post_voucher", _alt_pattern(r"post_voucher\b", r"\bpost\s+(?:a\s+)?voucher\b", r"/vouchers/.{0,24}/post\b")),
        ("cancel_voucher", _alt_pattern(r"cancel_voucher\b", r"\bcancel\s+(?:a\s+)?voucher\b", r"/vouchers/.{0,24}/cancel\b")),
        ("create_grn_from_po", _alt_pattern(r"create_grn_from_po", r"\bgrn\b")),
        ("create_credit_note", _alt_pattern(r"create_credit_note", r"credit[\s_-]?note")),
        ("create_stock_adjustment", _alt_pattern(r"create_stock_adjustment", r"stock[\s_-]?adjustment")),
        ("create_stock_transfer", _alt_pattern(r"create_stock_transfer", r"stock[\s_-]?transfer")),
    ], ["api/inventory-service/router/vouchers_api.py"])
T13_HOOKS_REQ = EnumRequirement(
    "EP_hooks", "all 5 voucher frontend hooks named, backed by inventoryApi.ts", [
        ("useGetVouchersQuery", r"useGetVouchersQuery"),
        ("useGetVoucherQuery", r"useGetVoucherQuery"),
        ("useCreateVoucherMutation", r"useCreateVoucherMutation"),
        ("usePostVoucherMutation", r"usePostVoucherMutation"),
        ("useCancelVoucherMutation", r"useCancelVoucherMutation"),
    ], ["client/ecommclient-web/ekamweb/src/lib/api/services/inventory/inventoryapi.ts"])
T13_TABLES_REQ = EnumRequirement(
    "EP_tables", "voucher-owned tables named (Voucher's own bare name is NOT_EVALUATED by design -- "
    "'voucher(s)' is this task's own generic topic noun and cannot be discriminated from casual mention "
    "by substring matching; VoucherSeries/VoucherVersion are distinctive enough to check)", [
        ("Voucher", None),
        ("VoucherSeries", _alt_pattern(r"VoucherSeries", r"voucher_series", r"voucher\s+series")),
        ("VoucherVersion", _alt_pattern(r"VoucherVersion", r"voucher_version", r"voucher\s+version")),
    ], ["api/inventory-service/models.py"])
# Phase 1.35: the Phase 1.34 EP_gap_disclosure instance (T13_GAP_DISCLOSURE_REQ, hardcoded
# voucher operation names + frontend path + voucher regex, all living in this GENERIC file) was
# removed entirely -- not redesigned-in-place, not moved elsewhere under a new name. The
# CAPABILITY it provided is now CrossReferenceRequirement above, which carries no project-
# specific content; this means T13b's own live acceptance no longer has an instance enforcing
# its specific gap-disclosure obligation (an honest, reported trade-off -- see
# docs/reports/phase-1.35-report.md section on why re-wiring a T13b-specific instance back in
# here, even by reusing T13_ENDPOINTS_REQ/T13_HOOKS_REQ's own item lists via
# CrossReferenceRequirement, would still be task-specific WIRING embedded in generic
# acceptance.py -- the same architectural pattern this phase's audit flags more broadly for
# T13_ENDPOINTS_REQ/T13_HOOKS_REQ/T13_TABLES_REQ/T13_VOUCHER_POLICY themselves, pre-dating this
# phase, and explicitly out of this phase's own narrow scope to migrate wholesale).
T13_VOUCHER_POLICY = EvidencePolicy(
    name="t13-voucher-audit",
    requirements=[],   # T13a names no files (deliberately); T13b's 3 named files get their own Requirements
                       # from derive_policy()'s existing per-path mechanism and are merged in, not duplicated here.
    enum_requirements=[T13_ENDPOINTS_REQ, T13_HOOKS_REQ, T13_TABLES_REQ])

ANY_SOURCE = "*"      # Phase 1.20: a generic requirement -- ANY successful source read of this attempt satisfies it


def _req_source(req, srcs):
    if list(req.any_of_paths) == [ANY_SOURCE]:
        return next(iter(srcs.values()), None) if srcs else None
    for p, t in srcs.items():
        for cand in req.any_of_paths:
            if p.endswith(norm_path(cand)):
                return t
    return None


def gate_evaluate(policy, outcomes, text, semantic_mappings: dict | None = None) -> dict:
    """Only SUCCESS source_read outcomes of THIS attempt satisfy R1-R4; R5 needs source-text evidence of each hop.
    `semantic_mappings` (Phase 1.36): forwarded to cross_reference_result() unchanged -- see its own docstring."""
    if policy is None:
        return {"applicable": False, "satisfied": [], "missing": [], "reads": {}, "enum_detail": [], "not_evaluated": []}
    srcs = _read_sources(outcomes)
    sat, miss = [], []
    by_req = {}
    for r in policy.requirements:
        t = _req_source(r, srcs)
        generic = list(r.any_of_paths) == [ANY_SOURCE]
        calls = sorted({o.seq for o in outcomes if o.outcome == SUCCESS and o.evidence_kind == "source_read"
                        and (generic or any(p.endswith(norm_path(c)) for p in o.paths_read for c in r.any_of_paths))})
        by_req[r.id] = t
        (sat if t is not None else miss).append({"id": r.id, "description": r.description, "satisfied_by_calls": calls})
    link_results = []
    if policy.links:
        report = (text or "").lower()
        for ln in policy.links:
            f_src, t_src = by_req.get(ln.from_req), by_req.get(ln.to_req)
            problems = []
            if f_src is None or t_src is None:
                problems.append("a required file in this hop was not read")
            else:
                if not re.search(ln.from_regex, f_src):
                    problems.append("source-side relationship pattern not found in the 'from' file as read")
                if not re.search(ln.to_regex, t_src):
                    problems.append("source-side relationship pattern not found in the 'to' file as read")
            missing_tok = [t for t in ln.report_tokens if t.lower() not in report]
            if missing_tok:
                problems.append("report does not name: " + ", ".join(missing_tok))
            link_results.append({"id": ln.id, "ok": not problems, "problems": problems})
        ok5 = not miss and all(x["ok"] for x in link_results)
        (sat if ok5 else miss).append({"id": "R5_traceable_relationships",
                                       "description": "evidence-backed relationships between upload, storage, registration, verification",
                                       "links": link_results})
    # Phase 1.23: content-level enumeration obligations. SATISFIED -> sat (same shape as a Requirement,
    # decide()'s n_sat/missing-id logic only reads "id"); mandatory MISSING/INSUFFICIENT_EVIDENCE -> miss
    # (so decide()'s existing PARTIAL/UNRESOLVED/BLOCKED computation picks these up with zero changes to
    # decide() itself); optional-and-unsatisfied and NOT_EVALUATED NEVER enter sat or miss -- Track B
    # property 7 (optional must not block) and property 6 (an unverifiable obligation must be reported,
    # not guessed as satisfied OR held against the attempt as missing).
    enum_detail, not_evaluated = [], []
    for er in policy.enum_requirements:
        res = enum_requirement_result(er, srcs, text)
        enum_detail.append(res)
        if res["status"] == "SATISFIED":
            sat.append({"id": res["id"], "description": res["description"], "items": res["items"]})
        elif res["status"] == "NOT_EVALUATED":
            not_evaluated.append({"id": res["id"], "description": res["description"], "items": res["items"]})
        elif er.mandatory:
            miss.append({"id": res["id"], "description": res["description"], "items": res["items"]})
        else:
            not_evaluated.append({"id": res["id"], "description": res["description"], "items": res["items"],
                                  "note": "optional, unsatisfied -- does not block completion"})
    # Phase 1.34/1.35: two-sided comparison obligations -- same satisfied/missing/not_evaluated
    # folding as the enum_requirements loop just above, via cross_reference_result() instead of
    # enum_requirement_result() (see CrossReferenceRequirement's own docstring for why). UNCERTAIN
    # (the right side's own evidence was never read, so matching could not be determined either
    # way) folds into not_evaluated, same as NOT_EVALUATED/optional-unsatisfied -- never held
    # against the attempt as a failure, per Track B's absence-vs-proof distinction.
    xref_detail = []
    for xr in policy.cross_reference_requirements:
        res = cross_reference_result(xr, srcs, text, semantic_mappings=semantic_mappings)
        xref_detail.append(res)
        if res["status"] == "SATISFIED":
            sat.append({"id": res["id"], "description": res["description"], "items": res["items"]})
        elif res["status"] == "UNCERTAIN":
            not_evaluated.append({"id": res["id"], "description": res["description"], "items": res["items"],
                                  "note": "right-side evidence never read -- matching undetermined, not held against the attempt"})
        elif xr.mandatory:
            miss.append({"id": res["id"], "description": res["description"], "items": res["items"]})
        else:
            not_evaluated.append({"id": res["id"], "description": res["description"], "items": res["items"],
                                  "note": "optional, unsatisfied -- does not block completion"})
    return {"applicable": True, "satisfied": sat, "missing": miss, "links": link_results,
            "reads": {p: True for p in srcs}, "enum_detail": enum_detail, "not_evaluated": not_evaluated,
            "cross_reference_detail": xref_detail}


# ---------------------------------------------------------------------------- attempts and registry
@dataclass
class Attempt:
    task_id: str
    work_item_id: str
    attempt_id: str
    generation: int
    workflow_run_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    workflow_status: Optional[str] = None
    terminal: dict = field(default_factory=dict)
    outcomes: list = field(default_factory=list)
    text: str = ""
    # Phase 1.22: the complete diagnostic trace (every turn: narration, tool-call rounds,
    # tool results, every assistant response) -- kept SEPARATE from `text`, which is now
    # the selected canonical terminal answer only. Never consumed by acceptance or
    # build_final(); evidence only. See swarm/eng_workflow.py's _Trace.terminal_text for
    # the selection boundary and terminal_gate() below for how a missing terminal result
    # is refused rather than silently replaced by this trace.
    trace_text: str = ""
    source: str = ""
    state: str = "RUNNING"            # RUNNING | FINISHED | SUPERSEDED | CANCELLED
    preloaded: dict = field(default_factory=dict)
    # Phase 1.36: the Researcher's own final session_state (agno's RunOutput.session_state),
    # captured once per attempt in swarm/eng_workflow.py's run_attempt() right alongside
    # att.text/att.outcomes above. Read by decide() for the "semantic_mapping" key a Researcher
    # may have written via the EXISTING update_session_state tool -- no new MCP tool, no
    # hive-mcp change, no redeploy needed for this mechanism. See cross_reference_result()'s
    # own docstring for the exact {left_id: {"maps_to": ..., "evidence": ...}} convention.
    session_state: dict = field(default_factory=dict)


class Registry:
    """In-memory attempts/generations/accepted results. Durable storage is Phase 1.15."""

    def __init__(self):
        self.current_gen: dict = {}
        self.attempts: dict = {}
        self.accepted: dict = {}
        self.withheld: list = []
        self.log: list = []
        self._n = itertools.count(1)

    def start(self, task_id, work_item_id, workflow_run_id=None, attempt_id=None, generation=None) -> Attempt:
        """`attempt_id`/`generation` (Phase 1.17): when the durable ledger allocated them, the in-memory registry adopts
        exactly those values instead of numbering its own."""
        g = self.current_gen.get(work_item_id, -1) + 1 if generation is None else generation
        for a in self.attempts.values():
            if a.work_item_id == work_item_id and a.state in ("RUNNING", "FINISHED") and a.generation < g:
                a.state = "SUPERSEDED"
        self.current_gen[work_item_id] = g
        a = Attempt(task_id, work_item_id, attempt_id or f"att-{next(self._n):03d}-g{g}", g, workflow_run_id)
        self.attempts[a.attempt_id] = a
        self.log.append(("start", a.attempt_id, g))
        return a

    def cancel(self, attempt_id: str) -> None:
        """The APPLICATION's decision that this attempt is dead. Does not depend on the Agent/tool actually stopping."""
        self.attempts[attempt_id].state = "CANCELLED"
        self.log.append(("cancel", attempt_id))

    def validity(self, a: Attempt) -> str:
        reg = self.attempts.get(a.attempt_id)
        if reg is None:
            return "UNKNOWN_ATTEMPT"
        if reg.state == "CANCELLED":
            return "ATTEMPT_CANCELLED"
        if reg.state == "SUPERSEDED" or a.generation < self.current_gen.get(a.work_item_id, 0):
            return "SUPERSEDED_STALE_GENERATION"
        if a.generation != reg.generation:
            return "GENERATION_MISMATCH"
        return "CURRENT"

    def apply(self, a: Attempt, d: "Decision", result: dict) -> dict:
        """Mirror a registration the DURABLE ledger already decided (the ledger is the authority; this keeps the
        in-process view, which finalization reads, identical to it). Never accepts anything the ledger did not."""
        rec = {"work_item_id": a.work_item_id, "attempt_id": a.attempt_id, "generation": a.generation,
               "status": d.acceptance, "agent_run_id": a.agent_run_id, "workflow_run_id": a.workflow_run_id}
        if result.get("registered") and d.acceptance in (ACCEPTED_COMPLETE, ACCEPTED_PARTIAL):
            self.accepted[a.work_item_id] = rec
            self.log.append(("accept", a.attempt_id, d.acceptance))
        elif result.get("reason") != "IDEMPOTENT_DUPLICATE":
            self.withheld.append(rec)
            self.log.append(("withhold", a.attempt_id, d.acceptance))
        d.registration = result
        return result

    def register(self, a: Attempt, d: "Decision") -> dict:
        v = self.validity(a)
        if v != "CURRENT":
            self.log.append(("reject", a.attempt_id, v))
            return {"registered": False, "reason": v}
        if d.acceptance == NOT_ACCEPTED:
            self.log.append(("reject", a.attempt_id, "not-accepted-execution"))
            return {"registered": False, "reason": "NOT_ACCEPTED:" + (d.not_accepted_cause or d.execution_status)}
        if any(x["attempt_id"] == a.attempt_id for x in self.accepted.values()) or \
                any(x["attempt_id"] == a.attempt_id for x in self.withheld):
            return {"registered": False, "reason": "IDEMPOTENT_DUPLICATE", "no_status_regression": True}
        rec = {"work_item_id": a.work_item_id, "attempt_id": a.attempt_id, "generation": a.generation,
               "status": d.acceptance, "agent_run_id": a.agent_run_id, "workflow_run_id": a.workflow_run_id}
        if d.acceptance in (ACCEPTED_COMPLETE, ACCEPTED_PARTIAL):
            prev = self.accepted.get(a.work_item_id)
            if prev and prev["generation"] > a.generation:
                return {"registered": False, "reason": "WOULD_OVERWRITE_NEWER_GENERATION"}
            self.accepted[a.work_item_id] = rec
            self.log.append(("accept", a.attempt_id, d.acceptance))
            return {"registered": True, "reason": d.acceptance, "generation": a.generation}
        self.withheld.append(rec)
        self.log.append(("withhold", a.attempt_id, d.acceptance))
        return {"registered": False, "reason": "WITHHELD_" + d.acceptance, "generation": a.generation}


# ---------------------------------------------------------------------------- terminal gate
def terminal_gate(a: Attempt) -> tuple:
    """Agent terminal outcome from the Agent's OWN facts. Workflow status and attempt validity are separate layers."""
    t = a.terminal or {}
    if t.get("cancelled_event"):
        return "CANCELLED", "agent emitted RunCancelled"
    if t.get("timed_out"):
        return "TIMED_OUT", "attempt exceeded its deadline"
    if t.get("run_error"):
        return "FAILED", f"agent RunError: {str(t['run_error'])[:120]}"
    if t.get("stream_closed_early"):
        return "INCOMPLETE", "stream closed before a terminal event"
    if t.get("result_selection_status") == "MISSING_TERMINAL":
        # Phase 1.22: the Agno run itself reported a normal completion (RunCompleted,
        # no error/cancel/early-close -- all three checks above already passed), but
        # the structural terminal-result boundary (swarm/eng_workflow.py's
        # _Trace.terminal_text: the last model turn, only if it was never followed by
        # a ToolCallStarted) found no content to select. This is a DISTINCT failure
        # mode from every case above: the agent did not fail, error, or get cut off --
        # it simply produced no selectable terminal answer. Routed through the SAME
        # "not COMPLETED" -> NOT_ACCEPTED path as those cases (decide()'s own
        # `elif ex != "COMPLETED"` branch), not a new acceptance rule: a missing
        # terminal result must never reach the assessor, which would otherwise see an
        # empty a.text and could vacuously report zero unsupported claims as COMPLETE.
        return "INCOMPLETE", "agent completed but no terminal-result segment was selected (see result_selection_status)"
    ros = str(t.get("run_output_status") or "").lower()
    if not t.get("completed_event") or "completed" not in ros:
        return "INCOMPLETE", (f"no normal terminal completion (completed_event={t.get('completed_event')}, "
                              f"run_output_status={t.get('run_output_status')})")
    return "COMPLETED", "agent completed normally"


# ---------------------------------------------------------------------------- attempt-scoped assessor feed
def build_assessor_state(a: Attempt, outcomes):
    """A FRESH state object per attempt, fed only from THIS attempt's classified outcomes through the production
    recorder. FAILED/REFUSED/TIMED_OUT -> err bucket (no evidence obtained); CANCELLED/UNKNOWN -> not recorded."""
    from swarm import team as T
    ev = T._new_delegation_evidence(T.unavailable_tool_snapshot().get("count", 0))
    ev["attempt_id"] = a.attempt_id
    ev["compose_routes"] = True     # Phase 1.16: same-file router-prefix composition is recognised on this path only
    for o in outcomes:
        if o.tool in STATE_TOOLS:
            continue
        if o.outcome == SUCCESS and o.evidence_kind != "state":
            if o.tool in READ_TOOLS and o.evidence_kind != "source_read":
                # executed but returned no genuine source content: not evidence
                T._record_call_in_evidence(ev, o.tool, {}, "", ok=True)
            else:
                T._record_call_in_evidence(ev, o.tool, o.args, o.result_text or "", ok=True)
        elif o.outcome in (FAILED, REFUSED, TIMED_OUT):
            T._record_call_in_evidence(ev, o.tool, o.args, o.result_text or "", ok=False)
            if o.outcome == REFUSED:
                ev["refused"][o.tool] = ev["refused"].get(o.tool, 0) + 1
    ns = SimpleNamespace(members=[], _delegation_generation={KEY: a.generation},
                         _delegation_progress={"calls": {}, "evidence": {KEY: ev}},
                         _delegation_preloaded={KEY: dict(a.preloaded)}, _unavailable_forced_generation={},
                         _result_assessments={}, _member_assessments={})
    return ns, ev


# ---------------------------------------------------------------------------- decision pipeline
@dataclass
class Decision:
    identity: dict
    workflow_status: Optional[str]
    execution_status: str
    execution_reason: str
    counts: dict
    gate: dict
    validity: str = "CURRENT"
    assessor_status: Optional[str] = None
    assessor_reason: Optional[str] = None
    assessor_claims: Optional[dict] = None
    acceptance: str = NOT_ACCEPTED
    overrides: list = field(default_factory=list)
    reason: str = ""
    registration: Optional[dict] = None
    steps: list = field(default_factory=list)
    not_accepted_cause: Optional[str] = None   # why NOT_ACCEPTED when it is not an Agent/validity cause
    # Phase 1.23: reported SEPARATELY from `acceptance` (which stays the existing combined/restricted
    # verdict, derivation unchanged) so a reader can distinguish "no valid terminal result was even
    # selected" from "a coverage obligation is missing" from "accuracy of what WAS said is in question" --
    # Track C's explicit requirement not to collapse these into one score.
    terminal_integrity: str = "N/A"    # VALID | MISSING | N/A (gate not reached: error/cancel/timeout upstream)
    coverage_status: str = "N/A"       # SATISFIED | MISSING_MANDATORY | N/A (no coverage policy applicable)
    # Phase 1.25: durable recovery provenance, read from a.terminal (set by
    # eng_workflow.recover_and_reconcile() before this decide() call) and carried into
    # decision_to_dict()'s extra_json -- so the final, committed decision row for a
    # recovery attempt is self-describing without needing application logs: is_recovery,
    # which attempt it recovered from, why, what was missing before, and the per-attempt/
    # task-level budget snapshot at the moment this attempt ran.
    is_recovery: bool = False
    recovery_info: Optional[dict] = None   # None for an initial attempt; a dict (see recover_and_reconcile) for a recovery
    budget: Optional[dict] = None          # per-attempt + task-level discovery/read consumption (see run_attempt)


def _to_acceptance(assessor_status: str) -> str:
    return {"COMPLETE": ACCEPTED_COMPLETE, "PARTIAL": ACCEPTED_PARTIAL, "UNRESOLVED": UNRESOLVED,
            "BLOCKED": BLOCKED}.get(assessor_status, UNRESOLVED)


def decide(a: Attempt, policy, registry: Registry = None, outcomes=None, register: bool = True) -> Decision:
    """The single application-owned acceptance pipeline. Order is fixed and each stage is recorded in `steps`.
    1 identity -> 2 Agent terminal state -> 3 attempt validity -> 4 tool-outcome integrity -> 5 required-evidence
    policy -> 6 existing assessor (this attempt's evidence only) -> 7 reconcile (gates only restrict) -> 8 register."""
    from swarm import team as T
    outcomes = a.outcomes if outcomes is None else outcomes
    ident = {"task_id": a.task_id, "work_item_id": a.work_item_id, "attempt_id": a.attempt_id,
             "generation": a.generation, "workflow_run_id": a.workflow_run_id, "agent_run_id": a.agent_run_id,
             "source": a.source}
    steps = [("1.identity", ident)]
    ex, ex_reason = terminal_gate(a)
    steps.append(("2.agent_terminal_state", {"workflow": a.workflow_status, "agent": ex, "reason": ex_reason}))
    cnt = count_outcomes(outcomes)
    ns, ev = build_assessor_state(a, outcomes)
    try:
        asm = T._assessment_for(ns, KEY, a.text or "", generation=a.generation, attempt_id=a.attempt_id,
                                fail_closed=True)
    except Exception as e:  # noqa: BLE001 -- Phase 1.15: an assessor error is NOT an Agent failure and NEVER accepts
        asm = {"status": "ERROR", "reason": repr(e)[:300], "claims": [], "supported": [], "unsupported": []}
    # Phase 1.36: a Researcher-supplied semantic mapping lives in the attempt's OWN final
    # session_state (captured in swarm/eng_workflow.py's run_attempt()), under the
    # "semantic_mapping" key -- see cross_reference_result()'s own docstring for the exact
    # convention. Absent entirely for any attempt that never wrote one (the common case).
    _semantic_mappings = (a.session_state or {}).get("semantic_mapping") or {}
    gate = gate_evaluate(policy, outcomes, a.text, semantic_mappings=_semantic_mappings)
    d = Decision(ident, a.workflow_status, ex, ex_reason, cnt, gate, assessor_status=asm["status"],
                 assessor_reason=asm["reason"],
                 assessor_claims={"claims": len(asm["claims"]), "supported": len(asm["supported"]),
                                  "unsupported": list(asm["unsupported"])[:20]})
    # Phase 1.23: reported independent of `acceptance`'s combined derivation (see Decision's own docstring note).
    # Phase 1.24: RECONCILED is a third, honest provenance -- the text was deterministically
    # assembled by the application from two prior valid attempts (see eng_workflow.reconcile_attempts()),
    # not freshly selected from a live model turn. It is a genuinely valid terminal result (never
    # MISSING), but kept distinguishable from a live TERMINAL_SELECTED rather than conflated with it.
    rss = (a.terminal or {}).get("result_selection_status")
    d.terminal_integrity = {"TERMINAL_SELECTED": "VALID", "MISSING_TERMINAL": "MISSING",
                            "RECONCILED": "VALID_RECONCILED"}.get(rss, "N/A")
    d.coverage_status = "N/A" if not gate["applicable"] else ("MISSING_MANDATORY" if gate["missing"] else "SATISFIED")
    # Phase 1.25: durable recovery provenance -- read from a.terminal, set (only for a recovery
    # attempt) by eng_workflow.recover_and_reconcile() before this decide() call runs. An initial
    # attempt's terminal never carries "recovery_info", so is_recovery stays False and recovery_info
    # stays None for it -- no behavior change for any non-recovery attempt.
    d.recovery_info = (a.terminal or {}).get("recovery_info")
    d.is_recovery = d.recovery_info is not None
    d.budget = (a.terminal or {}).get("budget")
    d.validity = registry.validity(a) if registry else "CURRENT"
    steps.append(("3.attempt_validity", d.validity))
    if d.validity != "CURRENT":
        d.acceptance = NOT_ACCEPTED
        d.overrides.append({"gate": "attempt-validity", "assessor": asm["status"], "final": NOT_ACCEPTED, "why": d.validity})
        d.reason = f"attempt not current: {d.validity}"
    elif ex != "COMPLETED":
        d.acceptance = NOT_ACCEPTED
        d.overrides.append({"gate": "terminal-state", "assessor": asm["status"], "final": NOT_ACCEPTED, "why": f"{ex}: {ex_reason}"})
        d.reason = f"agent {ex}: {ex_reason}; no result accepted (assessor verdict {asm['status']} is diagnostic only)"
    else:
        steps.append(("4.tool_outcome_integrity", {"counts": cnt["all"], "unknown_never_success": True}))
        steps.append(("5.required_evidence_policy", gate))
        if asm["status"] == "ERROR":
            d.acceptance = NOT_ACCEPTED
            d.not_accepted_cause = "ASSESSOR_ERROR"
            d.overrides.append({"gate": "assessor-error", "assessor": "ERROR", "final": NOT_ACCEPTED, "why": asm["reason"]})
            d.reason = f"assessor raised ({asm['reason'][:120]}); the Agent completed but nothing is accepted"
            steps.append(("6-7.assessor_reconciled", {"assessor": "ERROR", "final": NOT_ACCEPTED}))
            if registry is not None and register:
                d.registration = registry.register(a, d)
                steps.append(("8.register", d.registration))
            d.steps = steps
            return d
        final = asm["status"] if asm["status"] in _RESTRICTION else "UNRESOLVED"
        if gate["applicable"] and gate["missing"]:
            n_req = len(policy.requirements)
            n_sat = sum(1 for s in gate["satisfied"] if not s["id"].startswith("R5"))
            prevented = cnt["investigation"][FAILED] + cnt["investigation"][REFUSED] + cnt["investigation"][TIMED_OUT]
            if n_sat == 0 and prevented > 0 and cnt["investigation"][SUCCESS] == 0:
                target = "BLOCKED"
            elif n_sat == 0:
                target = "UNRESOLVED"
            else:
                target = "PARTIAL"
            if _RESTRICTION[target] > _RESTRICTION[final]:
                d.overrides.append({"gate": "required-evidence", "assessor": final, "final": target,
                                    "why": "missing: " + ", ".join(m["id"] for m in gate["missing"])})
                final = target
        steps.append(("6-7.assessor_reconciled", {"assessor": asm["status"], "final": final}))
        d.acceptance = _to_acceptance(final)
        d.reason = (f"assessor: {asm['reason']}" if not d.overrides else "; ".join(
            f"{o['gate']} restricted {o['assessor']}->{o['final']} ({o['why']})" for o in d.overrides))
        if asm["status"] in _RESTRICTION:   # invariant: restrict only, never upgrade
            assert _RESTRICTION[final] >= _RESTRICTION[asm["status"]], "gate attempted to upgrade the assessor verdict"
    if registry is not None and register:
        d.registration = registry.register(a, d)
        steps.append(("8.register", d.registration))
    d.steps = steps
    return d


# ---------------------------------------------------------------------------- Phase 1.24: recovery eligibility
def recovery_eligible(d: Decision) -> bool:
    """Phase 1.24 Track 5: application-controlled and fully deterministic -- the Coordinator and
    Researcher are never asked whether to retry. Recovery is considered ONLY when ALL of:
      - execution completed normally (terminal_gate's own COMPLETED, never FAILED/INCOMPLETE/
        CANCELLED/TIMED_OUT -- those already use the separate, pre-existing RETRYABLE_TERMINALS
        path, checked by the caller before this one, and never doubled up with recovery);
      - a valid terminal result was actually selected (Phase 1.22's terminal_integrity VALID or,
        for a second recovery round that is never currently triggered, VALID_RECONCILED -- never
        on a MISSING or unknown terminal);
      - the accuracy assessor actually ran without erroring;
      - a MANDATORY coverage obligation is specifically missing or insufficient -- coverage_status
        is exactly MISSING_MANDATORY, never SATISFIED (nothing to recover) or N/A (no evidence
        policy applies, or only optional/unmapped obligations remain, which must never block or
        trigger recovery per Track 5's own explicit list)."""
    return (d.execution_status == "COMPLETED"
            and d.terminal_integrity in ("VALID", "VALID_RECONCILED")
            and d.assessor_status not in (None, "ERROR")
            and d.coverage_status == "MISSING_MANDATORY")


# Phase 1.29d Track 2.2: a bounded incremental extension, granted at most once per work
# item (the architecture runs exactly one recovery attempt, so there is exactly one
# extension-decision point) -- these are initial-policy examples per the mission's own
# text ("not immutable values"), not load-bearing constants: 30,000 at a time, capped so
# the task's cumulative read/discovery total can never exceed 120,000 (the 90,000 initial
# allocation plus one bounded increment). Separate from the per-call producer maximum
# (hive-mcp's own _MAX_RENDERED_CHARS/_MAX_RANGED_READ_CHARS, untouched by this phase) and
# from the task-scoped initial allocation (_DELEGATION_READ_CEILING's own default, also
# untouched -- this only ever RAISES the ceiling a specific recovery attempt is given).
TASK_READ_EXTENSION_INCREMENT = 30_000
TASK_READ_ABSOLUTE_CEILING = 120_000
TASK_DISCOVERY_EXTENSION_INCREMENT = 30_000
TASK_DISCOVERY_ABSOLUTE_CEILING = 120_000
# A remaining allowance at or below this, when the original attempt finished with mandatory
# coverage missing, is the deterministic signal that resource exhaustion -- not something
# else -- was plausibly the blocker. Matches swarm/team.py's own
# _MIN_USEFUL_ADAPTED_RESERVATION (2,000) with headroom: well below this, no single call
# could have proceeded even with Phase 1.29d's own adaptive reservation.
EXTENSION_LOW_REMAINING_THRESHOLD = 5_000
# The original attempt must have actually spent a meaningful amount of its allowance on
# reading/discovery -- a 0-char or near-0 spend means retrieval itself stalled or never
# started, which an extension cannot fix and must not be mistaken for exhaustion.
EXTENSION_MIN_PROGRESS_CHARS = 1_000


def extension_eligible(d: Decision, orig_rec) -> dict:
    """Phase 1.29d Track 2.2: application-controlled and fully deterministic -- the
    Coordinator and Researcher are never asked whether to extend, and nothing here reads
    model text. Every input is a ledger/budget fact already recorded for the ORIGINAL
    attempt (orig_rec), the same attempt whose Decision (d) already passed
    recovery_eligible(d) -- this function decides ONLY whether that already-triggered
    recovery attempt should ALSO receive a bounded task-budget extension, never whether
    recovery itself happens.

    All of the following must hold for either read or discovery to be extended:
      - a mandatory coverage obligation is specifically missing (same coverage_status
        check recovery_eligible already made; recomputed here so this function is
        correct standalone, e.g. for scratch testing, without relying on caller order);
      - the ORIGINAL attempt's own recorded remaining allowance for that category was at
        or below EXTENSION_LOW_REMAINING_THRESHOLD -- i.e. exhaustion, not something else,
        is the plausible blocker (a MISSING_MANDATORY result reached with budget to spare
        means more budget would not help, and none is granted);
      - the ORIGINAL attempt actually spent at least EXTENSION_MIN_PROGRESS_CHARS chars in
        that category -- meaningful progress, not a stall;
      - the proposed new ceiling (current ceiling + the bounded increment, capped) is
        STRICTLY above the current ceiling -- i.e. there is genuine room left under the
        configured absolute ceiling; at the absolute ceiling already, no extension.

    Returns a dict that always includes "eligible" (true if EITHER category would be
    extended) plus every fact the decision was made on and the exact new ceiling(s) --
    recorded via st.emit() whether or not the extension ends up eligible, and folded into
    the recovery attempt's own terminal["budget"]["extension"] when it does, for a durable,
    inspectable record of why (Track 2.3: "Every extension must have a durable ledger
    record... Record the reason, outstanding obligations, prior allocation, additional
    allocation, new allocation.")."""
    budget = (orig_rec.attempt.terminal or {}).get("budget") or {}
    read_remaining = budget.get("task_read_remaining")
    disc_remaining = budget.get("task_discovery_remaining")
    attempt_read = budget.get("attempt_read_chars", 0) or 0
    attempt_discovery = budget.get("attempt_discovery_chars", 0) or 0
    current_read_ceiling = budget.get("task_read_ceiling")
    current_disc_ceiling = budget.get("task_discovery_ceiling")

    obligation_missing = d.coverage_status == "MISSING_MANDATORY" and bool(
        [m["id"] for m in d.gate["missing"]] if d.gate.get("applicable") else [])

    read_exhausted = (read_remaining is not None and read_remaining <= EXTENSION_LOW_REMAINING_THRESHOLD
                       and attempt_read >= EXTENSION_MIN_PROGRESS_CHARS)
    disc_exhausted = (disc_remaining is not None and disc_remaining <= EXTENSION_LOW_REMAINING_THRESHOLD
                       and attempt_discovery >= EXTENSION_MIN_PROGRESS_CHARS)

    new_read_ceiling = (min((current_read_ceiling or 0) + TASK_READ_EXTENSION_INCREMENT, TASK_READ_ABSOLUTE_CEILING)
                         if (obligation_missing and read_exhausted and current_read_ceiling is not None) else None)
    new_disc_ceiling = (min((current_disc_ceiling or 0) + TASK_DISCOVERY_EXTENSION_INCREMENT, TASK_DISCOVERY_ABSOLUTE_CEILING)
                         if (obligation_missing and disc_exhausted and current_disc_ceiling is not None) else None)
    read_extension = (new_read_ceiling - current_read_ceiling) if (new_read_ceiling is not None
                       and new_read_ceiling > current_read_ceiling) else 0
    disc_extension = (new_disc_ceiling - current_disc_ceiling) if (new_disc_ceiling is not None
                       and new_disc_ceiling > current_disc_ceiling) else 0

    return {
        "eligible": read_extension > 0 or disc_extension > 0,
        "obligation_missing": obligation_missing,
        "missing": [m["id"] for m in d.gate["missing"]] if d.gate.get("applicable") else [],
        "task_read_remaining": read_remaining, "task_discovery_remaining": disc_remaining,
        "attempt_read_chars": attempt_read, "attempt_discovery_chars": attempt_discovery,
        "prior_read_ceiling": current_read_ceiling, "prior_discovery_ceiling": current_disc_ceiling,
        "read_extension": read_extension, "discovery_extension": disc_extension,
        "new_read_ceiling": current_read_ceiling + read_extension if read_extension else None,
        "new_discovery_ceiling": current_disc_ceiling + disc_extension if disc_extension else None,
    }


def requirement_source_paths(policy, req_id: str) -> list:
    """The file paths (or ANY_SOURCE) a missing requirement/enum-requirement needs evidence from,
    resolved from the SAME policy object that produced the obligation in the first place -- never
    guessed or hardcoded per task (Track 4: reuse the existing policy structures)."""
    for r in policy.requirements:
        if r.id == req_id:
            return list(r.any_of_paths)
    for er in policy.enum_requirements:
        if er.id == req_id:
            return list(er.source_paths)
    for xr in policy.cross_reference_requirements:
        if xr.id == req_id:
            # both sides, deduplicated -- a recovery attempt resolving this obligation may need
            # either the left-side source (if presence there was the gap) or the right-side
            # source (to confirm/disclose the absence), and the caller does not know in advance
            # which this specific missing item needs.
            seen, out = set(), []
            for p in list(xr.left_source_paths) + list(xr.right_source_paths):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
            return out
    return []


def summarize(d: Decision) -> dict:
    return {"attempt": d.identity["attempt_id"], "generation": d.identity["generation"], "workflow": d.workflow_status,
            "agent": d.execution_status, "validity": d.validity,
            "outcomes_S_F_R_C_T_U": [d.counts["all"][k] for k in OUTCOMES], "source_files_read": len(d.counts["source_files_read"]),
            "requirements_missing": [m["id"] for m in d.gate["missing"]] if d.gate["applicable"] else "n/a",
            "assessor": d.assessor_status, "acceptance": d.acceptance, "reason": d.reason[:200],
            "registration": (d.registration or {}).get("reason"),
            # Phase 1.23: the five dimensions Track C requires reported separately.
            "terminal_integrity": d.terminal_integrity, "accuracy": d.assessor_status,
            "coverage": d.coverage_status, "execution": d.execution_status, "overall_acceptance": d.acceptance,
            # Phase 1.25: recovery/budget provenance, reported separately rather than inferred.
            "is_recovery": d.is_recovery, "recovery_info": d.recovery_info, "budget": d.budget}
