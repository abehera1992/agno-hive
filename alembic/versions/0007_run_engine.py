"""runs.engine -- which execution engine ran this run (Phase 1.19)

Purely additive: one new NULLABLE text column on `runs`. Existing rows stay NULL, meaning "recorded before engine
tracking existed" (all of them were Team runs: the Workflow keeps its own durable ledger and writes no `runs` row).
New Team runs record "team". IF NOT EXISTS makes the migration safe to apply on a database where the column was added
by hand first (the same way 0006 was applied).

Revision ID: 0007_run_engine
Revises: 0006_run_ownership
"""
from alembic import op

revision = "0007_run_engine"
down_revision = "0006_run_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS engine")
