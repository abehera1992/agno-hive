"""Run the 30-task battery against a live hive, and score both axes objectively.

    python -m training.battery.run                       # all 30, sequential
    python -m training.battery.run --family enumeration  # one family
    python -m training.battery.run --tasks E1,S4,F1       # named tasks
    python -m training.battery.run --score-only out.json  # re-score, no hive calls

Both axes are computed here; neither is a judgement call any more.

ACCURACY comes from the task's own checker (training/battery/checks.py), which reads
ground truth off the repo.

CONTAINMENT is the 2x2 of "was the answer right" against "did a guard say something",
using swarm.team's OWN _GUARD_BANNERS tuple rather than a regex reinvented here -- the
list the guards are actually written against:

                   answer right                    answer wrong
    no banner      contained (quiet)               SILENT FAILURE
    banner         recovered / FALSE ALARM         contained (disclosed)

The top-right cell is what most of this work has been about. The bottom-left matters
just as much and had no name before: a guard firing on correct work trains the reader
to discount every banner, and it already happened -- subset19's T13a shipped an
"unverified claims" banner over five citations that were exactly right. Scoring it
explicitly stops that counting as a containment success.

That bottom-left cell splits in two, which the first version of this file missed. A
guard that ATTACHES recovered content is not a false alarm: the banner is the reason
the answer is correct. battery1's E1 said "there are 24 Python files" and named none,
the guard appended the run's own directory listing, and the delivered artifact scored
24/24. Filing that under FALSE ALARM would penalise the one mechanism here that
reliably turns a bad answer into a good one. `recovered` counts as contained;
FALSE ALARM does not.

Results are written incrementally, so a run killed halfway still yields everything
finished before it died.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from .tasks import TASKS

RUNS_DIR = Path(__file__).resolve().parents[1] / "data" / "battery"
BASE = {
    "project_id": "ekam",
    "mcp_url": "http://100.87.159.86:9000/mcp",
    "mcp_urls": ["http://100.87.159.86:9003/mcp"],
    "read_only": True,
    "team": "engineering",
}
DEFAULT_URL = "http://100.96.86.82:9001/run"


def _banners(text: str) -> list[str]:
    """Which guard banners this answer carries, per swarm.team's own list."""
    # Deliberately NOT wrapped in a try/except that falls back to []. Importing the
    # swarm pulls in agno and sqlalchemy, and on a machine missing them an except-and-
    # return-[] would score every answer as carrying no banner -- which reads as
    # perfect containment and a pile of silent failures. A missing import must stop
    # the run, not quietly invert the result.
    from swarm.team import _GUARD_BANNERS
    return [b for b in _GUARD_BANNERS if b in (text or "")]


def _repaired(text: str) -> bool:
    """Did a guard ATTACH recovered content, rather than merely warn?

    Reuses the marker set training/sources/guard_repairs.py already maintains for the
    same distinction -- it harvests preference pairs from exactly these guards, so the
    two must agree on which ones repair. Duplicating the list here is how they would
    drift.
    """
    from ..sources.guard_repairs import _REPAIRING_MARKERS
    return any(m in (text or "") for m in _REPAIRING_MARKERS)


def _next_out() -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ns = [int(m.group(1)) for f in RUNS_DIR.glob("battery*.json")
          if (m := re.match(r"battery(\d+)\.json", f.name))]
    return RUNS_DIR / ("battery%d.json" % ((max(ns) if ns else 0) + 1))


def select(args) -> list:
    tasks = TASKS
    if args.family:
        tasks = [t for t in tasks if t.family == args.family]
    if args.tasks:
        want = {x.strip() for x in args.tasks.split(",")}
        tasks = [t for t in tasks if t.id in want]
    return tasks


def execute(tasks, url: str, out: Path, timeout: int, solo: bool = False) -> list[dict]:
    rows: list[dict] = []
    for t in tasks:
        if not t.available():
            print("%-5s %-16s SKIP (ground truth unavailable)" % (t.id, t.family), flush=True)
            rows.append({"id": t.id, "family": t.family, "skipped": True})
            continue
        t0 = time.time()
        try:
            with httpx.Client(timeout=timeout) as cl:
                r = cl.post(url, json=dict(BASE, task=t.prompt, solo=solo))
                r.raise_for_status()
            row = {"id": t.id, "family": t.family, "prompt": t.prompt,
                   "text": r.json().get("result") or "", "secs": round(time.time() - t0)}
        except Exception as exc:
            row = {"id": t.id, "family": t.family, "prompt": t.prompt, "text": "",
                   "secs": round(time.time() - t0), "error": repr(exc)[:300]}
        rows.append(row)
        io.open(out, "w", encoding="utf-8").write(json.dumps(rows, indent=2))
        v = t.check(row.get("text", ""))
        print("%-5s %-16s %5ss %7d chars  %-4s  %s" % (
            t.id, t.family, row["secs"], len(row.get("text", "")),
            "PASS" if v.passed else "FAIL", v.detail[:60]), flush=True)
    return rows


def score(rows: list[dict]) -> None:
    by_id = {t.id: t for t in TASKS}
    acc = Counter()
    cont = Counter()
    per_family = {}
    detail_rows = []

    for row in rows:
        if row.get("skipped"):
            continue
        t = by_id.get(row["id"])
        if t is None:
            continue
        text = row.get("text") or ""
        v = t.check(text)
        b = _banners(text)
        if row.get("error"):
            cell = "errored"
        elif v.passed and not b:
            cell = "quiet-correct"
        elif v.passed and b and _repaired(text):
            # A guard that REPAIRS is not a false alarm -- the banner is the reason the
            # answer is correct. battery1's E1 was scored FALSE ALARM for exactly this:
            # the model said "there are 24 Python files" and named none, the guard
            # attached the run's own directory listing, and the delivered artifact
            # passed with 24/24. Calling that a false alarm would penalise the one
            # mechanism on this pipeline that reliably turns a bad answer into a good
            # one, and would push toward removing it.
            cell = "recovered"
        elif v.passed and b:
            cell = "FALSE ALARM"
        elif not v.passed and b:
            cell = "disclosed"
        else:
            cell = "SILENT FAILURE"

        acc[(v.kind, v.passed)] += 1
        cont[cell] += 1
        f = per_family.setdefault(t.family, Counter())
        f["n"] += 1
        f["pass"] += int(v.passed)
        f[cell] += 1
        detail_rows.append((t.id, t.family, v.passed, cell, v.detail, b))

    print("\n" + "=" * 76)
    print("BATTERY RESULT")
    print("=" * 76)
    print("\n%-5s %-16s %-6s %-15s %s" % ("id", "family", "acc", "containment", "detail"))
    for tid, fam, passed, cell, det, b in detail_rows:
        print("%-5s %-16s %-6s %-15s %s" % (tid, fam, "PASS" if passed else "FAIL", cell, det[:52]))

    comp_ok, comp_n = acc[("computed", True)], acc[("computed", True)] + acc[("computed", False)]
    heur_ok, heur_n = acc[("heuristic", True)], acc[("heuristic", True)] + acc[("heuristic", False)]
    print("\nACCURACY")
    print("  computed  %d/%d" % (comp_ok, comp_n))
    print("  heuristic %d/%d   (stance-matching; reported apart, never added in)"
          % (heur_ok, heur_n))
    print("\nCONTAINMENT")
    for cell in ("quiet-correct", "recovered", "disclosed", "SILENT FAILURE",
                 "FALSE ALARM", "errored"):
        if cont[cell]:
            print("  %-15s %d" % (cell, cont[cell]))
    good = cont["quiet-correct"] + cont["disclosed"] + cont["recovered"]
    tot = sum(cont[c] for c in ("quiet-correct", "recovered", "disclosed",
                                "SILENT FAILURE", "FALSE ALARM"))
    if tot:
        print("  -> contained %d/%d" % (good, tot))

    print("\nBY FAMILY")
    print("  %-16s %4s %5s  %s" % ("family", "n", "pass", "containment breaches"))
    for fam in sorted(per_family):
        c = per_family[fam]
        breaches = []
        if c["SILENT FAILURE"]:
            breaches.append("%d silent" % c["SILENT FAILURE"])
        if c["FALSE ALARM"]:
            breaches.append("%d false-alarm" % c["FALSE ALARM"])
        print("  %-16s %4d %5d  %s" % (fam, c["n"], c["pass"], ", ".join(breaches) or "-"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--family")
    ap.add_argument("--tasks")
    ap.add_argument("--out")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--score-only", help="score an existing results json, no hive calls")
    # A/B: same tasks, same scoring, one variable -- who authors the answer.
    ap.add_argument("--solo", action="store_true",
                    help="ask hive to deliver the member's answer, not the coordinator's")
    args = ap.parse_args()

    if args.score_only:
        rows = json.loads(io.open(args.score_only, encoding="utf-8").read())
        score(rows)
        return 0

    tasks = select(args)
    if not tasks:
        print("no tasks selected")
        return 1
    out = Path(args.out) if args.out else _next_out()
    print("running %d tasks -> %s\n" % (len(tasks), out))
    print("mode: %s" % ("SOLO (member authors)" if args.solo else "TEAM (coordinator authors)"))
    print()
    rows = execute(tasks, args.url, out, args.timeout, solo=args.solo)
    score(rows)
    print("\nwrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
