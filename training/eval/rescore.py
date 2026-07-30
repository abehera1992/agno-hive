"""Re-apply scorers to an ALREADY-RECORDED eval report, without re-querying the model.

    python -m training.eval.rescore --report training/eval/candidate_v2.json \
        --out training/eval/candidate_v2_rescored.json --dims grounding

Why this exists: the v2 gate (2026-07-30) was decided partly by a SCORER defect, not by
model behaviour — the eight `C-refuse-*` cases demanded the literal token "read" as a
proxy for "declined and pointed at the source", so correct refusals phrased with "check"
scored 0.00 on Axis B. Fixing the scorer does not require spending another hour of GPU
re-serving a 61 GB checkpoint: the model's words are already on disk.

THE TRUNCATION HAZARD — read before adding a dimension to --dims:

`harness.run_case` stores `output: text[:400]`. Any response longer than that is CUT.
Re-scoring a truncated response silently invents a result: a `guard` case whose emitted
code ran past 400 chars would be judged on a fragment, and a `forbidden` token sitting at
char 500 would read as absent — turning a VIOLATION into a pass. That is strictly worse
than the defect being fixed, because it fails in the direction of promoting.

So this tool refuses rather than guesses:
  * every case is checked against the truncation limit BEFORE scoring;
  * a truncated case keeps its ORIGINAL recorded score and is reported as skipped;
  * `--dims` is explicit, so re-scoring is always a deliberate, narrow act.

Full re-runs of the model remain the only way to change a truncated case's score.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from training.eval.harness import SCORERS, load_cases

# Must match the slice in harness.run_case (`text[:400]`). A response at or above this
# length may have lost content, so it cannot be re-judged from the stored copy.
TRUNCATION_LIMIT = 400


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="recorded harness report to re-score")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dims", default="grounding",
                    help="comma-separated scorers to re-apply (default: grounding)")
    ap.add_argument("--label-suffix", default=" [rescored]")
    a = ap.parse_args()

    dims = [d.strip() for d in a.dims.split(",") if d.strip()]
    unknown = [d for d in dims if d not in SCORERS]
    if unknown:
        raise SystemExit(f"unknown scorer(s): {unknown}")
    if "tool_call" in dims:
        # score_tool_call reads the raw response object (tool_calls live outside
        # `content`), and the report only keeps the text. It can never be re-scored here.
        raise SystemExit("tool_call cannot be re-scored offline: the report stores text, "
                         "not the response object that carries tool_calls.")

    report = json.loads(Path(a.report).read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in load_cases()}

    changed, skipped, missing = [], [], []
    for r in report.get("results", []):
        case = cases.get(r["id"])
        if case is None:
            missing.append(r["id"])
            continue
        text = r.get("output") or ""
        for dim in dims:
            if dim not in r.get("scores", {}):
                continue
            if len(text) >= TRUNCATION_LIMIT:
                skipped.append(f"{r['id']}/{dim}")
                continue
            new, detail = SCORERS[dim][1](case, text)
            new = round(new, 3)
            if new != r["scores"][dim]:
                changed.append((r["id"], dim, r["scores"][dim], new))
                r["scores"][dim] = new
                r.setdefault("details", {})[dim] = detail

    # Re-aggregate EXACTLY as harness.main does, including merging `structural` into
    # `guard` — a divergence here would make the gate compare incomparable numbers.
    by_axis: dict[str, list[float]] = {}
    for r in report.get("results", []):
        for dim, val in r.get("scores", {}).items():
            by_axis.setdefault(dim, []).append(val)
    agg = {d: round(statistics.mean(v), 3) for d, v in by_axis.items() if v}
    d_vals = by_axis.get("guard", []) + by_axis.get("structural", [])
    if d_vals:
        agg["guard"] = round(statistics.mean(d_vals), 3)

    old = report.get("aggregate", {})
    report["aggregate"] = agg
    report["label"] = report.get("label", "") + a.label_suffix
    report["rescored"] = {
        "source_report": str(a.report),
        "dims": dims,
        "changed": len(changed),
        "skipped_truncated": skipped,
    }

    print(f"re-scored {dims} on {a.report}")
    for cid, dim, o, n in changed:
        print(f"  {cid:38s} {dim:11s} {o:.2f} -> {n:.2f}")
    if not changed:
        print("  (no score changed)")
    if skipped:
        print(f"\n  SKIPPED {len(skipped)} case/dim pair(s) — output truncated at "
              f"{TRUNCATION_LIMIT} chars, original score kept:")
        for s in skipped:
            print(f"    {s}")
    if missing:
        print(f"\n  !! {len(missing)} recorded case(s) no longer exist in cases/: {missing}")

    print("\naggregate:")
    for k in ("tool_call", "grounding", "citation", "guard"):
        if k in agg:
            o = old.get(k)
            d = f"  ({o:.3f} -> {agg[k]:.3f})" if o is not None and o != agg[k] else ""
            print(f"  {k:11s} {agg[k]:.3f}{d}")

    Path(a.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
