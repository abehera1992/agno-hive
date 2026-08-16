"""Phase 2 eval harness — five DECOUPLED scorers against a live OpenAI-compatible model.

    python -m training.eval.harness --base-url http://localhost:8003/v1 --model local-shared

Why decoupled: case E-2 (2026-07-30) produced a perfectly correct parameter name and
type annotation attached to a fabricated line number. A single pass/fail scorer marks
that a total failure and hides a real improvement over E-1; a single "accuracy" number
would let citation regressions hide behind grounding gains. Each dimension is therefore
scored and reported separately, and a case only participates in the dimensions it declares.

Scorers
  A. tool_call   — % of tool-using cases where the model emits a well-formed call with
                   the right tool name and parseable args (syntax, not judgement).
  B. grounding   — % of required facts present, IGNORING any line numbers. Fabricated
                   facts (declared per case) score negative.
  C. citation    — of the line numbers actually asserted, how many are right. Asserting
                   nothing and describing the location in prose is a PASS, not an
                   abstention: on a large file that is the correct behaviour.
  D. guard       — % adherence to a project guard (required/forbidden substrings).
  E. repetition  — does a single completion converge, or does it degenerate into
                   self-repetition? Added 2026-08-16 after four LIVE swarm incidents
                   (verbatim sentence looped for 60,000+ chars over 17+ minutes;
                   an escalating reworded self-correction spiral that never repeated
                   verbatim but never landed on an answer either; a narration leak; a
                   false-positive liveness kill) none of which any of A-D could ever
                   have caught — those are all single-shot correctness scorers, and the
                   incidents were properties of long, multi-turn GENERATION, not of what
                   a completion said. This is a proxy, not equivalent coverage: it can
                   only see degeneracy that fits inside one bounded completion at
                   temperature 0, not the actual multi-turn streamed drift that produced
                   the live incidents. A real check of THAT requires routing real tasks
                   through the candidate once it is served (see RUNBOOK Phase 3b/5) and
                   watching whether swarm/team.py's own liveness/repetition detector
                   fires — this scorer exists so a candidate that is obviously prone to
                   short-horizon looping gets caught before that later, more expensive
                   step, not instead of it.

Every scorer returns (score in 0..1, detail string) so failures are explainable.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CASES_DIR = Path(__file__).parent / "cases"

# A line-number assertion in prose.
# Must tolerate markdown between the word and the digits — the first version allowed
# only 3 non-word chars and so MISSED "**Line number**: `209`", reporting "no line
# asserted" for an output that plainly asserted one. That silently awarded full
# citation credit to any fabricated line formatted that way, i.e. the scorer failed
# at precisely the thing it exists to detect. Widened to allow an optional
# "number"/"no." plus up to 12 non-digit, same-line characters.
_LINE_CLAIM_RE = re.compile(r"\blines?\b(?:\s*(?:numbers?|nos?\.?))?[^\d\n]{0,12}(\d{1,5})", re.I)

# Ported from swarm/team.py's _looks_like_repetition_loop / _REPETITION_* constants
# rather than imported: that module pulls in agno + the full swarm dependency chain,
# and this harness is deliberately stdlib-only so it can run standalone against any
# OpenAI-compatible endpoint. Keep these four constants and _looks_like_repetition_loop
# in sync with swarm/team.py by hand if that detector's calibration changes again —
# the values below (as of 2026-08-16) match it exactly.
_REPETITION_LOOKBACK_CHARS = 4000
_REPETITION_MIN_SEGMENT_LEN = 60
_REPETITION_FILLER_WORDS = frozenset({
    "even", "really", "very", "actually", "now", "again", "just", "simply",
})
_REPETITION_PREFIX_CHARS = 100
# How wide a slice of the completion to treat as one "new segment" when walking the
# text looking for a repeat of something earlier in the SAME completion. The runtime
# detector checks streamed batches as they arrive (~700-900 chars each in the real
# incident); a full offline completion has no natural batch boundary, so a fixed
# window is used instead. Narrower than the runtime's typical batch so a shorter
# eval completion (capped at a few thousand tokens, not tens of thousands of chars)
# still gets multiple scan points instead of just one or two.
_REPETITION_SCAN_WINDOW_CHARS = 400


def _normalize_for_repetition_check(text: str) -> str:
    words = text.split()
    return " ".join(w for w in words if w.lower() not in _REPETITION_FILLER_WORDS)


def _looks_like_repetition_loop(new_segment: str, prior_content: str) -> bool:
    """True if `new_segment` looks like a repeat (verbatim, or lightly reworded /
    escalating) of something in the recent lookback window of `prior_content`,
    rather than genuine new progress. See swarm/team.py's own copy for the full
    incident history this logic is calibrated against."""
    normalized_new = " ".join(new_segment.split())
    if len(normalized_new) < _REPETITION_MIN_SEGMENT_LEN:
        return False
    normalized_prior = " ".join(prior_content[-_REPETITION_LOOKBACK_CHARS:].split())
    if normalized_new in normalized_prior:
        return True

    filler_stripped_new = _normalize_for_repetition_check(new_segment)
    if len(filler_stripped_new) < _REPETITION_MIN_SEGMENT_LEN:
        return False
    filler_stripped_prior = _normalize_for_repetition_check(
        prior_content[-_REPETITION_LOOKBACK_CHARS:]
    )
    if filler_stripped_new in filler_stripped_prior:
        return True

    prefix = filler_stripped_new[:_REPETITION_PREFIX_CHARS]
    if len(prefix) < _REPETITION_MIN_SEGMENT_LEN:
        return False
    return prefix in filler_stripped_prior


# ── model client ──────────────────────────────────────────────────────────────

def call_model(
    base_url: str, model: str, messages: list[dict], tools: list[dict] | None = None,
    timeout: int = 180, temperature: float = 0.0, max_tokens: int = 800,
) -> dict:
    payload: dict[str, Any] = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── scorers ───────────────────────────────────────────────────────────────────

def score_tool_call(case: dict, resp: dict) -> tuple[float, str]:
    """A. Well-formed tool call with the expected name and parseable JSON args."""
    msg = resp["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        return 0.0, "no tool_calls emitted (answered in prose instead)"
    fn = calls[0].get("function", {})
    name = fn.get("name", "")
    want = case["expect_tool"]
    if name != want:
        return 0.0, f"wrong tool: called {name!r}, expected {want!r}"
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError as e:
        return 0.33, f"tool {name!r} correct but arguments are not valid JSON ({e})"
    missing = [k for k in case.get("expect_args", []) if k not in args]
    if missing:
        return 0.66, f"tool + JSON ok, missing arg(s): {missing}"
    return 1.0, f"well-formed call to {name} with {sorted(args)}"


def _strip_line_claims(text: str) -> str:
    return _LINE_CLAIM_RE.sub(" ", text)


def _fact_present(fact: str, body: str) -> bool:
    """A required/forbidden fact may offer alternatives with `|`; any one is a match.

    Added 2026-07-30 after the v2 gate. The eight `C-refuse-*` cases required the literal
    token "read" as a stand-in for the BEHAVIOUR "declined and pointed at the source".
    A textbook-correct refusal — "you'll need to CHECK the file directly or provide its
    content" — scored grounding 0/1 while the citation scorer scored the SAME response
    1.0. All 8 sat at exactly 0.00 in both baseline and candidate, dragging Axis B by
    ~30 points and making a scorer defect look like a model failure.

    The fix belongs in the case vocabulary, not in the scorer: alternation lets a case
    say "any of these words satisfies me" without the scorer learning case-specific
    semantics. Plain substring matching is kept deliberately — switching to word-bounded
    matching here would silently move the B-extract scores too, and this change has to
    stay attributable to the defect it fixes.
    """
    return any(alt in body for alt in
               (a.strip() for a in fact.lower().split("|")) if alt)


def score_grounding(case: dict, text: str) -> tuple[float, str]:
    """B. Required facts present; fabricated facts penalised. Line numbers ignored."""
    body = _strip_line_claims(text).lower()
    required = case.get("required_facts", [])
    forbidden = case.get("forbidden_facts", [])

    hit = [f for f in required if _fact_present(f, body)]
    bad = [f for f in forbidden if _fact_present(f, body)]

    base = len(hit) / len(required) if required else 1.0
    penalty = 0.5 * (len(bad) / len(forbidden)) if forbidden else 0.0
    score = max(0.0, base - penalty)

    parts = [f"facts {len(hit)}/{len(required)}"]
    if bad:
        parts.append(f"FABRICATED: {bad}")
    miss = [f for f in required if f not in hit]
    if miss:
        parts.append(f"missing: {miss}")
    return score, "; ".join(parts)


def score_repetition(case: dict, text: str) -> tuple[float, str]:
    """E. Convergence — does this completion degenerate into self-repetition?

    Binary, not graduated: the runtime detector this mirrors doesn't grade severity
    either, it just decides "loop or not" at each check point. Walks the completion
    in fixed windows, treating each window as a freshly-generated segment and
    everything before it as the prior content — the same shape the runtime detector
    sees while streaming, just replayed against a completed string instead of live
    deltas."""
    window = _REPETITION_SCAN_WINDOW_CHARS
    for i in range(window, len(text), window):
        segment = text[i:i + window]
        prior = text[:i]
        if _looks_like_repetition_loop(segment, prior):
            return 0.0, f"repetition/degeneracy detected at char offset {i}"
    return 1.0, "no repetition/degeneracy detected"


def score_citation(case: dict, text: str) -> tuple[float, str]:
    """C. Precision of asserted line numbers. Prose-only location is a PASS."""
    claimed = [int(n) for n in _LINE_CLAIM_RE.findall(text)]
    correct = set(case.get("correct_lines", []))

    if not claimed:
        return 1.0, "no line asserted (prose location) — correct on a large file"
    right = [n for n in claimed if n in correct]
    wrong = [n for n in claimed if n not in correct]
    score = len(right) / len(claimed)
    return score, f"asserted {claimed}; correct {right or '[]'}; FABRICATED {wrong or '[]'}"


def _token_present(token: str, text: str) -> bool:
    """Substring match, but word-bounded for bare identifiers.

    Naive `in` matching auto-failed correct answers: a case with
    forbidden=["Session"] and required=["AsyncSession"] flagged a VIOLATION on the
    correct code, because "Session" is a substring of "AsyncSession". Identifiers are
    matched on word boundaries; anything containing punctuation (e.g. "db.execute(")
    falls back to plain substring, where boundaries do not apply.
    """
    if re.fullmatch(r"\w+", token):
        return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text, re.I) is not None
    return token.lower() in text.lower()


def score_guard(case: dict, text: str) -> tuple[float, str]:
    """D. Guard adherence via required/forbidden tokens (word-bounded for identifiers)."""
    req = case.get("guard_required", [])
    forb = case.get("guard_forbidden", [])
    hit = [s for s in req if _token_present(s, text)]
    bad = [s for s in forb if _token_present(s, text)]
    base = len(hit) / len(req) if req else 1.0
    score = 0.0 if bad else base
    detail = f"required {len(hit)}/{len(req)}"
    if bad:
        detail += f"; VIOLATION present: {bad}"
    return score, detail


# ── structural (AST) checks for Axis D ────────────────────────────────────────
# Token matching can only express a guard whose rule IS a token (GUARD 18 -> `text(`).
# Most guards are STRUCTURAL — ordering, positional-vs-keyword, presence of a kwarg,
# snapshot-before-mutation — and cannot be scored by substring at all. These parse the
# model's code and check the actual shape, so the rule is measured rather than its
# incidental vocabulary.

_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.S)


def _extract_code(text: str) -> str:
    blocks = _CODE_FENCE_RE.findall(text)
    return "\n\n".join(blocks) if blocks else text


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name:
                yield name, node


def _chk_call_order(tree, spec) -> tuple[bool, str]:
    """`first` must be called before `second` (e.g. flush() before commit())."""
    order = [(n.lineno, name) for name, n in _calls(tree)]
    firsts = [ln for ln, nm in order if nm == spec["first"]]
    seconds = [ln for ln, nm in order if nm == spec["second"]]
    if not firsts:
        return False, f"{spec['first']}() never called"
    if not seconds:
        return False, f"{spec['second']}() never called"
    return (min(firsts) < max(seconds),
            f"{spec['first']}()@{min(firsts)} vs {spec['second']}()@{max(seconds)}")


def _chk_kwarg_present(tree, spec) -> tuple[bool, str]:
    """A call to `call` must carry every kwarg in `kwargs`."""
    for name, node in _calls(tree):
        if name != spec["call"]:
            continue
        have = {k.arg for k in node.keywords if k.arg}
        missing = [k for k in spec["kwargs"] if k not in have]
        if not missing:
            return True, f"{name}(...) has {spec['kwargs']}"
        return False, f"{name}(...) missing kwarg(s) {missing}; has {sorted(have)}"
    return False, f"no call to {spec['call']}()"


def _chk_positional_before_keyword(tree, spec) -> tuple[bool, str]:
    """In `call`, a positional arg mentioning `token` must precede keyword args.

    Python enforces this syntactically, so a violation is a SyntaxError — meaning the
    real test is whether the model emits parseable code at all. Scored via the parse
    plus a check that the token really is positional, not passed as a kwarg.
    """
    for name, node in _calls(tree):
        if name != spec["call"]:
            continue
        pos_src = " ".join(ast.dump(a) for a in node.args)
        if spec["token"] in pos_src:
            return True, f"{spec['token']} is positional in {name}(...)"
        kw_src = " ".join(ast.dump(k.value) for k in node.keywords)
        if spec["token"] in kw_src:
            return False, f"{spec['token']} passed as a KEYWORD in {name}(...)"
        return False, f"{spec['token']} absent from {name}(...)"
    return False, f"no call to {spec['call']}()"


def _chk_absent_call(tree, spec) -> tuple[bool, str]:
    """`call` must NOT appear anywhere (e.g. no ForeignKey on a tenant_id column)."""
    hits = [ln for ln, nm in [(n.lineno, nm) for nm, n in _calls(tree)] if nm == spec["call"]]
    return (not hits, "absent" if not hits else f"{spec['call']}() present at line(s) {hits}")


def _chk_assign_before_mutate(tree, spec) -> tuple[bool, str]:
    """A snapshot must be read into a local BEFORE the attribute is reassigned."""
    attr = spec["attr"]
    reads, writes = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == attr:
                    writes.append(node.lineno)
            src = ast.dump(node.value)
            if f"attr='{attr}'" in src:
                reads.append(node.lineno)
    if not writes:
        return False, f"`.{attr}` is never assigned"
    if not reads:
        return False, f"old `.{attr}` never snapshotted before mutation"
    return (min(reads) < min(writes),
            f"snapshot@{min(reads)} vs mutation@{min(writes)}")


_STRUCT = {
    "call_order": _chk_call_order,
    "kwarg_present": _chk_kwarg_present,
    "positional_before_keyword": _chk_positional_before_keyword,
    "absent_call": _chk_absent_call,
    "assign_before_mutate": _chk_assign_before_mutate,
}


def score_structural(case: dict, text: str) -> tuple[float, str]:
    """D (structural). Parses the emitted code and checks its SHAPE against the rule."""
    code = _extract_code(text)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return 0.0, f"emitted code does not parse ({e.msg} line {e.lineno})"

    results = []
    for spec in case["structural"]:
        fn = _STRUCT.get(spec["type"])
        if fn is None:
            return 0.0, f"unknown structural check {spec['type']!r}"
        ok, detail = fn(tree, spec)
        results.append((ok, f"{spec['type']}: {detail}"))

    passed = sum(1 for ok, _ in results if ok)
    return passed / len(results), "; ".join(d for _, d in results)


SCORERS = {
    "tool_call": ("A", score_tool_call),
    "grounding": ("B", score_grounding),
    "citation": ("C", score_citation),
    "guard": ("D", score_guard),
    "structural": ("D", score_structural),
    "repetition": ("E", score_repetition),
}

# Both `guard` and `structural` measure Axis D; the harness aggregates them together.
AXIS_OF = {"tool_call": "A", "grounding": "B", "citation": "C", "guard": "D",
           "structural": "D", "repetition": "E"}


# ── runner ────────────────────────────────────────────────────────────────────

def run_case(case: dict, base_url: str, model: str) -> dict:
    messages = [{"role": "user", "content": case["prompt"]}]
    if case.get("system"):
        messages.insert(0, {"role": "system", "content": case["system"]})

    # Axis E cases need room for a loop to actually manifest — the harness default of
    # 800 tokens is tuned for the short, single-fact answers A-D cases expect, and
    # would truncate a repetition scan before it could see anything.
    max_tokens = case.get("max_tokens", 800)

    t0 = time.time()
    try:
        resp = call_model(base_url, model, messages, tools=case.get("tools"),
                           max_tokens=max_tokens)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"id": case["id"], "error": f"{type(e).__name__}: {e}", "scores": {}}
    elapsed = time.time() - t0

    msg = resp["choices"][0]["message"]
    text = msg.get("content") or ""

    scores, details = {}, {}
    for dim in case["scorers"]:
        _, fn = SCORERS[dim]
        s, d = fn(case, resp) if dim == "tool_call" else fn(case, text)
        scores[dim], details[dim] = round(s, 3), d

    return {
        "id": case["id"], "kind": case.get("kind", ""), "elapsed_s": round(elapsed, 1),
        "scores": scores, "details": details, "output": text[:400],
    }


def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8003/v1")
    ap.add_argument("--model", default="local-shared")
    ap.add_argument("--out", default="training/eval/baseline.json")
    ap.add_argument("--label", default="untuned baseline")
    a = ap.parse_args()

    cases = load_cases()
    print(f"running {len(cases)} case(s) against {a.model} @ {a.base_url}\n")

    results = []
    for c in cases:
        r = run_case(c, a.base_url, a.model)
        results.append(r)
        if r.get("error"):
            print(f"  {r['id']:28s} ERROR {r['error']}")
        else:
            summary = "  ".join(f"{d}={r['scores'][d]:.2f}" for d in r["scores"])
            print(f"  {r['id']:28s} {summary}   ({r['elapsed_s']}s)")

    # Aggregate per AXIS, not per scorer: `guard` (token) and `structural` (AST) are
    # two ways of measuring Axis D and must combine into one number, or the gate would
    # see two half-populated axes instead of one properly-sized one.
    by_axis: dict[str, list[float]] = {}
    for r in results:
        for dim, val in r.get("scores", {}).items():
            by_axis.setdefault(dim, []).append(val)
    agg = {d: round(statistics.mean(v), 3) for d, v in by_axis.items() if v}
    d_vals = by_axis.get("guard", []) + by_axis.get("structural", [])
    if d_vals:
        agg["guard"] = round(statistics.mean(d_vals), 3)   # gate reads `guard` for Axis D

    print("\n" + "=" * 62)
    print(f"BASELINE — {a.label}")
    print("=" * 62)
    for dim, (letter, _) in SCORERS.items():
        if dim not in agg:
            continue
        # `guard` holds the MERGED Axis D score (token + structural), so its n must
        # count both scorers or the printed n contradicts the printed percentage.
        keys = {"guard", "structural"} if dim == "guard" else {dim}
        n = sum(1 for r in results if keys & set(r.get("scores", {})))
        note = "  (Axis D sub-score, already inside guard)" if dim == "structural" else ""
        print(f"  {letter}. {dim:11s} {agg[dim]*100:5.1f}%   (n={n}){note}")
    errs = [r for r in results if r.get("error")]
    if errs:
        print(f"  !! {len(errs)} case(s) errored")
    print("=" * 62)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"label": a.label, "model": a.model, "base_url": a.base_url,
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "aggregate": agg, "results": results}, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
