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


async def _run_namespaced_query(query: str, project_id: str, mode: str,
                                 only_need_context: bool) -> str:
    """Shared execution for lightrag_query and lightrag_query_context --
    same namespace fan-out, same failure/empty handling (see the inline
    comments below for why that handling exists), only the QueryParam's own
    only_need_context flag differs. Kept as one function rather than two
    near-duplicates so the empty/error-conflation fix only ever needs
    maintaining in one place.

    only_need_context=True skips LightRAG's own LLM synthesis stage
    entirely (confirmed directly against the deployed LightRAG 1.5.4:
    QueryParam.only_need_context, lightrag/base.py) -- measured live,
    2026-10-24, same "ekam" index: hybrid mode went from ~100-180s
    (synthesis) to 7.5s (context only); local mode from a similar order to
    0.8s. The returned text is the raw retrieved entity/relationship/chunk
    context (still real, grounded data -- not a guess), not a completed
    answer.

    Note (confirmed live the same day): a context-only query essentially
    never returns the literal "[no results]" sentinel, even for a
    deliberately nonsensical query -- retrieval returns its closest
    top-k matches regardless of true relevance, unlike the synthesis path,
    which can affirmatively conclude "nothing relevant" and say so. The
    "[no results]" check below therefore still applies (harmlessly) to the
    synthesis path and is a no-op in practice for the context-only path;
    callers evaluating context-only results must not assume a nonempty
    response is necessarily ON-TOPIC -- that judgment stays downstream,
    exactly like every other retrieval-only lead in this architecture.
    """
    if mode not in ("local", "global", "hybrid", "naive", "mix"):
        return f"Invalid mode '{mode}'. Use: local, global, hybrid."
    try:
        project_rag, global_rag = await asyncio.gather(
            _get_ready_rag(project_id),
            _get_ready_rag(_GLOBAL),
        )
        param = QueryParam(mode=mode, user_prompt=_GROUNDING_GUARD,
                           only_need_context=only_need_context)

        # Query both namespaces in parallel
        project_result, global_result = await asyncio.gather(
            project_rag.aquery(query, param=param),
            global_rag.aquery(query, param=param),
            return_exceptions=True,
        )

        # Namespace failures used to be silently indistinguishable from a
        # genuine empty match: return_exceptions=True turns a raised error into
        # an Exception OBJECT, the string-only checks below never matched it,
        # and both branches fell through to the same "[no results]" sentinel a
        # real empty query also returns -- a caller had no way to tell "nothing
        # is indexed for this" from "the backend could not be reached".
        # Collecting failures separately (bounded, type name + truncated
        # message only -- never a raw credential/token, which a vector-store or
        # embedding connection error does not carry) preserves every existing
        # response shape unchanged for the two common cases (clean success,
        # clean empty) and only adds information for the two that were
        # previously conflated.
        parts = []
        failures = []
        if isinstance(project_result, Exception):
            failures.append(f"project namespace ({project_id}): "
                             f"{type(project_result).__name__}: {str(project_result)[:200]}")
        elif isinstance(project_result, str) and project_result.strip() and project_result != "[no results]":
            parts.append(f"── Project ({project_id}) ──\n{project_result}")
        if isinstance(global_result, Exception):
            failures.append(f"global memory: "
                             f"{type(global_result).__name__}: {str(global_result)[:200]}")
        elif isinstance(global_result, str) and global_result.strip() and global_result != "[no results]":
            parts.append(f"── Global memory ──\n{global_result}")

        if parts:
            result = "\n\n".join(parts)
            if failures:
                result += (
                    f"\n\n── Note: {len(failures)} namespace(s) failed and are "
                    f"NOT reflected above -- treat this result as incomplete: "
                    f"{'; '.join(failures)} ──")
            return result
        if failures:
            # No usable evidence from either namespace, and at least one of
            # them failed outright rather than genuinely finding nothing --
            # report it the same way the outer except below already does, so
            # every existing caller checking for the "Query failed:" prefix
            # classifies this correctly as a failure, not an empty result.
            return f"Query failed: {'; '.join(failures)}"
        return "[no results]"
    except Exception as exc:
        return f"Query failed: {exc}"


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

    Runs full synthesis (an LLM composes a prose answer from the retrieved
    context) -- slower (~100-180s observed) but produces a directly
    readable answer. For a fast, raw-evidence lead instead of a composed
    answer, use lightrag_query_context.
    """
    return await _run_namespaced_query(query, project_id, mode, only_need_context=False)


@mcp.tool()
async def lightrag_query_context(query: str, project_id: str, mode: str = "hybrid") -> str:
    """Query LightRAG for raw retrieval context, skipping LLM synthesis.

    Same namespace fan-out and modes as lightrag_query, but returns the
    retrieved entity/relationship/chunk context directly instead of having
    an LLM compose a prose answer from it -- measured 2026-10-24 against
    the deployed "ekam" index: ~0.8-8.3s versus ~100-180s for the
    synthesis path, across a symbol lookup, a relationship query, and a
    broad discovery query. Intended for callers that want a fast lead to
    verify themselves (e.g. a prefetch step before ordinary file search),
    not a final answer.

    The result is real retrieved data, not a guess -- but is NOT filtered
    for topical relevance the way a synthesized answer can be (retrieval
    returns its closest matches regardless of true relevance). Callers
    must treat this as an unverified lead requiring their own confirmation,
    exactly like a synthesized lightrag_query result.

    Args:
        query:      The question or search query.
        project_id: Project namespace to query within.
        mode:       Retrieval mode — local | global | hybrid.
    """
    return await _run_namespaced_query(query, project_id, mode, only_need_context=True)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
