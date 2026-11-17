"""MCP-facing wrapper around the persistent skills registry (Phase 1.57b).

Thin glue only: every function here resolves identity via
skills_identity.resolve_identity() and then calls an EXISTING
skills_registry.py function — list_platform_skills/list_third_party_skills/
resolve_skill/register_skill. No ownership/visibility logic is reimplemented
here (Phase 1.57b's own instruction: "keep CLI ownership filtering and
registry ownership enforcement centralized through existing registry APIs").

These three tools are the ONLY way hive-mcp exposes the registry today.
tools/skills.py's list_skills()/load_skill() (the filesystem catalog) are
completely unmodified and still the only thing wired into an agent's own
tool surface — nothing here is registered for agent use, only for the CLI
(see hive-mcp/main.py's _tool() registrations and cli/hive's /skill command).

Identity note: there is no per-call request/session context reaching an MCP
tool invocation today (confirmed in Phase 1.57a's own report). Every function
below calls resolve_identity() with no trusted_* arguments, so in practice
only HIVE_SKILLS_IDENTITY_MODE=development resolves to a usable identity here
-- "authenticated" mode will correctly raise (surfaced as an "Error: ..."
string, never a crash), which is expected until a later phase adds real
request-scoped identity. Phase 1.57b's own instructions are explicit about
this: "Do not implement authentication; use the existing development
identity provider."
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))         # tools/ itself -- sibling bare imports below
sys.path.insert(0, str(Path(__file__).parent.parent))  # hive-mcp/ -- not directly needed here, kept
                                                        # for consistency with every other tools/*.py

import config
import skills_registry as reg
import skill_import_staging as staging
from skills_identity import IdentityResolutionError, resolve_identity

import structlog

log = structlog.get_logger()


def _resolve_db_path():
    """(db_path, None) on success, or (None, error_string) on a legacy/new
    persistence CONFLICT (Phase H.1) -- centralizes the one failure mode every
    function below must handle identically, the same way IdentityResolutionError
    already is. A conflict means skills_registry.default_db_path() refused to
    pick a side (see config.PersistenceConflictError's own docstring); this
    surfaces that as the SAME clean "Error: ..." string shape every other
    failure in this module already uses, instead of an unhandled exception."""
    try:
        return reg.default_db_path(), None
    except config.PersistenceConflictError as e:
        return None, f"Error: {e}"


def _summary(rec: reg.SkillRecord, scope: str) -> dict:
    return {
        "skill_id": rec.skill_id,
        "name": rec.name,
        "category": rec.category,
        "version": rec.version,
        "source": rec.source,
        "scope": scope,  # "platform" | "yours" -- never any other identity's
    }


def skill_registry_list() -> str:
    """List every platform skill plus the active identity's OWN third-party
    skills (latest enabled version of each) -- never another tenant's or
    another user's. Returns a JSON array of {skill_id, name, category,
    version, source, scope}. Returns an "Error: ..." string (never raises)
    if identity resolution itself fails (e.g. authenticated mode with no
    trusted identity available -- see module docstring)."""
    db_path, err = _resolve_db_path()
    if err:
        return err
    try:
        identity = resolve_identity()
    except IdentityResolutionError as e:
        return f"Error: {e}"

    items = [_summary(r, "platform") for r in reg.list_platform_skills(db_path)]
    items += [_summary(r, "yours") for r in reg.list_third_party_skills(db_path, identity)]
    log.info("skill_registry_mcp.list", mode=identity.mode, count=len(items))
    return json.dumps(items)


def skill_registry_info(skill_id: str) -> str:
    """Metadata + description for ONE skill this identity can access
    (platform, or this identity's own third-party skill) -- resolved through
    resolve_skill(), so a skill_id belonging to someone else is reported
    not-found exactly like a skill_id that doesn't exist at all (see
    skills_registry.SkillNotFoundError's own docstring). Returns a JSON
    object, or an "Error: ..." string."""
    db_path, err = _resolve_db_path()
    if err:
        return err
    try:
        identity = resolve_identity()
    except IdentityResolutionError as e:
        return f"Error: {e}"

    try:
        rec = reg.resolve_skill(db_path, identity, skill_id)
    except reg.SkillNotFoundError:
        return f"Error: skill '{skill_id}' not found or not accessible to the active identity."

    preview = rec.content if len(rec.content) <= 1000 else rec.content[:1000] + "... (truncated)"
    return json.dumps({
        "skill_id": rec.skill_id,
        "name": rec.name,
        "skill_type": rec.skill_type,
        "category": rec.category,
        "version": rec.version,
        "source": rec.source,
        "description": rec.description,
        "content_hash": rec.content_hash,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "supporting_files": sorted(rec.supporting_files.keys()),
        "content_preview": preview,
    })


def skill_registry_resolve(skill_id: str) -> str:
    """Resolve ONE skill_id to its version/hash-pinned metadata ONLY -- no
    content, no supporting_files (Phase 1.57c). This is the lean counterpart
    to skill_registry_info: a workflow-side caller deciding whether a
    REQUESTED skill_id is valid/authorized needs the pin (skill_id, version,
    content_hash, category, source), never the body -- fetching content for
    a decision that doesn't act on it yet would cross more data over the
    network than the decision needs, and nothing downstream of this phase
    is allowed to inject content anyway (that's Phase 1.57d).

    Same identity/ownership path as skill_registry_info -- resolved via
    resolve_identity() + resolve_skill(), so a skill_id belonging to another
    identity is reported not-found exactly like a nonexistent one; disabled
    records are likewise already excluded by resolve_skill() itself. Returns
    a JSON object, or an "Error: ..." string (never raises)."""
    db_path, err = _resolve_db_path()
    if err:
        return err
    try:
        identity = resolve_identity()
    except IdentityResolutionError as e:
        return f"Error: {e}"

    try:
        rec = reg.resolve_skill(db_path, identity, skill_id)
    except reg.SkillNotFoundError:
        return f"Error: skill '{skill_id}' not found or not accessible to the active identity."

    return json.dumps({
        "skill_id": rec.skill_id,
        "name": rec.name,
        "category": rec.category,
        "version": rec.version,
        "source": rec.source,
        "content_hash": rec.content_hash,
    })


def skill_registry_import(
    name: str,
    content: str,
    skill_type: str = "project",
    skill_id: str = "",
    description: str = "",
    source: str = "",
    supporting_files_json: str = "{}",
) -> str:
    """Register a new third-party skill (or a new version of an existing
    one) owned by the ACTIVE IDENTITY ONLY -- tenant_id/user_id come
    exclusively from resolve_identity(); there is no parameter here through
    which a caller could supply or override them. `content` and every value
    inside `supporting_files_json` are stored verbatim as opaque text/data --
    NEVER parsed as code, executed, imported, or evaluated by this function
    or anything it calls (see skills_registry.register_skill(), which only
    ever INSERTs these as TEXT column values).

    skill_id defaults to a stable slug of `name` (skills_registry.
    generate_skill_id()) when not given explicitly. Re-importing the same
    skill_id appends a new version (skills_registry.register_skill()'s
    existing append-only behavior) and is REJECTED with a generic error --
    no detail about the real owner -- if it would change ownership or
    category (skills_registry.OwnershipConflictError, Phase 1.57a.1),
    including a name collision with a skill some OTHER identity already
    owns.

    Returns a JSON object on success, or an "Error: ..." string."""
    db_path, err = _resolve_db_path()
    if err:
        return err
    try:
        identity = resolve_identity()
    except IdentityResolutionError as e:
        return f"Error: {e}"

    if not identity.tenant_id or not identity.user_id:
        return (
            "Error: no active third-party identity is configured — set "
            "HIVE_SKILLS_DEV_TENANT_ID and HIVE_SKILLS_DEV_USER_ID (development "
            "mode) before importing a third-party skill."
        )

    try:
        supporting_files = json.loads(supporting_files_json) if supporting_files_json else {}
        if not isinstance(supporting_files, dict):
            raise ValueError("supporting_files_json must decode to a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return f"Error: invalid supporting_files_json — {e}"

    resolved_skill_id = skill_id.strip() or reg.generate_skill_id(name)

    try:
        version = reg.register_skill(
            db_path,
            skill_id=resolved_skill_id,
            name=name,
            skill_type=skill_type or "project",
            category=reg.THIRD_PARTY,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            content=content,
            supporting_files=supporting_files,
            source=source or "cli-import",
            description=description or None,
        )
    except reg.OwnershipConflictError:
        return (
            f"Error: a skill with id '{resolved_skill_id}' already exists under "
            "different ownership — choose a different name or an explicit "
            "skill_id that isn't already taken."
        )
    except reg.InvalidSkillRecordError as e:
        return f"Error: {e}"

    log.info(
        "skill_registry_mcp.imported",
        skill_id=resolved_skill_id, version=version,
        tenant_id=identity.tenant_id, user_id=identity.user_id,
    )
    return json.dumps({
        "skill_id": resolved_skill_id,
        "version": version,
        "name": name,
        "category": reg.THIRD_PARTY,
        "tenant_id": identity.tenant_id,
        "user_id": identity.user_id,
    })


# ── Staged import (Phase H.2) — fixes the /skill import HTTP 413 failure ────
# skill_registry_import() above sends the ENTIRE package (content + every
# supporting file, JSON-encoded) in ONE tools/call request. The mcp SDK's
# Streamable HTTP transport hard-caps that at 4 MiB
# (mcp.server.transport_security.DEFAULT_MAX_REQUEST_BODY_SIZE) with no
# override FastMCP exposes at the pinned version — a package whose encoded
# payload exceeds it is rejected by transport middleware with HTTP 413
# BEFORE skill_registry_import() (or any application code) ever runs; see
# docs/runbooks/architecture-acceptance-runs/Phase-H.2-report.md. These four
# tools are the alternative path for anything that large: begin() + one or
# more batch() calls + finalize(), each request comfortably under the
# ceiling. Nothing staged this way is visible to skill_registry_list/info/
# resolve (and therefore CLI /skill or Workflow skill forwarding) until
# finalize() atomically publishes it through the SAME, unmodified
# skills_registry.register_skill() skill_registry_import() already uses —
# see skill_import_staging.py for the staging logic itself.

def skill_registry_import_begin(
    name: str,
    content: str,
    skill_type: str = "project",
    skill_id: str = "",
    description: str = "",
    source: str = "",
    expected_file_count: int = 0,
) -> str:
    """Start a staged third-party skill import. Returns a JSON object with
    `import_id` (pass it to every skill_registry_import_batch/_finalize/
    _abort call that follows), `skill_id`, and `expected_file_count` — or an
    "Error: ..." string. Use this instead of skill_registry_import() whenever
    the full package would not comfortably fit in one ~4 MiB request."""
    db_path, err = _resolve_db_path()
    if err:
        return err
    try:
        identity = resolve_identity()
    except IdentityResolutionError as e:
        return f"Error: {e}"

    if not identity.tenant_id or not identity.user_id:
        return (
            "Error: no active third-party identity is configured — set "
            "HIVE_SKILLS_DEV_TENANT_ID and HIVE_SKILLS_DEV_USER_ID (development "
            "mode) before importing a third-party skill."
        )

    resolved_skill_id = skill_id.strip() or reg.generate_skill_id(name)
    try:
        handle = staging.begin_import(
            db_path,
            skill_id=resolved_skill_id, name=name, skill_type=skill_type or "project",
            category=reg.THIRD_PARTY, tenant_id=identity.tenant_id, user_id=identity.user_id,
            content=content, source=source or "cli-import", description=description or None,
            expected_file_count=expected_file_count,
        )
    except reg.OwnershipConflictError:
        return (
            f"Error: a skill with id '{resolved_skill_id}' already exists under "
            "different ownership — choose a different name or an explicit "
            "skill_id that isn't already taken."
        )
    except (reg.InvalidSkillRecordError, ValueError) as e:
        return f"Error: {e}"

    log.info(
        "skill_registry_mcp.import_begun",
        import_id=handle.import_id, skill_id=handle.skill_id,
        expected_file_count=handle.expected_file_count,
    )
    return json.dumps({
        "import_id": handle.import_id,
        "skill_id": handle.skill_id,
        "expected_file_count": handle.expected_file_count,
    })


def skill_registry_import_batch(import_id: str, files_json: str) -> str:
    """Upload one batch of supporting files for an in-progress staged import.
    `files_json` must decode to the same {rel_path: {"encoding", "data"}}
    shape skill_registry_import()'s supporting_files_json already uses — split
    the full file set across several calls, each comfortably under ~2 MiB
    serialized (hard-rejected over 3 MiB). Idempotent per file: re-sending a
    rel_path with content identical to what was already uploaded is a safe
    no-op, so a batch whose response was lost can simply be retried as-is.
    Re-sending a rel_path with DIFFERENT content is rejected outright — abort
    and restart the import to change a file. Returns a JSON object with
    received/total file counts, or an "Error: ..." string."""
    try:
        files = json.loads(files_json) if files_json else {}
        if not isinstance(files, dict):
            raise ValueError("files_json must decode to a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return f"Error: invalid files_json — {e}"

    db_path, err = _resolve_db_path()
    if err:
        return err

    try:
        result = staging.add_batch(db_path, import_id, files)
    except (staging.ImportNotFoundError, staging.ImportStateError,
            staging.BatchTooLargeError, staging.FileConflictError, ValueError) as e:
        return f"Error: {e}"

    return json.dumps(result)


def skill_registry_import_finalize(import_id: str) -> str:
    """Verify every expected file arrived, then publish the staged import
    atomically through the existing register_skill() path — same return
    shape as skill_registry_import(). Safe to call again after a successful
    finalize (returns the same cached result rather than publishing twice).
    Returns an "Error: ..." string if the import is incomplete (upload the
    remaining batches and retry), was already aborted, or doesn't exist."""
    db_path, err = _resolve_db_path()
    if err:
        return err

    try:
        result = staging.finalize_import(db_path, import_id)
    except (staging.ImportNotFoundError, staging.ImportStateError, staging.IncompleteImportError) as e:
        return f"Error: {e}"
    except reg.OwnershipConflictError:
        return (
            "Error: this skill_id was registered under different ownership by "
            "another import in the meantime — choose a different skill_id."
        )

    log.info(
        "skill_registry_mcp.import_finalized",
        import_id=import_id, skill_id=result["skill_id"], version=result["version"],
    )
    return json.dumps(result)


def skill_registry_import_abort(import_id: str) -> str:
    """Discard an in-progress staged import and every file uploaded for it —
    a no-op if it was already aborted. Returns an "Error: ..." string if the
    import was already finalized (a published skill cannot be un-published
    this way)."""
    db_path, err = _resolve_db_path()
    if err:
        return err

    try:
        result = staging.abort_import(db_path, import_id)
    except staging.ImportStateError as e:
        return f"Error: {e}"

    log.info("skill_registry_mcp.import_aborted", import_id=import_id)
    return json.dumps(result)
