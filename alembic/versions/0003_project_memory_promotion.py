"""project memory promotion -- project_memory_promotions

Phase F of the durable-memory architecture: a PROJECT-owned table, deliberately
outside the session-owned tree 0002_durable_backbone created (chat_sessions ->
runs -> executions/claims -> tool_calls/evidence/claim_evidence). A promoted
memory must survive session deletion, so this table has NO ForeignKey into
that tree at all -- claim_id/run_id/execution_id are plain reference copies,
and statement/evidence_snapshot/evidence_hash are snapshots taken at promotion
time. See swarm/db.py's own comment on this table for the full reasoning.

project_id is a bare Text scope key, exactly like failure_log.project_id/
task_outcome_queue.project_id (0001_baseline) -- there is no `projects` table
in this schema to foreign-key into.

This is a genuinely NEW migration, not an edit to 0001/0002 in place: unlike
those two (which were fixing defects in not-yet-deployed revisions), this adds
new functionality on top of an already-accepted schema, so it gets its own
revision and history entry per standard practice.

Revision ID: 0003_project_memory_promotion
Revises: 0002_durable_backbone
Create Date: (Phase F)
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_project_memory_promotion"
down_revision = "0002_durable_backbone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_memory_promotions",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("claim_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("run_id", sa.Text, nullable=True),
        sa.Column("execution_id", sa.Text, nullable=True),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("claim_status", sa.Text, nullable=False),
        sa.Column("evidence_snapshot", sa.Text, nullable=True),
        sa.Column("evidence_hash", sa.Text, nullable=True),
        sa.Column("validated_by", sa.Text, nullable=False),
        sa.Column("feedback_notes", sa.Text, nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "claim_id",
                             name="project_memory_promotions_dedupe_uq"),
    )
    op.create_index("project_memory_promotions_project_idx",
                     "project_memory_promotions", ["project_id", "promoted_at"])


def downgrade() -> None:
    op.drop_table("project_memory_promotions")
