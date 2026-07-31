"""Deterministic claim checking — grep an answer's factual claims against the repo.

No LLM is involved, which is the entire point: a model cannot be trusted to audit its own
output, and a second model just adds a second thing that can hallucinate. This extracts
the checkable claims from a piece of text and greps for each one.

Why it exists (measured on EkamApp, 2026-07-30). main.py already carries a MANDATORY
instruction to cite file+line from code actually read this run. Asked for a bulk-delete
hook that does not exist, the swarm answered:

    "useDeleteItemMutation ... targets /api/inventoryservice/items with DELETE"

inventing both the hook's role and a collection-level endpoint, in 5.6s, with no tool
call. Told explicitly to grep first, the same swarm answered correctly in 14.3s. So the
model verifies when compelled and not otherwise, and instruction-level fixes have already
been tried and observed to fail. The remaining option is to check the output afterwards.

What it catches, honestly stated:
  * INVENTED SYMBOLS      — fully. `useBulkDeleteItems` is either in the repo or it isn't.
  * WRONG LINE NUMBERS    — fully. Read the line, compare it to the claim.
  * INVENTED PATHS/ROUTES — fully, same as symbols.
  * MISATTRIBUTED SYMBOLS — NOT caught. `deleteItem` exists; the false part is the claim
    that it deletes many. Deciding that needs to read intent, which is what a reviewer or
    a human is for. This tool marks the symbol FOUND and says nothing about the claim.
Treat a clean report as "nothing provably invented", never as "the answer is correct".
"""

from __future__ import annotations

import re
import shutil
import subprocess

from config import PROJECT_ROOT

from .context import _RG_EXCLUDES

# Backticked code spans are where models put symbols they are asserting exist.
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
# path/to/file.ext:123  — the citation form the instructions ask for.
_FILE_LINE_RE = re.compile(r"([A-Za-z0-9_\-./]+\.[A-Za-z0-9]{1,6}):(\d{1,6})")
# API routes, asserted constantly and invented almost as often.
_ROUTE_RE = re.compile(r"(/api/[A-Za-z0-9_\-/{}.]+)")
# A bare identifier worth grepping: not prose, not a number.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")

# Words that show up in backticks constantly and mean nothing on their own. Grepping
# them wastes a subprocess and returns thousands of hits.
_NOISE = {
    "true", "false", "null", "none", "string", "number", "boolean", "int", "str",
    "float", "bool", "dict", "list", "any", "void", "async", "await", "return",
    "const", "let", "var", "def", "class", "import", "export", "type", "interface",
    "get", "post", "put", "delete", "patch", "data", "error", "result", "value",
}

_MAX_CLAIMS = 25   # subprocess per claim; keep the whole check inside a few seconds


def _rg(pattern: str, fixed: bool = True, glob_filter: str = "") -> list[str]:
    rg = shutil.which("rg")
    if not rg:
        return []
    cmd = [rg, "-n", "--no-heading", "--max-count", "1"]
    if fixed:
        cmd.append("-F")
    if glob_filter:
        cmd += ["--glob", glob_filter]
    for ex in _RG_EXCLUDES:
        cmd += ["--glob", ex]
    cmd.append(pattern)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=str(PROJECT_ROOT), timeout=20)
        return [l for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def _read_line(rel_path: str, lineno: int) -> str | None:
    p = (PROJECT_ROOT / rel_path)
    try:
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i == lineno:
                    return line.rstrip("\n")
    except Exception:
        return None
    return None


def verify_claims(answer: str, glob_filter: str = "") -> str:
    """
    Check an answer's factual claims against the repository. Read-only, no approval.

    Run this on your OWN answer before returning it whenever you have named a symbol,
    a file:line, or an API route. It greps for each claim and reports what does not
    exist. If something comes back NOT FOUND, correct the answer — do not ship it.

    Args:
        answer:      The text to check (your drafted answer, or another agent's).
        glob_filter: Optional glob to narrow the search, e.g. '**/*.tsx'. Leave empty
                     to search the whole project.

    Limits: proves EXISTENCE, not correctness. A symbol that exists but does not do what
    the answer claims will pass — this cannot read intent.
    """
    if not answer or not answer.strip():
        return "verify_claims: nothing to check (empty answer)."

    # ── collect candidate claims ──────────────────────────────────────────────
    idents: list[str] = []
    for span in _BACKTICK_RE.findall(answer):
        tok = span.strip().rstrip("()").strip()
        if _IDENT_RE.match(tok) and tok.lower() not in _NOISE:
            if tok not in idents:
                idents.append(tok)

    file_lines: list[tuple[str, int]] = []
    for path, num in _FILE_LINE_RE.findall(answer):
        pair = (path, int(num))
        if pair not in file_lines:
            file_lines.append(pair)

    routes: list[str] = []
    for r in _ROUTE_RE.findall(answer):
        r = r.rstrip(".,;)")
        if r not in routes:
            routes.append(r)

    if not (idents or file_lines or routes):
        return ("verify_claims: no checkable claims found (no backticked symbols, "
                "file:line citations, or /api/ routes). Nothing to verify.")

    out: list[str] = ["verify_claims — deterministic grep of the claims in this answer", ""]
    problems = 0

    # ── symbols ───────────────────────────────────────────────────────────────
    if idents:
        out.append(f"SYMBOLS ({len(idents[:_MAX_CLAIMS])} checked):")
        for tok in idents[:_MAX_CLAIMS]:
            hits = _rg(tok, fixed=True, glob_filter=glob_filter)
            if hits:
                out.append(f"  FOUND      {tok:38s} {hits[0][:90]}")
            else:
                problems += 1
                out.append(f"  NOT FOUND  {tok:38s} <-- does not exist in the project")
        out.append("")

    # ── file:line citations ───────────────────────────────────────────────────
    if file_lines:
        out.append(f"CITATIONS ({len(file_lines[:_MAX_CLAIMS])} checked):")
        for path, num in file_lines[:_MAX_CLAIMS]:
            line = _read_line(path, num)
            if line is None:
                problems += 1
                out.append(f"  BAD        {path}:{num} <-- file or line does not exist")
            else:
                out.append(f"  LINE {num:<6} {path}")
                out.append(f"             | {line.strip()[:100]}")
        out.append("")

    # ── routes ────────────────────────────────────────────────────────────────
    if routes:
        out.append(f"ROUTES ({len(routes[:_MAX_CLAIMS])} checked):")
        for r in routes[:_MAX_CLAIMS]:
            # Route literals are usually built from a prefix plus a path segment, so the
            # full string rarely appears verbatim. Search the most distinctive segment
            # instead — a whole-string miss would be a false alarm, which would train
            # agents to ignore this tool.
            segs = [s for s in r.split("/") if s and "{" not in s and s != "api"]
            probe = max(segs, key=len) if segs else r
            hits = _rg(probe, fixed=True, glob_filter=glob_filter)
            if hits:
                out.append(f"  PLAUSIBLE  {r:44s} (segment {probe!r} found)")
            else:
                problems += 1
                out.append(f"  NOT FOUND  {r:44s} <-- no trace of segment {probe!r}")
        out.append("")

    if problems:
        out.append(f"VERDICT: {problems} claim(s) could NOT be found in the project. "
                   f"Fix the answer before returning it — a NOT FOUND symbol or a BAD "
                   f"citation is fabrication, not a near miss.")
    else:
        out.append("VERDICT: every checked claim exists in the project. NOTE: this "
                   "proves existence only. It does NOT confirm the symbol does what the "
                   "answer says it does.")
    return "\n".join(out)
