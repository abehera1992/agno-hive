"""Phase 2 eval harness — four DECOUPLED scorers against a live OpenAI-compatible model.

    python -m training.eval.harness --base-url http://localhost:8003/v1 --model qwen3-coder-30b

Why decoupled: case E-2 (2026-07-30) produced a perfectly correct parameter name and
type annotation attached to a fabricated line number. A single pass/fail scorer marks
that a total failure and hides a real improvement over E-1; a single "accuracy" number
would let citation regressions hide behind grounding gains. Each dimension is therefore
scored and reported separately, and a case only participates in the dimensions it declares.

Scorers
  A. tool_call  — % of tool-using cases where the model emits a well-formed call with
                  the right tool name and parseable args (syntax, not judgement).
  B. grounding  — % of required facts present, IGNORING any line numbers. Fabricated
                  facts (declared per case) score negative.
  C. citation   — of the line numbers actually asserted, how many are right. Asserting
                  nothing and describing the location in prose is a PASS, not an
                  abstention: on a large file that is the correct behaviour.
  D. guard      — % adherence to a project guard (required/forbidden substrings).

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


# ── model client ──────────────────────────────────────────────────────────────

def call_model(
    base_url: str, model: str, messages: list[dict], tools: list[dict] | None = None,
    timeout: int = 180, temperature: float = 0.0,
) -> dict:
    payload: dict[str, Any] = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": 800,
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


def score_grounding(case: dict, text: str) -> tuple[float, str]:
    """B. Required facts present; fabricated facts penalised. Line numbers ignored."""
    body = _strip_line_claims(text).lower()
    required = case.get("required_facts", [])
    forbidden = case.get("forbidden_facts", [])

    hit = [f for f in required if f.lower() in body]
    bad = [f for f in forbidden if f.lower() in body]

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
}

# Both `guard` and `structural` measure Axis D; the harness aggregates them together.
AXIS_OF = {"tool_call": "A", "grounding": "B", "citation": "C", "guard": "D", "structural": "D"}


# ── runner ────────────────────────────────────────────────────────────────────

def run_case(case: dict, base_url: str, model: str) -> dict:
    messages = [{"role": "user", "content": case["prompt"]}]
    if case.get("system"):
        messages.insert(0, {"role": "system", "content": case["system"]})

    t0 = time.time()
    try:
        resp = call_model(base_url, model, messages, tools=case.get("tools"))
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
    ap.add_argument("--model", default="qwen3-coder-30b")
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
