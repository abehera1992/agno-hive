"""Project indexing tool — walks the project, chunks files, inserts into LightRAG.

Connects to the LightRAG MCP server via Streamable HTTP and calls
lightrag_insert for each chunk. Tracks SHA-256 state for incremental
re-indexing so unchanged files are skipped.
"""
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT

_STATE_DIR  = Path(os.getenv("HIVE_INDEX_STATE_DIR", str(PROJECT_ROOT / ".hive-index-state")))
_CHUNK_SIZE = 4000   # chars for non-Python files
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".next", "dist", "build",
    ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", "coverage",
    ".tox", ".eggs",
}
_SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".bin", ".exe", ".dll", ".so", ".dylib", ".zip",
    ".tar", ".gz", ".pdf", ".lock",
}


# ── File chunking ─────────────────────────────────────────────────────────────

def _py_chunks(path: Path) -> list[str]:
    """Extract module docstring + each function/class as a separate chunk."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree   = ast.parse(source)
    except Exception:
        return _text_chunks(path)

    chunks = []
    rel = path.relative_to(PROJECT_ROOT).as_posix()

    # Module-level docstring
    if (ast.get_docstring(tree) or "").strip():
        chunks.append(f"File: {rel}\nType: module\n\n{ast.get_docstring(tree)}")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        try:
            segment = ast.get_source_segment(source, node) or ""
        except Exception:
            continue
        if len(segment) < 30:
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        doc  = ast.get_docstring(node) or ""
        header = f"File: {rel}\nType: {kind}\nName: {node.name}"
        if doc:
            header += f"\nDocstring: {doc}"
        chunks.append(f"{header}\n\n{segment[:_CHUNK_SIZE]}")

    return chunks if chunks else _text_chunks(path)


def _text_chunks(path: Path) -> list[str]:
    """Split any text file into fixed-size chunks with a file/type header."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    rel  = path.relative_to(PROJECT_ROOT).as_posix()
    ext  = path.suffix.lstrip(".") or "text"
    header = f"File: {rel}\nType: {ext}\n\n"
    chunks = []
    for i in range(0, len(text), _CHUNK_SIZE):
        chunk = text[i : i + _CHUNK_SIZE]
        if chunk.strip():
            chunks.append(header + chunk)
    return chunks


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── State tracking ────────────────────────────────────────────────────────────

def _load_state(project_id: str) -> dict:
    f = _STATE_DIR / f"{project_id}.json"
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        return {}


def _save_state(project_id: str, state: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    (_STATE_DIR / f"{project_id}.json").write_text(json.dumps(state))


# ── LightRAG MCP client ───────────────────────────────────────────────────────

async def _insert_via_mcp(lightrag_url: str, text: str, project_id: str) -> bool:
    """Send one chunk to LightRAG MCP server via Streamable HTTP."""
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
        async with streamablehttp_client(lightrag_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "lightrag_insert",
                    {"text": text, "project_id": project_id},
                )
                return not (result.isError if hasattr(result, "isError") else False)
    except Exception:
        return False


# ── Main tool ─────────────────────────────────────────────────────────────────

async def index_project(
    project_id: str,
    lightrag_url: str,
    glob_filter: str = "**/*",
    force: bool = False,
) -> str:
    """
    Index the project into LightRAG for semantic search and knowledge graph queries.

    Walks the project directory, chunks files (Python files are parsed with AST
    for function/class-level granularity; all other files use text windows).
    Skips unchanged files based on SHA-256 checksums unless force=True.
    Connects to the LightRAG MCP server at lightrag_url via Streamable HTTP.

    Args:
        project_id:   Namespace for this project in LightRAG (e.g. 'ekam', 'myapp').
        lightrag_url: LightRAG MCP server URL (e.g. 'http://100.96.86.82:9002/mcp').
        glob_filter:  Glob to restrict which files are indexed (default: all files).
        force:        Re-index all files even if unchanged (default: False).

    Examples:
        index_project('myapp', 'http://100.96.86.82:9002/mcp')
        index_project('myapp', 'http://100.96.86.82:9002/mcp', glob_filter='**/*.py')
        index_project('myapp', 'http://100.96.86.82:9002/mcp', force=True)
    """
    state      = {} if force else _load_state(project_id)
    new_state  = dict(state)

    files_seen = files_skipped = chunks_sent = errors = 0
    lines      = [f"Indexing project '{project_id}' → {lightrag_url}"]

    for p in sorted(PROJECT_ROOT.glob(glob_filter)):
        if not p.is_file():
            continue
        rel = p.relative_to(PROJECT_ROOT).as_posix()

        # Skip ignored dirs and binary extensions
        parts = rel.split("/")
        if any(part in _IGNORE_DIRS or part.startswith(".") for part in parts):
            continue
        if p.suffix.lower() in _SKIP_EXTS:
            continue

        files_seen += 1
        try:
            sha = _sha(p)
        except Exception:
            continue

        if not force and state.get(rel) == sha:
            files_skipped += 1
            continue

        # Choose chunker
        chunks = _py_chunks(p) if p.suffix == ".py" else _text_chunks(p)

        ok = True
        for chunk in chunks:
            success = await _insert_via_mcp(lightrag_url, chunk, project_id)
            if success:
                chunks_sent += 1
            else:
                errors += 1
                ok = False

        if ok:
            new_state[rel] = sha

    _save_state(project_id, new_state)

    lines += [
        f"Files scanned:  {files_seen}",
        f"Files skipped:  {files_skipped}  (unchanged)",
        f"Files indexed:  {files_seen - files_skipped}",
        f"Chunks sent:    {chunks_sent}",
        f"Errors:         {errors}",
        "Done — project knowledge is now queryable via lightrag_query.",
    ]
    return "\n".join(lines)
