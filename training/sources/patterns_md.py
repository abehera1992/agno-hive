"""Source: `patterns/*.md` guards -> synthetic preference pairs.

Each guard is already written as an explicit WRONG -> CORRECT pair with a rationale,
which is exactly the shape DPO wants. This is the highest-value-per-effort source.

Real structure (verified against EkamApp/patterns/ekam-code-generation-guards.md,
2026-07-30): 38 guards under `## GUARD N: title`; 31 carry both markers inside a
SINGLE fenced block, 1 splits them across two fences, 6 are prose-only (no code).
Markers are comment-style and language-dependent: `# WRONG` / `# CORRECT` in python,
`// WRONG` / `// CORRECT` in ts/tsx. Prose-only guards are skipped, not guessed at.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ..schema import Record, Source

# `## GUARD 18: Wrap raw SQL strings in text() ...`
_GUARD_RE = re.compile(r"^## (GUARD \d+:.*?)$", re.M)
# fenced block + its language tag
_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.S)
# a WRONG/CORRECT marker line in any comment style
_WRONG_RE = re.compile(r"^\s*(?:#|//)\s*WRONG\b.*$", re.M | re.I)
_CORRECT_RE = re.compile(r"^\s*(?:#|//)\s*CORRECT\b.*$", re.M | re.I)


def _split_fence(code: str) -> tuple[str, str] | None:
    """Split one fence containing both markers into (wrong, correct)."""
    w = _WRONG_RE.search(code)
    c = _CORRECT_RE.search(code)
    if not (w and c) or c.start() < w.start():
        return None
    wrong = code[w.end(): c.start()].strip("\n ")
    correct = code[c.end():].strip("\n ")
    return (wrong, correct) if wrong and correct else None


class PatternsMdSource(Source):
    name = "patterns_md"

    def __init__(self, path: str):
        # Accepts a directory of *.md or a single file.
        self.path = Path(path)

    def _files(self) -> list[Path]:
        if self.path.is_file():
            return [self.path]
        return sorted(self.path.glob("*.md"))

    def load(self) -> Iterable[Record]:
        for f in self._files():
            text = f.read_text(encoding="utf-8")
            hits = list(_GUARD_RE.finditer(text))
            for i, m in enumerate(hits):
                title = m.group(1).strip()
                body = text[m.end(): hits[i + 1].start() if i + 1 < len(hits) else len(text)]

                pair = None
                lang = ""
                for fm in _FENCE_RE.finditer(body):
                    lang, code = fm.group(1), fm.group(2)
                    pair = _split_fence(code)
                    if pair:
                        break

                if not pair:
                    # Prose-only guard, or markers we cannot split with confidence.
                    # Skipping is deliberate: a fabricated "correct" side would poison
                    # the very behaviour this corpus is meant to teach.
                    continue

                wrong, correct = pair
                # Rationale = prose before the first fence; gives the model the WHY,
                # not just the WHAT.
                rationale = body[: body.find("```")].strip()

                yield Record(
                    kind="pref",
                    source=self.name,
                    prompt=(
                        f"Project rule — {title}\n\n{rationale}\n\n"
                        "Write the code that follows this rule."
                    ),
                    chosen=correct,
                    rejected=wrong,
                    meta={"file": f.name, "guard": title.split(":")[0], "lang": lang or "unknown"},
                )
