"""Combine N single-pass agent reports into best / majority / strict aggregations.

    python -m training.eval.aggregate_runs training/eval/pass_*.json

Why separate from the harness: a 40-case x 3-repeat run is 120 agent calls (~30 min) and
kept being killed mid-run by the background-task lifetime, twice losing the whole result.
Three independent single passes each finish in ~7 min, and combining them afterwards is
strictly more robust — a lost pass costs one third of the work, not all of it.

Three aggregations, because they answer different questions and reporting one hides the
spread. Measured 2026-07-31: three runs of IDENTICAL code scored 90.0 / 92.5 / 87.5,
with the entire swing coming from a handful of adversarial cases, so a single-run number
cannot resolve a one-case gap.
  BEST     — got it right at least once. Capability ceiling; the most generous reading.
  MAJORITY — right more often than not. Typical behaviour.
  STRICT   — right EVERY time. What "reliable" actually means, since in production you
             get one answer, not three.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]]
    if len(paths) < 2:
        raise SystemExit("usage: aggregate_runs.py <report1.json> <report2.json> [...]")

    passes: dict[str, int] = {}
    order: list[str] = []
    for p in paths:
        rep = json.loads(p.read_text(encoding="utf-8"))
        for r in rep["results"]:
            cid = r["id"]
            if cid not in passes:
                passes[cid] = 0
                order.append(cid)
            # Accept both report shapes. The harness originally wrote a boolean "ok" per
            # case; once it gained --repeats it writes "passes"/"repeats" instead. Reading
            # only "ok" silently scored every case 0 and produced a 0/40 aggregate from
            # passes that were 37/40 and 35/40 — wrong in the safe direction only because
            # it was absurd enough to notice.
            if "passes" in r:
                passes[cid] += int(r["passes"]) if r.get("repeats", 1) == 1 else (1 if r["passes"] else 0)
            else:
                passes[cid] += 1 if r.get("ok") else 0

    n_runs, n = len(paths), len(order)
    best = [c for c in order if passes[c] >= 1]
    major = [c for c in order if passes[c] * 2 > n_runs]
    strict = [c for c in order if passes[c] == n_runs]
    flaky = [c for c in order if 0 < passes[c] < n_runs]
    never = [c for c in order if passes[c] == 0]

    print(f"aggregated {n_runs} pass(es) over {n} case(s)\n")
    for c in order:
        k = passes[c]
        mark = "PASS " if k == n_runs else ("FLAKY" if k else "FAIL ")
        print(f"  {mark} {c:34s} {k}/{n_runs}")
    print("\n" + "=" * 72)
    for label, sel, note in (
        ("BEST    ", best,   "got it right at least once (capability)"),
        ("MAJORITY", major,  "right more often than not (typical)"),
        ("STRICT  ", strict, "right EVERY time (reliability)"),
    ):
        pct = 100.0 * len(sel) / n if n else 0.0
        print(f"  {label}  {len(sel):>3}/{n} = {pct:5.1f}%   {note}")
    print("=" * 72)
    if flaky:
        print(f"  FLAKY ({len(flaky)}): {', '.join(flaky)}")
    if never:
        print(f"  NEVER PASSED ({len(never)}): {', '.join(never)}")

    out = Path("training/eval/agent_report_aggregate.json")
    out.write_text(json.dumps({
        "runs": [str(p) for p in paths], "cases": n,
        "best": len(best), "majority": len(major), "strict": len(strict),
        "pct": {"best": round(100.0 * len(best) / n, 1),
                "majority": round(100.0 * len(major) / n, 1),
                "strict": round(100.0 * len(strict) / n, 1)},
        "flaky": flaky, "never_passed": never,
        "passes": passes,
    }, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
