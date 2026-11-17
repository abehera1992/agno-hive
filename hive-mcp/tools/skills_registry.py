"""Persistent skills registry (Phase 1.57a) — SQLite-backed ownership model for
AGNOHive skills, independent of the filesystem catalog tools/skills.py already
serves to agents.

Deliberately NOT wired into tools/skills.py's list_skills()/load_skill() yet.
Those two MCP tools keep serving the existing hive-mcp/skills/ +
PROJECT_ROOT/skills/ filesystem catalog exactly as before — this module adds a
SEPARATE, persistent store with real ownership, meant for a later phase (CLI
/skill, request forwarding, workflow injection — all explicitly out of scope
here) to read from. This phase's job is the schema, the ownership enforcement,
and the identity seam (tools/skills_identity.py) it is resolved through — not
making anything live use it yet.

Exactly two categories, enforced both by a SQLite CHECK constraint and by this
module's own Python validation before every INSERT (defense in depth — the
CHECK constraint is the real backstop for any future direct-SQL caller that
skips this module's functions; the Python check gives a clear exception
instead of a raw sqlite3.IntegrityError):

  platform     -- tenant_id IS NULL AND user_id IS NULL. Reachable by every
                  identity, always, independent of tenant/user.
  third_party  -- tenant_id IS NOT NULL AND user_id IS NOT NULL. Reachable
                  ONLY by the exact (tenant_id, user_id) pair that owns it.

Versioning is append-only: register_skill() always INSERTs a new row at
MAX(version)+1 for that skill_id, never UPDATEs or DELETEs an existing row —
"do not silently overwrite historical versions" per Phase 1.57a's own
instructions. (skill_id, version) is the table's composite primary key, which
also makes a silent overwrite structurally impossible (a repeat INSERT at the
same version would raise sqlite3.IntegrityError, not silently replace a row).

Security note on "unauthorized vs missing": get_third_party_skill() raises the
EXACT SAME SkillNotFoundError, with the exact same message shape, whether a
skill_id does not exist at all, exists for a DIFFERENT (tenant_id, user_id)
pair, or exists but is disabled. This is deliberate — a caller asking for
Tenant B's skill while authenticated as Tenant A must not be able to
distinguish "that skill doesn't exist" from "that skill exists but isn't
yours" from the error alone; either response would leak the existence of
other tenants' data.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))         # tools/ itself -- sibling bare import below
sys.path.insert(0, str(Path(__file__).parent.parent))  # hive-mcp/ -- `import config`
import config
from skills_identity import Identity

import structlog

log = structlog.get_logger()

_SKILLS_DIRNAME = ".hive_skills"
_DB_FILENAME = "skills_registry.db"
_GITIGNORE_COMMENT = "# hive-mcp persistent skills registry (Phase 1.57a) — machine-local, not source"
_GITIGNORE_ENTRY = f"{_SKILLS_DIRNAME}/"

PLATFORM = "platform"
THIRD_PARTY = "third_party"
_VALID_CATEGORIES = (PLATFORM, THIRD_PARTY)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id         TEXT NOT NULL,
    version          INTEGER NOT NULL,
    name             TEXT NOT NULL,
    skill_type       TEXT NOT NULL,
    category         TEXT NOT NULL CHECK (category IN ('platform', 'third_party')),
    tenant_id        TEXT,
    user_id          TEXT,
    content          TEXT NOT NULL,
    supporting_files TEXT,
    source           TEXT,
    description      TEXT,
    content_hash     TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    approved         INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (skill_id, version),
    CHECK (
        (category = 'platform'    AND tenant_id IS NULL     AND user_id IS NULL)
        OR
        (category = 'third_party' AND tenant_id IS NOT NULL AND user_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skills_tenant_user ON skills(tenant_id, user_id);
"""


class SkillNotFoundError(Exception):
    """Missing, wrong-owner, or disabled — all look identical from outside this
    module on purpose. See module docstring's security note."""


class InvalidSkillRecordError(Exception):
    """Raised by register_skill() for a category/tenant_id/user_id combination
    that would violate the ownership model, BEFORE hitting the database — a
    clear Python exception instead of a raw sqlite3.IntegrityError from the
    CHECK constraint, which is the backstop, not the primary guard."""


class OwnershipConflictError(Exception):
    """Raised by register_skill() (Phase 1.57a.1) when a new version of an
    EXISTING skill_id would change its logical ownership — a different
    category, a different tenant_id, or a different user_id than every prior
    version of that same skill_id already carries. Ownership is fixed the
    moment a skill_id's first version is registered and is immutable for
    every version after that; this is the enforcement point.

    Deliberately does not include the existing owner's actual tenant_id/
    user_id in the message — the caller attempting the conflicting
    registration has no legitimate need to learn who the real owner is,
    matching this module's existing "don't leak cross-tenant existence/
    ownership info through an error message" posture (see SkillNotFoundError's
    own docstring)."""


@dataclass(frozen=True)
class SkillRecord:
    skill_id: str
    version: int
    name: str
    skill_type: str
    category: str
    tenant_id: str | None
    user_id: str | None
    content: str
    supporting_files: dict
    source: str | None
    description: str | None
    content_hash: str
    enabled: bool
    approved: bool
    created_at: str
    updated_at: str


def _row_to_record(row: sqlite3.Row) -> SkillRecord:
    return SkillRecord(
        skill_id=row["skill_id"],
        version=row["version"],
        name=row["name"],
        skill_type=row["skill_type"],
        category=row["category"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        content=row["content"],
        supporting_files=json.loads(row["supporting_files"]) if row["supporting_files"] else {},
        source=row["source"],
        description=row["description"] if "description" in row.keys() else None,
        content_hash=row["content_hash"],
        enabled=bool(row["enabled"]),
        approved=bool(row["approved"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def default_db_path() -> Path:
    """HIVE_SKILLS_DB_PATH if explicitly set, otherwise a dedicated
    PROJECT_ROOT/.hive_skills/skills_registry.db — persists across a container
    restart because PROJECT_ROOT is the host-mounted project volume, with no
    compose/volume change needed. Mirrors tools/scratch.py's identical
    "dedicated dot-directory under PROJECT_ROOT" precedent."""
    if config.HIVE_SKILLS_DB_PATH:
        return Path(config.HIVE_SKILLS_DB_PATH)
    return config.PROJECT_ROOT / _SKILLS_DIRNAME / _DB_FILENAME


def ensure_skills_dir(db_path: Path) -> None:
    """Create db_path's parent dir if missing, and self-register a .gitignore
    entry for it under PROJECT_ROOT the first time — same best-effort pattern
    as tools/scratch.py's ensure_scratch_dir(). Skipped entirely when db_path
    was overridden outside PROJECT_ROOT (e.g. an isolated test database), since
    there is then no reason to touch the project's .gitignore at all."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if config.PROJECT_ROOT not in db_path.parents:
            return
    except Exception:
        return
    gitignore = config.PROJECT_ROOT / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if _GITIGNORE_ENTRY not in existing:
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            with gitignore.open("a", encoding="utf-8") as f:
                f.write(f"{sep}{_GITIGNORE_COMMENT}\n{_GITIGNORE_ENTRY}\n")
    except OSError:
        pass


def _widen_schema_if_needed(conn: sqlite3.Connection) -> None:
    """Introspect-then-ALTER for columns added after a database may already
    exist, same pattern (and same reason) as swarm/db.py's
    _existing_team_role_models_columns/_TEAM_ROLE_MODELS_NEW_COLUMNS: a plain
    CREATE TABLE IF NOT EXISTS never widens an existing table, and a blind
    ALTER would fail loudly on a database that already has the column. In
    practice every database this registry has ever created already has
    `description` (Phase 1.57b added it to _SCHEMA before any real/persisted
    database existed — see Phase 1.57a/1.57a.1 reports), but this stays as the
    correct defensive handling for any database created before this change."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
    if "description" not in cols:
        conn.execute("ALTER TABLE skills ADD COLUMN description TEXT")


def _connect(db_path: Path) -> sqlite3.Connection:
    ensure_skills_dir(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.executescript(_SCHEMA)
        _widen_schema_if_needed(conn)
    return conn


def _validate_category(category: str, tenant_id: str | None, user_id: str | None) -> None:
    if category not in _VALID_CATEGORIES:
        raise InvalidSkillRecordError(
            f"category must be one of {_VALID_CATEGORIES!r}, got {category!r}"
        )
    if category == PLATFORM and (tenant_id is not None or user_id is not None):
        raise InvalidSkillRecordError(
            "category='platform' requires tenant_id=None and user_id=None"
        )
    if category == THIRD_PARTY and (not tenant_id or not user_id):
        raise InvalidSkillRecordError(
            "category='third_party' requires both tenant_id and user_id"
        )


def _content_hash(content: str, supporting_files: dict) -> str:
    payload = content + "\x00" + json.dumps(supporting_files, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def generate_skill_id(name: str) -> str:
    """Derive a stable skill_id from a human-readable name (Phase 1.57b) —
    lowercase, non [a-z0-9-] runs collapsed to a single '-', leading/trailing
    '-' stripped. Falls back to a short random id if the name has no usable
    characters at all (e.g. a name that is pure emoji/punctuation), so this
    never returns an empty string. Centralized here, not duplicated by the
    CLI or the import tool, so "stable skill_id" has exactly one definition."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or f"skill-{uuid.uuid4().hex[:8]}"


def register_skill(
    db_path: Path,
    *,
    skill_id: str,
    name: str,
    skill_type: str,
    category: str,
    content: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    supporting_files: dict | None = None,
    source: str | None = None,
    description: str | None = None,
    approved: bool = False,
) -> int:
    """Insert a new version of `skill_id` and return the version number
    assigned. Never overwrites an existing version — always MAX(version)+1
    (1 for a brand-new skill_id). Raises InvalidSkillRecordError up front for
    a category/tenant_id/user_id combination the ownership model forbids,
    before any database write is attempted.

    Raises OwnershipConflictError (Phase 1.57a.1) if `skill_id` already has at
    least one version and this call's (category, tenant_id, user_id) differs
    from it — ownership is fixed at a skill_id's first version and immutable
    across every version after that, including platform<->third_party
    transitions (caught because category is part of the compared tuple).

    Concurrency: the ownership read and the version-number read happen inside
    one BEGIN IMMEDIATE transaction, which takes SQLite's write (RESERVED)
    lock up front rather than only at the final INSERT/COMMIT. A second,
    concurrent register_skill() call for the SAME skill_id blocks (up to the
    connection's busy timeout) until the first call's transaction commits or
    rolls back — it can never read ownership from a half-committed state or
    race the first call to decide the next version number. This is what makes
    the invariant hold under concurrency, not merely under a single caller:
    whichever call's transaction commits first permanently fixes (or
    reconfirms) the skill_id's ownership before the second call's own
    ownership check ever reads from the table."""
    _validate_category(category, tenant_id, user_id)
    supporting_files = supporting_files or {}
    now = datetime.now(timezone.utc).isoformat()
    content_hash = _content_hash(content, supporting_files)

    conn = _connect(db_path)
    conn.isolation_level = None  # manual transaction control (see docstring)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                """
                SELECT category, tenant_id, user_id FROM skills
                WHERE skill_id = ? ORDER BY version DESC LIMIT 1
                """,
                (skill_id,),
            ).fetchone()
            if existing is not None:
                existing_tuple = (existing["category"], existing["tenant_id"], existing["user_id"])
                new_tuple = (category, tenant_id, user_id)
                if existing_tuple != new_tuple:
                    raise OwnershipConflictError(
                        f"skill_id '{skill_id}' already exists with different "
                        "ownership (category/tenant_id/user_id) — ownership is "
                        "immutable across versions of the same skill_id."
                    )
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version FROM skills WHERE skill_id = ?",
                (skill_id,),
            ).fetchone()
            next_version = row["max_version"] + 1
            conn.execute(
                """
                INSERT INTO skills (
                    skill_id, version, name, skill_type, category,
                    tenant_id, user_id, content, supporting_files, source,
                    description, content_hash, enabled, approved, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    skill_id, next_version, name, skill_type, category,
                    tenant_id, user_id, content, json.dumps(supporting_files), source,
                    description, content_hash, int(approved), now, now,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        log.info(
            "skills_registry.registered",
            skill_id=skill_id, version=next_version, category=category,
            tenant_id=tenant_id, user_id=user_id,
        )
        return next_version
    finally:
        conn.close()


def _fetch_latest(
    conn: sqlite3.Connection, *, category: str, skill_id: str,
    tenant_id: str | None, user_id: str | None, version: int | None,
) -> sqlite3.Row | None:
    if version is not None:
        return conn.execute(
            """
            SELECT * FROM skills
            WHERE skill_id = ? AND version = ? AND category = ?
              AND tenant_id IS ? AND user_id IS ?
            """,
            (skill_id, version, category, tenant_id, user_id),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM skills
        WHERE skill_id = ? AND category = ? AND tenant_id IS ? AND user_id IS ?
        ORDER BY version DESC LIMIT 1
        """,
        (skill_id, category, tenant_id, user_id),
    ).fetchone()


def get_platform_skill(db_path: Path, skill_id: str, version: int | None = None) -> SkillRecord:
    conn = _connect(db_path)
    try:
        row = _fetch_latest(
            conn, category=PLATFORM, skill_id=skill_id,
            tenant_id=None, user_id=None, version=version,
        )
        if row is None or not row["enabled"]:
            raise SkillNotFoundError(f"platform skill '{skill_id}' not found")
        return _row_to_record(row)
    finally:
        conn.close()


def list_platform_skills(db_path: Path) -> list[SkillRecord]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.* FROM skills s
            JOIN (
                SELECT skill_id, MAX(version) AS max_version FROM skills
                WHERE category = 'platform' GROUP BY skill_id
            ) m ON s.skill_id = m.skill_id AND s.version = m.max_version
            WHERE s.category = 'platform' AND s.enabled = 1
            ORDER BY s.skill_id
            """
        ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def get_third_party_skill(
    db_path: Path, identity: Identity, skill_id: str, version: int | None = None,
) -> SkillRecord:
    """Raises SkillNotFoundError — identical message whether the skill doesn't
    exist, belongs to a different (tenant_id, user_id), or is disabled. See
    module docstring's security note."""
    if not identity.tenant_id or not identity.user_id:
        raise SkillNotFoundError(f"third-party skill '{skill_id}' not found")
    conn = _connect(db_path)
    try:
        row = _fetch_latest(
            conn, category=THIRD_PARTY, skill_id=skill_id,
            tenant_id=identity.tenant_id, user_id=identity.user_id, version=version,
        )
        if row is None or not row["enabled"]:
            raise SkillNotFoundError(f"third-party skill '{skill_id}' not found")
        return _row_to_record(row)
    finally:
        conn.close()


def list_third_party_skills(db_path: Path, identity: Identity) -> list[SkillRecord]:
    """Returns [] (not an error) when identity has no active tenant/user —
    there is nothing unauthorized about asking to list zero reachable
    third-party skills; the empty-identity state itself is valid (see
    skills_identity.resolve_identity())."""
    if not identity.tenant_id or not identity.user_id:
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.* FROM skills s
            JOIN (
                SELECT skill_id, MAX(version) AS max_version FROM skills
                WHERE category = 'third_party' AND tenant_id = ? AND user_id = ?
                GROUP BY skill_id
            ) m ON s.skill_id = m.skill_id AND s.version = m.max_version
            WHERE s.category = 'third_party' AND s.tenant_id = ? AND s.user_id = ?
              AND s.enabled = 1
            ORDER BY s.skill_id
            """,
            (identity.tenant_id, identity.user_id, identity.tenant_id, identity.user_id),
        ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def resolve_skill(
    db_path: Path, identity: Identity, skill_id: str, version: int | None = None,
) -> SkillRecord:
    """The single entry point a future caller (CLI /skill, request forwarding —
    both explicitly out of scope for this phase) should use: platform skills
    first, then this identity's own third-party skills, so ownership logic
    lives in exactly one place rather than being re-implemented per caller."""
    try:
        return get_platform_skill(db_path, skill_id, version)
    except SkillNotFoundError:
        pass
    return get_third_party_skill(db_path, identity, skill_id, version)
