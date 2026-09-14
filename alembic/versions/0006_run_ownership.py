"""run ownership -- runs.owner_worker_id/owner_lease_expires_at for Phase K
(distributed execution & concurrency safety)

Purely additive: two new NULLABLE columns on the existing `runs` table
(0002_durable_backbone). Every existing row gets NULL/NULL ("unowned"),
identical in meaning to before this migration existed -- no backfill, no
destructive change, no new table. See swarm/db.py's own comment on these
two columns, and swarm/execution_store.py's acquire_run_ownership/
release_run_ownership for the atomic UPDATE...WHERE mechanism that uses
them (a single statement, portable across SQLite and PostgreSQL, no
in-process lock and no queue/broker).

Revision ID: 0006_run_ownership
Revises: 0005_projects
Create Date: (Phase K)
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_run_ownership"
down_revision = "0005_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("owner_worker_id", sa.Text, nullable=True))
    op.add_column("runs", sa.Column("owner_lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "owner_lease_expires_at")
    op.drop_column("runs", "owner_worker_id")
