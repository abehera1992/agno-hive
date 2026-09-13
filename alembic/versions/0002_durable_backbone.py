"""durable execution/evidence backbone -- runs, executions, tool_calls,
evidence, claims, claim_evidence

Schema-only (Phase B of the durable-memory architecture): no runtime code
persists to these tables yet -- see swarm/execution_context.py's module
docstring for Phase A's runtime-only model these tables mirror.

Also repairs a known, live-confirmed drift on existing installations:
task_outcome_queue_dedupe_idx (declared in 0001_baseline, and in swarm/db.py's
metadata since 2026-08-29) was found missing from an existing deployment's
actual database during the accepted architecture review, because
create_all()-based bootstrapping never adds an index to an already-existing
table. Repaired here via `CREATE UNIQUE INDEX IF NOT EXISTS` -- a no-op
wherever the index already exists (a fresh install that ran 0001_baseline's
own copy for real), and the actual fix wherever it's missing (an existing
installation stamped past 0001_baseline).

Revision ID: 0002_durable_backbone
Revises: 0001_baseline
Create Date: (Phase B)
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_durable_backbone"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("session_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_type", sa.Text, nullable=True),
        sa.Column("team_name", sa.Text, nullable=True),
        sa.Column("task_preview", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index("runs_session_idx", "runs", ["session_id", "started_at"])

    op.create_table(
        "executions",
        sa.Column("execution_id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("run_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_execution_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("executions.execution_id", ondelete="CASCADE"), nullable=True),
        sa.Column("agent_name", sa.Text, nullable=False),
        sa.Column("execution_type", sa.Text, nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index("executions_run_idx", "executions", ["run_id", "started_at"])
    op.create_index("executions_parent_idx", "executions", ["parent_execution_id"])

    op.create_table(
        "tool_calls",
        sa.Column("tool_call_id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("execution_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("executions.execution_id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("arguments", sa.JSON, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("tool_calls_execution_idx", "tool_calls", ["execution_id", "started_at"])

    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("tool_call_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("tool_calls.tool_call_id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("evidence_tool_call_idx", "evidence", ["tool_call_id"])
    op.create_index("evidence_content_hash_idx", "evidence", ["content_hash"])

    op.create_table(
        "claims",
        sa.Column("claim_id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("run_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("executions.execution_id", ondelete="SET NULL"), nullable=True),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="unverified"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("claims_run_idx", "claims", ["run_id"])

    op.create_table(
        "claim_evidence",
        sa.Column("claim_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("claims.claim_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("evidence_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("evidence.evidence_id", ondelete="CASCADE"), primary_key=True),
    )

    # Known-drift repair (see this file's own module docstring). IF NOT EXISTS
    # is supported by both dialects this project targets (Postgres and
    # SQLite), so this is a genuine no-op wherever 0001_baseline's own copy of
    # this index already ran for real, and the actual fix on an existing,
    # stamped-past-0001 installation.
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS task_outcome_queue_dedupe_idx "
        "ON task_outcome_queue (project_id, task_hash)"
    ))


def downgrade() -> None:
    op.drop_table("claim_evidence")
    op.drop_table("claims")
    op.drop_table("evidence")
    op.drop_table("tool_calls")
    op.drop_table("executions")
    op.drop_table("runs")
    # Deliberately does NOT drop task_outcome_queue_dedupe_idx -- it is the
    # CORRECT, intended shape of a table 0001_baseline (not this revision)
    # owns; a downgrade of this revision should not reintroduce known drift.
