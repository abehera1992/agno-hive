"""Source: Postgres `failure_log` -> preference pairs (new rows) or SFT (legacy rows).

Two row shapes exist and they are NOT interchangeable:

  INSTRUMENTED (rejected_output + corrected_output present, added 2026-07-30)
      -> a true DPO/ORPO triple: (task, rejected, chosen)

  LEGACY (those columns NULL — every row up to and including ids 61/62)
      -> we have the task and the human correction, but NOT the model's verbatim
         bad output, so a preference pair CANNOT be honestly constructed. Emitted
         as SFT instead, teaching "given this task, this is the correction that
         applies". Fabricating a plausible `rejected` side here would be inventing
         training data, which is exactly the failure mode this corpus fights.

Runtime failures (agent != 'output_quality') are excluded by default: a stack trace
from a transient tool error is not a behavioural lesson.
"""

from __future__ import annotations

import os
from typing import Iterable

from ..schema import Record, Source

_FEEDBACK_PREFIX = "[USER FEEDBACK] "


class FailureLogSource(Source):
    name = "failure_log"

    def __init__(
        self,
        postgres_uri: str | None = None,
        project_id: str | None = None,
        human_only: bool = True,
    ):
        super().__init__()
        self.postgres_uri = postgres_uri or os.getenv(
            "POSTGRES_URI", "postgresql://agno:agno@localhost:5432/agno_graph"
        )
        self.project_id = project_id
        self.human_only = human_only

    def load(self) -> Iterable[Record]:
        import psycopg

        where, params = [], []
        if self.project_id:
            where.append("project_id = %s")
            params.append(self.project_id)
        if self.human_only:
            where.append("agent = 'output_quality'")
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        with psycopg.connect(self.postgres_uri) as conn:
            rows = conn.execute(
                f"""
                SELECT id, task, error_message, rejected_output, corrected_output
                FROM failure_log {clause} ORDER BY id
                """,
                params,
            ).fetchall()

        for rid, task, err, rejected, corrected in rows:
            note = (err or "")
            if note.startswith(_FEEDBACK_PREFIX):
                note = note[len(_FEEDBACK_PREFIX):]
            note = note.strip()
            if not (task or "").strip() or not note:
                self.drop("missing task or note")
                continue

            if (rejected or "").strip() and (corrected or "").strip():
                yield Record(
                    kind="pref",
                    source=self.name,
                    prompt=task,
                    chosen=corrected.strip(),
                    rejected=rejected.strip(),
                    meta={"failure_log_id": rid, "shape": "instrumented", "why": note},
                )
            else:
                yield Record(
                    kind="sft",
                    source=self.name,
                    messages=[
                        {"role": "user", "content": task},
                        {"role": "assistant", "content": note},
                    ],
                    meta={"failure_log_id": rid, "shape": "legacy_correction_only"},
                )
