"""Bootstrap phase — runs once at startup before Team construction.

Opens a raw MCP client session, fetches project patterns via file tools,
and returns the combined context string.
"""
from mcp import ClientSession
from mcp.client.sse import sse_client


async def bootstrap(
    mcp_url: str,
    timeout: int,
    patterns_glob: str = "patterns/**/*.md",
) -> str:
    """Return project_context fetched from the MCP server.

    Falls back to "" if the MCP server is unreachable.
    """
    try:
        async with sse_client(mcp_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await _load_from_session(session, patterns_glob)
    except Exception as exc:
        print(f"[agno-hive] bootstrap warning: {exc}")
        return ""


async def _load_from_session(session: ClientSession, patterns_glob: str) -> str:
    return await _fetch_patterns(session, patterns_glob)


async def _fetch_patterns(session: ClientSession, patterns_glob: str) -> str:
    # Primary: discover and read pattern files
    try:
        find_result = await session.call_tool("find_files", {"pattern": patterns_glob})
        paths_text = _extract_text(find_result)
        paths = [p.strip() for p in paths_text.splitlines() if p.strip()]
        if paths:
            parts = []
            for path in paths:
                try:
                    content_result = await session.call_tool("get_file_content", {"path": path})
                    content = _extract_text(content_result)
                    if content:
                        parts.append(content)
                except Exception:
                    pass
            if parts:
                return "\n\n---\n\n".join(parts)
    except Exception:
        pass

    # Fallback: full project context
    try:
        ctx_result = await session.call_tool("get_project_context", {})
        return _extract_text(ctx_result)
    except Exception:
        return ""


def _extract_text(result) -> str:
    if not result or not result.content:
        return ""
    return "\n".join(
        item.text
        for item in result.content
        if hasattr(item, "text") and item.text
    )
