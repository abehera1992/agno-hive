"""Single source of truth for paths hive-mcp must not search, read, index or write.

Before this existed the same idea was spelled out four times — context.py, index.py,
scan.py and (not at all) files.py — so each tool disagreed about what was off-limits and
writes were unguarded entirely. One list, consulted by every path.

PROJECT-INDEPENDENCE. The BASE sets below name only things that mean the same in every
repository: version control, dependency trees, build output, bytecode, lockfiles. A
vendored third-party stack or a generated export directory is specific to one project and
belongs in that project's env file:

    EXCLUDE_DIRS=signoz,graphify-out,vendored-stack
    EXCLUDE_GLOBS=**/infra/vendored-stack/**,**/generated-out/**

FAIL-SAFE DEFAULTS. Two entries stay in BASE despite looking project-flavoured, because
the cost of a project forgetting to configure them is not symmetric:
  * "backups"          — commonly holds database dumps, which have held secrets. Indexing
                         one leaks it into a vector store that agents then quote from.
  * ".hive-index-state" — hive-mcp's own bookkeeping; indexing it feeds the tool itself
                         back into the index.
A project that genuinely wants these read can override with EXCLUDE_ALLOW.
"""

from __future__ import annotations

import fnmatch

import config

# Directory NAMES matched against any path component. Generic across languages/tooling.
_BASE_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor",
    "__pycache__", ".venv", "venv", "env", ".tox", ".eggs", "eggs",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", ".turbo", "target", "out",
    "coverage", "htmlcov",
    # fail-safe (see module docstring)
    "backups", ".hive-index-state",
}

# Glob patterns, used for ripgrep --glob and for fnmatch on relative paths.
_BASE_GLOBS = [
    "**/node_modules/**", "**/.git/**", "**/dist/**", "**/build/**",
    "**/.next/**", "**/coverage/**", "**/__pycache__/**", "**/.venv/**",
    "**/vendor/**", "**/backups/**",
    "**/*.min.js", "**/*.min.css", "**/*.map",
    "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "**/*.lock",
]

# Binary / non-source extensions — never useful to grep, index or return.
_BASE_EXTS = {
    ".pyc", ".pyo", ".class", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".pdf",
    ".woff", ".woff2", ".ttf", ".eot", ".zip", ".gz", ".tar", ".7z", ".jar",
}

EXCLUDE_DIRS: set[str] = _BASE_DIRS | set(config.EXCLUDE_DIRS)
EXCLUDE_GLOBS: list[str] = _BASE_GLOBS + list(config.EXCLUDE_GLOBS)
EXCLUDE_EXTS: set[str] = _BASE_EXTS
# Escape hatch: paths matching these are allowed even if a rule above excludes them.
# Checked FIRST so a project can re-open exactly one thing without disabling a whole rule.
ALLOW_GLOBS: list[str] = list(config.EXCLUDE_ALLOW)

# hive-mcp's OWN scratch directory (tools/scratch.py) -- hive-owned and intentionally
# readable, unlike .git/.venv/etc. Not a project EXCLUDE_ALLOW entry because this isn't
# project-specific configuration, it's a hive-mcp implementation detail that must always
# work: get_file_content() calls is_excluded() before serving any path (see
# test_context_exclusions.py), so without this exception a model could never read back
# more than the preview of an offloaded result -- the whole feature would be silently
# useless. Exact directory-name match only (checked against parts[0] below), not a
# prefix, so e.g. ".hive_scratchpad/" is not accidentally swept into the exception too.
_HIVE_OWNED_DIRS = {".hive_scratch"}


def rg_args() -> list[str]:
    """Negative --glob arguments for a ripgrep invocation.

    Must cover BOTH EXCLUDE_DIRS and EXCLUDE_GLOBS -- confirmed live 2026-08-11 this
    previously covered only EXCLUDE_GLOBS, so a project excluding a vendored
    directory the documented way (EXCLUDE_DIRS=signoz, per this module's own
    docstring) got zero ripgrep-level protection: is_excluded() correctly refused a
    DIRECT get_file_content('signoz/...') call, but find_files()/_find_by_basename()/
    search_files()/count_matches() -- every ripgrep-backed tool sharing this function
    -- still freely listed and searched signoz's own files, because rg_args() itself
    never translated EXCLUDE_DIRS into a ripgrep exclusion at all. That's how a
    disambiguation candidate list for an ambiguous basename like 'index.tsx' ended up
    offering signoz/frontend/src/hooks/useDarkMode/index.tsx as if it were a real
    candidate in the project.
    """
    out: list[str] = []
    for d in EXCLUDE_DIRS:
        out += ["--glob", f"!**/{d}/**"]
    for g in EXCLUDE_GLOBS:
        out += ["--glob", g if g.startswith("!") else f"!{g}"]
    return out


def is_excluded(rel_path: str) -> bool:
    """True if this project-relative path is off-limits to read, write, search or index."""
    rel = str(rel_path).replace("\\", "/")
    # A literal "./" PREFIX only -- NOT str.lstrip("./"), which strips a *set* of
    # characters repeatedly from the left, not a prefix string. That previously ate
    # the leading dot off any dotted path too (".git/config" -> "git/config"),
    # silently defeating exclusion for any dot-directory that isn't ALSO listed
    # without its dot elsewhere in EXCLUDE_DIRS. ".venv" masked this (both ".venv"
    # and "venv" are listed), ".git" did not (only the dotted form is listed) --
    # confirmed live 2026-08-14: is_excluded(".git/config") returned False.
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        return False
    parts = [p for p in rel.split("/") if p]
    if parts and parts[0] in _HIVE_OWNED_DIRS:
        return False
    for allow in ALLOW_GLOBS:
        if fnmatch.fnmatch(rel, allow):
            return False
    # Leading dot-directories are skipped, but NOT dotfiles at the root: a repo's
    # .env.example / .gitignore are legitimately readable, and excluding every dotted
    # name would hide them.
    if any(p in EXCLUDE_DIRS for p in parts[:-1]) or (len(parts) > 1 and
                                                      any(p.startswith(".") and p not in (".",)
                                                          for p in parts[:-1])):
        return True
    if parts[-1] in EXCLUDE_DIRS:
        return True
    dot = parts[-1].rfind(".")
    if dot > 0 and parts[-1][dot:].lower() in EXCLUDE_EXTS:
        return True
    return any(fnmatch.fnmatch(rel, g.lstrip("!")) for g in EXCLUDE_GLOBS)
