"""Target-scoped context for the acting Coder — Phase 2, Experiment 2.

Tests one hypothesis: does context delivered DIRECTLY to the member that acts beat
the current Coordinator-mediated path? Off unless CONTEXTPACK_ENABLED is set, so the
control arm is byte-identical to production.

Two design decisions are load-bearing and were argued before any code was written.

TARGETS COME FROM THE TASK, NOT THE DELEGATION. I4's control failure was that the
Coordinator understood a two-file task and delegated only the backend half; the
frontend was never delegated at all. A pack built from the delegation text could
therefore never reach the half that was dropped. Deriving targets from the frozen task
means the pack can carry both -- and it also means a positive result is "direct
target-scoped delivery, INCLUDING targets the Coordinator dropped", not "context
delivery alone". That confound is real and is stated in the report rather than hidden.

WHAT IS DELIBERATELY ABSENT. No file bodies, no prior answers, no prior task text.
swarm/feedback.py's load_success_context earned those three constraints the hard way:
injecting a prior task made a model execute the OLD task, and injecting an answer body
manufactures confidently-stale citations. Paths and declared symbols are the durable
half. A rubric hint is never included -- the pack must not teach the test.

Everything here is assembled ONCE per run, before the team runs, and read from
`team._context_pack` at delegation time. The hook does no I/O.
"""
from __future__ import annotations

import os

# Hard ceiling on the rendered pack. A cap is what keeps a positive result
# attributable to WHICH context was delivered rather than to how much: without it the
# treatment arm is simply "the Coder got more text". Sections are dropped in the order
# below when over budget; resolved targets are never dropped, because they are the
# thing under test.
MAX_PACK_CHARS = 4000
_DROP_ORDER = ("prior_findings", "failure_corrections", "declarations")

# One line per file, so a wide task cannot crowd out the rest of the pack.
_MAX_DECLS_PER_FILE = 40
_MAX_FINDING_CHARS = 700


def enabled() -> bool:
    """Off unless explicitly switched on. The control arm must be production."""
    return os.getenv("CONTEXTPACK_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def extract_targets(task: str) -> list[str]:
    """Path-shaped tokens in the TASK text, in order, deduplicated.

    Uses swarm.team's own extractor and phase0's classifier rather than a third copy:
    those two already handle this project's real syntax (route groups, dynamic
    segments) and already reject dotted prose like `@router.get` and `e.g`. A private
    reimplementation here would drift from both.
    """
    try:
        from swarm.team import _PATH_TOKEN_RE, _balanced_path
        from swarm.phase0 import classify_citation
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for raw in _PATH_TOKEN_RE.findall(task or ""):
        tok = _balanced_path(raw.replace("\\", "/"))
        if tok and tok not in out and classify_citation(tok) == "path":
            out.append(tok)
    return out


async def build(task: str, hive_mcp_url, hive_mcp_tools=None,
                failure_context: str = "") -> dict | None:
    """Resolve the task's targets once, before the run. None when nothing resolves.

    A target that cannot be verified to exist is OMITTED, never asserted. Stating that
    a file exists when the lookup only failed to find it is the exact failure this
    codebase hit when a path guard told a model a real file did not exist and the model
    reported the denial as fact.
    """
    targets = extract_targets(task)
    if not targets:
        return None
    try:
        from swarm.team import _repo_file_text, _extract_declarations
    except Exception:  # noqa: BLE001
        return None

    resolved: list[dict] = []
    for path in targets:
        text = await _repo_file_text(path, hive_mcp_url, hive_mcp_tools)
        if not text:
            continue                      # unknown is not "missing" -- say nothing
        decls = _extract_declarations(text)
        resolved.append({
            "path": path,
            "lines": text.count("\n") + 1,
            "declarations": decls[:_MAX_DECLS_PER_FILE],
            "declaration_total": len(decls),
        })
    if not resolved:
        return None
    return {
        "targets": resolved,
        "failure_corrections": (failure_context or "").strip(),
        "multi_file": len(resolved) > 1,
    }


def render(pack: dict, member_results: dict | None = None,
           already_examined: list[str] | None = None) -> str:
    """The text appended to a Coder delegation, capped at MAX_PACK_CHARS."""
    if not pack or not pack.get("targets"):
        return ""

    targets_block = ["── TARGETS FOR THIS TASK (read from the repository before this "
                     "run; each one verified to exist) ──"]
    for t in pack["targets"]:
        targets_block.append(f"- {t['path']}  ({t['lines']} lines)")
    if pack.get("multi_file"):
        targets_block.append(
            "This task requires changes in ALL of the files listed above. Staging only "
            "some of them leaves it incomplete.")

    decl_block = []
    for t in pack["targets"]:
        if not t["declarations"]:
            continue
        shown = ", ".join(t["declarations"])
        more = (f" (+{t['declaration_total'] - len(t['declarations'])} more)"
                if t["declaration_total"] > len(t["declarations"]) else "")
        decl_block.append(f"- {t['path']} already declares: {shown}{more}")
    if decl_block:
        decl_block.insert(0, "── ALREADY DECLARED IN THOSE FILES (name:line) ──")

    findings_block = []
    for member, text in (member_results or {}).items():
        if not text:
            continue
        hit = [t["path"] for t in pack["targets"] if t["path"] in text]
        if not hit:
            continue
        findings_block.append(f"- {member} reported on {', '.join(hit)}:\n"
                              f"  {text.strip()[:_MAX_FINDING_CHARS]}")
    if findings_block:
        findings_block.insert(0, "── WHAT A TEAMMATE ALREADY FOUND ABOUT THESE FILES ──")

    examined_block = []
    if already_examined:
        examined_block = ["── ALREADY OPENED THIS RUN ── " + ", ".join(already_examined[:10])]

    corrections_block = []
    if pack.get("failure_corrections"):
        corrections_block = ["── CORRECTIONS FROM PAST RUNS ──",
                             pack["failure_corrections"][:800]]

    sections = {
        "targets": targets_block,
        "declarations": decl_block,
        "prior_findings": findings_block,
        "already_examined": examined_block,
        "failure_corrections": corrections_block,
    }

    def _assemble(active: dict) -> str:
        parts = [l for key in ("targets", "declarations", "prior_findings",
                               "already_examined", "failure_corrections")
                 for l in active.get(key, [])]
        return ("\n\n── CONTEXT PACK ──\n" + "\n".join(parts)) if parts else ""

    out = _assemble(sections)
    for key in _DROP_ORDER:                      # shed until it fits; targets never go
        if len(out) <= MAX_PACK_CHARS:
            break
        sections[key] = []
        out = _assemble(sections)
    return out[:MAX_PACK_CHARS]
