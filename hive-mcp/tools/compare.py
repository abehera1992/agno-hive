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


# ── Phase J-A: identity vs attribute (2026-09-23) ────────────────────────────────────
#
# Root cause traced against a live run (Phase I T13b ZGX validation, then confirmed by
# replaying this exact, unmodified algorithm against the real files): the backend
# declares `PUT /vouchers/{voucher_id}/post` and the frontend's RTK Query definition
# for the same operation declares `method: "post"`. Before this section, `_joins`
# treated (method, path) as ONE atomic identity test -- method was checked FIRST and
# gated everything else, so a method disagreement made a same-path pair invisible to
# each other entirely: reported as LEFT_ONLY, indistinguishable from a genuinely
# unrelated route.
#
# The fix is not a PUT<->POST equivalence rule (that just moves the special-casing
# one level down and doesn't generalize to the next attribute that disagrees). It is
# recognising that the path was always the real identity signal, and method is an
# ATTRIBUTE of the identified operation -- a property that can legitimately differ
# between two independently-written representations, and whose difference is itself
# the interesting, reportable finding, not a reason to treat the pair as unrelated.
#
# _path_identity_match is exactly _joins' own path-suffix logic, unchanged, with the
# method check removed. _joins itself is kept, unchanged in behaviour, as the FULL
# match test (identity + every attribute agreeing) -- MATCHED still requires exactly
# what it always required.
def _path_identity_match(left_path: str, right_path: str) -> bool:
    """Same route identity: one normalized path is a path-boundary suffix of the
    other. This is the identity half of what _joins used to do in one step -- see
    the Phase J-A module comment above for why method is no longer part of it."""
    a, b = _norm_path(left_path), _norm_path(right_path)
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


def _joins(left: tuple[str, str], right: tuple[str, str]) -> bool:
    """FULL match: same method, AND the same route identity (path). Unchanged
    behaviour from before Phase J-A -- MATCHED still means every attribute agrees,
    not just identity. A same-identity pair whose method disagrees no longer falls
    through silently to "unrelated"; see _path_identity_match's own callers in
    compare_enumerations for where that pair is classified instead (PARTIAL_MATCH)."""
    lm, lp = left
    rm, rp = right
    if lm != rm:
        return False
    return _path_identity_match(lp, rp)


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

    # One side empty, the other not: the "both empty" guard above cannot see this,
    # and everything below treats a one-sided-empty result identically to a genuine
    # gap -- a real one at that scale ("13 left-only, 0 right") is legitimately rare
    # even for actually-incomplete coverage. Live incident, Groundedness Battery R5
    # T2 (2026-09-11): the Coordinator delegated a wrong frontend target, and this
    # tool was called with the wrong (but real, parseable) file as the empty side --
    # 13 real backend routes against 0 extracted frontend endpoints, reported as a
    # bare "LEFT ONLY (13)" that read exactly like a real, checkable gap. Mechanical
    # and project-agnostic: it fires on the extraction COUNT alone, never on which
    # file is "correct" -- the same check fires whichever side is empty, and does not
    # know or care what a right answer would have contained. Warns rather than
    # refuses, because a side can legitimately have zero of a construct (a brand-new
    # router file with no routes yet is real); the data stays fully visible below so
    # the reader can judge it, per this tool's own "the basis is visible" design.
    if bool(left) != bool(right):
        empty_path, empty_kind = (right_path, right_kind) if not right else (left_path, left_kind)
        full_path, full_count = (left_path, len(left)) if not right else (right_path, len(right))
        warning = (
            f"WARNING: {empty_path} yielded ZERO {empty_kind} while {full_path} "
            f"yielded {full_count}. A result this lopsided usually means {empty_path} "
            f"is the wrong file for this comparison (empty file, wrong extension "
            f"family, or a file that legitimately defines none of this construct) "
            f"rather than a real {full_count}-item gap. Confirm {empty_path} is the "
            f"intended target before treating anything below as a finding."
        )
    else:
        warning = None

    # Phase J-A: two-stage classification, identity first, then attributes.
    #
    # Stage 1 (full match, unchanged): _joins still requires identity (path) AND
    # every attribute (today: method) to agree -- MATCHED means exactly what it
    # always meant.
    #
    # Stage 2 (new): a left item with NO full match is no longer assumed unrelated
    # to everything on the right. It is checked against _path_identity_match alone
    # -- same operation, by path -- and if exactly one right-side item (not already
    # claimed by a full match) shares that identity, the pair is a PARTIAL_MATCH:
    # same operation, differing on a named attribute, with the difference reported
    # explicitly rather than the pair simply vanishing into LEFT_ONLY.
    #
    # J-A scope: when identity alone matches MORE than one remaining right-side
    # candidate, this stays on the existing first-candidate behaviour (the same
    # "no silent invention beyond what's needed" limit _joins itself already had,
    # and the same category of documented-not-solved gap
    # test_wrong_but_plausible_target_is_not_caught_by_this_check already pins for
    # a different case). Multi-candidate disambiguation is Phase J-B's own,
    # dedicated scope -- not addressed here, not silently guessed at here either.
    #
    # left_only/right_only and the TOTALS line keep their EXACT pre-existing
    # meaning and text shape ("items with no match at all"): a partial-matched
    # item is, by construction, no longer "no match at all", so it correctly
    # leaves left_only -- existing consumers that parse the TOTALS line
    # (swarm/team.py's _comparison_gap_counts) will see a smaller left-only count
    # for exactly the cases this phase targets. That is the intended, documented
    # consequence of this change, not an accident -- see the Phase J-A report for
    # the explicit statement that downstream "is this a gap" interpretation is
    # Phase J-C's job, not redefined here.
    #
    # Phase J-B (2026-09-23): the J-A loop above used
    # `next((r for r in right if _path_identity_match(...)), None)` -- first
    # identity candidate wins, silently, both for full matches and for partial-
    # match fallback. Confirmed live by tracing it directly: when more than one
    # right-side item shares an identity with a left-side item (one-to-many), or
    # the reverse -- one right-side item is the sole identity candidate for more
    # than one left-side item (many-to-one) -- the old loop would silently commit
    # to whichever candidate came first in file order and never report that a
    # choice was made. That is exactly the false-coverage risk an evidence-
    # oriented comparison must not produce.
    #
    # Replaced with an explicit two-pass, index-based classification:
    #   1. For every left item, collect ALL right items sharing its identity
    #      (path) -- not just the first.
    #   2. For every right item, collect ALL left items that would, in turn,
    #      consider IT a candidate (the reverse view) -- this is what catches
    #      many-to-one, which a left-only candidate count can't see on its own.
    #   3. A pairing is only safe to reconcile (MATCH/PARTIAL_MATCH) when it is
    #      genuinely 1:1 in BOTH directions: the left item has exactly one
    #      identity candidate, AND that candidate has exactly one claimant.
    #      Anything else -- >1 candidate on either side -- is AMBIGUOUS, with
    #      every real candidate retained and rendered, never silently resolved.
    #
    # No fuzzy matching, no similarity score, no assignment/optimisation solver
    # (explicitly out of scope): this is still pure deterministic identity
    # comparison, just no longer collapsing "more than one" down to "the first
    # one" without saying so.
    #
    # Two stages, in order -- NOT a single identity-only pass. Caught live against
    # the real EkamApp files while validating this section: GET /vouchers and
    # POST /vouchers share one path-only identity, and so do their two real
    # right-side counterparts -- a path-only-first pass therefore saw 2 candidates
    # for EACH and reported both as AMBIGUOUS, destroying two genuine, unambiguous
    # matches that method already discriminates perfectly. Method is an attribute,
    # not identity -- but when it IS available and it already narrows a path-only
    # group down to exactly one full (identity + attribute) match, that is the
    # single strongest evidence a pairing can have, and it is checked FIRST.
    # Path-only identity is the fallback for whatever remains unmatched, exactly
    # as J-A's own original two-stage design already established -- J-B only adds
    # "collect every candidate, in each stage" instead of "take the first one".
    n_right = len(right)

    # Each partial-match entry: (left_item, right_item, [(attribute, left_val, right_val), ...])
    # Each ambiguous entry: (left_item, [candidate_right_items], reason)
    matched, partial, left_only, ambiguous = [], [], [], []
    # accounted_right: every right index that now has SOME real relationship to a
    # left item -- matched, partial-matched, or named as a candidate in an
    # AMBIGUOUS entry. Anything left out of this set at the end genuinely has no
    # relationship to anything on the left, which is what RIGHT_ONLY means.
    accounted_right: set[int] = set()
    resolved_left: set[int] = set()

    # Stage 1: full match (identity + every attribute agrees) -- unchanged intent
    # from before Phase J-A, just collecting every candidate instead of the first.
    full_candidates = [[j for j, r in enumerate(right) if _joins(l, r)] for l in left]
    full_claimants = [
        [i for i, js in enumerate(full_candidates) if j in js] for j in range(n_right)
    ]
    for i, l in enumerate(left):
        js = full_candidates[i]
        if not js:
            continue  # no full match -- stage 2 decides this item's fate
        if len(js) > 1 or len(full_claimants[js[0]]) > 1:
            # A genuine duplicate-declaration case (two right-side items with the
            # identical method+path, or two left-side items sharing one) -- rare,
            # but the same "never silently pick one" rule applies.
            ambiguous.append((l, [right[j] for j in js],
                              f"{len(js)} right-side item(s) fully match (same "
                              f"identity AND attributes)"
                              if len(js) > 1 else
                              f"its full match is also claimed by "
                              f"{len(full_claimants[js[0]]) - 1} other left-side "
                              f"item(s)"))
            resolved_left.add(i)
            accounted_right.update(js)
            continue
        j = js[0]
        matched.append((l, right[j]))
        accounted_right.add(j)
        resolved_left.add(i)

    # Stage 2: identity-only fallback, for every left item stage 1 left unresolved,
    # searching only right-side items stage 1 has not already claimed.
    remaining_right = [j for j in range(n_right) if j not in accounted_right]
    id_candidates = {
        i: [j for j in remaining_right if _path_identity_match(l[1], right[j][1])]
        for i, l in enumerate(left) if i not in resolved_left
    }
    id_claimants = {
        j: [i for i, js in id_candidates.items() if j in js] for j in remaining_right
    }

    for i, l in enumerate(left):
        if i in resolved_left:
            continue
        js = id_candidates[i]
        if not js:
            left_only.append(l)
            continue
        if len(js) > 1:
            # One-to-many: this left item's identity alone matches more than one
            # remaining right-side item. Every real candidate is retained; none
            # is chosen.
            ambiguous.append((l, [right[j] for j in js],
                              f"{len(js)} right-side items share this identity"))
            accounted_right.update(js)
            continue
        j = js[0]
        if len(id_claimants[j]) > 1:
            # Many-to-one: this left item's ONLY remaining identity candidate is
            # also claimed by other not-yet-resolved left-side items, so treating
            # it as a clean 1:1 pairing for THIS item would silently reuse the
            # same right-side record and report false coverage for whichever
            # item lost the race.
            others = [left[k][0] + " " + left[k][1]
                      for k in id_claimants[j] if k != i]
            ambiguous.append((l, [right[j]],
                              f"its only remaining right-side candidate "
                              f"({right[j][0]} {right[j][1]}) is also claimed "
                              f"by: " + ", ".join(others)))
            accounted_right.add(j)
            continue
        # Genuinely 1:1 among what stage 1 left behind -- attribute reconciliation
        # (we already know from stage 1 that this is NOT a full match, so this is
        # always a difference, never a coincidental match here).
        r = right[j]
        accounted_right.add(j)
        partial.append((l, r, [("http_method", l[0], r[0])]))

    right_only = [r for j, r in enumerate(right) if j not in accounted_right]

    def block(title, items, fmt=lambda i: f"{i[0]} {i[1]}"):
        if not items:
            return f"{title} (0):\n  (none)"
        return f"{title} ({len(items)}):\n" + "\n".join(f"  {fmt(i)}" for i in items)

    def _partial_fmt(p):
        l, r, diffs = p
        diff_str = "; ".join(f"{attr}: {lv} != {rv}" for attr, lv, rv in diffs)
        return f"{_norm_path(l[1])}   {l[0]} {l[1]}  <->  {r[0]} {r[1]}   ({diff_str})"

    def _ambig_fmt(a):
        l, candidates, reason = a
        cand_str = ", ".join(f"{c[0]} {c[1]}" for c in candidates)
        return f"{l[0]} {l[1]}   candidates: {cand_str}   -- {reason}, no pairing selected"

    hooks = sorted(set(_TS_HOOK_RE.findall(right_text)))
    parts = [
        f"compare_enumerations — {left_path}  vs  {right_path}",
        "join: two-stage -- identity (path-boundary suffix match, exact string, no "
        "inference), then attribute agreement (http_method). Same identity with a "
        "differing attribute is PARTIAL MATCHES, not LEFT ONLY/RIGHT ONLY. Identity "
        "matching more than one candidate on either side is AMBIGUOUS -- no "
        "candidate is ever silently selected.",
    ]
    if warning:
        parts += ["", warning]
    parts += [
        "",
        block(f"LEFT — {left_kind} in {left_path}", left),
        "",
        block(f"RIGHT — {right_kind} in {right_path}", right),
        "",
        block("MATCHED", matched, lambda p: f"{p[0][0]} {p[0][1]}   <->   {p[1][1]}"),
        "",
        block("PARTIAL MATCHES — same identity (path), differing on a named "
              "attribute", partial, _partial_fmt),
        "",
        block("AMBIGUOUS — deterministic identity produced more than one plausible "
              "pairing; none was selected", ambiguous, _ambig_fmt),
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
              f"left-only {len(left_only)}, right-only {len(right_only)}.",
              f"PARTIAL-MATCH TOTAL: {len(partial)} (same identity, differing "
              f"attribute -- not counted in left-only/right-only above).",
              f"AMBIGUOUS TOTAL: {len(ambiguous)} (left-side item(s) whose identity "
              f"matched more than one candidate, or whose only candidate was itself "
              f"contested -- not counted in matched/partial/left-only/right-only "
              f"above; see the AMBIGUOUS block for every retained candidate)."]
    return "\n".join(parts)
