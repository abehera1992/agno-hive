"""Common record schema shared by every training data source.

One normalised shape so `train.py` never needs to know which source a row came
from, and so mixed SFT + preference corpora can live in one JSONL file.

Two record kinds:

  SFT         {"kind": "sft",  "messages": [{role, content}, ...]}
  PREFERENCE  {"kind": "pref", "prompt": str, "chosen": str, "rejected": str}

Both carry `source` and `meta` for provenance — needed for the quality report and
for excluding a source from a run without re-exporting.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Literal


@dataclass
class Record:
    kind: Literal["sft", "pref"]
    source: str
    meta: dict[str, Any] = field(default_factory=dict)

    # sft
    messages: list[dict[str, str]] | None = None
    # pref
    prompt: str | None = None
    chosen: str | None = None
    rejected: str | None = None

    def validate(self) -> str | None:
        """Return an error string if this record is unusable, else None."""
        if self.kind == "sft":
            if not self.messages or len(self.messages) < 2:
                return "sft record needs >=2 messages"
            for m in self.messages:
                if not m.get("content", "").strip():
                    return "sft record has an empty message"
        elif self.kind == "pref":
            for f in ("prompt", "chosen", "rejected"):
                if not (getattr(self, f) or "").strip():
                    return f"pref record missing {f}"
            if self.chosen.strip() == self.rejected.strip():
                return "pref record chosen == rejected (no signal)"
        else:
            return f"unknown kind {self.kind!r}"
        return None

    def dedupe_key(self) -> str:
        """Content hash used to drop duplicates across sources."""
        if self.kind == "sft":
            basis = json.dumps(self.messages, sort_keys=True)
        else:
            basis = f"{self.prompt}\x00{self.chosen}\x00{self.rejected}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        d = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(d, ensure_ascii=False)


class Source:
    """Base class for a data source. Subclasses implement `load()`.

    `drops` exists so source-internal filtering is VISIBLE in the quality report.
    A source that silently discards rows produces a report claiming "rejected: none"
    while quietly throwing away a third of the corpus — which is worse than no report,
    because it looks like evidence of cleanliness. Any `continue` in a load() must be
    accompanied by a self.drop("reason").
    """

    name: str = "unnamed"

    def __init__(self) -> None:
        self.drops: Counter[str] = Counter()

    def drop(self, reason: str) -> None:
        self.drops[reason] += 1

    def load(self) -> Iterable[Record]:  # pragma: no cover - interface
        raise NotImplementedError
