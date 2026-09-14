"""projects -- the normalized project/tenant ownership anchor for Phase J
(enterprise multi-tenancy, isolation & authorization)

Every existing table's project_id column (chat_sessions, failure_log,
task_outcome_queue, project_memory_promotions) has always been a bare
STRING convention with no row of its own to anchor to. This table gives
those strings a real parent, and a nullable tenant_id establishes the
"project -> tenant" chain Phase J's ownership model needs -- WITHOUT
retrofitting a tenant_id column onto every durable table.

Deliberately no ForeignKey added FROM any existing table TO this one:
those project_id columns predate this table and an existing installation
may already hold values with no corresponding row here. Adding a hard
constraint now would make upgrading destructive (any INSERT under an
unregistered project_id would then fail). Ownership is established and
verified at the application layer instead -- see swarm/db.py's own
comment on this table, and swarm/execution_store.py's ensure_project/
resolve_tenant_for_project/promote_session_claims/rehydrate_run.

Purely additive: one new, independent table. Nothing existing changes
shape.

Revision ID: 0005_projects
Revises: 0004_checkpoints
Create Date: (Phase J)
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_projects"
down_revision = "0004_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("projects")
