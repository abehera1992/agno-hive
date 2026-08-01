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


def run_one(url: str, project: str, task: str, team: str, timeout: int,
            mcp_url: str = "", mcp_urls: tuple[str, ...] = (),
            read_only: bool = True) -> tuple[str, float]:
    """POST one task to the swarm.

    mcp_url / mcp_urls MUST mirror what the real caller sends. agno_run (the MCP tool
    that is how hive is actually used) passes the project MCP as `mcp_url` and hive-mcp
    as `mcp_urls`; omitting them silently runs a DIFFERENT pipeline — hive-mcp never
    connects, so verify_claims, the write tools and 46 others are simply absent.
    Measured 2026-07-31: the whole 40-case suite had been scored against that reduced
    configuration, which is why the post-answer verification guard never fired once
    during a harness run despite working when invoked through agno_run.
    """
    payload: dict = {"task": task, "project_id": project, "team": team}
    if read_only:
        # Enforced server-side by stripping mutating tools, not by asking the model.
        # Without it the eval MUTATES the project: a "write a component" case staged
        # .hive_proposed files into invented directories, and kept doing so even when
        # the prompt explicitly forbade write_file (observed 2026-07-31).
        payload["read_only"] = True
    if mcp_url:
        payload["mcp_url"] = mcp_url
    if mcp_urls:
        payload["mcp_urls"] = list(mcp_urls)
    body = json.dumps(payload).encode()
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
    # Defaults mirror EkamApp mcp-server/tools/agno.py so the harness exercises the SAME
    # pipeline a real agno_run call does. Do not drop these: without mcp_urls, hive-mcp
    # never connects and the run is missing verify_claims and every write tool.
    ap.add_argument("--mcp-url", default="http://100.87.159.86:9000/mcp")
    ap.add_argument("--mcp-urls", default="http://100.87.159.86:9003/mcp",
                    help="comma-separated; hive-mcp must be here")
    ap.add_argument("--out", default="training/eval/agent_report.json")
    ap.add_argument("--only", default="", help="substring filter on case id")
    ap.add_argument("--allow-writes", action="store_true",
                    help="permit the run to mutate the project (default: read-only). "
                         "The eval must not write; this exists only for deliberate "
                         "write-path testing.")
    ap.add_argument("--repeats", type=int, default=3,
                    help="run each case N times; single runs cannot resolve this suite's variance")
    ap.add_argument("--aggregate", choices=("best", "majority", "strict"), default="best",
                    help="which aggregation the pass/fail exit code is judged on")
    a = ap.parse_args()

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if a.only:
        cases = [c for c in cases if a.only in c["id"]]

    def score_once(c: dict) -> tuple[bool | None, dict, dict, float, str]:
        """Returns ok=True/False, or ok=None when the RUN ITSELF failed.

        None is not a third flavour of wrong answer — it means no answer was obtained.
        Previously a timeout or transport error was stored as the response text and
        scored like any other reply, so infrastructure trouble silently depressed the
        accuracy figure and was indistinguishable from the model being wrong. That
        happened three times on 2026-07-31 (lost tracing, missing mcp_urls, a 300s
        timeout) and each time the artifact was reported as a model failure. Errored
        runs are now counted and reported separately, never scored.
        """
        text, elapsed = run_one(a.url, a.project, c["prompt"], a.team, a.timeout,
                                a.mcp_url, tuple(u.strip() for u in a.mcp_urls.split(',') if u.strip()),
                                not a.allow_writes)
        if text.startswith("__ERROR__"):
            return None, {}, {"error": text[:200]}, elapsed, text
        g, gd = score_grounding(c, text)
        scores, details = {"grounding": round(g, 3)}, {"grounding": gd}
        if "correct_lines" in c:
            cs, cd = score_citation(c, text)
            scores["citation"], details["citation"] = round(cs, 3), cd
        # A case is CORRECT only if every scored dimension is perfect. Partial credit is
        # right for tracking training progress but wrong for "what fraction of answers
        # can a human trust", which is what the target is about.
        return all(v >= 1.0 for v in scores.values()), scores, details, elapsed, text

    results = []
    print(f"running {len(cases)} case(s) x {a.repeats} repeat(s) against {a.url}\n")
    for c in cases:
        runs = [score_once(c) for _ in range(a.repeats)]
        scored = [r for r in runs if r[0] is not None]
        errored = len(runs) - len(scored)
        k = sum(1 for ok, *_ in scored if ok)
        n_scored = len(scored)
        # Aggregations are computed over SCORED runs only. A case whose every run
        # errored has no measurement at all and must not be counted as a pass or a
        # failure — it is reported in its own bucket so it cannot quietly move the
        # headline number in either direction.
        worst = (min(scored, key=lambda r: sum(r[1].values())) if scored
                 else min(runs, key=lambda r: r[3]))
        results.append({
            "id": c["id"], "passes": k, "repeats": a.repeats,
            "scored_runs": n_scored, "errored_runs": errored,
            "best": bool(n_scored) and k >= 1,
            "majority": bool(n_scored) and k * 2 > n_scored,
            "strict": bool(n_scored) and k == n_scored,
            "no_measurement": n_scored == 0,
            "scores": worst[1], "details": worst[2],
            "elapsed_s": round(sum(r[3] for r in runs), 1),
            "output": worst[4][:600],
        })
        if n_scored == 0:
            mark = "ERROR"
        elif k == n_scored:
            mark = "PASS "
        elif k:
            mark = "FLAKY"
        else:
            mark = "FAIL "
        suffix = f"  [{errored} errored]" if errored else ""
        print(f"  {mark} {c['id']:34s} {k}/{n_scored} passes{suffix}")
        if n_scored == 0:
            print(f"        NO MEASUREMENT: {worst[2].get('error','')[:130]}")
        elif k < n_scored:
            print(f"        worst: {worst[2].get('grounding','')[:140]}")

    n = len(results)
    measured = [r for r in results if not r["no_measurement"]]
    nm = len(measured)
    tally = {m: sum(1 for r in measured if r[m]) for m in ("best", "majority", "strict")}
    # Denominator is MEASURED cases, not all cases. Scoring an unmeasured case as a
    # failure understates accuracy; scoring it as a pass overstates it. Both are worse
    # than reporting a smaller, honest denominator alongside the error count.
    pct = {m: (100.0 * v / nm if nm else 0.0) for m, v in tally.items()}
    unmeasured = [r["id"] for r in results if r["no_measurement"]]
    total_errored = sum(r["errored_runs"] for r in results)

    print("\n" + "=" * 72)
    # Three aggregations, because they answer different questions and only reporting one
    # would hide the spread. Measured 2026-07-31: three runs of IDENTICAL code scored
    # 90.0 / 92.5 / 87.5 on this suite, all of the swing coming from 5 adversarial cases.
    # A single-run number cannot resolve a one-case gap against that much variance.
    if unmeasured or total_errored:
        print(f"  !! {total_errored} run(s) errored; {len(unmeasured)} case(s) have NO "
              f"measurement and are excluded from the denominator ({nm} of {n} scored)")
    print(f"  BEST-of-{a.repeats}     {tally['best']:>3}/{nm} = {pct['best']:5.1f}%   "
          f"capability — got it right at least once")
    print(f"  MAJORITY     {tally['majority']:>3}/{nm} = {pct['majority']:5.1f}%   "
          f"typical behaviour — right more often than not")
    print(f"  STRICT (all) {tally['strict']:>3}/{nm} = {pct['strict']:5.1f}%   "
          f"reliability — right EVERY time")
    print("=" * 72)
    flaky = [r["id"] for r in measured if 0 < r["passes"] < r["scored_runs"]]
    always = [r["id"] for r in measured if r["passes"] == 0]
    if unmeasured:
        print(f"  NO MEASUREMENT (errored): {', '.join(unmeasured)}")
    if flaky:
        print(f"  FLAKY (non-deterministic): {', '.join(flaky)}")
    if always:
        print(f"  ALWAYS FAILING:            {', '.join(always)}")
    print(f"\n  Target >= 95% is judged on {a.aggregate.upper()}.")

    Path(a.out).write_text(json.dumps(
        {"repeats": a.repeats, "aggregate": a.aggregate,
         "accuracy_pct": round(pct[a.aggregate], 1),
         "passed": tally[a.aggregate], "total": n,
         "tally": tally, "pct": {k: round(v, 1) for k, v in pct.items()},
         "flaky": flaky, "always_failing": always,
         "unmeasured": unmeasured, "errored_runs": total_errored,
         "scored_cases": nm, "all_cases": n,
         "results": results}, indent=1), encoding="utf-8")
    print(f"wrote {a.out}")
    raise SystemExit(0 if pct[a.aggregate] >= 95.0 else 1)


if __name__ == "__main__":
    main()
