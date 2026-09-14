"""checkpoints -- durable Run execution-state checkpoints for Phase H
(cross-run continuity, checkpointing & execution rehydration)

RUN-owned, unlike 0003_project_memory_promotion's project_memory_promotions:
a checkpoint is prior EXECUTION STATE for one specific Run, not project
knowledge, so it DOES cascade-delete with its run (and therefore its
session) -- the opposite lifecycle choice from project_memory_promotions,
deliberately. See swarm/db.py's own comment on this table for the full
reasoning.

Genuinely new functionality (not an edit to an existing not-yet-deployed
revision), so it gets its own migration and history entry, following
0003's own precedent.

Revision ID: 0004_checkpoints
Revises: 0003_project_memory_promotion
Create Date: (Phase H)
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_checkpoints"
down_revision = "0003_project_memory_promotion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("run_id", sa.Text,
                  sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("last_execution_id", sa.Text,
                  sa.ForeignKey("executions.execution_id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_status", sa.Text, nullable=False),
        sa.Column("state_hash", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "sequence", name="checkpoints_run_sequence_uq"),
    )
    op.create_index("checkpoints_run_idx", "checkpoints", ["run_id", "sequence"])


def downgrade() -> None:
    op.drop_table("checkpoints")
