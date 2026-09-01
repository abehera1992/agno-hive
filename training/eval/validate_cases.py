"""Validate the eval cases themselves, before they are ever used to judge a model.

An eval case can be wrong in ways that look exactly like a model failure: a rule the
prompt never states, a required token that is a local variable name from some reference
snippet, a required string that contains a forbidden one. Every such case scores a
competent model at zero and is indistinguishable, in the aggregate, from a real
regression. Axis D read 21.4% for months partly for these reasons.

This checks the instrument instead of the model. Two kinds of check:

  SELF-TEST (dynamic) -- a case carrying `selftest_pass`/`selftest_fail` is scored
  through the real scorer. The known-correct answer must score 1.0 and the known-wrong
  answer 0.0. A case that cannot be passed, or cannot be failed, is a broken case.

  LINT (static) -- conditions that make a case unpassable regardless of the answer:
  an empty rule, a required token that never appears in the prompt (the model was never
  told), a required/forbidden substring collision, an unknown structural check type.

Exit code is non-zero if anything fails, so this can gate a suite change.

Run:  python validate_cases.py [--cases cases]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from harness import SCORERS, _token_present

# Prose that frames every guard prompt; not part of the stated rule.
_FRAME = ("Project rule:", "Write code that follows this rule. Output only code.")


def _rule_text(case: dict) -> str:
    p = case.get("prompt", "")
    for f in _FRAME:
        p = p.replace(f, " ")
    return p.strip()


def lint(case: dict) -> list[str]:
    """Static conditions that make a case unpassable by construction."""
    bad: list[str] = []
    cid = case.get("id", "?")
    # Only guard cases carry a stated rule; A/B/C prompts are short questions by design.
    if case.get("kind") == "guard":
        rule = _rule_text(case)
        if len(rule) < 60:
            bad.append(f"{cid}: rule text is {len(rule)} chars - the prompt states no rule")

    req = case.get("guard_required", [])
    forb = case.get("guard_forbidden", [])

    for r in req:
        if r.lower() not in case.get("prompt", "").lower():
            bad.append(f"{cid}: required token {r!r} never appears in the prompt - "
                       f"the model is not told to write it")
    for r in req:
        for f in forb:
            if f.lower() in r.lower():
                bad.append(f"{cid}: required {r!r} contains forbidden {f!r} - "
                           f"satisfying the requirement trips the prohibition")
            elif r.lower() in f.lower():
                bad.append(f"{cid}: forbidden {f!r} contains required {r!r} - "
                           f"any answer is at risk of a spurious violation")

    if "guard" in case.get("scorers", []) and not req:
        bad.append(f"{cid}: token-scored but has no required tokens - scores 1.0 for any "
                   f"answer that merely avoids the forbidden ones")

    from harness import _STRUCT
    for spec in case.get("structural", []):
        if spec.get("type") not in _STRUCT:
            bad.append(f"{cid}: unknown structural check {spec.get('type')!r}")
    return bad


def selftest(case: dict) -> list[str]:
    """Score the guard's own CORRECT and WRONG blocks through the real scorer."""
    bad: list[str] = []
    cid = case.get("id", "?")
    if "selftest_pass" not in case and "selftest_fail" not in case:
        return [f"{cid}: no self-test - correctness of this case is unverified"]

    for key, want, label in (("selftest_pass", 1.0, "known-CORRECT"),
                             ("selftest_fail", 0.0, "known-WRONG")):
        body = case.get(key)
        if not body:
            continue
        for scorer_name in case.get("scorers", []):
            _axis, fn = SCORERS[scorer_name]
            got, detail = fn(case, body)
            if abs(got - want) > 1e-9:
                bad.append(f"{cid}[{scorer_name}]: {label} answer scored {got:.2f}, "
                           f"expected {want:.2f} - {detail}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="cases")
    a = ap.parse_args()

    files = sorted(Path(a.cases).glob("*.json"))
    if not files:
        print(f"no case files under {a.cases}/", file=sys.stderr)
        return 2

    problems: list[str] = []
    checked = untested = 0
    for f in files:
        case = json.loads(io.open(f, encoding="utf-8").read())
        problems += lint(case)
        if "selftest_pass" in case or "selftest_fail" in case:
            problems += selftest(case)
            checked += 1
        else:
            untested += 1

    print(f"{len(files)} cases: {checked} self-tested, {untested} without a self-test")
    if problems:
        print(f"\n{len(problems)} problem(s):\n")
        for p in problems:
            print("  x", p)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
