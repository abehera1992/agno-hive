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
    "repetition": "repetition_min",
}

# Axes a config's `gate:` block doesn't set a floor for are scored and printed by the
# harness but not enforced here. This is how `repetition` (Axis E, added 2026-08-16)
# rolls out: an unvalidated new scorer with no track record yet shouldn't be able to
# block a promotion on n=1 confidence, so existing configs (qwen3-30b.yaml,
# qwen36-35b.yaml) are read as-is, un-edited, and simply never enforce it. A config
# opts in by adding `repetition_min:` to its own `gate:` block once the scorer has
# enough runs behind it to trust a floor.
OPTIONAL_AXES = {"repetition"}


def resolve_floor(spec: float | str, baseline: float | None, axis: str) -> float:
    """Resolve a gate floor, which may be absolute or measured against the baseline.

    An absolute number states "this axis must reach X regardless of what the untuned
    model does" — right for an axis we are TRAINING to a standard (citation), and right
    for one that must stay perfect (tool_call).

    The string "baseline" means "no absolute bar; only forbid going backwards", and
    resolves to `baseline - REGRESSION_TOLERANCE`. Added 2026-07-30 for Axis D after the
    v2 gate: guard adherence is project KNOWLEDGE (38 rules in patterns/*.md), not a
    behaviour, and is delivered to the agents through context, which stays current when a
    rule changes. Fine-tuning a checkpoint to memorise it would need a retrain per rule
    and can silently contradict the repo. So D is no longer a training target — but a
    candidate that gets WORSE at it is still a candidate we reject.

    Written as a rule rather than the number it currently evaluates to (0.480), because a
    hardcoded floor is exactly what went stale here before: guard_min sat at 0.98 from
    when the suite had n=2, long after the real baseline had moved to 0.500.
    """
    if isinstance(spec, str):
        if spec != "baseline":
            raise SystemExit(f"{axis}: floor must be a number or \"baseline\", got {spec!r}")
        if baseline is None:
            raise SystemExit(f"{axis}: floor is \"baseline\" but the baseline report does "
                             f"not score this axis — cannot resolve.")
        return baseline - REGRESSION_TOLERANCE
    return float(spec)


def n_cases(report: dict, axis: str) -> int:
    """Count cases that scored this AXIS. `structural` is a second Axis D scorer."""
    keys = {"guard", "structural"} if axis == "guard" else {axis}
    return sum(1 for r in report.get("results", [])
               if keys & set(r.get("scores", {})))


def dry_run(cfg: dict) -> int:
    """Check ONLY that every axis carries enough cases to be scored meaningfully.

    Separate from the real gate on purpose. The promotion gate compares a TRAINED
    candidate and is expected to FAIL against an untuned baseline — if it passed, the
    untuned model would already meet the promotion bar and Phase 3 would be pointless.
    This checks suite readiness, which is the thing that can be verified before training.
    """
    import glob
    from collections import Counter

    cases_dir = Path(__file__).parent / "cases"
    counts: Counter[str] = Counter()
    for f in glob.glob(str(cases_dir / "*.json")):
        case = json.loads(Path(f).read_text(encoding="utf-8"))
        for s in case.get("scorers", []):
            # `guard` (token) and `structural` (AST) are both Axis D.
            counts["guard" if s == "structural" else s] += 1

    need = cfg["gate"].get("min_cases_per_axis", 1)
    print("=" * 62)
    print(f"GATE DRY-RUN — suite readiness (min_cases_per_axis = {need})")
    print("=" * 62)
    ok = True
    for axis, letter in (("tool_call", "A"), ("grounding", "B"),
                         ("citation", "C"), ("guard", "D")):
        n = counts[axis]
        good = n >= need
        ok &= good
        print(f"  {letter}. {axis:11s} n={n:3d}   {'PASS' if good else f'FAIL (need {need})'}")
    # Informational only: an optional axis never blocks suite readiness (see
    # OPTIONAL_AXES), so its dry-run line doesn't feed into `ok`.
    if "repetition" in counts:
        n = counts["repetition"]
        print(f"  E. repetition n={n:3d}   (informational — not enforced by this config)")
    print("=" * 62)
    print("RESULT: suite is ready to score a candidate."
          if ok else "RESULT: suite NOT ready — expand the short axes.")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--candidate", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="check per-axis case counts only; no model comparison")
    a = ap.parse_args()

    if a.dry_run:
        sys.exit(dry_run(yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))))
    if not (a.baseline and a.candidate):
        raise SystemExit("--baseline and --candidate are required unless --dry-run")

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

        if axis in OPTIONAL_AXES and key not in gate:
            # This config hasn't opted this axis into enforcement (see OPTIONAL_AXES).
            # Still surface the number if the harness scored it — silently dropping a
            # real score from the printed table would look like the scorer never ran.
            if c is not None:
                delta = f"{(c - b):+.3f}" if b is not None else "n/a"
                print(f"{axis:12s} {b if b is not None else float('nan'):7.3f} {c:7.3f} "
                      f"{delta:>8s} {'n/a':>7s} {n:4d}  INFO (not enforced)")
            continue

        floor = resolve_floor(gate[key], b, axis)
        floor_note = "  (= baseline - tol)" if isinstance(gate[key], str) else ""

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
              f"{floor:7.2f} {n:4d}  {'PASS' if not verdicts else ' + '.join(verdicts)}"
              f"{floor_note}")

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
