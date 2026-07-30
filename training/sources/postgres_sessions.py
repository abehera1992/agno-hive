"""Source: `session_messages` + `chat_sessions` -> (task -> accepted outcome) SFT pairs.

Turns real hive traffic into supervised examples. The hard part is that NOT every
assistant reply is a good target — the table stores whatever the swarm produced,
including failures. Filtering is therefore the substance of this adapter, not an
afterthought; an unfiltered export would train the model to reproduce its own bugs.

Excluded (see `_REJECT_MARKERS`): error envelopes, timeouts, context-window
overflows, empty/stub replies, and turns that merely announce a staged action.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

from ..schema import Record, Source

# Substrings that mark an assistant turn as NOT a good training target.
_REJECT_MARKERS = (
    "litellm.",                      # provider error envelopes
    "InternalServerError",
    "ContextWindowExceededError",
    "Connection error",
    "agno_run timed out",
    "Traceback (most recent call last)",
    "action_pending",                # staged-write announcement, not an answer
    "review_pending",
    "I cannot provide",              # refusal/non-answer shapes
    "I'm unable to proceed",
    "cannot be verified",
)

_MIN_USER_CHARS = 20
_MIN_ASSISTANT_CHARS = 80
_MAX_CHARS = 8000                    # keep sequences trainable


class PostgresSessionsSource(Source):
    name = "postgres_sessions"

    def __init__(self, postgres_uri: str | None = None, project_id: str | None = None):
        self.postgres_uri = postgres_uri or os.getenv(
            "POSTGRES_URI", "postgresql://agno:agno@localhost:5432/agno_graph"
        )
        self.project_id = project_id

    def load(self) -> Iterable[Record]:
        import psycopg

        clause, params = "", []
        if self.project_id:
            clause = "WHERE s.project_id = %s"
            params.append(self.project_id)

        with psycopg.connect(self.postgres_uri) as conn:
            rows = conn.execute(
                f"""
                SELECT m.session_id, m.id, m.role, m.content, s.project_id
                FROM session_messages m
                JOIN chat_sessions s ON s.id = m.session_id
                {clause}
                ORDER BY m.session_id, m.id
                """,
                params,
            ).fetchall()

        # Walk each session in order, pairing each user turn with the reply that follows.
        pending: tuple[str, str] | None = None  # (session_id, user_content)
        for sid, _mid, role, content, project in rows:
            content = (content or "").strip()
            if role == "user":
                pending = (str(sid), content) if len(content) >= _MIN_USER_CHARS else None
                continue
            if role != "assistant" or pending is None or pending[0] != str(sid):
                continue

            user_text = pending[1]
            pending = None

            if len(content) < _MIN_ASSISTANT_CHARS:
                continue
            if any(mark in content for mark in _REJECT_MARKERS):
                continue
            if len(user_text) > _MAX_CHARS or len(content) > _MAX_CHARS:
                continue

            yield Record(
                kind="sft",
                source=self.name,
                messages=[
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": content},
                ],
                meta={"session_id": str(sid), "project_id": project},
            )
