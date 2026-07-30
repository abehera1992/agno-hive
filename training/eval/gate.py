"""Phase 3 -> 4 promotion gate. Exits non-zero if the candidate must not ship.

    python -m training.eval.gate --config training/config/qwen3-30b.yaml \
        --baseline training/eval/baseline.json \
        --candidate training/eval/candidate.json

Deliberately a script with an exit code, not a paragraph of judgement: Stage D removed
production A/B, so this offline comparison is the ONLY signal before a checkpoint swap.
It must be mechanical and hard to rationalise around at 2am.

Two independent conditions, both required:
  1. absolute thresholds from the config `gate:` block
  2. NO REGRESSION vs the recorded baseline on any axis (tolerance below)

`min_cases_per_axis` guards against the current failure mode of the eval set itself:
with n=2, Axis C can only score 0 / 50 / 100, so a ">=80%" threshold silently means
"100%". Refusing to score a thin axis is safer than pretending the number is real.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REGRESSION_TOLERANCE = 0.02   # 2 points of noise allowed before calling it a regression

AXES = {
    "tool_call": "tool_call_min",
    "grounding": "grounding_min",
    "citation": "citation_min",
    "guard": "guard_min",
}


def n_cases(report: dict, axis: str) -> int:
    return sum(1 for r in report.get("results", []) if axis in r.get("scores", {}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", required=True)
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    gate = cfg["gate"]
    base = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
    cand = json.loads(Path(a.candidate).read_text(encoding="utf-8"))

    min_n = gate.get("min_cases_per_axis", 1)
    failures: list[str] = []

    print("=" * 74)
    print("PROMOTION GATE  (Phase 3 -> 4)")
    print(f"  baseline : {base.get('label')}")
    print(f"  candidate: {cand.get('label')}")
    print("=" * 74)
    print(f"{'axis':12s} {'base':>7s} {'cand':>7s} {'delta':>8s} {'min':>7s} {'n':>4s}  verdict")

    for axis, key in AXES.items():
        b = base.get("aggregate", {}).get(axis)
        c = cand.get("aggregate", {}).get(axis)
        n = n_cases(cand, axis)
        floor = gate[key]

        if c is None:
            failures.append(f"{axis}: candidate did not score this axis")
            print(f"{axis:12s} {'-':>7s} {'-':>7s} {'-':>8s} {floor:7.2f} {n:4d}  MISSING")
            continue

        verdicts = []
        if n < min_n:
            verdicts.append(f"TOO FEW CASES (<{min_n})")
            failures.append(f"{axis}: only {n} case(s), need >={min_n} for a trustworthy score")
        if c < floor:
            verdicts.append("BELOW FLOOR")
            failures.append(f"{axis}: {c:.3f} < required {floor:.2f}")
        if b is not None and c < b - REGRESSION_TOLERANCE:
            verdicts.append("REGRESSION")
            failures.append(f"{axis}: regressed {b:.3f} -> {c:.3f}")

        delta = f"{(c - b):+.3f}" if b is not None else "n/a"
        print(f"{axis:12s} {b if b is not None else float('nan'):7.3f} {c:7.3f} {delta:>8s} "
              f"{floor:7.2f} {n:4d}  {'PASS' if not verdicts else ' + '.join(verdicts)}")

    print("=" * 74)
    if failures:
        print("RESULT: DO NOT PROMOTE\n")
        for f in failures:
            print(f"  - {f}")
        print("\nDo NOT run FP8 quantisation. Fix the corpus or the eval set and retrain.")
        sys.exit(1)

    print("RESULT: PROMOTE — all axes pass their floor with no regression.")
    print("Next: quantise the merged BF16 to FP8, archive the current checkpoint, swap the")
    print("compose `serve` line, recreate vllm-coord (~4 min).")
    sys.exit(0)


if __name__ == "__main__":
    main()
