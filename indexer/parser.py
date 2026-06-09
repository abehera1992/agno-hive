"""Parse source files into LightRAG-friendly text chunks.

Python files are chunked at AST boundaries (functions, classes) using the
built-in ast module. All other file types fall back to fixed-size text chunks.
"""
import ast
import hashlib
import re
from pathlib import Path

# tiktoken raises ValueError on special tokens like <|endoftext|> by default.
# Strip them from source text before chunking so the indexer never errors on
# files that happen to contain these strings (minified JS, model outputs, etc).
_SPECIAL_TOKEN_RE = re.compile(r"<\|[a-zA-Z0-9_]+\|>")

# Directories and extensions to skip entirely
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".next", ".mypy_cache", ".pytest_cache",
}
_SKIP_EXT = {
    ".pyc", ".pyo", ".pyd", ".so", ".lock", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
    ".zip", ".tar", ".gz", ".bin", ".exe",
}

# Max characters per chunk (~1K tokens, well within qwen3-embedding's 32K context)
_MAX_CHUNK_CHARS = 4000


def iter_files(repo_path: Path) -> list[Path]:
    return sorted(
        p for p in repo_path.rglob("*")
        if p.is_file()
        and not any(d in _SKIP_DIRS for d in p.parts)
        and p.suffix.lower() not in _SKIP_EXT
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def chunk_file(path: Path, repo_root: Path) -> list[str]:
    """Return a list of richly annotated text chunks for this file."""
    rel = str(path.relative_to(repo_root))
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    text = _SPECIAL_TOKEN_RE.sub("", text)
    if not text.strip():
        return []
    if path.suffix.lower() == ".py":
        return _chunk_python(text, rel)
    return _chunk_generic(text, rel, path.suffix.lower())


# ── Python AST chunker ────────────────────────────────────────────────────────

def _chunk_python(source: str, filepath: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _chunk_generic(source, filepath, ".py")

    chunks = []
    lines = source.splitlines()

    # Module docstring
    doc = ast.get_docstring(tree)
    if doc:
        chunks.append(f"File: {filepath}\nType: module\n\n{doc}")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        if len(body) > _MAX_CHUNK_CHARS:
            body = _summarise_large(node, lines)
        node_doc = ast.get_docstring(node) or ""
        header = f"File: {filepath}\nType: {kind}\nName: {node.name}\n"
        if node_doc:
            header += f"Docstring: {node_doc}\n"
        chunks.append(f"{header}\n{body}")

    return chunks or _chunk_generic(source, filepath, ".py")


def _summarise_large(node: ast.AST, lines: list[str]) -> str:
    """For very large nodes, keep header + method signatures only."""
    parts = ["\n".join(lines[node.lineno - 1 : node.lineno + 4])]
    if isinstance(node, ast.ClassDef):
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = lines[child.lineno - 1].strip()
                doc = ast.get_docstring(child)
                parts.append(f"  {sig}" + (f"  # {doc[:80]}" if doc else ""))
    return "\n".join(parts)


# ── Generic chunker ───────────────────────────────────────────────────────────

def _chunk_generic(text: str, filepath: str, suffix: str) -> list[str]:
    lang = suffix.lstrip(".")
    chunks = []
    for i, start in enumerate(range(0, len(text), _MAX_CHUNK_CHARS)):
        piece = text[start : start + _MAX_CHUNK_CHARS]
        header = f"File: {filepath}\nType: {lang}" + (f"\n(part {i + 1})" if i else "")
        chunks.append(f"{header}\n\n{piece}")
    return chunks
