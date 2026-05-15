"""Bootstrap phase — runs once at startup before Team construction.

Opens a raw MCP client session, fetches project patterns via file tools,
and returns the combined context string.
"""
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def bootstrap(
    mcp_url: str,
    timeout: int,
    patterns_glob: str = "patterns/**/*.md",
    extra_urls: list[str] | None = None,
) -> str:
    """Return project_context fetched from the first reachable MCP server.

    Tries extra_urls (hive-mcp) first, falls back to mcp_url (project-mcp).
    Returns "" if all servers are unreachable.
    """
    urls_to_try = [u for u in (extra_urls or []) + [mcp_url] if u]
    for url in urls_to_try:
        try:
            async with streamablehttp_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await _load_from_session(session, patterns_glob)
                    if result:
                        print(f"[agno-hive] bootstrap loaded from {url}")
                    return result
        except Exception as exc:
            print(f"[agno-hive] bootstrap warning ({url}): {exc}")
    return ""


async def _load_from_session(session: ClientSession, patterns_glob: str) -> str:
    parts = []

    # Priority 1: hive.md — pre-built project context snapshot written by scan_project_context.
    # Injected first so the coordinator sees grounded project knowledge before anything else.
    hive = await _fetch_hive_md(session)
    if hive:
        parts.append(hive)

    # Priority 2: pattern files (patterns/**/*.md)
    patterns = await _fetch_patterns(session, patterns_glob)
    if patterns:
        parts.append(patterns)

    return "\n\n---\n\n".join(parts) if parts else ""


async def _fetch_hive_md(session: ClientSession) -> str:
    """Try to read hive.md from the project root via the MCP server."""
    try:
        result = await session.call_tool("get_file_content", {"path": "hive.md"})
        content = _extract_text(result)
        if content and "not found" not in content.lower()[:40]:
            return content
    except Exception:
        pass
    return ""


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
