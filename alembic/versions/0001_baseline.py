"""baseline -- the four existing Hive app-storage tables

Represents chat_sessions/session_messages/failure_log/task_outcome_queue
EXACTLY as swarm/db.py already declares them (verified column-by-column
against a live ZGX deployment during the accepted architecture review --
no drift found except the task_outcome_queue_dedupe_idx index, which was
confirmed MISSING there; that repair lives in 0002, not here, since this
revision represents the intended baseline shape, and existing installations
are expected to be STAMPED past this revision rather than have its DDL
executed against them -- see docs/guide/migrations.md).

Existing installation: `alembic stamp 0001_baseline` (after confirming the
live schema actually matches what create() below declares -- stamping does
NOT verify this; see hive migrate's own validation step).

Fresh installation: this revision runs for real as part of `alembic upgrade
head`.

Revision ID: 0001_baseline
Revises:
Create Date: (Phase B)
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("persist", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("summary_through", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current_leaf_id", sa.Integer, nullable=True),
    )
    op.create_index(
        "chat_sessions_project_idx", "chat_sessions",
        ["project_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "session_messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Uuid(as_uuid=False),
                  sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("parent_message_id", sa.Integer,
                  sa.ForeignKey("session_messages.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index(
        "session_messages_session_idx", "session_messages",
        ["session_id", "created_at"],
    )
    op.create_index(
        "session_messages_parent_idx", "session_messages", ["parent_message_id"],
    )

    op.create_table(
        "failure_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("task", sa.Text, nullable=False),
        sa.Column("error_type", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("error_message", sa.Text, nullable=False, server_default=""),
        sa.Column("agent", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("rejected_output", sa.Text, nullable=True),
        sa.Column("corrected_output", sa.Text, nullable=True),
    )
    op.create_index(
        "failure_log_project_idx", "failure_log",
        ["project_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "task_outcome_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("task", sa.Text, nullable=False),
        sa.Column("result", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=True),
        # NOT server_default: swarm/db.py declares these as Python-side
        # `default=` (applied by SQLAlchemy Core on INSERT, never part of the
        # DDL), and the live ZGX database confirms neither column carries a
        # DB-level default (its own `\d task_outcome_queue` shows a blank
        # Default for both). A server_default here would be new, accidental
        # database-level semantics this baseline must not introduce -- if a
        # DB-level default is wanted later, that belongs in its own migration,
        # not silently folded into the baseline.
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("task_hash", sa.Text, nullable=True),
        sa.Column("doc_path", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index(
        "task_outcome_queue_pending_idx", "task_outcome_queue",
        ["status", "created_at"],
    )
    # This IS the intended baseline shape, so the index is declared here for a
    # FRESH install, where this revision's DDL actually executes. An EXISTING
    # installation is stamped PAST this revision instead (its DDL never runs
    # there), and is confirmed (accepted architecture review, live ZGX check)
    # to be MISSING this exact index -- 0002_durable_backbone separately
    # repairs that via an idempotent `CREATE UNIQUE INDEX IF NOT EXISTS`, so
    # both a fresh install (via this line) and an existing one (via 0002's
    # repair) end up with the index either way.
    op.create_index(
        "task_outcome_queue_dedupe_idx", "task_outcome_queue",
        ["project_id", "task_hash"], unique=True,
    )


def downgrade() -> None:
    op.drop_table("task_outcome_queue")
    op.drop_table("failure_log")
    op.drop_table("session_messages")
    op.drop_table("chat_sessions")
