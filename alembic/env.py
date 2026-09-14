"""Alembic environment for agnohive's own app-storage database.

Reuses swarm.db.get_engine()/resolve_database_url() rather than building a
second, independent database connection mechanism -- the same engine
(including its StaticPool special-case for `sqlite:///:memory:` and its
PRAGMA foreign_keys=ON connect listener, see swarm/db.py's _build_engine())
that the application itself uses. This matters concretely for tests against
an in-memory SQLite database: a migration run through a SEPARATE engine would
apply to a different, unrelated `:memory:` database that vanishes the moment
that connection closes, leaving nothing for a test to inspect afterward.

target_metadata is swarm.db.metadata -- chat_sessions/session_messages/
failure_log/task_outcome_queue plus the Phase B durable-backbone tables (runs/
executions/tool_calls/evidence/claims/claim_evidence). Never the routing
metadata (model_catalog/team_role_models etc., swarm.db.routing_metadata) and
never any AGE/LightRAG table -- those are never declared in swarm.db.metadata
at all, so `alembic revision --autogenerate` can never propose DDL for them by
construction, not by convention.
"""
import asyncio
from logging.config import fileConfig

from alembic import context

import swarm.db as db

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = db.metadata


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql` style offline generation -- not part of
    this project's actual migration path (hive migrate always runs online
    against a real connection), kept only because it costs nothing and is the
    standard Alembic template shape."""
    context.configure(
        url=db.resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # db.get_engine() -- NOT a freshly constructed engine -- see this file's
    # own module docstring for why that distinction is load-bearing for
    # `:memory:` SQLite.
    connectable = db.get_engine()
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
