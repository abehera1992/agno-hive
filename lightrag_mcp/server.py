"""LightRAG MCP server — exposes lightrag_insert and lightrag_query as MCP tools over Streamable HTTP."""
import asyncio
import os
import re
from dotenv import load_dotenv

# Strip tiktoken special tokens (e.g. <|endoftext|>) that raise ValueError on encode.
_SPECIAL_TOKEN_RE = re.compile(r"<\|[a-zA-Z0-9_]+\|>")

load_dotenv()

from mcp.server.fastmcp import FastMCP
from lightrag import QueryParam

from .rag import get_rag

_mcp_host = os.getenv("LIGHTRAG_MCP_HOST", "0.0.0.0")
_mcp_port = int(os.getenv("LIGHTRAG_MCP_PORT", "9002"))
mcp = FastMCP("lightrag", host=_mcp_host, port=_mcp_port)

_GLOBAL = "global"  # cross-project shared namespace
_initialized: set[str] = set()  # tracks which project RAGs have been initialized


async def _get_ready_rag(project_id: str):
    """Return a fully initialized LightRAG instance, calling initialize_storages() once per project."""
    rag = get_rag(project_id)
    if project_id not in _initialized:
        await rag.initialize_storages()
        _initialized.add(project_id)
    return rag


@mcp.tool()
async def lightrag_insert(text: str, project_id: str, file_path: str = "") -> str:
    """Index a block of text into LightRAG for the given project.

    Args:
        text:       The text content to index (code, docs, notes, etc.)
        project_id: Project namespace — keeps each project's data isolated.
        file_path:  Source file the text came from (relative path). Stored as
                    doc metadata so stale versions can be deleted when the
                    file is re-indexed (see lightrag_delete_by_file).
    """
    try:
        rag = await _get_ready_rag(project_id)
        clean = _SPECIAL_TOKEN_RE.sub("", text)
        if file_path:
            await rag.ainsert(clean, file_paths=file_path)
        else:
            await rag.ainsert(clean)
        return f"Indexed {len(text)} characters for project '{project_id}'."
    except Exception as exc:
        return f"Insert failed: {exc}"


@mcp.tool()
async def lightrag_delete_by_file(file_path: str, project_id: str) -> str:
    """Delete all previously indexed documents that came from a source file.

    Call BEFORE re-inserting a changed file's chunks — LightRAG indexing is
    append-only, so without this, stale versions of changed files accumulate
    and can win retrieval over the current content.

    Args:
        file_path:  The relative source path whose old docs should be removed.
        project_id: Project namespace to delete from.
    """
    try:
        import asyncpg
        from config.config import config

        rag = await _get_ready_rag(project_id)
        conn = await asyncpg.connect(config.postgres_uri)
        try:
            rows = await conn.fetch(
                "SELECT id FROM agno.lightrag_doc_status WHERE workspace=$1 AND file_path=$2",
                project_id, file_path,
            )
        finally:
            await conn.close()
        ids = [r["id"] for r in rows]
        for doc_id in ids:
            await rag.adelete_by_doc_id(doc_id)
        return f"Deleted {len(ids)} stale document(s) for '{file_path}' in project '{project_id}'."
    except Exception as exc:
        return f"Delete failed: {exc}"


@mcp.tool()
async def lightrag_insert_global(text: str) -> str:
    """Index a cross-project insight into the shared global memory.

    Use this for learnings that apply across all projects — architectural
    patterns, conventions, debugging approaches, or reusable solutions.
    These are retrievable by any project via lightrag_query.

    Args:
        text: The insight or pattern to store globally.
    """
    try:
        rag = await _get_ready_rag(_GLOBAL)
        await rag.ainsert(_SPECIAL_TOKEN_RE.sub("", text))
        return f"Indexed {len(text)} characters into global memory."
    except Exception as exc:
        return f"Global insert failed: {exc}"


@mcp.tool()
async def lightrag_query(query: str, project_id: str, mode: str = "hybrid") -> str:
    """Query LightRAG — searches both project-specific and global memory.

    Results from the project namespace and global shared namespace are merged,
    giving agents cross-project context alongside project-specific knowledge.

    Modes:
      local  — entity-centric: finds specific files, symbols, exact syntax.
      global — relationship-centric: finds cross-module themes and patterns.
      hybrid — runs both and merges results (default, recommended).

    Args:
        query:      The question or search query.
        project_id: Project namespace to query within.
        mode:       Retrieval mode — local | global | hybrid.
    """
    if mode not in ("local", "global", "hybrid", "naive", "mix"):
        return f"Invalid mode '{mode}'. Use: local, global, hybrid."
    try:
        project_rag, global_rag = await asyncio.gather(
            _get_ready_rag(project_id),
            _get_ready_rag(_GLOBAL),
        )
        param = QueryParam(mode=mode)

        # Query both namespaces in parallel
        project_result, global_result = await asyncio.gather(
            project_rag.aquery(query, param=param),
            global_rag.aquery(query, param=param),
            return_exceptions=True,
        )

        parts = []
        if isinstance(project_result, str) and project_result.strip() and project_result != "[no results]":
            parts.append(f"── Project ({project_id}) ──\n{project_result}")
        if isinstance(global_result, str) and global_result.strip() and global_result != "[no results]":
            parts.append(f"── Global memory ──\n{global_result}")

        return "\n\n".join(parts) if parts else "[no results]"
    except Exception as exc:
        return f"Query failed: {exc}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
