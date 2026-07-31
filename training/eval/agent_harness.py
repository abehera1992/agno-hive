"""Agent-level eval — scores the SWARM end to end, not the bare model.

    python -m training.eval.agent_harness --url http://100.96.86.82:9001 --project ekam

training/eval/harness.py sends one prompt straight to the model over HTTP with no tools.
That measures the weights. It is the wrong instrument for the question "is hive right
about this repo", because the swarm has file reads, patterns/ context and a reviewer, and
measurably behaves differently: the same question that the bare model answers from priors
the swarm may answer by reading the file — or may still answer from priors, which is the
thing worth measuring.

Cases are deliberately NOT excerpt-carrying. Each one forces the agent to go and look,
because the failure being measured is "did it check, or did it guess".

Scoring reuses training.eval.harness scorers so agent and model numbers stay comparable.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

from training.eval.harness import score_citation, score_grounding

CASES_PATH = Path(__file__).parent / "agent_cases.json"


def run_one(url: str, project: str, task: str, team: str, timeout: int) -> tuple[str, float]:
    body = json.dumps({"task": task, "project_id": project, "team": team}).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/run", data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except Exception as e:
        return f"__ERROR__ {type(e).__name__}: {e}", time.time() - t0
    text = payload.get("result") or payload.get("output") or json.dumps(payload)
    return text, time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://100.96.86.82:9001")
    ap.add_argument("--project", default="ekam")
    ap.add_argument("--team", default="engineering")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default="training/eval/agent_report.json")
    ap.add_argument("--only", default="", help="substring filter on case id")
    a = ap.parse_args()

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if a.only:
        cases = [c for c in cases if a.only in c["id"]]

    results, failures = [], []
    print(f"running {len(cases)} agent case(s) against {a.url}\n")
    for c in cases:
        text, elapsed = run_one(a.url, a.project, c["prompt"], a.team, a.timeout)
        g, gd = score_grounding(c, text)
        scores = {"grounding": round(g, 3)}
        details = {"grounding": gd}
        if "correct_lines" in c:
            cs, cd = score_citation(c, text)
            scores["citation"] = round(cs, 3)
            details["citation"] = cd
        # A case counts as CORRECT only if every scored dimension is perfect. Partial
        # credit is right for tracking training progress but wrong for "what fraction of
        # answers can a human trust", which is what the >95% target is about.
        ok = all(v >= 1.0 for v in scores.values())
        results.append({"id": c["id"], "ok": ok, "scores": scores,
                        "details": details, "elapsed_s": round(elapsed, 1),
                        "output": text[:600]})
        if not ok:
            failures.append(c["id"])
        flag = "PASS" if ok else "FAIL"
        dims = "  ".join(f"{k}={v:.2f}" for k, v in scores.items())
        print(f"  {flag}  {c['id']:34s} {dims}   ({elapsed:.1f}s)")
        if not ok:
            print(f"        {details.get('grounding','')[:150]}")

    n = len(results)
    passed = sum(1 for r in results if r["ok"])
    pct = 100.0 * passed / n if n else 0.0
    print("\n" + "=" * 68)
    print(f"AGENT ACCURACY: {passed}/{n} = {pct:.1f}%   (target > 95%)")
    for dim in ("grounding", "citation"):
        vals = [r["scores"][dim] for r in results if dim in r["scores"]]
        if vals:
            print(f"  mean {dim:10s} {statistics.mean(vals):.3f}  (n={len(vals)})")
    if failures:
        print(f"  failing: {', '.join(failures)}")
    print("=" * 68)

    Path(a.out).write_text(json.dumps(
        {"accuracy_pct": round(pct, 1), "passed": passed, "total": n,
         "results": results}, indent=1), encoding="utf-8")
    print(f"wrote {a.out}")
    raise SystemExit(0 if pct > 95.0 else 1)


if __name__ == "__main__":
    main()
