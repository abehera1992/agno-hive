"""Source: synthetic citation-restraint preference pairs, generated from real code.

WHY THIS EXISTS
---------------
The v1 corpus has 33 preference pairs, of which exactly **2** (failure_log 61/62)
concern citation behaviour. Axis C is the weakest axis (50%) and the stated target of
Phase 3 — but two examples cannot move a 30B model's behaviour. DPO on that corpus
would either no-op or overfit those two strings.

Citation ground truth is FREE and VERIFIABLE: read a file, and you know the true line
of every symbol in it. So the pairs can be synthesised deterministically at whatever
volume is needed, with zero hand-labelling and zero fabrication risk — the `chosen`
side is read off disk, never invented.

Two complementary shapes, matching the two real failures we captured:

  WITH-EXCERPT   (mirrors E-2 / failure_log 62)
      Prompt includes a numbered excerpt containing the symbol.
      chosen   = the TRUE line number
      rejected = a plausible but wrong line number
      Teaches: when the evidence is in front of you, read it off — don't approximate.

  NO-EXCERPT     (mirrors E-1 / failure_log 61)
      Prompt names a file the model cannot see, and gives no tools.
      chosen   = decline + describe how to find it (prose location, no number)
      rejected = a confident specific answer with an invented line
      Teaches: absent evidence, restraint beats fluency.

The NO-EXCERPT shape is the important one: E-1 is the failure that actually costs us,
and its correct behaviour ("I need to read the file") is the behaviour a fine-tune can
realistically install.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Iterable

from ..schema import Record, Source

# Top-level python/ts definitions worth asking about.
_DEF_RE = re.compile(
    r"^(?:async\s+def|def|class)\s+(\w+)|^(?:export\s+)?(?:interface|type|const)\s+(\w+)",
    re.M,
)

_DECLINE_TEMPLATES = [
    "I can't give you a line number for `{sym}` without reading "
    "`{file}` — I don't have its contents here. Read the file (or grant a file-read "
    "tool) and I'll quote the exact line. What I can say is that `{sym}` is a "
    "top-level definition in that module.",
    "I don't have `{file}` in front of me, so any line number I gave for `{sym}` "
    "would be a guess. Fetch the file and I'll cite it precisely.",
]


def _wrong_line(true_line: int, total: int, rng: random.Random) -> int:
    """A plausible-but-wrong line: near-miss or a confident-sounding round number."""
    for _ in range(12):
        cand = rng.choice([
            true_line + rng.choice([-1, 1, 2, -2, 7, -7]),
            rng.randint(1, max(2, min(total, 200))),
        ])
        if cand != true_line and cand >= 1:
            return cand
    return max(1, true_line + 3)


class SyntheticCitationSource(Source):
    name = "synthetic_citation"

    def __init__(
        self,
        root: str,
        globs: tuple[str, ...] = ("**/*.py", "**/*.ts", "**/*.tsx"),
        max_per_file: int = 3,
        seed: int = 42,
        skip_dirs: tuple[str, ...] = ("node_modules", ".git", "__pycache__", ".next", "dist", "build"),
    ):
        super().__init__()
        self.root = Path(root)
        self.globs = globs
        self.max_per_file = max_per_file
        self.rng = random.Random(seed)
        self.skip_dirs = skip_dirs

    def _files(self) -> list[Path]:
        out: list[Path] = []
        for g in self.globs:
            for p in self.root.glob(g):
                if p.is_file() and not any(s in p.parts for s in self.skip_dirs):
                    out.append(p)
        return sorted(out)

    def load(self) -> Iterable[Record]:
        for path in self._files():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                self.drop("unreadable file")
                continue
            if len(lines) < 40:
                self.drop("file too short to be a realistic citation target")
                continue

            text = "\n".join(lines)
            hits = []
            for m in _DEF_RE.finditer(text):
                sym = m.group(1) or m.group(2)
                lineno = text[: m.start()].count("\n") + 1
                if sym and not sym.startswith("_"):
                    hits.append((sym, lineno))
            if not hits:
                self.drop("no top-level definitions found")
                continue

            rel = path.relative_to(self.root).as_posix()
            for sym, lineno in self.rng.sample(hits, min(self.max_per_file, len(hits))):
                wrong = _wrong_line(lineno, len(lines), self.rng)

                # ---- shape 1: evidence present -> cite it exactly ----------------
                lo, hi = max(1, lineno - 3), min(len(lines), lineno + 3)
                excerpt = "\n".join(f"{i:6d}|{lines[i-1]}" for i in range(lo, hi + 1))
                yield Record(
                    kind="pref",
                    source=self.name,
                    prompt=(
                        f"Here is a numbered excerpt from `{rel}`:\n\n{excerpt}\n\n"
                        f"On which line is `{sym}` defined?"
                    ),
                    chosen=f"`{sym}` is defined on line {lineno} of `{rel}`.",
                    rejected=f"`{sym}` is defined on line {wrong} of `{rel}`.",
                    meta={"shape": "with_excerpt", "file": rel, "symbol": sym, "true_line": lineno},
                )

                # ---- shape 2: no evidence -> restraint --------------------------
                yield Record(
                    kind="pref",
                    source=self.name,
                    prompt=(
                        f"In `{rel}`, on which line is `{sym}` defined? "
                        f"Give the exact line number."
                    ),
                    chosen=self.rng.choice(_DECLINE_TEMPLATES).format(sym=sym, file=rel),
                    rejected=f"`{sym}` is defined on line {wrong} of `{rel}`.",
                    meta={"shape": "no_excerpt", "file": rel, "symbol": sym, "true_line": lineno},
                )
