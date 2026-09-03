"""Deterministic set comparison across two files.

Why this exists
---------------
Counting was taken out of the model's head and given to ripgrep -- count_matches says
so in its own docstring: "NEVER count by reading a file and tallying in your head --
that is unreliable and is treated as a fabrication." Set comparison never got the same
treatment, and it is the operation behind every recurring two-sided failure measured on
the T1-T13 battery through 2026-09-02:

    "6 endpoints have no hook"          reports the difference, discards both operands
    "there are no gaps"                 asserts the difference is empty
    "all 16 routers are accounted for"  asserts one side is complete
    "no additional files are involved"  asserts a set is closed

Four symptoms, one cause: a set difference across two files read at different times,
computed in the model's head, reported without the operands. Measured on the stored
answers, ~40% of the completeness claims that came out of that operation were false,
and no guard could check them -- a guard holding one side cannot evaluate a claim about
two.

This returns both lists, the matched pairs, and each side's leftovers, computed by
string comparison. The agent's job changes from "compute and report" to "call and
relay", which is the only transformation that has moved the numbers on this system.

The join
--------
Endpoints are spelled differently on each side of the same codebase:

    backend    @router.get("/status")
    frontend   endpoint: "/api/businessservice/business/status"

so the join is a SUFFIX match on the URL path, plus the HTTP method. That is exact
string work, not inference -- both sides literally contain the path. A wrong join is
worse than no join: a false "gap" teaches readers to ignore the finding. So anything
that does not join exactly is reported as unmatched rather than guessed at, and the
output always shows what was matched to what so the basis is visible.

Nothing here is EkamApp-specific except the shape of the two extractors, which key off
FastAPI's @router decorator and RTK Query's `endpoint:` field. Both are framework
conventions, not project ones.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import PROJECT_ROOT

# Backend: FastAPI/APIRouter decorators. Captures method and the router-relative path.
_PY_ROUTE_RE = re.compile(
    r"@\w+\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

# Frontend: RTK Query endpoint definitions. `endpoint:` and `method:` sit in the same
# object literal a line or two apart, so the method is looked for in a window after the
# path rather than on the same line.
# `endpoint:` in an object literal, but also `let endpoint = "..."` -- businessApi.ts
# builds one path that way before appending query params, and requiring the colon
# silently dropped it, reporting a covered endpoint as a gap.
_TS_ENDPOINT_RE = re.compile(r"\bendpoint\s*[:=]\s*[\"'`]([^\"'`]+)[\"'`]")

# Path parameters are positional; their names are arbitrary and differ across the
# boundary by convention -- FastAPI writes "{notification_id}" where the TypeScript
# template writes "${id}". Comparing them literally reports a matched pair as a gap.
# Query strings go too: "?${qs.toString()}" is not part of the route's identity.
_PARAM_RE = re.compile(r"\$\{[^}]*\}|\{[^}]*\}")
_TS_METHOD_RE = re.compile(r"method:\s*[\"'`](get|post|put|patch|delete)[\"'`]", re.I)
_TS_METHOD_WINDOW = 240

# A named export that reads as an RTK hook, for the "which hooks exist" half.
_TS_HOOK_RE = re.compile(r"\buse[A-Z]\w*(?:Query|Mutation)\b")

_MAX_ITEMS = 400


def _read(rel_path: str) -> tuple[str, str | None]:
    """Return (text, error). Never raises -- an unreadable side is reported, not thrown."""
    p = (PROJECT_ROOT / rel_path).resolve()
    try:
        p.relative_to(Path(PROJECT_ROOT).resolve())
    except ValueError:
        return "", f"path escapes the project root: {rel_path}"
    if not p.is_file():
        return "", f"not a file: {rel_path}"
    try:
        return p.read_text(encoding="utf-8", errors="ignore"), None
    except Exception as exc:            # pragma: no cover - unreadable file
        return "", f"could not read {rel_path}: {exc}"


def _routes_from_python(text: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for m in _PY_ROUTE_RE.finditer(text):
        item = (m.group(1).upper(), m.group(2))
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:_MAX_ITEMS]


def _routes_from_ts(text: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for m in _TS_ENDPOINT_RE.finditer(text):
        window = text[m.end():m.end() + _TS_METHOD_WINDOW]
        meth = _TS_METHOD_RE.search(window)
        item = ((meth.group(1).upper() if meth else "GET"), m.group(1))
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:_MAX_ITEMS]


def _extract(rel_path: str, text: str) -> tuple[list[tuple[str, str]], str]:
    """Pick an extractor from the file's own language. Returns (items, kind_label)."""
    if rel_path.endswith((".py",)):
        return _routes_from_python(text), "@router routes"
    if rel_path.endswith((".ts", ".tsx", ".js", ".jsx")):
        return _routes_from_ts(text), "RTK Query endpoints"
    return [], "unrecognised file type"


def _norm_path(p: str) -> str:
    """Route identity: parameters collapsed to {}, query string dropped."""
    return _PARAM_RE.sub("{}", p.split("?", 1)[0]).rstrip("/")


def _joins(left: tuple[str, str], right: tuple[str, str]) -> bool:
    """Same method, and one path is a path-boundary suffix of the other."""
    lm, lp = left
    rm, rp = right
    if lm != rm:
        return False
    a, b = _norm_path(lp), _norm_path(rp)
    if a == b:
        return True
    # "/status" matches "/api/businessservice/business/status" and must NOT match
    # "/business-status". A leading "/" on the shorter side already enforces that:
    # endswith("/status") is false for "/business-status", whose tail is "-status".
    # An earlier version also demanded the remaining prefix end in "/", which double
    # counts the same separator and made every join fail -- 13 routes, 15 endpoints,
    # 0 matched. Caught by running it on the real pair rather than an example.
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return short.startswith("/") and long_.endswith(short)


def compare_enumerations(left_path: str, right_path: str) -> str:
    """
    Compare the endpoints defined in two files — DETERMINISTIC, computed by string match.

    USE THIS FOR ANY "which of X has no Y" / "are there gaps" / "is everything covered"
    question spanning two files. NEVER work the difference out by reading both files and
    comparing in your head — that is the single most common source of wrong answers on
    this kind of task, and a conclusion reported without both lists cannot be checked.

    Returns both enumerations in full, the pairs that matched, and what is left over on
    each side. Backend routes (`@router.get("/x")`) join to frontend RTK Query endpoints
    (`endpoint: "/api/svc/x"`) on HTTP method plus a path-boundary suffix match, so the
    join is exact string comparison. Anything that does not join exactly is listed as
    unmatched rather than guessed at.

    Args:
        left_path:  repo-relative path, e.g. 'API/business-service/router/business_api.py'
        right_path: repo-relative path, e.g. 'Client/.../services/business/businessApi.ts'

    Example:
        compare_enumerations('API/business-service/router/business_api.py',
                             'Client/EcommClient-Web/ekamweb/src/lib/api/services/'
                             'business/businessApi.ts')
    """
    left_text, err_l = _read(left_path)
    right_text, err_r = _read(right_path)
    if err_l or err_r:
        return "compare_enumerations failed: " + "; ".join(e for e in (err_l, err_r) if e)

    left, left_kind = _extract(left_path, left_text)
    right, right_kind = _extract(right_path, right_text)
    if not left and not right:
        return (f"No endpoints found in either file. Extracted {left_kind} from "
                f"{left_path} and {right_kind} from {right_path}; if these are not "
                f"route-defining files, this tool is the wrong one for them.")

    matched, left_only = [], []
    matched_right = set()
    for l in left:
        hit = next((r for r in right if _joins(l, r)), None)
        if hit is None:
            left_only.append(l)
        else:
            matched.append((l, hit))
            matched_right.add(hit)
    right_only = [r for r in right if r not in matched_right]

    def block(title, items, fmt=lambda i: f"{i[0]} {i[1]}"):
        if not items:
            return f"{title} (0):\n  (none)"
        return f"{title} ({len(items)}):\n" + "\n".join(f"  {fmt(i)}" for i in items)

    hooks = sorted(set(_TS_HOOK_RE.findall(right_text)))
    parts = [
        f"compare_enumerations — {left_path}  vs  {right_path}",
        f"join: HTTP method + path-boundary suffix match (exact string, no inference)",
        "",
        block(f"LEFT — {left_kind} in {left_path}", left),
        "",
        block(f"RIGHT — {right_kind} in {right_path}", right),
        "",
        block("MATCHED", matched, lambda p: f"{p[0][0]} {p[0][1]}   <->   {p[1][1]}"),
        "",
        block("LEFT ONLY — defined on the left with no match on the right", left_only),
        "",
        block("RIGHT ONLY — present on the right with no match on the left", right_only),
    ]
    if hooks:
        parts += ["", f"HOOKS EXPORTED BY {right_path} ({len(hooks)}):",
                  "\n".join(f"  {h}" for h in hooks)]
    parts += ["",
              f"TOTALS: left {len(left)}, right {len(right)}, matched {len(matched)}, "
              f"left-only {len(left_only)}, right-only {len(right_only)}."]
    return "\n".join(parts)
