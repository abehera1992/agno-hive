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

# Grounding guard — appended to every query's generation prompt. Counters the
# RAG failure mode where a leading question ("does it do X via an AI agent?")
# gets its presupposition confirmed: the index holds only code that EXISTS, so
# the absence of a feature cannot be retrieved, and the model otherwise stitches
# a semantically-near file into a false "yes". Forces evidence-or-abstain.
_GROUNDING_GUARD = (
    "Answer strictly from the retrieved context about code that ACTUALLY EXISTS. "
    "Do not confirm a capability just because the question presupposes it. "
    "If the context does not contain direct evidence for a claim, say "
    "'not found in the indexed code' for that claim instead of inferring it from "
    "a similar or adjacent file. Cite the file/function for every concrete claim."
)


async def _get_ready_rag(project_id: str):
    """Return a fully initialized LightRAG instance, calling initialize_storages() once per project."""
    rag = get_rag(project_id)
    if project_id not in _initialized:
        await rag.initialize_storages()
        _initialized.add(project_id)
    return rag


@mcp.tool()
async def lightrag_insert(text: str, project_id: str, file_path: str = "", doc_id: str = "") -> str:
    """Index a block of text into LightRAG for the given project.

    Args:
        text:       The text content to index (code, docs, notes, etc.)
        project_id: Project namespace — keeps each project's data isolated.
        file_path:  Source file the text came from. Doubles as LightRAG's
                    citation-display value AND its filename-dedup identity key
                    — see below, this is NOT safe to pass as a bare relative
                    path when a caller sends more than one document that could
                    share a basename.
        doc_id:     Explicit, caller-assigned unique id for this document.
                    Recommended alongside a unique file_path (see below) so
                    deletion doesn't depend on replicating LightRAG's own
                    id-hash algorithm, which could change between versions.

    CAUTION, confirmed 2026-08-02, two layers deep — passing a unique doc_id
    ALONE does NOT prevent duplicate rejection:

    1. When doc_id is omitted, LightRAG derives one from
       `normalize_document_file_path(file_path)`, which collapses to the
       BASENAME only (`Path(file_path).name`), dropping the directory.

    2. The real gate is a SEPARATE, hard-coded check in
       `apipeline_enqueue_documents` (lightrag/pipeline.py, step "3a.
       Filename-based dedup"): before insert, it looks up any EXISTING
       doc_status row with the same basename and rejects the new document as
       a duplicate if one is found — regardless of what doc_id was supplied.
       Verified empirically: two distinct files sharing a basename, and
       separately two chunks of one file (same file_path for both), BOTH
       still got rejected with distinct explicit doc_ids on each call — the
       second arrival logged "Duplicate document detected (filename)", was
       marked `status: failed`, and produced ZERO chunks in storage.

    The only real fix is making file_path ITSELF basename-unique per document
    — e.g. hive-mcp's index_project folds the full relative path plus a chunk
    index into the final path segment (`rel.replace("/", "__") +
    f"::chunk{i}"`) before it ever reaches `Path(...).name`. Real blast
    radius before this fix: EkamApp has 39 files named page.tsx, 17
    __init__.py, 11 main.py — every file beyond the first with a given
    basename was silently dropped, and independently, any file with more
    than one chunk only ever kept its first chunk. Trade-off: citations then
    show the folded path (e.g. "Client__routes__home__page.tsx::chunk0")
    instead of a clean "page.tsx" — LightRAG's insert API has no separate
    display-only field, so correctness has to win that trade.
    """
    try:
        rag = await _get_ready_rag(project_id)
        clean = _SPECIAL_TOKEN_RE.sub("", text)
        kwargs = {}
        if file_path:
            kwargs["file_paths"] = file_path
        if doc_id:
            kwargs["ids"] = doc_id
        await rag.ainsert(clean, **kwargs)
        return f"Indexed {len(text)} characters for project '{project_id}'."
    except Exception as exc:
        return f"Insert failed: {exc}"


@mcp.tool()
async def lightrag_delete_by_id(doc_id: str, project_id: str) -> str:
    """Delete one previously indexed document by its exact id.

    The reliable counterpart to lightrag_delete_by_file (see its docstring for
    why file_path lookups don't work): callers that assigned an explicit
    doc_id on insert (see lightrag_insert) should delete by that same id
    directly, rather than trying to look a document up by file_path.

    Args:
        doc_id:     The exact document id to delete (as passed to lightrag_insert).
        project_id: Project namespace to delete from.
    """
    try:
        rag = await _get_ready_rag(project_id)
        await rag.adelete_by_doc_id(doc_id)
        return f"Deleted document '{doc_id}' in project '{project_id}'."
    except Exception as exc:
        return f"Delete failed: {exc}"


@mcp.tool()
async def lightrag_delete_by_file(file_path: str, project_id: str) -> str:
    """Delete all previously indexed documents whose stored file_path matches exactly.

    CAUTION — confirmed 2026-08-02: `agno.lightrag_doc_status.file_path` stores
    only the BASENAME LightRAG derived internally (normalize_document_file_path),
    never the full relative path a caller may have passed to lightrag_insert. Two
    consequences:
      1. Calling this with a full relative path (e.g. "training/data/x.jsonl")
         will match ZERO rows even though matching documents exist — the
         comparison never succeeds. Pass a bare basename instead.
      2. Passing a basename deletes EVERY document across the ENTIRE project
         that shares it — not just the one file you intended. EkamApp alone has
         39 files named page.tsx, 17 __init__.py, 11 main.py: calling this with
         file_path="page.tsx" would delete content indexed from all of them.

    Prefer lightrag_delete_by_id with an explicit doc_id you control (see
    lightrag_insert) for anything file-specific. hive-mcp's index_project no
    longer calls this tool for exactly this reason — kept for ad-hoc/manual
    cleanup by basename where the collision is understood and intended.

    Args:
        file_path:  The BASENAME (not full path) whose old docs should be removed.
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
        param = QueryParam(mode=mode, user_prompt=_GROUNDING_GUARD)

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
