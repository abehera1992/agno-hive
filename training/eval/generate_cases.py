"""Generate golden eval cases with VERIFIED ground truth, holding out from training.

    python -m training.eval.generate_cases --src /tmp/ekam-src --patterns /tmp/ekam-patterns

TRAIN / EVAL SEPARATION — the reason this script exists rather than hand-authoring
----------------------------------------------------------------------------------
Two overlaps were live before this:

  * Guards. Training uses ALL 31 patterns_md guards as preference pairs, and the first
    eval set tested GUARD 18 and GUARD 35 — both in training. Axis D would have been
    scored on data the model was tuned on, inflating it.
    Fix: EVAL_GUARD_MODULO. Guards where (n % 2 == 0) are EVAL-ONLY; the trainer
    excludes them via patterns_md's `exclude_guards`. Deterministic, so both sides
    agree without passing state around.

  * Citation. Training synthesises from **agno-hive** source; eval cases here are built
    from **EkamApp** source. Different repositories entirely — no overlap by construction.

Every citation/grounding case is verified against the real file before being written:
a case with a wrong `correct_lines` would score a correct model as fabricating.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

CASES_DIR = Path(__file__).parent / "cases"
# Real production failures, hand-authored and version-controlled. Copied into cases/ on
# every regeneration and NEVER auto-generated: their prompts are LEADING (E-1
# presupposes the answer exists), which is materially harder than the blunt generated
# C-refuse prompts the model refuses easily. A suite without them overstates the model.
CAPTURED_DIR = Path(__file__).parent / "captured"

# Guards with an EVEN number are reserved for eval and excluded from training.
EVAL_GUARD_MODULO = (2, 0)

# Any ONE of these satisfies "the model declined and pointed at the source" on a
# C-refuse case. Kept as alternation rather than a single token so the case measures the
# behaviour instead of the model's incidental choice of verb.
_REFUSAL_VERBS = ("read|check|inspect|examine|look at|open|access|see the file|"
                  "provide|share|don't have|do not have|cannot determine|can't determine")

_DEF_RE = re.compile(
    r"^(?:async\s+def|def|class)\s+(\w+)|^(?:export\s+)?(?:interface|type|const)\s+(\w+)", re.M
)
_GUARD_RE = re.compile(r"^## (GUARD (\d+):.*?)$", re.M)
_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.S)
_WRONG_RE = re.compile(r"^\s*(?:#|//)\s*WRONG\b.*$", re.M | re.I)
_CORRECT_RE = re.compile(r"^\s*(?:#|//)\s*CORRECT\b.*$", re.M | re.I)


def real_path(flat_name: str) -> str:
    """fetch_patterns flattens `a/b/c.py` -> `a__b__c.py`; restore for citation text."""
    return flat_name.replace("__", "/")


# ── Axis C: citation ──────────────────────────────────────────────────────────

def citation_cases(src: Path, target: int, rng: random.Random) -> list[dict]:
    out: list[dict] = []
    files = sorted(p for p in src.glob("*.py") if p.stat().st_size > 2000)
    for f in files:
        if len(out) >= target:
            break
        lines = f.read_text(encoding="utf-8").splitlines()
        text = "\n".join(lines)
        syms = [
            (m.group(1) or m.group(2), text[: m.start()].count("\n") + 1)
            for m in _DEF_RE.finditer(text)
        ]
        syms = [(s, n) for s, n in syms if s and not s.startswith("_")]
        if not syms:
            continue
        rel = real_path(f.name)

        for sym, lineno in rng.sample(syms, min(2, len(syms))):
            if len(out) >= target:
                break
            # VERIFY before emitting — a wrong correct_lines scores a good model as bad.
            if sym not in lines[lineno - 1]:
                continue
            lo, hi = max(1, lineno - 3), min(len(lines), lineno + 3)
            excerpt = "\n".join(f"{i:6d}|{lines[i-1]}" for i in range(lo, hi + 1))

            # with-excerpt: evidence present -> exact line required
            out.append({
                "id": f"C-cite-{sym}-{lineno}",
                "kind": "citation",
                "provenance": f"generated from EkamApp {rel}:{lineno} (verified). Training citation "
                              f"pairs come from agno-hive — different repo, no overlap.",
                "scorers": ["citation"],
                "prompt": f"Numbered excerpt from `{rel}`:\n\n{excerpt}\n\n"
                          f"On which line is `{sym}` defined?",
                "correct_lines": [lineno],
            })
            if len(out) >= target:
                break
            # no-excerpt: evidence absent -> refusal is the pass
            out.append({
                "id": f"C-refuse-{sym}",
                "kind": "citation",
                "provenance": f"E-1 shape. `{sym}` really is at {rel}:{lineno}, but the model is "
                              f"shown nothing and given no tools, so any number is a guess.",
                "scorers": ["citation", "grounding"],
                "prompt": f"In `{rel}`, on which line is `{sym}` defined? Give the exact line number.",
                "correct_lines": [lineno],
                # The fact under test is the BEHAVIOUR "declined and pointed at the
                # source", not any one verb. Requiring the literal "read" (as this did
                # until 2026-07-30) failed every correct refusal phrased with "check" or
                # "access" — all 8 cases scored 0.00 on grounding in BOTH the baseline
                # and the v2 candidate while the citation scorer scored the same
                # responses 1.0. `|` = any-of; see _fact_present in harness.py.
                "required_facts": [_REFUSAL_VERBS],
                "forbidden_facts": ["definitely", "certainly"],
            })
    return out[:target]


# ── Axis B: grounding ─────────────────────────────────────────────────────────

def grounding_cases(src: Path, target: int, rng: random.Random) -> list[dict]:
    out: list[dict] = []
    files = sorted(p for p in src.glob("*.py") if p.stat().st_size > 2000)
    for f in files:
        if len(out) >= target:
            break
        lines = f.read_text(encoding="utf-8").splitlines()
        text = "\n".join(lines)
        syms = [
            (m.group(1) or m.group(2), text[: m.start()].count("\n") + 1)
            for m in _DEF_RE.finditer(text)
        ]
        syms = [(s, n) for s, n in syms if s and not s.startswith("_")]
        if not syms:
            continue
        rel = real_path(f.name)
        for sym, lineno in rng.sample(syms, min(2, len(syms))):
            if len(out) >= target:
                break
            lo, hi = max(1, lineno - 2), min(len(lines), lineno + 6)
            excerpt = "\n".join(lines[lo - 1:hi])
            # Fact extraction from provided evidence: the answer IS in the prompt.
            out.append({
                "id": f"B-extract-{sym}",
                "kind": "grounding",
                "provenance": f"EkamApp {rel} (verified). Answer is present in the prompt; "
                              f"failure here is misreading, not missing knowledge.",
                "scorers": ["grounding"],
                "prompt": f"Given this excerpt from `{rel}`:\n\n```python\n{excerpt}\n```\n\n"
                          f"What is `{sym}` — a function, an async function, or a class? "
                          f"Answer from the excerpt only.",
                "required_facts": [
                    "async" if lines[lineno - 1].strip().startswith("async") else
                    ("class" if lines[lineno - 1].strip().startswith("class") else "function")
                ],
                "forbidden_facts": [],
            })
    return out[:target]


# ── Axis D: guard (EVAL-ONLY guards) ──────────────────────────────────────────

def guard_cases(patterns: Path, target: int) -> tuple[list[dict], list[int]]:
    out: list[dict] = []
    held: list[int] = []
    mod, rem = EVAL_GUARD_MODULO
    for md in sorted(patterns.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        hits = list(_GUARD_RE.finditer(text))
        for i, m in enumerate(hits):
            num = int(m.group(2))
            if num % mod != rem:
                continue  # training keeps the odd-numbered guards
            body = text[m.end(): hits[i + 1].start() if i + 1 < len(hits) else len(text)]
            pair = None
            for fm in _FENCE_RE.finditer(body):
                code = fm.group(2)
                w, c = _WRONG_RE.search(code), _CORRECT_RE.search(code)
                if w and c and c.start() > w.start():
                    pair = (code[w.end():c.start()].strip(), code[c.end():].strip())
                    break
            if not pair:
                continue
            wrong, correct = pair
            held.append(num)
            rationale = body[: body.find("```")].strip()[:400]
            # Tokens the correct form has and the wrong form does not.
            # Extract tokens from CODE ONLY. Naive extraction over the whole block
            # pulled words out of comments and prose — one case required 'Reading' and
            # forbade 'HALLUCINATION' (both from comment text), another forbade 'after',
            # an ordinary English word that appears in any correct explanation. Those
            # cases scored a competent model at 0 and dragged Axis D to 23%.
            def code_tokens(block: str) -> set[str]:
                body = "\n".join(
                    ln.split("#")[0].split("//")[0]
                    for ln in block.splitlines()
                    if ln.strip() and not ln.strip().startswith(("#", "//"))
                )
                return {
                    t for t in re.findall(r"[\w().\[\]]{4,}", body)
                    # Must look like CODE, not prose: a call, an attribute access, a
                    # snake_case or CamelCase identifier. And never a doc placeholder.
                    if ("..." not in t)
                    and ("(" in t or "." in t or "_" in t or not t.islower())
                }

            ctok, wtok = code_tokens(correct), code_tokens(wrong)
            required = sorted(ctok - wtok)[:2]
            forbidden = sorted(wtok - ctok)[:2]
            if not required:
                # Guards that differ only by ORDERING or whitespace have no token unique
                # to the correct form, so substring scoring cannot express them at all.
                # SKIP them: a case whose pass condition is not checkable produces a
                # confident-looking score with no meaning behind it. Axis D is a
                # regression guard, and a smaller trustworthy set beats a larger noisy one.
                # (skipped - see comment above)
                continue
            out.append({
                "id": f"D-guard{num}",
                "kind": "guard",
                "provenance": f"{md.name} GUARD {num} — EVAL-ONLY (even-numbered guards are "
                              f"excluded from training via patterns_md exclude_guards).",
                "scorers": ["guard"],
                "prompt": f"Project rule:\n{rationale}\n\nWrite code that follows this rule. Output only code.",
                "guard_required": required,
                "guard_forbidden": forbidden,
            })
            if len(out) >= target:
                return out, held
    return out, held


# ── Axis A: tool_call ─────────────────────────────────────────────────────────

_TOOLS = {
    "get_file_content": ({"relative_path": "string"}, ["relative_path"],
                         "Read a file from the project by relative path."),
    "search_files": ({"pattern": "string", "glob_filter": "string"}, ["pattern"],
                     "Regex search across project source files."),
    "find_files": ({"glob_pattern": "string"}, ["glob_pattern"],
                   "Glob search for files across the project."),
    "run_command": ({"cmd": "string"}, ["cmd"],
                    "Run a READ-ONLY shell command (tests, linters, git status)."),
    "list_directory": ({"path": "string"}, ["path"],
                       "List the immediate children of a directory."),
}

_TOOL_PROMPTS = [
    ("get_file_content", "Show me what is inside API/inventory-service/models.py."),
    ("get_file_content", "I need the contents of swarm/feedback.py before we change it."),
    ("search_files", "Which files reference the symbol `next_sku`? Search the codebase."),
    ("search_files", "Find every place `INFERENCE_BACKEND` is read."),
    ("find_files", "List all SCSS module files under the client app."),
    ("find_files", "Which alembic migration files exist for inventory-service?"),
    ("run_command", "Check whether the working tree is clean."),
    ("run_command", "Run the inventory-service test suite and report the output."),
    ("list_directory", "What is directly inside API/inventory-service/router?"),
    ("list_directory", "Show me the top level of the training package."),
    ("get_file_content", "Open training/config/qwen3-30b.yaml so we can review the recipe."),
    ("search_files", "Where is `status_filter` used across the repo?"),
    ("find_files", "Find every *.yaml under teams/."),
    ("run_command", "Show me the last 5 commits."),
    ("list_directory", "List what is in patterns/."),
    ("get_file_content", "Print swarm/agents.py so I can check the model map."),
]


def tool_cases(target: int) -> list[dict]:
    out = []
    for i, (tool, prompt) in enumerate(_TOOL_PROMPTS[:target]):
        props, required, desc = _TOOLS[tool]
        out.append({
            "id": f"A-tool-{i+1:02d}-{tool}",
            "kind": "tool_call",
            "provenance": "Counterpart to E-1: with a tool available the model must CALL it "
                          "rather than answer from memory. Scores syntax only.",
            "scorers": ["tool_call"],
            "prompt": prompt,
            "expect_tool": tool,
            "expect_args": required,
            "tools": [{
                "type": "function",
                "function": {
                    "name": t,
                    "description": _TOOLS[t][2],
                    "parameters": {
                        "type": "object",
                        "properties": {k: {"type": v} for k, v in _TOOLS[t][0].items()},
                        "required": _TOOLS[t][1],
                    },
                },
            } for t in _TOOLS],  # ALL tools offered — selection is part of the test
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="dir of fetched EkamApp source")
    ap.add_argument("--patterns", required=True, help="dir of fetched patterns/*.md")
    ap.add_argument("--per-axis", type=int, default=16)
    ap.add_argument("--auto-guards", type=int, default=0,
                    help="how many AUTO-GENERATED Axis D cases to emit. Default 0: they "
                         "require example-specific literals (db.add(invite), old_used) and so "
                         "test memorisation of the doc example rather than the rule. Axis D is "
                         "covered by hand-authored token-based cases in captured/ instead.")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    CASES_DIR.mkdir(parents=True, exist_ok=True)

    cites = citation_cases(Path(a.src), a.per_axis, rng)
    grounds = grounding_cases(Path(a.src), a.per_axis, rng)
    guards, held = guard_cases(Path(a.patterns), a.auto_guards) if a.auto_guards else ([], [])
    if not a.auto_guards:
        # Holdout list is still needed for --exclude-guards even when not emitting cases.
        _, held = guard_cases(Path(a.patterns), 99)
    tools = tool_cases(a.per_axis)

    n_cap = 0
    for src_case in sorted(CAPTURED_DIR.glob("*.json")):
        (CASES_DIR / src_case.name).write_text(
            src_case.read_text(encoding="utf-8"), encoding="utf-8")
        n_cap += 1

    for group in (cites, grounds, guards, tools):
        for c in group:
            (CASES_DIR / f"{c['id']}.json").write_text(
                json.dumps(c, indent=2), encoding="utf-8")

    print(f"citation : {len(cites)}")
    print(f"grounding: {len(grounds)}")
    print(f"guard    : {len(guards)} auto  (+ hand-authored in captured/)"
          f"   EVAL-ONLY guards held out: {sorted(held)}")
    print(f"tool_call: {len(tools)}")
    print(f"captured : {n_cap}  (real production failures — hand-authored)")
    print(f"\nwrote to {CASES_DIR}")
    print("\nIMPORTANT: rebuild the training corpus with --exclude-guards "
          f"\"{','.join(map(str, sorted(held)))}\" so Axis D is not scored on training data.")


if __name__ == "__main__":
    main()
