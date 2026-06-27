"""Project context and file-reading tools.

Generic — works with any project layout. Discovers context files
(CLAUDE.md, AGENTS.md, README.md, DOCS.md) automatically from PROJECT_ROOT.
"""
import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT

_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".next", "dist", "build",
    ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", "coverage",
    ".tox", ".eggs", "*.egg-info",
}

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


def get_file_content(relative_path: str, offset: int = 0, limit: int = 0) -> str:
    """
    Read a file from the project by its path relative to the project root.
    Always read a file before editing it — get the exact content to use as old_string in apply_diff.

    Large files are NOT dumped whole (that overflows the model context): a file over
    ~200KB returns a structural SKELETON (signatures + docstrings, bodies elided) for code,
    or HEAD+TAIL for data files. Pass offset/limit to read an exact line range of any file.

    Args:
        relative_path: e.g. 'src/api/routes.py', 'package.json', 'docker-compose.yml'
        offset: 0-based first line for a ranged read (default 0 = start of file)
        limit:  number of lines to return from offset (default 0 = to end / whole file)
    """
    target = PROJECT_ROOT / relative_path
    if not target.exists():
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
        body = "\n".join(lines[start:end])
        return f"# {relative_path} — lines {start}..{min(end, len(lines))} of {len(lines)}\n{body}"

    if len(data) <= _MAX_FULL_BYTES:
        return data

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


# When PROJECT_ROOT is a monorepo, short paths like "src/lib/**" are often
# relative to a subdirectory (e.g. the frontend root), not PROJECT_ROOT itself.
# Try these prefixes in order before giving up.
_GLOB_FALLBACK_PREFIXES = [
    "Client/EcommClient-Web/ekamweb",  # src/lib/** → ekamweb/src/lib/**
    "**",                               # src/lib/** → **/src/lib/**
]


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
        try:
            result = subprocess.run(
                [rg, "-n", "-i", "--no-heading", "--glob", glob_filter,
                 "--max-count", "1", pattern],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
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
            return "\n".join(out)
        except Exception:
            pass  # fall through to Python re

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
                                return "\n".join(results)
            except Exception:
                continue
    except Exception as e:
        return f"search_files failed: {e}"

    return "\n".join(results) if results else f"No matches for: {pattern}"


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
