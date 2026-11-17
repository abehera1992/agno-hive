"""hive-mcp configuration — reads from environment with sensible defaults."""
import os
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
# Absolute path override for the registry's SQLite file. Unset by default -> a
# dedicated PROJECT_ROOT/.hive_skills/skills_registry.db, self-registered into
# .gitignore the same way tools/scratch.py's .hive_scratch/ already is — persists
# across a container restart (PROJECT_ROOT is the host-mounted volume) with no
# compose/volume change needed.
HIVE_SKILLS_DB_PATH        = os.getenv("HIVE_SKILLS_DB_PATH", "")
