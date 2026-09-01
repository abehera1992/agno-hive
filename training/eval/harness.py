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
    semantics.

    Word boundaries added 2026-08-31, matching what `_token_present` has always done and
    for the same reason. Plain substring had been kept deliberately, on the argument that
    boundary matching would silently move other scores — but it produced the defect twice
    in one afternoon on the new absence-trap cases. First a bare "no" alternative matched
    inside "TenantNotificationPrefs", passing a known-WRONG answer; removing "no" then
    failed eight answers whose entire text was the word "No". Both are the
    "Session" ⊂ "AsyncSession" bug the token scorer already guards against. Bare
    identifiers are matched on word boundaries; anything carrying punctuation
    ("String(100)", "0.4", "does not") stays plain substring, where boundaries do not
    apply. The case self-tests in validate_cases.py now cover this either way.
    """
    for alt in (a.strip() for a in fact.lower().split("|")):
        if not alt:
            continue
        if re.fullmatch(r"\w+", alt):
            if re.search(rf"(?<!\w){re.escape(alt)}(?!\w)", body):
                return True
        elif alt in body:
            return True
    return False


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
    """SOME call to `call` must carry every kwarg in `kwargs`.

    Scans every matching call rather than judging the first one. A realistic answer
    contains several calls to the same constructor and only one of them is the call the
    rule is about; stopping at the first produced a false failure on correct code.
    """
    seen = []
    for name, node in _calls(tree):
        if name != spec["call"]:
            continue
        have = {k.arg for k in node.keywords if k.arg}
        missing = [k for k in spec["kwargs"] if k not in have]
        if not missing:
            return True, f"{name}(...)@{node.lineno} has {spec['kwargs']}"
        seen.append(f"@{node.lineno} missing {missing}")
    if not seen:
        return False, f"no call to {spec['call']}()"
    return False, f"no {spec['call']}(...) carries {spec['kwargs']}: {'; '.join(seen)}"


def _chk_positional_before_keyword(tree, spec) -> tuple[bool, str]:
    """In `call`, a positional arg mentioning `token` must precede keyword args.

    Python enforces this syntactically, so a violation is a SyntaxError — meaning the
    real test is whether the model emits parseable code at all. Scored via the parse
    plus a check that the token really is positional, not passed as a kwarg.

    Scans every call to `call`, not just the first. A models file legitimately contains
    many `Column(...)` calls and only one carries the ForeignKey; judging the first one
    reported "ForeignKey absent" on an answer that had it correctly placed two lines down.
    """
    n_calls = 0
    for name, node in _calls(tree):
        if name != spec["call"]:
            continue
        n_calls += 1
        pos_src = " ".join(ast.dump(a) for a in node.args)
        if spec["token"] in pos_src:
            return True, f"{spec['token']} is positional in {name}(...)@{node.lineno}"
        kw_src = " ".join(ast.dump(k.value) for k in node.keywords)
        if spec["token"] in kw_src:
            return False, f"{spec['token']} passed as a KEYWORD in {name}(...)@{node.lineno}"
    if not n_calls:
        return False, f"no call to {spec['call']}()"
    return False, f"{spec['token']} absent from all {n_calls} {spec['call']}(...) call(s)"


def _chk_absent_call(tree, spec) -> tuple[bool, str]:
    """`call` must NOT appear anywhere (e.g. no ForeignKey on a tenant_id column)."""
    hits = [ln for ln, nm in [(n.lineno, nm) for nm, n in _calls(tree)] if nm == spec["call"]]
    return (not hits, "absent" if not hits else f"{spec['call']}() present at line(s) {hits}")


def _chk_assign_before_mutate(tree, spec) -> tuple[bool, str]:
    """A snapshot must be read into a local BEFORE the attribute is reassigned.

    `attr` is OPTIONAL. Omitted, the check applies to every attribute the code
    reassigns — so a model that picks its own field and variable names still passes.
    Which names the model chooses is not the rule; the ordering is. Pinning `attr` to
    one name is what made the token version of this case unpassable: it demanded the
    reference implementation's own local (`old_used`), a string the prompt never states.
    """
    want = spec.get("attr")
    writes: dict[str, list[int]] = {}
    reads: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Attribute) and (want is None or t.attr == want):
                writes.setdefault(t.attr, []).append(node.lineno)
        # A snapshot is `some_local = <expr>.attr` — target must be a plain name.
        if any(isinstance(t, ast.Name) for t in node.targets):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Attribute) and (want is None or sub.attr == want):
                    reads.setdefault(sub.attr, []).append(node.lineno)
    if not writes:
        return False, (f"`.{want}` is never assigned" if want
                       else "no attribute is ever reassigned")
    bad = []
    for attr, wl in writes.items():
        rl = reads.get(attr, [])
        if not rl:
            bad.append(f"`.{attr}` mutated@{min(wl)}, never snapshotted first")
        elif min(rl) >= min(wl):
            bad.append(f"`.{attr}` snapshot@{min(rl)} not before mutation@{min(wl)}")
    if bad:
        return False, "; ".join(bad)
    return True, "; ".join(f"`.{a}` snapshot@{min(reads[a])} < mutation@{min(writes[a])}"
                           for a in writes)


def _kwarg_str(node: ast.Call, name: str) -> str | None:
    """The literal string value of `name=` on this call, if it is a plain constant."""
    for k in node.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant) \
                and isinstance(k.value.value, str):
            return k.value.value
    return None


def _chk_kwarg_substring(tree, spec) -> tuple[bool, str]:
    """`inner=`'s string must appear inside `outer=`'s string on the same call.

    GUARD 16: inserting with apply_diff means the anchor stays in BOTH old_string and
    new_string; drop it from new_string and the anchor is REPLACED rather than inserted
    after. Substring scoring cannot express this at all — it is a relationship between
    two arguments, not the presence of a token — which is why the guard had no case.
    """
    seen = 0
    for name, node in _calls(tree):
        if name != spec["call"]:
            continue
        inner, outer = _kwarg_str(node, spec["inner"]), _kwarg_str(node, spec["outer"])
        if inner is None or outer is None:
            continue
        seen += 1
        if inner and inner in outer:
            return True, (f"{spec['inner']} is preserved inside {spec['outer']} "
                          f"@{node.lineno}")
    if not seen:
        return False, (f"no {spec['call']}() call carries literal {spec['inner']}= and "
                       f"{spec['outer']}=")
    return False, (f"{spec['inner']} does not appear inside {spec['outer']} — the anchor "
                   f"is replaced, not inserted after")


def _chk_kwarg_multiline(tree, spec) -> tuple[bool, str]:
    """`kwarg=`'s string must span more than one line.

    GUARD 10: an apply_diff old_string has to be unique in the target file, and the guard's
    remedy is "include enough surrounding context". Uniqueness is a property of a file the
    eval does not have, but the remedy is checkable: a single bare line like
    `await db.commit()` is what collides, and a multi-line anchor is what fixes it.
    """
    seen = 0
    for name, node in _calls(tree):
        if name != spec["call"]:
            continue
        val = _kwarg_str(node, spec["kwarg"])
        if val is None:
            continue
        seen += 1
        if "\n" in val:
            return True, (f"{spec['kwarg']} spans {val.count(chr(10)) + 1} lines "
                          f"@{node.lineno}")
    if not seen:
        return False, f"no {spec['call']}() call carries a literal {spec['kwarg']}="
    return False, (f"{spec['kwarg']} is a single line — not enough context to be unique "
                   f"in the target file")


def _chk_arg_not_suffix(tree, spec) -> tuple[bool, str]:
    """The call's path argument must not end with `suffix`.

    GUARD 14: apply_diff takes the ORIGINAL path, never the `.hive_proposed` one — the
    server reads the staged file itself. A forbidden-substring rule would reject the
    correct answer, because the correct answer legitimately names `.hive_proposed` on its
    get_file_content line. Scoping the check to this one call's own argument is exactly
    what substring matching cannot do.
    """
    seen = 0
    for name, node in _calls(tree):
        if name != spec["call"]:
            continue
        vals = [a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        kw = _kwarg_str(node, spec.get("kwarg", "")) if spec.get("kwarg") else None
        if kw is not None:
            vals.append(kw)
        seen += 1
        if not vals:
            # Path passed as a variable (`apply_diff(path, ...)`). That cannot violate
            # the rule, so it is a pass, not an unknown. Treating it as a failure marked
            # a correct composite answer wrong on 2 of 3 checks it had actually satisfied.
            continue
        bad = [v for v in vals if v.endswith(spec["suffix"])]
        if bad:
            return False, (f"{spec['call']}(...)@{node.lineno} targets {bad[0]!r}, which "
                           f"ends with {spec['suffix']!r}")
    if not seen:
        return False, f"no call to {spec['call']}()"
    return True, f"no {spec['call']}() call targets a {spec['suffix']!r} path"


_STRUCT = {
    "call_order": _chk_call_order,
    "kwarg_present": _chk_kwarg_present,
    "positional_before_keyword": _chk_positional_before_keyword,
    "absent_call": _chk_absent_call,
    "assign_before_mutate": _chk_assign_before_mutate,
    "kwarg_substring": _chk_kwarg_substring,
    "kwarg_multiline": _chk_kwarg_multiline,
    "arg_not_suffix": _chk_arg_not_suffix,
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
        "scores": scores, "details": details,
        "finish_reason": resp["choices"][0].get("finish_reason"),
        # 2000, not 400: at 400 a saved answer no longer parses as Python, so re-scoring a
        # stored result off the report reported a syntax error that the live run never saw.
        "output": text[:2000],
    }


def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8003/v1")
    ap.add_argument("--model", default="local-shared")
    ap.add_argument("--out", default="training/eval/baseline.json")
    ap.add_argument("--label", default="untuned baseline")
    ap.add_argument("--only", default="",
                    help="run only cases whose id starts with this prefix (e.g. 'D-' for "
                         "Axis D alone). Aggregates then cover just that subset.")
    a = ap.parse_args()

    cases = load_cases()
    if a.only:
        cases = [c for c in cases if c["id"].startswith(a.only)]
        if not cases:
            raise SystemExit(f"--only {a.only!r} matched no cases")
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

    # ── discriminating power ─────────────────────────────────────────────────
    # A headline average hides which cases are actually doing the measuring. On the
    # 2026-08-31 widening the generated cases all scored 100% while the hand-authored
    # ones — each written from a real observed failure — sat at 57%. The aggregate rose
    # and the information content fell. Printing the split makes that impossible to miss:
    # if `generated` is saturated, adding more of it is not a measurement improvement.
    origins: dict[str, list[float]] = {}
    for r, c in zip(results, cases):
        vals = list(r.get("scores", {}).values())
        if vals:
            origins.setdefault(c.get("origin", "hand"), []).extend(vals)
    if len(origins) > 1:
        print("by case origin (all axes pooled):")
        for name in sorted(origins):
            v = origins[name]
            print(f"  {name:22s} {statistics.mean(v)*100:5.1f}%   (n={len(v)})")
        gen = origins.get("generated", [])
        if gen and statistics.mean(gen) >= 0.99:
            print("  ^ generated cases are saturated — they add n, not discrimination.")
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
