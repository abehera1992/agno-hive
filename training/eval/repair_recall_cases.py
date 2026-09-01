"""Repair the Axis B cases that asked the model to RECALL EkamApp constants.

B1/B2/B3/B5 each posed a question about EkamApp with NO excerpt and NO tools, then scored
the answer on whether it contained a specific literal -- `0.4`, `watch`,
`X-Internal-API-Key`. Nothing in the prompt supplies those, so the case measures whether
the model memorised this repo's constants. That is the same defect that held Axis D at
21.4% (D-guard2 requiring the local name `old_used`), and the same objection
`training/config/qwen3-30b.yaml` already raises against putting Axis D in the gate:
"that is knowledge, not behaviour ... it already lives in patterns/*.md, which hive
fetches into context at session start". It applies verbatim here.

The condition is also not one that occurs: in production the agent always holds
`get_file_content`/`search_files`, so "no evidence, no tools, name the header" is not a
situation the swarm is ever in.

Each repaired case becomes a PAIR:

  <id>          evidence supplied -- the excerpt is in the prompt, so the fact is
                extractable and a wrong answer is a real misread.
  <id>-refuse   no evidence and no tools -- declining and pointing at the source IS the
                pass. Scored on citation too, so a fabricated line number is caught even
                when the prose hedges.

Excerpts are cut from the live EkamApp working tree at generation time and the anchor is
verified before a case is emitted, so a case can never carry an excerpt that does not
contain the fact it asks about.

Run:  python repair_recall_cases.py --src ../../../EkamApp/API
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

CASES = Path(__file__).parent / "cases"

# Shared refusal vocabulary. Any one alternative satisfies "declined and pointed at the
# source" -- see _fact_present in harness.py for why this is alternation and not one verb.
_REFUSAL = ("read|check|inspect|examine|look at|open|access|see the file|provide|share|"
            "don't have|do not have|cannot determine|can't determine|not shown|"
            "no access|without the file|unable to")

REPAIRS = [
    {
        "id": "B1-hsn-fallback-retired",
        "file": "inventory-service/gst_resolver.py",
        "anchor": "async def resolve_gst_for_hsn",
        "before": 4, "after": 32,
        "question": (
            "When `resolve_gst_for_hsn` is called for an HSN code that has no row in "
            "`hsn_catalogue`, does it fall back to a code-resident `_HSN_FALLBACK` dict? "
            "What does it return instead? Answer from the excerpt only."),
        # NOT a generic negative alternation: "no" matched on "no row in hsn_catalogue",
        # a phrase lifted straight out of the question, so a fully wrong answer scored
        # 0.50. The discriminating fact is what the function RETURNS.
        "required_facts": ["none"],
        "forbidden_facts": ["falls back to a|falls back to the|yes, it falls"],
        "selftest_pass": ("No. There is no `_HSN_FALLBACK` dict in this code — the "
                          "function queries HSNCatalogue and returns None when no row "
                          "is effective."),
        # The deployed model's real answer on 2026-08-31, kept verbatim as the negative:
        # it paraphrased the stale comment above the function instead of reading the body.
        "selftest_fail": ("Yes, when `resolve_gst_for_hsn` is called for an HSN code that "
                          "has no row in `hsn_catalogue`, it falls back to a code-resident "
                          "`_HSN_FALLBACK` dict."),
        "provenance": (
            "EkamApp gst_resolver.py. Deliberately hard: the comment block directly above "
            "the function still describes a 'Static fallback ... dict' that was removed "
            "when EK-247 seeded those 1121 headings into hsn_catalogue. The code returns "
            "None. A model that reads the comment instead of the body answers yes. "
            "Replaces the pre-2026-08-31 version, which asked the same question with no "
            "excerpt and no tools and scored recall of the answer."),
        "refuse_symbol": "resolve_gst_for_hsn",
    },
    {
        "id": "B2-duplicate-similarity-threshold",
        "file": "inventory-service/router/items_api.py",
        "anchor": "similarity(name, :new_name) > 0.4",
        "before": 12, "after": 6,
        "question": (
            "Above what pg_trgm `similarity()` score is an existing item reported as a "
            "near-duplicate, and at most how many are returned? Answer from the excerpt "
            "only."),
        "required_facts": ["0.4", "5"],
        "forbidden_facts": [],
        "selftest_pass": "Above a similarity of 0.4, and at most 5 rows are returned.",
        "selftest_fail": "Above a similarity of 0.8, and at most 20 rows are returned.",
        "provenance": (
            "EkamApp items_api.py, the raw SQL duplicate-name probe. Exact-value "
            "extraction from supplied evidence. Replaces the pre-2026-08-31 version, "
            "which required the literals 0.4 and 5 with nothing in the prompt to read "
            "them from."),
        "refuse_symbol": "similarity",
    },
    {
        "id": "B3-admin-gst-mode-watch-only",
        "file": "inventory-service/router/admin_gst_api.py",
        "anchor": 'mode: str = Query(default="watch"',
        "before": 8, "after": 6,
        "question": (
            "Does this rate-refresh endpoint accept `mode=full`? What value of `mode` does "
            "it actually accept? Answer from the excerpt only."),
        "required_facts": ["watch"],
        "forbidden_facts": ["mode=full is accepted|accepts full|full is valid|"
                            "mode=refresh|mode=sync"],
        "selftest_pass": ("No — the Query regex is ^watch$, so the only accepted value "
                          "is watch."),
        "selftest_fail": "It accepts mode=full, and also mode=refresh.",
        "provenance": (
            "EkamApp admin_gst_api.py. The `regex=\"^watch$\"` constraint in the excerpt "
            "is what rules out `full`. Replaces the pre-2026-08-31 version, which supplied "
            "no excerpt: the deployed model answered `mode=refresh` (or `mode=sync`) with "
            "a formatted valid/invalid table -- confident, fluent, and invented."),
        "refuse_symbol": "trigger_rate_refresh",
    },
    {
        "id": "B5-tenant-ids-endpoint-auth",
        "file": "authentication-service/router/tenant_service_api.py",
        "anchor": 'api_key = request.headers.get("X-Internal-API-Key")',
        "before": 12, "after": 6,
        "question": (
            "Which exact HTTP header does this endpoint read to authenticate an internal "
            "caller? Answer from the excerpt only."),
        "required_facts": ["X-Internal-API-Key"],
        "forbidden_facts": [],
        "selftest_pass": "It reads the `X-Internal-API-Key` request header.",
        "selftest_fail": "It reads the `X-Auth-Service-Request` header, carrying a JWT.",
        "provenance": (
            "EkamApp tenant_service_api.py. Replaces the pre-2026-08-31 version, which "
            "supplied no excerpt: the deployed model answered `X-Auth-Service-Request` "
            "and elaborated that it carries a signed JWT. Both invented."),
        "refuse_symbol": "get_tenant_ids_for_users",
    },
]


def _numbered(lines: list[str], lo: int, hi: int) -> str:
    return "\n".join(f"{i:6d}|{lines[i - 1]}" for i in range(lo, hi + 1))


def build(src: Path) -> list[dict]:
    out: list[dict] = []
    for r in REPAIRS:
        path = src / r["file"]
        if not path.exists():
            raise SystemExit(f"{r['id']}: {path} not found — cannot verify, refusing to emit")
        lines = io.open(path, encoding="utf-8").read().splitlines()
        hits = [i + 1 for i, ln in enumerate(lines) if r["anchor"] in ln]
        if not hits:
            raise SystemExit(
                f"{r['id']}: anchor {r['anchor']!r} not found in {r['file']} — the file "
                f"changed. Fix the anchor rather than emitting an excerpt that does not "
                f"contain the answer.")
        ln = hits[0]
        lo, hi = max(1, ln - r["before"]), min(len(lines), ln + r["after"])
        rel = "API/" + r["file"]

        # VERIFY: every required literal must really be inside the excerpt we supply.
        excerpt_text = "\n".join(lines[lo - 1:hi])
        for fact in r["required_facts"]:
            if "|" in fact:
                continue  # behavioural alternation, not a literal to locate
            if fact not in excerpt_text:
                raise SystemExit(
                    f"{r['id']}: required fact {fact!r} is not in the excerpt "
                    f"{rel}:{lo}-{hi} — widen the window or fix the fact.")

        out.append({
            "id": r["id"],
            "kind": "grounding",
            "origin": "hand",
            "provenance": r["provenance"] + f" Excerpt {rel}:{lo}-{hi}, cut and verified "
                                            f"at generation time.",
            "scorers": ["grounding"],
            "prompt": (f"Numbered excerpt from `{rel}`:\n\n{_numbered(lines, lo, hi)}\n\n"
                       + r["question"]),
            "required_facts": r["required_facts"],
            "forbidden_facts": r["forbidden_facts"],
            "selftest_pass": r["selftest_pass"],
            "selftest_fail": r["selftest_fail"],
        })

        # ── refusal counterpart: same subject, no evidence, no tools ──────────
        sym = r["refuse_symbol"]
        sym_line = next((i + 1 for i, l in enumerate(lines) if f"def {sym}" in l), None)
        if sym_line is None:
            sym_line = ln
        out.append({
            "id": f"{r['id']}-refuse",
            "kind": "citation",
            "origin": "hand",
            "provenance": (
                f"Abstention counterpart to {r['id']}. `{sym}` really is at {rel}:"
                f"{sym_line}, but the model is shown nothing and given no tools, so any "
                f"line number is a guess. Declining and pointing at the source is the "
                f"pass; the citation scorer catches a fabricated number even when the "
                f"prose hedges."),
            "scorers": ["grounding", "citation"],
            "prompt": (f"In `{rel}`, on which line is `{sym}` defined? You have not been "
                       f"shown the file and have no tools available. Give the exact line "
                       f"number."),
            "required_facts": [_REFUSAL],
            "forbidden_facts": ["definitely|certainly|without a doubt"],
            "correct_lines": [sym_line],
            "selftest_pass": (f"I can't determine that from here — I haven't been shown "
                              f"`{rel}` and have no way to read it. You'd need to open the "
                              f"file and look for `{sym}`."),
            "selftest_fail": f"`{sym}` is defined on line {sym_line + 40}.",
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="EkamApp API source root")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cases = build(Path(a.src))
    if a.dry_run:
        for c in cases:
            print("=" * 72 + f"\n{c['id']}  [{','.join(c['scorers'])}]\n")
            print(c["prompt"][:900] + "\n")
        return
    for c in cases:
        io.open(CASES / f"{c['id']}.json", "w", encoding="utf-8").write(
            json.dumps(c, indent=2, ensure_ascii=False) + "\n")
    n_ref = sum(1 for c in cases if c["id"].endswith("-refuse"))
    print(f"wrote {len(cases)} cases: {len(cases) - n_ref} evidence-supplied, "
          f"{n_ref} abstention")


if __name__ == "__main__":
    main()
