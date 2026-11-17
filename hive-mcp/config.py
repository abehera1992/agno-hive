"""hive-mcp configuration — reads from environment with sensible defaults."""
import hashlib
import os
import re
import subprocess
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

PROJECT_ROOT       = Path(os.getenv("PROJECT_ROOT", "/project"))
MCP_HOST           = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT           = int(os.getenv("MCP_PORT", "9000"))
MCP_NAME           = "hive-mcp"
WRITE_REVIEW       = os.getenv("WRITE_REVIEW", "true").lower() == "true"

# ── exclusions (search, read, write, index — one list, see tools/exclusions.py) ──
# hive-mcp ships only language/tooling-generic exclusions. Anything specific to a repo —
# a vendored third-party stack, a generated export dir — is configured here, per project,
# and applies to EVERY path: grep, file read, file write, project scan and indexing.
#   EXCLUDE_DIRS=signoz,graphify-out,vendored-stack        (directory names)
#   EXCLUDE_GLOBS=**/infra/vendored-stack/**               (path globs)
#   EXCLUDE_ALLOW=docs/generated/api.md                    (re-open one path)
EXCLUDE_DIRS  = [d.strip() for d in os.getenv("EXCLUDE_DIRS", "").split(",") if d.strip()]
EXCLUDE_GLOBS = [g.strip() for g in os.getenv("EXCLUDE_GLOBS", "").split(",") if g.strip()]
EXCLUDE_ALLOW = [a.strip() for a in os.getenv("EXCLUDE_ALLOW", "").split(",") if a.strip()]

# URL prefixes that mark a string as an API route, for verify_claims. Comma-separated.
# "/api" is the common default but it is a convention, not a rule — a project routing
# under /v1, /rest or /graphql sets its own here. Set to empty to switch route checking
# off entirely (symbols and file:line citations are still checked).
#   e.g. ROUTE_PREFIXES=/api,/v1,/graphql

# Code-convention lint applied to fenced code blocks in an answer, by verify_claims.
# Rules are "regex::message", separated by ";;". Project-specific by design — hive-mcp
# ships NONE, because "which styling system" or "which import style" is a fact about a
# repo, not about software. Set them in the project env.
#   CODE_LINT_FORBID=className="::use styles.x, not a bare className string
#   CODE_LINT_REQUIRE=styles\.::components must reference SCSS module classes
# REQUIRE rules only apply when the answer actually contains a code block.
CODE_LINT_FORBID  = [r.strip() for r in os.getenv("CODE_LINT_FORBID", "").split(";;") if r.strip()]
CODE_LINT_REQUIRE = [r.strip() for r in os.getenv("CODE_LINT_REQUIRE", "").split(";;") if r.strip()]
ROUTE_PREFIXES = [p.strip().rstrip("/") for p in os.getenv("ROUTE_PREFIXES", "/api").split(",") if p.strip()]
# Hard ceiling on characters returned by a search tool. A result that overflows the
# model's context is worse than no result: the agent loses the conversation, not just
# the answer.
SEARCH_MAX_OUTPUT_CHARS = int(os.getenv("SEARCH_MAX_OUTPUT_CHARS", "20000"))
# Monorepo subdirectory prefixes tried when a short glob ("src/lib/**") is relative to a
# sub-root rather than PROJECT_ROOT. "**" is always tried last and works for any layout;
# set this only if a repo needs a specific prefix attempted first.
#   e.g. GLOB_FALLBACK_PREFIXES=Client/WebApp,services/api
GLOB_FALLBACK_PREFIXES  = [p.strip() for p in os.getenv("GLOB_FALLBACK_PREFIXES", "").split(",") if p.strip()]
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "false").lower() == "true"

# ── External platform integrations (optional — activate by setting the env var) ─
NOTION_API_KEY             = os.getenv("NOTION_API_KEY", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # path to JSON file

# ── Read-only SQL (optional — activate by setting DB_CONN_URL) ──────────────────
# Generic read-only DB grounding: db_schema() + db_query() let hive VERIFY facts against
# the live database instead of grepping files. Point at a READ-ONLY role's DSN, e.g.
# postgresql://hive_ro:<pw>@host.docker.internal:5433/ekamApp — the tool holds no schema
# knowledge; the access boundary is the role's grants. Tools register only when this is set.
#
# Deliberately named DB_CONN_URL, not project-specific — this is a generic
# read-only-SQL tool with no built-in opinion about
# which project or database it points at. The value itself lives in .env (see
# .env.example), NOT here or in docker-compose.hive.yml's committed defaults, so
# swapping to a different DB (a different project, a different environment, a
# throwaway local Postgres) is a one-line edit to .env — no code change, no image
# rebuild, no compose-file edit.
DB_CONN_URL   = os.getenv("DB_CONN_URL", "")
DB_MAX_ROWS   = int(os.getenv("DB_MAX_ROWS", "1000"))
DB_TIMEOUT_MS = int(os.getenv("DB_TIMEOUT_MS", "5000"))

# ── Persistent bash sessions + background jobs (bash_* tools) ───────────────────
# Session-scoped cwd persistence + background execution over plain subprocess
# management — NOT a PTY, no persisted env vars. Tools register only when this
# is set (rollback lever independent of a redeploy).
HIVE_BASH_TOOL_ENABLED            = os.getenv("HIVE_BASH_TOOL_ENABLED", "true").lower() == "true"
HIVE_BASH_MAX_OUTPUT_CHARS        = int(os.getenv("HIVE_BASH_MAX_OUTPUT_CHARS", "20000"))
HIVE_BASH_DEFAULT_TIMEOUT_SECONDS = int(os.getenv("HIVE_BASH_DEFAULT_TIMEOUT_SECONDS", "120"))
HIVE_BASH_MAX_TIMEOUT_SECONDS     = int(os.getenv("HIVE_BASH_MAX_TIMEOUT_SECONDS", "600"))
HIVE_BASH_MAX_SESSIONS            = int(os.getenv("HIVE_BASH_MAX_SESSIONS", "10"))
HIVE_BASH_MAX_BACKGROUND_JOBS     = int(os.getenv("HIVE_BASH_MAX_BACKGROUND_JOBS", "5"))
HIVE_BASH_SESSION_TTL_SECONDS     = int(os.getenv("HIVE_BASH_SESSION_TTL_SECONDS", "1800"))
HIVE_BASH_REAP_INTERVAL_SECONDS   = int(os.getenv("HIVE_BASH_REAP_INTERVAL_SECONDS", "60"))

# ── Migration runner (optional, review-gated — activate with MIGRATIONS_ENABLED=true) ─
# Lets hive APPLY Alembic migrations as the DB owner via the mounted docker socket.
# The owner password is read from the db container at run time (never stored here).
MIGRATIONS_ENABLED  = os.getenv("MIGRATIONS_ENABLED", "false").lower() == "true"
MIGRATION_DB_CONTAINER = os.getenv("MIGRATION_DB_CONTAINER", "")   # e.g. ekamapp-postgres-1
MIGRATION_DB_OWNER     = os.getenv("MIGRATION_DB_OWNER", "")       # e.g. abehera1992 (owner/superuser)
MIGRATION_DB_NAME      = os.getenv("MIGRATION_DB_NAME", "")        # e.g. ekamApp
MIGRATION_DB_HOST      = os.getenv("MIGRATION_DB_HOST", "postgres")  # host as seen from the service container
MIGRATION_DB_PORT      = os.getenv("MIGRATION_DB_PORT", "5432")
MIGRATION_SERVICES     = os.getenv("MIGRATION_SERVICES", "")       # "name:container,name:container"

# ── Persistent skills registry (Phase 1.57a) — ownership + identity resolution ──
# See tools/skills_registry.py (SQLite-backed registry) and tools/skills_identity.py
# (the one application-level identity provider every registry call goes through).
# Deliberately NOT wired into tools/skills.py's existing list_skills()/load_skill()
# yet — this phase ships the persistent schema + ownership/identity primitives only;
# request forwarding, CLI /skill, and workflow injection are explicit non-goals here.
#
# "development" (default): tenant_id/user_id come ONLY from HIVE_SKILLS_DEV_TENANT_ID/
# HIVE_SKILLS_DEV_USER_ID below, server-side config — never from any caller-supplied
# value, even in this mode. "authenticated": both IDs must come from the caller's own
# trusted application request context (resolve_identity()'s trusted_tenant_id/
# trusted_user_id arguments) — missing either is a hard failure, never a silent
# fall-through to development mode. No other value is accepted; skills_identity.py
# raises at resolution time for anything else, rather than defaulting to one of the two.
HIVE_SKILLS_IDENTITY_MODE  = os.getenv("HIVE_SKILLS_IDENTITY_MODE", "development").strip().lower()
HIVE_SKILLS_DEV_TENANT_ID  = os.getenv("HIVE_SKILLS_DEV_TENANT_ID", "") or None
HIVE_SKILLS_DEV_USER_ID    = os.getenv("HIVE_SKILLS_DEV_USER_ID", "") or None
# HIVE_SKILLS_DB_PATH is now resolved by the centralized Phase H block below —
# see that block's own docstring. Still settable as an explicit override exactly
# as before; only the DEFAULT (when unset) changed, from PROJECT_ROOT/.hive_skills/
# to HIVE_DATA_ROOT/registry/.

# ── Phase H — centralized Hive-owned persistence (hive-mcp's OWN data, never the
# connected project's) ──────────────────────────────────────────────────────────
#
# PROJECT_ROOT is the external workspace hive-mcp was pointed at — code to read,
# search, and (write-review permitting) edit. It was never meant to also be where
# HIVE ITSELF keeps state, but three subsystems grew up defaulting into it anyway
# (skills_registry.py's own module docstring even named the precedent explicitly:
# "same dedicated dot-directory under PROJECT_ROOT" tools/scratch.py already used).
# Confirmed via a full read-path/write-path inventory of every tool module
# (Phase H, read-only step) that exactly three subsystems are hive-mcp's OWN
# persistent data with no other legitimate reason to live inside the project tree:
#   - the skills registry (tools/skills_registry.py)
#   - the LightRAG indexing bookkeeping manifest (tools/index.py) -- NOT the
#     actual vector/graph index, which lives entirely in LightRAG's own Qdrant/AGE
#     storage outside hive-mcp; this is only the small per-file mtime/hash/
#     chunk-count state index_project() uses to skip unchanged files
#   - the oversized-tool-output scratch offload (tools/scratch.py)
#
# Explicitly NOT moved here, despite being hive-mcp-generated, because they are a
# deliberate HOST-VISIBLE HANDOFF CHANNEL, not internal state -- cli/hive reads
# these SAME paths directly off the host filesystem, never through hive-mcp:
#   - hive.md                  (tools/scan.py)            -- cli/hive's
#     _ensure_hive_context() checks Path.cwd()/hive.md on the HOST before every
#     task; Cline's .clinerules does the same. Moving the write target breaks
#     that existence check and forces a full rescan on every single invocation.
#   - .hive_pending_actions/   (tools/integrations/__init__.py) -- cli/hive's
#     _find_pending_actions() scans this exact directory on the HOST; it is the
#     confirm/reject handoff between the container and the CLI, not hive-mcp's
#     own bookkeeping.
#   - *.hive_proposed files    (tools/files.py)            -- co-located with
#     their real target file by design, for VS Code diffing; cannot be
#     centralized without breaking that adjacency.
# None of these three are touched by this phase.
HIVE_DATA_ROOT = Path(os.getenv("HIVE_DATA_ROOT", "/data/hive"))


def _git_remote_identity(root: Path) -> str | None:
    """`git remote get-url origin`, read-only, local-only (no network call — this
    just reads .git/config), best-effort. The PRIMARY signal for a stable project
    namespace: it identifies the PROJECT, not the host filesystem path it happens
    to be mounted at (PROJECT_ROOT is always literally "/project" inside every
    container regardless of which project is actually mounted there — useless as
    a namespace key on its own)."""
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=str(root),
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:  # noqa: BLE001 -- git missing/not a repo/anything else: fall through
        pass
    return None


def _mount_identity(root: Path) -> str:
    """Fallback namespace signal when there's no git remote: the MOUNTED
    DIRECTORY'S real (device, inode) pair, visible from inside the container even
    though the logical path string ("/project") never changes. Two different host
    directories bind-mounted to the same /project resolve to different (dev, ino)
    pairs; the SAME directory remounted later reproduces the same pair -- exactly
    the "deterministic, collision-resistant" property a namespace needs, with no
    operator configuration required."""
    try:
        st = root.stat()
        return f"{st.st_dev}:{st.st_ino}"
    except OSError:
        return "unknown"


def _resolve_project_namespace() -> str:
    """HIVE_PROJECT_NAMESPACE if set (explicit override, e.g. for a host where
    git/stat-based identity would be unstable); otherwise a human-readable slug
    of the mounted directory's name plus a short hash of its git-remote-or-mount
    identity, so two DIFFERENT projects can never collide even if both are ever
    mounted at /project at different times, and the SAME project mounted again
    later reproduces the identical namespace."""
    override = os.getenv("HIVE_PROJECT_NAMESPACE", "").strip()
    if override:
        return override
    slug = re.sub(r"[^a-z0-9-]+", "-", PROJECT_ROOT.name.lower()).strip("-") or "project"
    identity = _git_remote_identity(PROJECT_ROOT) or _mount_identity(PROJECT_ROOT)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


HIVE_PROJECT_NAMESPACE = _resolve_project_namespace()

# Structured trail of which branch each subsystem's resolution took — appended to
# by _resolve_persistent_path below, emitted by main.py at startup (Phase H.1),
# and directly inspectable by a caller (e.g. a scratch validation harness)
# without needing to re-derive the same logic. Each entry is a dict, not a plain
# string (Phase H.1 — a startup log needs subsystem/resolved_path/decision/
# existence/conflict as separate, reliably-extractable fields, not parsed back
# out of prose): {subsystem, resolved_path, decision, legacy_path,
# legacy_has_data, new_default_path, new_has_data, message}. `resolved_path` is
# None exactly when `decision == "conflict"` — see PersistenceConflictError below
# for what that actually means for the subsystem's own callers.
HIVE_PERSISTENCE_NOTES: list[dict] = []


class PersistenceConflictError(RuntimeError):
    """Raised (Phase H.1) when code actually TRIES TO USE a subsystem whose
    path resolution hit a legacy/new conflict — never raised by config.py
    itself at import time (that would crash every OTHER, non-conflicted
    subsystem too, and crash every lightweight tool that merely imports
    config.py to inspect it). Each consuming module (skills_registry.py,
    index.py, scratch.py) raises this at its own actual read/write entry
    point instead, exactly where "prevent that subsystem from silently
    operating against potentially stale or incomplete state" needs to bite."""


def require_resolved_path(resolved: str, subsystem: str) -> Path:
    """The ONE place "is this subsystem safe to use" is actually enforced.
    `resolved` is one of HIVE_SKILLS_DB_PATH/HIVE_INDEX_STATE_DIR/
    HIVE_SCRATCH_ROOT — empty string means _resolve_persistent_path hit a
    conflict for that subsystem and deliberately did NOT pick a side (Phase
    H.1's "fail closed", replacing Phase H's original "use the new path
    anyway" behavior). Every consuming module's real entry point calls this
    before touching its path, so a conflict blocks ONLY that one subsystem —
    everything else keeps working."""
    if not resolved:
        raise PersistenceConflictError(
            f"{subsystem}: BLOCKED — legacy and new-default data both exist and were not "
            f"automatically reconciled (see HIVE_PERSISTENCE_NOTES / startup log for both "
            f"paths). Resolve manually by setting the subsystem's explicit path override, "
            f"then restart. Refusing to guess which copy is current to avoid operating "
            f"against stale or incomplete state."
        )
    return Path(resolved)


def _file_has_data(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _dir_has_data(p: Path) -> bool:
    try:
        return p.is_dir() and any(p.iterdir())
    except OSError:
        return False


def _resolve_persistent_path(
    *, subsystem: str, explicit_env: str, legacy_path: Path, new_default: Path, data_present,
) -> Path | None:
    """The ONE place every Hive-owned persistence path is resolved (replaces each
    module inventing its own PROJECT_ROOT-relative fallback). Precedence:

      1. explicit_env set -> used exactly as given, no further checks. The
         operator has taken explicit control; this function trusts it completely.
      2. explicit_env unset -> compare `legacy_path` (the OLD, PROJECT_ROOT-
         relative default from before Phase H) against `new_default` (the
         HIVE_DATA_ROOT-relative default Phase H introduced):
           - data at legacy only  -> KEEP USING legacy_path. "Never silently
             select an empty destination over existing data."
           - data at new only, or neither -> use new_default (the normal
             fresh-install / already-migrated case).
           - data at BOTH -> CONFLICT (Phase H.1 — fail closed). Returns
             None. Neither path is touched, read from, or written to by this
             function; the subsystem is simply unusable until an operator
             resolves it explicitly (see require_resolved_path above) —
             replaces Phase H's original behavior of silently preferring the
             new path, which risked running against incomplete/stale state
             with no indication anything was wrong.

    `data_present` is a subsystem-specific existence predicate (_file_has_data for
    a single SQLite file, _dir_has_data for a directory of index-state files, or a
    predicate that always returns False for a subsystem — scratch — where nothing
    is ever worth preserving via this fallback, since it is disposable TTL-swept
    cache by design)."""
    explicit = os.getenv(explicit_env, "").strip()
    if explicit:
        HIVE_PERSISTENCE_NOTES.append({
            "subsystem": subsystem, "resolved_path": explicit, "decision": "explicit_override",
            "legacy_path": str(legacy_path), "legacy_has_data": data_present(legacy_path),
            "new_default_path": str(new_default), "new_has_data": data_present(new_default),
            "message": f"{subsystem}: explicit override {explicit_env}={explicit}",
        })
        return Path(explicit)

    legacy_has_data = data_present(legacy_path)
    new_has_data = data_present(new_default)

    if legacy_has_data and new_has_data:
        HIVE_PERSISTENCE_NOTES.append({
            "subsystem": subsystem, "resolved_path": None, "decision": "conflict",
            "legacy_path": str(legacy_path), "legacy_has_data": True,
            "new_default_path": str(new_default), "new_has_data": True,
            "message": (
                f"{subsystem}: CONFLICT — data exists at BOTH the legacy path ({legacy_path}) "
                f"and the new default ({new_default}). NEITHER was selected automatically; "
                f"both are left exactly as found. This subsystem will refuse to operate "
                f"(PersistenceConflictError) until {explicit_env} is set explicitly to pick one side."
            ),
        })
        return None
    if legacy_has_data and not new_has_data:
        HIVE_PERSISTENCE_NOTES.append({
            "subsystem": subsystem, "resolved_path": str(legacy_path), "decision": "legacy_preserved",
            "legacy_path": str(legacy_path), "legacy_has_data": True,
            "new_default_path": str(new_default), "new_has_data": False,
            "message": (
                f"{subsystem}: legacy data found at {legacy_path}; the new default ({new_default}) "
                f"is empty — continuing to use the LEGACY path rather than silently starting fresh "
                f"and orphaning it. Set {explicit_env} explicitly once migrated to move off this path."
            ),
        })
        return legacy_path
    HIVE_PERSISTENCE_NOTES.append({
        "subsystem": subsystem, "resolved_path": str(new_default), "decision": "fresh_install",
        "legacy_path": str(legacy_path), "legacy_has_data": False,
        "new_default_path": str(new_default), "new_has_data": new_has_data,
        "message": f"{subsystem}: using the new default ({new_default}); no legacy data found at {legacy_path}.",
    })
    return new_default


def _path_or_empty(p: Path | None) -> str:
    return str(p) if p is not None else ""


HIVE_SKILLS_DB_PATH = _path_or_empty(_resolve_persistent_path(
    subsystem="skills_registry",
    explicit_env="HIVE_SKILLS_DB_PATH",
    legacy_path=PROJECT_ROOT / ".hive_skills" / "skills_registry.db",
    new_default=HIVE_DATA_ROOT / "registry" / "skills_registry.db",
    data_present=_file_has_data,
))

# Preserves the existing HIVE_INDEX_STATE_DIR override name exactly (tools/index.py
# used to read this directly via os.getenv, bypassing config.py entirely — moved
# here per Phase H's "centralize path resolution in config.py" requirement).
# Namespaced under HIVE_PROJECT_NAMESPACE (not shared flatly under
# HIVE_DATA_ROOT/indexes/) so switching which project is mounted at /project,
# without also changing HIVE_DATA_ROOT, can never mix two projects' index state —
# each gets its own subdirectory, keyed by the namespace above.
HIVE_INDEX_STATE_DIR = _path_or_empty(_resolve_persistent_path(
    subsystem="index_state",
    explicit_env="HIVE_INDEX_STATE_DIR",
    legacy_path=PROJECT_ROOT / ".hive-index-state",
    new_default=HIVE_DATA_ROOT / "indexes" / HIVE_PROJECT_NAMESPACE,
    data_present=_dir_has_data,
))

# New override (Phase H) — tools/scratch.py used to hardcode PROJECT_ROOT/.hive_scratch
# with no override at all. data_present is always False here deliberately: scratch
# is disposable, TTL-swept offload storage by design (tools/scratch.py's own
# docstring) — there is never legacy content worth preserving via the
# keep-using-the-old-path fallback the other two subsystems get, so this always
# resolves to the new default unless explicitly overridden (and can never itself
# report a conflict, for the same reason).
HIVE_SCRATCH_ROOT = _path_or_empty(_resolve_persistent_path(
    subsystem="scratch",
    explicit_env="HIVE_SCRATCH_ROOT",
    legacy_path=PROJECT_ROOT / ".hive_scratch",
    new_default=HIVE_DATA_ROOT / "cache" / "scratch",
    data_present=lambda _p: False,
))

# ── Phase H.1 — persistence-root safety (startup gate, main.py calls this) ─────
# A writable directory inside the container is NOT proof of persistence — the
# container's own root filesystem (an ephemeral overlay) is writable too, and a
# typo'd HIVE_DATA_ROOT or a missing volume declaration would silently produce
# exactly that: a directory that works fine until the container is recreated,
# at which point everything written there is gone with no error at the time it
# happened. validate_data_root() distinguishes "writable" from "actually a
# distinct mount" using the standard mountpoint heuristic: a real bind mount or
# named volume has a different (device) number than the container's own root
# filesystem; a plain subdirectory of the container's own layer does not.
#
# Deliberately NOT run automatically at import time (unlike every HIVE_* path
# above) — this is a startup gate for the real server process (main.py calls it
# once, explicitly), not a property of the config module itself. Running it at
# import time would make every lightweight script that merely imports config.py
# to inspect a resolved path (every scratch validation driver this project has
# used so far) fail unless it also set the dev-override — this function stays
# opt-in-to-call so inspection tooling is unaffected.
HIVE_DATA_ROOT_ALLOW_EPHEMERAL = os.getenv("HIVE_DATA_ROOT_ALLOW_EPHEMERAL", "false").strip().lower() == "true"


def validate_data_root(allow_ephemeral: bool | None = None) -> dict:
    """Checks (never raises itself — the caller decides what to do with the
    result, which is what makes this testable without catching SystemExit):

      1. HIVE_DATA_ROOT exists. A missing mount point is always a
         misconfiguration (a real Docker volume mount always has its target
         directory already present the moment the container starts, even if
         empty) — this function does NOT auto-create it in production mode;
         silently creating a phantom directory would be exactly the "treat a
         writable container-internal directory as proof of persistence"
         mistake this check exists to catch.
      2. It is writable (a real write-and-delete probe, not just os.access,
         which can lie about effective permissions in some container setups).
      3. It is a genuinely distinct mount, not part of the container's own
         root filesystem — compared via st_dev against "/" (st_dev differs
         for a real bind mount or named volume; it's identical for a plain
         subdirectory of the same filesystem the container's root lives on).

    `allow_ephemeral` (default: HIVE_DATA_ROOT_ALLOW_EPHEMERAL) skips ONLY
    check 3 (and auto-creates the directory for checks 1/2 if missing) — the
    explicit development-only escape hatch for intentionally running without
    a mounted persistent volume (e.g. local testing on a host with no
    container at all, where "/" and any local temp dir are trivially the same
    filesystem). Checks 1 (now auto-create) and 2 (writability) still apply
    even in dev mode — a genuinely broken path should fail there too.

    Returns {"ok": bool, "reason": str, "checks": {...}} — never partial,
    always every field, so a caller can log the full picture either way."""
    ephemeral_ok = HIVE_DATA_ROOT_ALLOW_EPHEMERAL if allow_ephemeral is None else allow_ephemeral
    checks = {"exists": False, "writable": False, "distinct_mount": None}

    if not HIVE_DATA_ROOT.exists():
        if ephemeral_ok:
            try:
                HIVE_DATA_ROOT.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"ok": False, "reason": f"HIVE_DATA_ROOT ({HIVE_DATA_ROOT}) does not exist and "
                                                f"could not be created even in dev mode: {e}", "checks": checks}
        else:
            return {"ok": False, "reason": f"HIVE_DATA_ROOT ({HIVE_DATA_ROOT}) does not exist — a real "
                                            f"volume mount's target directory is always present at "
                                            f"container start, even if empty, so a missing directory "
                                            f"means the volume was never mounted (wrong path, or no "
                                            f"volume declared). Not auto-created in production mode.",
                    "checks": checks}
    checks["exists"] = True

    probe = HIVE_DATA_ROOT / ".hive_write_probe"
    try:
        probe.write_text("ok")
        probe.unlink()
        checks["writable"] = True
    except OSError as e:
        return {"ok": False, "reason": f"HIVE_DATA_ROOT ({HIVE_DATA_ROOT}) exists but is not writable: {e}",
                "checks": checks}

    if ephemeral_ok:
        checks["distinct_mount"] = None  # explicitly not checked in dev mode
        return {"ok": True, "reason": "development override — persistent-mount check skipped", "checks": checks}

    try:
        data_root_dev = HIVE_DATA_ROOT.stat().st_dev
        container_root_dev = Path("/").stat().st_dev
        distinct = data_root_dev != container_root_dev
    except OSError as e:
        return {"ok": False, "reason": f"could not stat HIVE_DATA_ROOT or '/' to check mount identity: {e}",
                "checks": checks}
    checks["distinct_mount"] = distinct
    if not distinct:
        return {
            "ok": False,
            "reason": f"HIVE_DATA_ROOT ({HIVE_DATA_ROOT}) is writable but is NOT a distinct mount from "
                      f"the container's own root filesystem (same device) — it is almost certainly a "
                      f"plain directory on the container's own ephemeral layer, not a mounted volume. "
                      f"Data written here will be LOST on the next container recreation with no error "
                      f"at the time it happens. Mount a real volume at HIVE_DATA_ROOT, or set "
                      f"HIVE_DATA_ROOT_ALLOW_EPHEMERAL=true to run without one anyway (development only).",
            "checks": checks,
        }
    return {"ok": True, "reason": "HIVE_DATA_ROOT exists, is writable, and is a distinct mount", "checks": checks}
