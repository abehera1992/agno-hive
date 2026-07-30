"""Phase 1 CLI — run the sources, dedupe, validate, export v1 JSONL + quality report.

    python -m training.build_dataset --out training/data/v1.jsonl --project ekam

Every rejected row is counted and reported by reason. A corpus this small (see the
report) can be destroyed by a handful of bad rows, so the report is the deliverable
just as much as the JSONL is.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .schema import Record
from .sources.patterns_md import PatternsMdSource
from .sources.failure_log import FailureLogSource
from .sources.postgres_sessions import PostgresSessionsSource


def build(patterns_path: str | None, project: str | None, postgres_uri: str | None):
    sources = []
    if patterns_path:
        sources.append(PatternsMdSource(patterns_path))
    sources.append(FailureLogSource(postgres_uri=postgres_uri, project_id=project))
    sources.append(PostgresSessionsSource(postgres_uri=postgres_uri, project_id=project))

    kept: list[Record] = []
    seen: set[str] = set()
    rejected = Counter()
    per_source_raw = Counter()
    per_source_kept = Counter()

    for src in sources:
        try:
            records = list(src.load())
        except Exception as exc:
            rejected[f"{src.name}: SOURCE FAILED ({type(exc).__name__}: {exc})"] += 1
            continue

        for r in records:
            per_source_raw[r.source] += 1
            err = r.validate()
            if err:
                rejected[f"{r.source}: {err}"] += 1
                continue
            key = r.dedupe_key()
            if key in seen:
                rejected[f"{r.source}: duplicate"] += 1
                continue
            seen.add(key)
            kept.append(r)
            per_source_kept[r.source] += 1

    return kept, rejected, per_source_raw, per_source_kept


def report(kept, rejected, raw, kept_by_src) -> str:
    kinds = Counter(r.kind for r in kept)
    lines = [
        "=" * 68,
        "TRAINING CORPUS v1 — QUALITY REPORT",
        "=" * 68,
        f"kept: {len(kept)}   (sft={kinds.get('sft', 0)}  pref={kinds.get('pref', 0)})",
        "",
        "per source (raw -> kept):",
    ]
    for s in sorted(raw):
        lines.append(f"  {s:22s} {raw[s]:5d} -> {kept_by_src.get(s, 0):5d}")
    lines += ["", "rejected (by reason):"]
    if rejected:
        for reason, n in rejected.most_common():
            lines.append(f"  {n:5d}  {reason}")
    else:
        lines.append("  (none)")

    prefs = [r for r in kept if r.kind == "pref"]
    if prefs:
        lines += ["", "preference-pair provenance:"]
        for shape, n in Counter(
            r.meta.get("shape", r.source) for r in prefs
        ).most_common():
            lines.append(f"  {n:5d}  {shape}")
    lines.append("=" * 68)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="training/data/v1.jsonl")
    ap.add_argument("--patterns", default=None, help="path to a project's patterns/ dir")
    ap.add_argument("--project", default=None, help="project_id filter for DB sources")
    ap.add_argument("--postgres-uri", default=None)
    args = ap.parse_args()

    kept, rejected, raw, kept_by_src = build(args.patterns, args.project, args.postgres_uri)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in kept:
            fh.write(r.to_json() + "\n")

    txt = report(kept, rejected, raw, kept_by_src)
    print(txt)
    out.with_suffix(".report.txt").write_text(txt, encoding="utf-8")
    print(f"\nwrote {out}  ({len(kept)} records)")


if __name__ == "__main__":
    main()
