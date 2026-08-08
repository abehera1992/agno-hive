"""Project context and file-reading tools.

Generic — works with any project layout. Discovers context files
(CLAUDE.md, AGENTS.md, README.md, DOCS.md) automatically from PROJECT_ROOT.
"""
import ast
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from config import PROJECT_ROOT

from .exclusions import EXCLUDE_DIRS, rg_args

# Exclusions live in one place and apply to search, read, write, scan and index alike.
# Project-specific entries come from EXCLUDE_DIRS / EXCLUDE_GLOBS in the project env.
_IGNORE_DIRS = EXCLUDE_DIRS
_RG_EXCLUDES = rg_args()          # already in ["--glob", "!pat", ...] form

_MAX_OUTPUT_CHARS = config.SEARCH_MAX_OUTPUT_CHARS


def _cap(text: str) -> str:
    """Clamp a tool result to _MAX_OUTPUT_CHARS. Applied on EVERY return path, including
    the pure-Python fallback: max_results bounds the number of LINES, not their length,
    and one matching line in a minified bundle or a JSON blob can be megabytes."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return (text[:_MAX_OUTPUT_CHARS]
            + f"\n... TRUNCATED at {_MAX_OUTPUT_CHARS} chars. Narrow `pattern` or pass a "
              f"`glob_filter` such as '**/*.tsx'.")

_CONTEXT_FILES = [
    "CLAUDE.md", "AGENTS.md", "GEMINI.md",
    "DOCS.md", "README.md", "CONTRIBUTING.md",
]


def _is_ignored(rel: str) -> bool:
    return any(
        p in _IGNORE_DIRS or p.startswith(".")
        for p in rel.replace("\\", "/").split("/")
    )


def _walk_project(root=None):
    """os.walk with ignored-dir pruning."""
    root = root or PROJECT_ROOT
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, str(root))
        for fname in filenames:
            rel = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            yield rel.replace("\\", "/")


def _matches_glob(rel: str, pattern: str) -> bool:
    """Match a relative path against a glob pattern, handling ** correctly."""
    import re as _re
    esc = _re.escape(pattern)
    esc = esc.replace(r"\*\*\/", "(?:.+/)?")
    esc = esc.replace(r"\*\*",   ".*")
    esc = esc.replace(r"\*",     "[^/]*")
    esc = esc.replace(r"\?",     "[^/]")
    return bool(_re.match(f"^{esc}$", rel))


def get_project_context() -> str:
    """
    Return project context by reading CLAUDE.md, AGENTS.md, DOCS.md, README.md,
    and CONTRIBUTING.md if they exist at the project root.
    Always call this at the start of any task to understand the project conventions.
    """
    _CAP = 40_000  # per-file cap so a huge CLAUDE.md/DOCS.md can't dominate the context
    parts = []
    for name in _CONTEXT_FILES:
        path = PROJECT_ROOT / name
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > _CAP:
                content = content[:_CAP] + (
                    f"\n\n… [{name} truncated at {_CAP:,} of {len(content):,} bytes — "
                    f"read it directly with get_file_content('{name}') for the rest]"
                )
            parts.append(f"# {name}\n\n{content}")
    if not parts:
        return "No context files found (CLAUDE.md, AGENTS.md, README.md, DOCS.md)."
    return "\n\n---\n\n".join(parts)


# Files at or below this size are returned whole. Larger files are reduced (skeleton
# for code, head+tail for data) so a single read can never overflow the agent context.
_MAX_FULL_BYTES = 200_000
_CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".kt", ".swift", ".scala",
}


def _py_skeleton(src: str):
    """Structural skeleton of Python source: imports + class/def signatures + the first
    docstring line, with bodies elided. Returns None if the source does not parse."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    lines = src.splitlines()
    out: list[str] = []

    def emit(node, indent):
        pad = "    " * indent
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = ast.get_source_segment(src, node)
            if seg:
                out.append(seg)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                ds = ast.get_source_segment(src, dec)
                if ds:
                    out.append(f"{pad}@{ds}")
            end = node.body[0].lineno - 1 if node.body else node.end_lineno
            end = max(end, node.lineno)  # guard one-line defs
            header = "\n".join(lines[node.lineno - 1:end]).rstrip()
            if header:
                out.append(header)
            doc = ast.get_docstring(node, clean=False)
            if doc:
                out.append(f'{pad}    """{doc.strip().splitlines()[0][:120]}"""')
            if isinstance(node, ast.ClassDef):
                members = [c for c in node.body
                           if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                for c in members:
                    emit(c, indent + 1)
                if not members:
                    out.append(f"{pad}    ...")
            else:
                out.append(f"{pad}    ...")
            out.append("")
        elif indent == 0 and isinstance(node, (ast.Assign, ast.AnnAssign)):
            seg = ast.get_source_segment(src, node)
            if seg:
                out.append(seg.splitlines()[0][:200])

    mod_doc = ast.get_docstring(tree, clean=False)
    if mod_doc:
        out.append(f'"""{mod_doc.strip().splitlines()[0][:160]}"""')
    for node in tree.body:
        emit(node, 0)
    return "\n".join(out).strip()


def _regex_skeleton(src: str):
    """Best-effort declaration-line skeleton for non-Python code (TS/JS/Go/Java/…)."""
    keep = []
    for ln in src.splitlines():
        s = ln.strip()
        if not s:
            continue
        if (s.startswith((
                "import ", "export ", "from ", "package ", "func ", "function ",
                "class ", "interface ", "type ", "enum ", "struct ", "trait ",
                "public ", "private ", "protected ", "def ", "module ", "@"))
                or re.match(r"^(export\s+)?(default\s+)?(async\s+)?function\b", s)
                or re.match(r"^(export\s+)?(const|let|var)\s+\w+\s*[:=]", s)
                or re.match(r"^[\w<>,\[\]\s]+\s+\w+\s*\([^)]*\)\s*[:{]?\s*$", s)):
            keep.append(ln.rstrip())
    return "\n".join(keep) if keep else None


def _numbered_lines(lines: list[str], start: int) -> str:
    """Prefix each line with its 1-based file line number (cat -n style).

    Callers cite `path:line` from what they read here — without a real number on
    every line, a model has to count instead of copy, and silently miscounts.
    Measured live 2026-08-04: without this, an answer citing a docstring's location
    was off by 90-215 lines despite reading the real file content correctly — the
    content was right, there was just nothing to read a line number FROM.
    """
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start=start))


def _find_by_basename(basename: str, max_results: int = 6) -> list[str]:
    """Locate every file in the project with this exact filename, regardless of
    directory. Used by get_file_content()'s not-found fallback below -- a guessed
    path commonly has the right filename but a wrong internal subdirectory (e.g.
    'src/components/user-management/Foo.tsx' guessed vs the real
    'Client/EcommClient-Web/ekamweb/src/components/portal/admin/users/Foo.tsx').
    That's a different failure than find_files()'s GLOB_FALLBACK_PREFIXES handles
    (a missing ROOT prefix on an otherwise-correct relative pattern) -- a basename
    search recovers regardless of how wrong the guessed directory structure is,
    as long as the filename itself is right, which it usually is."""
    import shutil
    rg = shutil.which("rg")
    pattern = f"**/{basename}"
    if rg:
        try:
            return _rg_glob(rg, pattern, max_results)
        except Exception:
            pass
    matches = []
    for p in sorted(PROJECT_ROOT.glob(pattern)):
        rel = p.relative_to(PROJECT_ROOT).as_posix()
        if _is_ignored(rel) or not p.is_file():
            continue
        matches.append(rel)
        if len(matches) >= max_results:
            break
    return matches


def get_file_content(relative_path: str, offset: int = 0, limit: int = 0) -> str:
    """
    Read a file from the project by its path relative to the project root.
    Always read a file before editing it — get the exact content to use as old_string in apply_diff.

    Large files are NOT dumped whole (that overflows the model context): a file over
    ~200KB returns a structural SKELETON (signatures + docstrings, bodies elided) for code,
    or HEAD+TAIL for data files. Pass offset/limit to read an exact line range of any file.

    Output uses cat -n style line numbers ("   123\tactual content") so file:line
    citations can be copied exactly instead of counted/guessed. Never include the
    line-number prefix itself when passing old_string/new_string to apply_diff —
    match only the actual file content after the tab.

    A guessed path that doesn't exist is NOT a dead end: if exactly one file in the
    project has that same filename elsewhere, it is read automatically (prefixed with
    a NOTE stating the correction — use the corrected path from then on). If several
    files share that filename, their paths are listed so the next call can go straight
    to the right one, instead of falling back to a separate find_files/search_files call.

    Args:
        relative_path: e.g. 'src/api/routes.py', 'package.json', 'docker-compose.yml'
        offset: 0-based first line for a ranged read (default 0 = start of file)
        limit:  number of lines to return from offset (default 0 = to end / whole file)
    """
    target = PROJECT_ROOT / relative_path
    if not target.exists():
        basename = Path(relative_path).name
        candidates = _find_by_basename(basename)
        if len(candidates) == 1 and candidates[0] != relative_path:
            corrected = candidates[0]
            note = (
                f"# NOTE: '{relative_path}' not found — '{corrected}' is the only file named "
                f"'{basename}' in the project; reading that instead. Use this exact path from now on.\n"
            )
            return note + get_file_content(corrected, offset, limit)
        if len(candidates) > 1:
            return (
                f"File not found: {relative_path}\n"
                f"{len(candidates)} files named '{basename}' exist — call get_file_content with the exact path:\n"
                + "\n".join(candidates)
            )
        return f"File not found: {relative_path}"
    if not target.is_file():
        return f"Not a file: {relative_path}"
    try:
        data = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Could not read {relative_path}: {e}"

    # Explicit line-range read — exact and bounded, takes precedence.
    if offset or limit:
        lines = data.splitlines()
        start = max(offset, 0)
        end = start + limit if limit > 0 else len(lines)
        body = _numbered_lines(lines[start:end], start + 1)
        return f"# {relative_path} — lines {start}..{min(end, len(lines))} of {len(lines)}\n{body}"

    if len(data) <= _MAX_FULL_BYTES:
        return _numbered_lines(data.splitlines(), 1)

    # Oversized file — reduce it so it can't blow the context window.
    ext = target.suffix.lower()
    if ext in _CODE_EXT:
        skel = _py_skeleton(data) if ext == ".py" else _regex_skeleton(data)
        if skel:
            return (
                f"# {relative_path} is {len(data):,} bytes — too large to return whole.\n"
                f"# STRUCTURAL SKELETON below (signatures + docstrings; bodies elided as ...).\n"
                f"# Read a specific part with get_file_content('{relative_path}', offset=<line>, limit=<n>).\n\n"
                + skel
            )
    head, tail = data[:8000], data[-4000:]
    return (
        f"# {relative_path} is {len(data):,} bytes — too large to return whole (data/non-code file).\n"
        f"# HEAD (8KB) + TAIL (4KB) shown; use search_files to find specific content,\n"
        f"# or get_file_content('{relative_path}', offset=<line>, limit=<n>) for a line range.\n\n"
        f"===== HEAD =====\n{head}\n\n===== TAIL =====\n{tail}"
    )


async def get_files_batch(paths: list[str]) -> str:
    """
    Read multiple files in ONE tool call, in parallel -- use this instead of several
    separate get_file_content() calls when you already know which files you need
    (e.g. a pattern file + the file you're about to edit + a related test file).

    Each file is read with get_file_content()'s exact same behavior (line numbers,
    skeleton-on-oversized, not-found message) -- this only parallelizes the I/O,
    it does not change what comes back for any individual file.

    Args:
        paths: relative paths, e.g. ['src/api/routes.py', 'src/api/models.py']
    """
    async def _read_one(p: str) -> str:
        return await asyncio.to_thread(get_file_content, p)

    outcomes = await asyncio.gather(*(_read_one(p) for p in paths), return_exceptions=True)
    sections = []
    for p, outcome in zip(paths, outcomes):
        if isinstance(outcome, BaseException):
            sections.append(f"=== {p} ===\nERROR: {outcome}")
        else:
            sections.append(f"=== {p} ===\n{outcome}")
    return _cap("\n\n".join(sections))


async def search_files_batch(pattern: str, glob_filters: list[str], max_results: int = 80) -> str:
    """
    Search the SAME pattern across multiple glob scopes in ONE tool call, in parallel --
    use this instead of several separate search_files() calls when checking a term
    across file types (e.g. '**/*.py' and '**/*.ts') or across multiple directories.

    Args:
        pattern:      Regex or literal string to search for (same as search_files)
        glob_filters: e.g. ['**/*.py', '**/*.ts'] -- one search per glob, in parallel
        max_results:  Max matching lines PER GLOB (default 80, same as search_files)
    """
    async def _search_one(g: str) -> str:
        return await asyncio.to_thread(search_files, pattern, g, max_results)

    outcomes = await asyncio.gather(*(_search_one(g) for g in glob_filters), return_exceptions=True)
    sections = []
    for g, outcome in zip(glob_filters, outcomes):
        if isinstance(outcome, BaseException):
            sections.append(f"=== glob: {g} ===\nERROR: {outcome}")
        else:
            sections.append(f"=== glob: {g} ===\n{outcome}")
    return _cap("\n\n".join(sections))


# When PROJECT_ROOT is a monorepo, short paths like "src/lib/**" are often relative to a
# subdirectory (e.g. a frontend root), not PROJECT_ROOT itself. Try these prefixes in
# order before giving up.
#
# Kept PROJECT-INDEPENDENT: "**" resolves src/lib/** to **/src/lib/** and works for any
# layout. A hardcoded subdirectory path used to sit here, which only helped one repo and
# was dead weight (or a wrong first guess) in every other. Projects needing an explicit
# prefix can set GLOB_FALLBACK_PREFIXES in their env file.
_GLOB_FALLBACK_PREFIXES = config.GLOB_FALLBACK_PREFIXES + ["**"]


def _rg_glob(rg: str, glob_pattern: str, max_results: int) -> list[str]:
    import subprocess
    result = subprocess.run(
        [rg, "--files", "--glob", glob_pattern],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
    )
    if not result.stdout and result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip())
    return sorted([l for l in result.stdout.splitlines() if l.strip()][:max_results])


def find_files(glob_pattern: str, max_results: int = 200) -> str:
    """
    Find files by glob pattern. Returns paths relative to project root.
    Uses ripgrep when available (fast, respects .gitignore); falls back to pathlib.

    Short paths relative to the frontend root (e.g. 'src/lib/**') are automatically
    resolved via fallback prefixes — agents do not need to know the full path.

    Args:
        glob_pattern: e.g. '**/*.py', 'src/**/*.ts', '**/docker-compose*.yml'

    Examples:
        find_files('**/*.py')               → all Python files
        find_files('src/**/*.ts')           → TypeScript in src/ (auto-prefixed if needed)
        find_files('src/lib/api/services/**') → frontend service files (auto-prefixed)
        find_files('**/Dockerfile*')        → all Dockerfiles
        find_files('**/.env*')              → env files
    """
    import shutil
    rg = shutil.which("rg")
    if rg:
        try:
            matches = _rg_glob(rg, glob_pattern, max_results)
            if not matches:
                for prefix in _GLOB_FALLBACK_PREFIXES:
                    matches = _rg_glob(rg, f"{prefix}/{glob_pattern}", max_results)
                    if matches:
                        break
            if not matches:
                return f"No matches for: {glob_pattern}"
            return f"{len(matches)} result(s) for '{glob_pattern}':\n" + "\n".join(matches)
        except Exception:
            pass  # fall through to pathlib

    matches = []
    patterns_to_try = [glob_pattern] + [f"{p}/{glob_pattern}" for p in _GLOB_FALLBACK_PREFIXES]
    try:
        for pat in patterns_to_try:
            for p in sorted(PROJECT_ROOT.glob(pat)):
                rel = p.relative_to(PROJECT_ROOT).as_posix()
                if _is_ignored(rel):
                    continue
                matches.append(rel + "/" if p.is_dir() else rel)
                if len(matches) >= max_results:
                    break
            if matches:
                break
    except Exception as e:
        return f"find_files failed: {e}"

    if not matches:
        return f"No matches for: {glob_pattern}"
    return f"{len(matches)} result(s) for '{glob_pattern}':\n" + "\n".join(matches)


def search_files(pattern: str, glob_filter: str = "**/*", max_results: int = 80) -> str:
    """
    Search file contents with a regex pattern. Returns matching lines with path:line: content.
    Uses ripgrep when available (fast, respects .gitignore); falls back to Python re.

    Args:
        pattern:     Regex or literal string to search for
        glob_filter: Restrict to files matching this glob (e.g. '**/*.py', '**/*.ts')
        max_results: Max matching lines to return (default 80)

    Examples:
        search_files('def handle_', '**/*.py')        → Python handler functions
        search_files('import.*React', '**/*.tsx')     → React imports
        search_files('WRITE_REVIEW', '**/*.py')       → env var usages
    """
    import subprocess, shutil
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "-i", "--no-heading", "--max-count", "1"]
        # Only pass glob_filter when it NARROWS. Passing the catch-all "**/*" as an
        # --glob INCLUDE overrides ripgrep's .gitignore handling, so rg descends into
        # node_modules and every other ignored tree. Measured 2026-07-30 on a large repo:
        #   symbol A: with "**/*" 55.0s / 3 matches  -- without 11.8s / 0 matches
        #   symbol B: with "**/*" 45.7s / 611 matches -- without 12.7s / 140 matches
        # Both slower AND wrong: the extra "matches" were all inside vendored dependency
        # code, so an agent checking whether a symbol exists in the PROJECT was handed
        # evidence that it does. The search tool was manufacturing support for the
        # fabrication it is supposed to help catch.
        if glob_filter and glob_filter not in ("**/*", "**", "*"):
            cmd += ["--glob", glob_filter]
        cmd += _RG_EXCLUDES
        cmd.append(pattern)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError(result.stderr.strip())
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            if not lines:
                return f"No matches for: {pattern}"
            # rg output: path:line:content — reformat to path:line: content
            out = []
            for ln in lines[:max_results]:
                parts = ln.split(":", 2)
                if len(parts) == 3:
                    out.append(f"{parts[0]}:{parts[1]}: {parts[2]}")
                else:
                    out.append(ln)
            text = "\n".join(out)
            if len(text) > _MAX_OUTPUT_CHARS:
                text = (text[:_MAX_OUTPUT_CHARS]
                        + f"\n... TRUNCATED at {_MAX_OUTPUT_CHARS} chars "
                          f"({len(lines)} matching files). Narrow `pattern` or pass a "
                          f"`glob_filter` such as '**/*.tsx'.")
            return text
        except subprocess.TimeoutExpired:
            # Do NOT fall through. The Python fallback re-reads the whole tree with no
            # timeout, so a 30s rg timeout escalated into a multi-minute hang — that is
            # what blew the agent's 300s MCP deadline on 2026-07-30 and left it answering
            # from priors instead of the repo. Fail fast so it narrows and retries.
            return (f"search timed out after 30s for pattern: {pattern!r}. "
                    f"Narrow it, or pass a `glob_filter` like '**/*.py' or '**/*.tsx'.")
        except Exception:
            pass  # genuine rg failure (not a timeout) — fall through to Python re

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Invalid regex: {e}"

    results = []
    try:
        for rel in _walk_project():
            if not _matches_glob(rel, glob_filter):
                continue
            fpath = PROJECT_ROOT / rel
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if regex.search(line):
                            results.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(results) >= max_results:
                                return _cap("\n".join(results))
            except Exception:
                continue
    except Exception as e:
        return f"search_files failed: {e}"

    return _cap("\n".join(results)) if results else f"No matches for: {pattern}"


def count_matches(pattern: str, glob_filter: str = "**/*",
                  fixed_string: bool = False, ignore_case: bool = False) -> str:
    """
    Count occurrences of a pattern across files — DETERMINISTIC, computed by ripgrep.

    USE THIS FOR ANY count / total / "how many" / "all" question over file contents.
    NEVER count by reading a file and tallying in your head — that is unreliable and is
    treated as a fabrication. This tool returns the EXACT total (occurrences, matching
    grep -oE ... | wc -l) plus a per-file breakdown. The first line is always
    "TOTAL: <n> ..." so the number can be read/relayed verbatim.

    Args:
        pattern:      regex to count (or a literal string if fixed_string=True).
        glob_filter:  restrict to files matching this glob, e.g. '**/*.py',
                      '**/gst_resolver.py'. Default '**/*' = every file.
        fixed_string: treat pattern as a literal string, not a regex.
        ignore_case:  case-insensitive match.

    Examples:
        count_matches(': *12\\.0', '**/gst_resolver.py')  → occurrences of ': 12.0'
        count_matches('def ', '**/*.py')                  → function-def lines
        count_matches('TODO', '**/*', fixed_string=True)  → literal TODO count
    """
    import subprocess, shutil
    rg = shutil.which("rg")
    if not rg:
        return "count_matches unavailable: ripgrep (rg) not installed in this environment"
    args = [rg, "--count-matches", "--glob", glob_filter]
    if fixed_string:
        args.append("-F")
    if ignore_case:
        args.append("-i")
    args.append(pattern)
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
        )
        # rg exit: 0 = matches, 1 = no matches, 2+ = real error
        if result.returncode not in (0, 1):
            return f"count_matches failed: {result.stderr.strip() or 'rg error'}"
        total = 0
        per_file: list[tuple[str, int]] = []
        for ln in result.stdout.splitlines():
            path, sep, cnt = ln.rpartition(":")
            if not sep:
                continue
            try:
                c = int(cnt)
            except ValueError:
                continue
            total += c
            per_file.append((path, c))
        head = (f"TOTAL: {total} match(es) for pattern {pattern!r} in '{glob_filter}' "
                f"across {len(per_file)} file(s)")
        if not per_file:
            return f"TOTAL: 0 match(es) for pattern {pattern!r} in '{glob_filter}'"
        body = "\n".join(f"  {p}: {c}" for p, c in sorted(per_file, key=lambda x: -x[1])[:50])
        return head + "\n" + body
    except subprocess.TimeoutExpired:
        return "count_matches failed: timed out after 30s"
    except Exception as e:
        return f"count_matches failed: {e}"


def list_directory_tree(max_depth: int = 3) -> str:
    """
    Return the full directory tree of the project up to max_depth levels deep.
    Shows directories only (no individual files) — no result cap.
    Use this for any overview or structure question before drilling into specific directories.
    Prefer this over find_files('**/*') for structure questions — no cap, always complete.

    Args:
        max_depth: How many levels deep to traverse (default 3)

    Examples:
        list_directory_tree()    → full project structure, 3 levels deep
        list_directory_tree(2)   → top 2 levels only
    """
    lines = []

    def _recurse(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(path.iterdir())
        except PermissionError:
            return
        dirs = [c for c in children
                if c.is_dir() and c.name not in _IGNORE_DIRS and not c.name.startswith(".")]
        for d in dirs:
            indent = "  " * (depth - 1)
            lines.append(f"{indent}{d.name}/")
            _recurse(d, depth + 1)

    _recurse(PROJECT_ROOT, 1)
    if not lines:
        return "No directories found."
    return f"Project directory tree (dirs only, {max_depth} levels deep):\n" + "\n".join(lines)


def list_directory(relative_path: str = "") -> str:
    """
    List the contents of a directory in the project.
    Useful for exploring unknown project structures.

    Args:
        relative_path: Path relative to project root (default: project root).
                       e.g. 'src/components', 'API/auth-service'
    """
    target = PROJECT_ROOT / relative_path if relative_path else PROJECT_ROOT
    if not target.exists():
        return f"Not found: {relative_path}"
    if not target.is_dir():
        return f"Not a directory: {relative_path}"

    entries = []
    try:
        for item in sorted(target.iterdir()):
            if item.name in _IGNORE_DIRS or item.name.startswith("."):
                continue
            marker = "[DIR] " if item.is_dir() else "[FILE]"
            entries.append(f"{marker} {item.name}")
    except Exception as e:
        return f"list_directory failed: {e}"

    if not entries:
        return f"Empty: {relative_path or '(project root)'}"
    return f"{relative_path or '(project root)'}  ({len(entries)} items):\n" + "\n".join(entries)
