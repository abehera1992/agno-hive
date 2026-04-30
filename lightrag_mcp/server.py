"""LightRAG MCP server — exposes lightrag_insert and lightrag_query as MCP tools over SSE."""
import os
from dotenv import load_dotenv

load_dotenv()

from mcp.server.fastmcp import FastMCP
from lightrag import QueryParam

from .rag import get_rag

mcp = FastMCP("lightrag")


@mcp.tool()
async def lightrag_insert(text: str, project_id: str) -> str:
    """Index a block of text into LightRAG for the given project.

    LightRAG extracts entities and relationships via the configured LLM
    and stores them in Qdrant (vectors) and PostgreSQL/AGE (graph).

    Args:
        text:       The text content to index (code, docs, notes, etc.)
        project_id: Project namespace — keeps each project's data isolated.
    """
    try:
        rag = get_rag(project_id)
        await rag.ainsert(text)
        return f"Indexed {len(text)} characters for project '{project_id}'."
    except Exception as exc:
        return f"Insert failed: {exc}"


@mcp.tool()
async def lightrag_query(query: str, project_id: str, mode: str = "hybrid") -> str:
    """Query LightRAG for the given project using the specified retrieval mode.

    Modes:
      local  — entity-centric: finds specific files, symbols, exact syntax.
               Best for: "What is the exact syntax of the User model in models.py?"
      global — relationship-centric: finds cross-module themes and patterns.
               Best for: "How does the entire Ekam project handle database transactions?"
      hybrid — runs both and merges results (default, recommended for most queries).

    Args:
        query:      The question or search query.
        project_id: Project namespace to query within.
        mode:       Retrieval mode — local | global | hybrid.
    """
    if mode not in ("local", "global", "hybrid", "naive", "mix"):
        return f"Invalid mode '{mode}'. Use: local, global, hybrid."
    try:
        rag = get_rag(project_id)
        result = await rag.aquery(query, param=QueryParam(mode=mode))
        return result or "[no results]"
    except Exception as exc:
        return f"Query failed: {exc}"


if __name__ == "__main__":
    port = int(os.getenv("LIGHTRAG_MCP_PORT", "9002"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
