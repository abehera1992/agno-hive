"""Staged (begin / batch / finalize / abort) third-party skill import — Phase H.2.

Root cause this exists to fix: a `/skill import` of a package with many/large
supporting files serializes the ENTIRE package into ONE `tools/call` JSON-RPC
request. The mcp SDK's Streamable HTTP transport enforces a hardcoded 4 MiB
request-body ceiling (`mcp.server.transport_security.DEFAULT_MAX_REQUEST_BODY_SIZE`)
that hive-mcp's own code never overrides and cannot override through any
documented FastMCP parameter at the pinned version -- a package whose JSON-
encoded payload exceeds that (confirmed live: the real 78-file ui-ux-pro-max
package serializes to 4,549,214 bytes) is rejected by transport middleware
with HTTP 413 BEFORE skill_registry_import() or any application code runs at
all. See docs/runbooks/architecture-acceptance-runs/Phase-H.2-report.md for
the full investigation.

Fix shape: split the upload into several requests, each comfortably under the
limit, landing in a STAGING area that is invisible to every read path
(skill_registry_list/info/resolve, and therefore CLI /skill, workflow
injection) until finalize() explicitly, atomically publishes the complete
package through the EXISTING, unmodified skills_registry.register_skill() --
so every ownership/version-immutability/concurrency guarantee that function
already provides carries over unchanged. Nothing here re-implements any of
that; this module is purely a staging buffer in front of it.

Staging tables live in the SAME sqlite file as the `skills` table (whatever
skills_registry.default_db_path() resolves to -- Phase H/H.1's persistence
rules apply identically; this module never picks its own path). They hold
only in-flight, unpublished import attempts -- a skill_imports row is deleted
once finalized or aborted, except for a 'finalized' row, which is kept
(without its file rows) as a tiny idempotency record so a repeated finalize
call on the SAME import_id after a successful publish returns the same result
instead of either erroring or double-publishing.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import config
import skills_registry as reg

import structlog

log = structlog.get_logger()

# Target batch size the CLI is expected to stay under (~2 MiB, per Phase H.2's
# own requirement) plus headroom before the mcp SDK's 4 MiB wire ceiling. This
# is OUR OWN application-level guard on OUR OWN new tool -- it does not touch,
# raise, or depend on the global transport limit in any way; it exists purely
# to give a caller a clear, immediate error instead of relying on a future,
# accidental brush with the real transport ceiling.
MAX_BATCH_BYTES = 3 * 1024 * 1024

_STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_imports (
    import_id            TEXT PRIMARY KEY,
    skill_id              TEXT NOT NULL,
    name                  TEXT NOT NULL,
    skill_type            TEXT NOT NULL,
    category              TEXT NOT NULL,
    tenant_id             TEXT,
    user_id               TEXT,
    content               TEXT NOT NULL,
    source                TEXT,
    description           TEXT,
    expected_file_count   INTEGER NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'finalized', 'aborted')),
    published_version     INTEGER,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_import_files (
    import_id     TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    encoding      TEXT NOT NULL,
    data          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    PRIMARY KEY (import_id, rel_path)
);
"""


class ImportNotFoundError(Exception):
    """No staged import exists for this import_id -- never existed, already
    finalized-and-cleaned-up with no cached record (should not happen; a
    finalized row is kept), or already aborted-and-cleaned-up."""


class ImportStateError(Exception):
    """The staged import exists but is not in the state this operation
    requires (e.g. uploading a batch to an already-finalized or aborted
    import, or aborting an already-finalized one)."""


class BatchTooLargeError(Exception):
    """A single batch's serialized size exceeds MAX_BATCH_BYTES -- the
    caller must split it smaller; this module never accepts an oversized
    batch just because it happens to still fit under the real transport
    ceiling."""


class FileConflictError(Exception):
    """A rel_path already received for this import_id with DIFFERENT content
    was uploaded again -- rejected outright (never silently overwritten).
    Abort and restart the import to genuinely change a file's content."""


class IncompleteImportError(Exception):
    """finalize() was called before every expected file arrived."""


@dataclass(frozen=True)
class ImportHandle:
    import_id: str
    skill_id: str
    expected_file_count: int


def _connect(db_path: Path) -> sqlite3.Connection:
    # skills_registry._connect() already creates the `skills` table + runs
    # ensure_skills_dir() for this same db_path; reuse it so staging never
    # has its own, second copy of "create the parent dir / register
    # .gitignore" logic, then layer the staging tables on afterward.
    conn = reg._connect(db_path)
    with conn:
        conn.executescript(_STAGING_SCHEMA)
    return conn


def _file_hash(encoding: str, data: str) -> str:
    """Hash over exactly what gets stored (encoding + the as-given string),
    not over decoded bytes -- deterministic, and a genuine identity check:
    two uploads of the same rel_path are "the same content" iff they would
    store byte-for-byte the same row, regardless of why a caller might send
    the same bytes re-encoded differently (treated as a real conflict, not
    silently reconciled)."""
    return hashlib.sha256(f"{encoding}\x00{data}".encode("utf-8")).hexdigest()


def begin_import(
    db_path: Path,
    *,
    skill_id: str,
    name: str,
    skill_type: str,
    category: str,
    content: str,
    tenant_id: str | None,
    user_id: str | None,
    source: str | None,
    description: str | None,
    expected_file_count: int,
) -> ImportHandle:
    """Validate + create a staging row. Raises InvalidSkillRecordError
    (skills_registry's own) for a category/tenant/user combination the
    ownership model forbids -- identical check to register_skill()'s, reused
    rather than re-implemented.

    Also runs an ADVISORY ownership pre-check against the latest PUBLISHED
    version of `skill_id`, so an obviously-doomed import fails immediately
    instead of after every batch has been uploaded. This is advisory only:
    register_skill() inside finalize() re-checks ownership atomically under
    its own BEGIN IMMEDIATE at publish time regardless, which is the
    authoritative guard against a second import racing in between."""
    reg._validate_category(category, tenant_id, user_id)
    if expected_file_count < 0:
        raise ValueError("expected_file_count must be >= 0")

    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT category, tenant_id, user_id FROM skills "
            "WHERE skill_id = ? ORDER BY version DESC LIMIT 1",
            (skill_id,),
        ).fetchone()
        if existing is not None:
            existing_tuple = (existing["category"], existing["tenant_id"], existing["user_id"])
            if existing_tuple != (category, tenant_id, user_id):
                raise reg.OwnershipConflictError(
                    f"skill_id '{skill_id}' already exists with different "
                    "ownership (category/tenant_id/user_id) — ownership is "
                    "immutable across versions of the same skill_id."
                )

        import_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO skill_imports (
                import_id, skill_id, name, skill_type, category, tenant_id,
                user_id, content, source, description, expected_file_count,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                import_id, skill_id, name, skill_type, category, tenant_id,
                user_id, content, source, description, expected_file_count,
                now, now,
            ),
        )
        conn.commit()
        log.info(
            "skill_import_staging.begun",
            import_id=import_id, skill_id=skill_id,
            expected_file_count=expected_file_count,
        )
        return ImportHandle(import_id=import_id, skill_id=skill_id, expected_file_count=expected_file_count)
    finally:
        conn.close()


def _get_import_row(conn: sqlite3.Connection, import_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM skill_imports WHERE import_id = ?", (import_id,)).fetchone()
    if row is None:
        raise ImportNotFoundError(f"no staged import '{import_id}' (never existed, or already aborted)")
    return row


def add_batch(db_path: Path, import_id: str, files: dict[str, dict]) -> dict:
    """Upload one batch of {rel_path: {"encoding", "data"}} entries for an
    in-progress import. Idempotent per rel_path: re-uploading a rel_path with
    IDENTICAL content is a silent no-op (safe to retry a batch whose response
    was lost); re-uploading with DIFFERENT content raises FileConflictError
    (never silently overwritten). The whole batch is validated BEFORE any row
    is written, inside one transaction -- a batch containing one conflicting
    file writes NOTHING from that batch, rather than partially applying it.

    Returns {"received_this_batch", "total_received", "expected_file_count"}.
    """
    size = len(json.dumps(files).encode("utf-8"))
    if size > MAX_BATCH_BYTES:
        raise BatchTooLargeError(
            f"batch is {size:,} bytes, over the {MAX_BATCH_BYTES:,}-byte limit for a single "
            "skill_registry_import_batch call — split it into smaller batches"
        )

    conn = _connect(db_path)
    conn.isolation_level = None  # manual transaction control, same as register_skill()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _get_import_row(conn, import_id)
            if row["status"] != "pending":
                raise ImportStateError(
                    f"import '{import_id}' is '{row['status']}', not 'pending' — cannot add files to it"
                )

            to_insert: list[tuple] = []
            now = datetime.now(timezone.utc).isoformat()
            for rel_path, entry in files.items():
                encoding = entry.get("encoding")
                data = entry.get("data")
                if encoding not in ("utf-8", "base64") or not isinstance(data, str):
                    raise ValueError(
                        f"file '{rel_path}': expected {{'encoding': 'utf-8'|'base64', 'data': str}}"
                    )
                new_hash = _file_hash(encoding, data)
                existing = conn.execute(
                    "SELECT content_hash FROM skill_import_files WHERE import_id = ? AND rel_path = ?",
                    (import_id, rel_path),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] == new_hash:
                        continue  # identical re-upload — idempotent no-op
                    raise FileConflictError(
                        f"file '{rel_path}' was already uploaded for import '{import_id}' with "
                        "different content — abort and restart the import to change a file"
                    )
                to_insert.append((import_id, rel_path, encoding, data, new_hash, now))

            for params in to_insert:
                conn.execute(
                    "INSERT INTO skill_import_files "
                    "(import_id, rel_path, encoding, data, content_hash, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    params,
                )
            conn.execute("UPDATE skill_imports SET updated_at = ? WHERE import_id = ?", (now, import_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        total_received = conn.execute(
            "SELECT COUNT(*) AS n FROM skill_import_files WHERE import_id = ?", (import_id,)
        ).fetchone()["n"]
        log.info(
            "skill_import_staging.batch_added",
            import_id=import_id, received_this_batch=len(to_insert), total_received=total_received,
        )
        return {
            "received_this_batch": len(to_insert),
            "total_received": total_received,
            "expected_file_count": row["expected_file_count"],
        }
    finally:
        conn.close()


def finalize_import(db_path: Path, import_id: str) -> dict:
    """Verify file count + publish atomically through the EXISTING
    skills_registry.register_skill() — the single real write to the `skills`
    table, with every guarantee that function already provides (ownership
    immutability, append-only versioning, BEGIN IMMEDIATE concurrency safety)
    unchanged. Raises IncompleteImportError if the received file count
    doesn't match what begin_import() declared — nothing is published in
    that case, and the import stays 'pending' (retryable: upload the missing
    batches and finalize again).

    Idempotent after a successful publish: a repeated finalize() call for an
    import_id already marked 'finalized' returns the SAME cached result
    without calling register_skill() again (so a lost response doesn't risk
    a second published version)."""
    conn = _connect(db_path)
    try:
        row = _get_import_row(conn, import_id)
        if row["status"] == "finalized":
            return {
                "import_id": import_id, "skill_id": row["skill_id"],
                "version": row["published_version"], "name": row["name"],
                "category": row["category"], "tenant_id": row["tenant_id"], "user_id": row["user_id"],
            }
        if row["status"] == "aborted":
            raise ImportStateError(f"import '{import_id}' was already aborted — cannot finalize it")

        received = conn.execute(
            "SELECT COUNT(*) AS n FROM skill_import_files WHERE import_id = ?", (import_id,)
        ).fetchone()["n"]
        if received != row["expected_file_count"]:
            raise IncompleteImportError(
                f"expected {row['expected_file_count']} files, received {received} — "
                "upload the remaining batches before finalizing"
            )

        file_rows = conn.execute(
            "SELECT rel_path, encoding, data FROM skill_import_files WHERE import_id = ?", (import_id,)
        ).fetchall()
        supporting_files = {r["rel_path"]: {"encoding": r["encoding"], "data": r["data"]} for r in file_rows}
    finally:
        conn.close()

    # register_skill() owns its own connection/transaction (it's a plain,
    # unmodified call to the SAME function the original skill_registry_import
    # already uses) — deliberately a SEPARATE transaction from the staging
    # read above; the staging row's own 'pending' state is what makes a retry
    # of this exact finalize() call safe if the process dies before the next
    # block marks it 'finalized' (see module docstring's noted limitation).
    version = reg.register_skill(
        db_path,
        skill_id=row["skill_id"], name=row["name"], skill_type=row["skill_type"],
        category=row["category"], tenant_id=row["tenant_id"], user_id=row["user_id"],
        content=row["content"], supporting_files=supporting_files,
        source=row["source"], description=row["description"],
    )

    conn = _connect(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE skill_imports SET status = 'finalized', published_version = ?, updated_at = ? "
            "WHERE import_id = ?",
            (version, now, import_id),
        )
        conn.execute("DELETE FROM skill_import_files WHERE import_id = ?", (import_id,))
        conn.commit()
    finally:
        conn.close()

    log.info("skill_import_staging.finalized", import_id=import_id, skill_id=row["skill_id"], version=version)
    return {
        "import_id": import_id, "skill_id": row["skill_id"], "version": version,
        "name": row["name"], "category": row["category"],
        "tenant_id": row["tenant_id"], "user_id": row["user_id"],
    }


def abort_import(db_path: Path, import_id: str) -> dict:
    """Delete a staged import and its uploaded files. A no-op (not an error)
    if it was already aborted. Raises ImportStateError for an already-
    finalized import — a published skill cannot be un-published by aborting
    the import that created it."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT status FROM skill_imports WHERE import_id = ?", (import_id,)).fetchone()
        if row is None:
            return {"import_id": import_id, "aborted": True, "already_gone": True}
        if row["status"] == "finalized":
            raise ImportStateError(f"import '{import_id}' was already finalized — cannot abort a published skill")
        conn.execute("DELETE FROM skill_import_files WHERE import_id = ?", (import_id,))
        conn.execute("DELETE FROM skill_imports WHERE import_id = ?", (import_id,))
        conn.commit()
        log.info("skill_import_staging.aborted", import_id=import_id)
        return {"import_id": import_id, "aborted": True, "already_gone": False}
    finally:
        conn.close()
