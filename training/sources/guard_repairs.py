"""Source: guard-repaired battery answers -> preference pairs (deficient vs delivered).

Every guard that REPAIRS rather than merely flags leaves a labelled pair behind, for
free, on every run. The model wrote one thing; the process delivered a better artifact
by attaching items it had already gathered. That is (rejected, chosen) with no
annotation effort and no invention:

    rejected  the model's own answer text, verbatim, up to the first guard banner
    chosen    the artifact that actually shipped — the same text plus the items the
              guard recovered from this run's own tool output

Both sides are REAL. Neither is written by hand. This matters because of the rule
failure_log.py states for its own legacy rows: "Fabricating a plausible `rejected`
side would be inventing training data, which is exactly the failure mode this corpus
fights." Here the constraint binds on the other side — the `chosen` is not a model
output, so it is labelled as such in meta (`chosen_is_artifact: true`) rather than
passed off as something a model produced.

Why this corpus, for this failure
---------------------------------
Measured over 14 subset runs (2026-09-02/03): the retrieval stage succeeds and
synthesis discards. T12 read 470,149 chars and named 8 of 23 routers; one member
relayed all 24 filenames in 739 chars; T6 answered "there are 24 Python files" and
listed none, 17 runs running, byte-identical. The deficiency is not knowledge, it is
output-shape discipline: compress the prose, never compress the list. Prompt-side
correction of exactly this measured null six times out of six, including a gate that
blocked a call and handed over the replacement with its arguments filled in.

That leaves preference training as the untried lever, and this is its data.

What is deliberately NOT harvested
----------------------------------
  * Answers with no repairing guard. A banner that only WARNS ("count disagrees")
    supplies no corrected side; a pair needs something better to point at.
  * Runs that errored. No answer, nothing to compare.
  * Pairs whose two sides are equal after normalisation — schema.validate() rejects
    those anyway ("no signal"), but dropping them here keeps the reason visible.
  * Anything from a liveness-killed run: that draft never passed a guard chain, so
    "delivered" does not mean "checked".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterable

from ..schema import Record, Source

# Guards that ATTACH recovered content. Only these produce a chosen side that is
# better than the model's own text; a warn-only banner has nothing to learn from.
_REPAIRING_MARKERS = (
    "The listing this run received for",          # directory recovery (T6)
    "enumerable lines this run read from",        # file-read recovery (T2, T13a)
    "THE COMPARISON, COMPUTED",                   # deterministic set difference
    "WHAT THE MEMBERS ACTUALLY REPORTED",         # member-findings recovery
)

# Every banner marker, used to split the model's own text from everything appended.
_ALL_MARKERS = (
    "**THE COUNT", "**UNVERIFIED", "**A COMPLETENESS", "**NAMES THAT", "**ASKED FOR",
    "**BOTH SIDES", "**THE COMPARISON", "**ANSWER REPORTS", "**WHAT THE MEMBERS",
    "**THE NUMBERS", "**RUN STOPPED", "**Unverified claims",
)

_TASKS = {
    "T2": ("List every endpoint defined in API/business-service/router/business_api.py, "
           "then list every RTK Query hook exported by the frontend's business API slice, "
           "and state which endpoints have no corresponding hook. Enumerate both sides in "
           "full before comparing."),
    "T6": ("Read API/inventory-service/router/ and list every Python file in it. Then state "
           "how many there are. Do not guess — base the answer on an actual directory listing."),
    "T11": ("When a seller uploads a document, which services are involved end to end, and "
            "which function in each one handles it? Name every file in the chain."),
    "T12": ("Write a detailed architectural overview of the inventory service: its routers, "
            "its models, its external dependencies, and how it integrates with the business "
            "service. Be thorough."),
    "T13a": ("Audit the vouchers module: list its endpoints, its database tables, and its "
             "frontend hooks, and identify anything present in the backend with no frontend "
             "counterpart."),
}

_MIN_MODEL_CHARS = 80          # below this the "answer" is a fragment, not an attempt
_MIN_RECOVERED_CHARS = 120     # below this the repair added nothing worth learning

# How many pairs may share ONE recovered block. Several different bad answers
# corrected to the same good shape is real preference signal -- "many ways of being
# wrong map to this right shape" -- so the cap is not 1. But it must be small:
# measured 2026-09-04 over 19 battery runs, a single 489-char recovered block
# appeared in 8 of 21 pairs (38% of the corpus) and T2 alone supplied 16 of 21 (76%).
# Record.dedupe_key cannot see this -- it hashes prompt+chosen+rejected, so pairs
# sharing a lesson but differing in the model text it corrects are all distinct
# records. Redundancy of LESSONS is a source-level concern and belongs here.
_MAX_PER_RECOVERED_BLOCK = 2


def _split_at_first_banner(text: str) -> tuple[str, str]:
    """(model's own text, everything the guards appended). Split at the FIRST marker.

    Not at the first "---": a horizontal rule is ordinary markdown and answers contain
    them. Splitting there measured an answer at 275 chars that was really 6,271 --
    wrong by a factor of 23, and it was reported before being checked.
    """
    hits = [text.find(m) for m in _ALL_MARKERS if text.find(m) > 0]
    if not hits:
        return text, ""
    cut = min(hits)
    return text[:cut].rstrip(), text[cut:].strip()


class GuardRepairsSource(Source):
    name = "guard_repairs"

    def __init__(self, runs_dir: str | os.PathLike, glob: str = "subset*.json"):
        super().__init__()
        self.runs_dir = Path(runs_dir)
        self.glob = glob
        # fingerprint of the recovered block -> how many pairs already carry it
        self._block_counts: dict[str, int] = {}

    def load(self) -> Iterable[Record]:
        if not self.runs_dir.is_dir():
            self.drop("runs_dir_missing")
            return
        for path in sorted(self.runs_dir.glob(self.glob)):
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.drop("unreadable_run_file")
                continue
            if not isinstance(rows, list):
                self.drop("run_file_not_a_list")
                continue
            for row in rows:
                if not isinstance(row, dict):
                    self.drop("row_not_a_dict")
                    continue
                rec = self._pair(row, path.stem)
                if rec is not None:
                    yield rec

    def _pair(self, row: dict, run_name: str) -> Record | None:
        tid = row.get("id")
        delivered = (row.get("text") or "").replace("\r", "")
        if row.get("error") or not delivered:
            self.drop("errored_run")
            return None
        if "RUN STOPPED EARLY" in delivered:
            # Liveness-killed: the draft never passed a guard chain, so the delivered
            # artifact is not a checked one. See this module's docstring.
            self.drop("liveness_killed_draft")
            return None
        if not any(m in delivered for m in _REPAIRING_MARKERS):
            self.drop("no_repairing_guard")
            return None

        model_text, appended = _split_at_first_banner(delivered)
        if len(model_text) < _MIN_MODEL_CHARS:
            self.drop("model_text_too_short")
            return None
        if len(appended) < _MIN_RECOVERED_CHARS:
            self.drop("recovered_block_too_small")
            return None
        task = _TASKS.get(tid)
        if not task:
            self.drop("unknown_task_id")
            return None
        if model_text.strip() == delivered.strip():
            self.drop("no_signal_identical_sides")
            return None

        # Cap how many pairs may teach the SAME correction. Without this the report
        # shows a healthy total for a corpus that is mostly one lesson repeated.
        fp = hashlib.sha1(appended.encode("utf-8")).hexdigest()
        if self._block_counts.get(fp, 0) >= _MAX_PER_RECOVERED_BLOCK:
            self.drop("recovered_block_over_represented")
            return None
        self._block_counts[fp] = self._block_counts.get(fp, 0) + 1

        return Record(
            kind="pref",
            source=self.name,
            prompt=task,
            rejected=model_text,
            chosen=delivered,
            meta={
                # build_dataset's report groups preference pairs by meta["shape"];
                # keying it per TEST makes the task imbalance visible in the report
                # itself rather than only under separate analysis.
                "shape": f"guard_repairs:{tid}",
                "run": run_name,
                "test": tid,
                "secs": row.get("secs"),
                # The chosen side is the DELIVERED ARTIFACT, not a model output. Flagged
                # so a later consumer can weight or exclude it knowingly rather than
                # discovering it by surprise.
                "chosen_is_artifact": True,
                "recovered_chars": len(appended),
                "model_chars": len(model_text),
            },
        )
