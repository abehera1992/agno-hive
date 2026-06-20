"""Migration runner — review-gated Alembic migration execution.

Lets hive APPLY an Alembic migration (DDL + seed) when explicitly asked. Gated two ways:
  1. The agent calls run_migration ONLY when the task tells it to (it never auto-runs migrations).
  2. WRITE_REVIEW stages it as action_pending — a human confirms (CLI / POST /actions/confirm)
     before it executes.

On confirm it runs `alembic <direction> <revision>` ONLINE inside the service container, connecting
as the DB OWNER — so DDL is permitted and op.bulk_insert seed rows are applied (online mode), unlike
alembic offline `--sql` which silently drops bulk_insert.

Activated by MIGRATIONS_ENABLED=true. Needs the host docker socket (already mounted) + config:
  MIGRATION_DB_CONTAINER, MIGRATION_DB_OWNER, MIGRATION_DB_NAME, MIGRATION_DB_HOST, MIGRATION_DB_PORT,
  MIGRATION_SERVICES (JSON {"<service-name>": "<service-container>"}).
The owner password is read from the db container at run time and is never logged or returned.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from config import WRITE_REVIEW
from tools.integrations import _stage_action, register_executor

_REV_RE = re.compile(r"^[A-Za-z0-9_:+\-]+$")  # alembic ids: hex, names, "head", "+1", "<from>:<to>"


def _services() -> dict:
    """Parse MIGRATION_SERVICES ("name:container,name:container") into a dict."""
    out: dict = {}
    for pair in (config.MIGRATION_SERVICES or "").split(","):
        pair = pair.strip()
        if ":" in pair:
            name, container = pair.split(":", 1)
            out[name.strip()] = container.strip()
    return out


def _owner_password() -> str:
    """Read the DB owner/superuser password from the db container at run time (never stored)."""
    out = subprocess.run(
        ["docker", "exec", config.MIGRATION_DB_CONTAINER, "printenv", "POSTGRES_PASSWORD"],
        capture_output=True, text=True, timeout=15,
    )
    return out.stdout.strip()


def _execute(tool: str, args: dict) -> str:
    if tool != "run":
        return f"unknown migration tool: {tool}"
    try:
        service   = args["service"]
        revision  = str(args.get("revision", "head"))
        direction = str(args.get("direction", "upgrade"))

        container = _services().get(service)
        if not container:
            return f"migration failed: unknown service '{service}' (configure MIGRATION_SERVICES)"
        if direction not in ("upgrade", "downgrade"):
            return "migration failed: direction must be 'upgrade' or 'downgrade'"
        if not _REV_RE.match(revision):
            return f"migration failed: invalid revision '{revision}'"
        if not (config.MIGRATION_DB_CONTAINER and config.MIGRATION_DB_OWNER and config.MIGRATION_DB_NAME):
            return "migration failed: MIGRATION_DB_CONTAINER/OWNER/NAME not configured"

        pw = _owner_password()
        if not pw:
            return "migration failed: could not read DB owner password from the db container"
        owner_url = (
            f"postgresql://{config.MIGRATION_DB_OWNER}:{pw}"
            f"@{config.MIGRATION_DB_HOST}:{config.MIGRATION_DB_PORT}/{config.MIGRATION_DB_NAME}"
        )

        # Pass the owner URL via STDIN (not argv) so the password never appears in `ps`/`docker inspect`.
        sh = f'read -r U; export POSTGRES_URL="$U"; exec alembic {direction} {revision}'
        proc = subprocess.run(
            ["docker", "exec", "-i", container, "sh", "-c", sh],
            input=owner_url + "\n", capture_output=True, text=True, timeout=300,
        )
        out = (proc.stdout + proc.stderr).strip()
        out = out.replace(owner_url, "<owner-url>").replace(pw, "***")  # redact, just in case
        status = "ok" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
        return f"alembic {direction} {revision} on {service}: {status}\n{out[-2000:]}"
    except subprocess.TimeoutExpired:
        return "migration failed: timed out after 300s"
    except Exception as e:
        return f"migration execute failed: {e}"


register_executor("migration", _execute)


def run_migration(service: str, revision: str = "head", direction: str = "upgrade") -> str:
    """
    Apply an Alembic migration for a service — REVIEW-GATED. Use this ONLY when the task explicitly
    asks you to run / apply a migration. Otherwise just write the migration file and report it as
    staged — do NOT run it.

    Runs `alembic <direction> <revision>` inside the service's container as the DB OWNER (so DDL is
    permitted and op.bulk_insert seed rows are applied — online mode). The write is staged for human
    approval when WRITE_REVIEW is enabled; it executes only after the human confirms.

    Args:
        service:   service name configured in MIGRATION_SERVICES (e.g. "inventory-service").
        revision:  target revision id, "head" (default), or a "<from>:<to>" range.
        direction: "upgrade" (default) or "downgrade".
    """
    summary = f"alembic {direction} {revision} on {service} (as DB owner)"
    args = {"service": service, "revision": revision, "direction": direction}
    if WRITE_REVIEW:
        return _stage_action("migration", "run", summary, args)
    return _execute("run", args)
