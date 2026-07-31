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


# An MCP tool that rejects its arguments answers with error TEXT, not an exception, so a
# bad call looks exactly like a successful read. Guarding on "not found" alone let a
# Pydantic argument error through as project knowledge — see _read_file.
_ERR_MARKERS = (
    "validation error",
    "unexpected keyword argument",
    "missing required argument",
    "error executing tool",
    "traceback (most recent call last)",
    "not found",
)


def _looks_like_error(text: str) -> bool:
    head = text[:300].lower()
    return any(m in head for m in _ERR_MARKERS)


async def _read_file(session: ClientSession, path: str) -> str:
    """Read one file via MCP, tolerating either argument name and rejecting error text.

    Both servers name this argument `relative_path` (EkamApp mcp-server/tools/context.py
    and hive-mcp /app/tools/context.py), but this called it `path` — so EVERY read here
    failed, and the Pydantic error was returned as the file's CONTENT. project_context
    came out as 4 KB of validation errors containing no hive.md and none of patterns/,
    which is how agents ended up with no SCSS-Modules rule and none of the 38 GUARDs
    while the code looked like it was loading them (verified on the live stack
    2026-07-30). `path` is kept as a fallback so a differently-shaped server still works.
    """
    for kw in ("relative_path", "path"):
        try:
            result = await session.call_tool("get_file_content", {kw: path})
            content = _extract_text(result)
            if content and not _looks_like_error(content):
                return content
        except Exception:
            continue
    return ""


async def _find_files(session: ClientSession, pattern: str) -> list[str]:
    """Glob via MCP, tolerating either argument name and rejecting error text.

    Same defect class as _read_file, found in the same session: the EkamApp server names
    this `glob_pattern`, this called it `pattern`, and the argument error came back as a
    RESULT rather than an exception. _fetch_patterns then saw no paths and fell through
    to get_project_context, so CLAUDE.md/DOCS.md loaded and patterns/ never did — which
    reads as "context is loading fine" right up until you grep it for '## GUARD'.
    """
    for kw in ("glob_pattern", "pattern"):
        try:
            result = await session.call_tool("find_files", {kw: pattern})
            text = _extract_text(result)
            if text and not _looks_like_error(text):
                paths = [p.strip() for p in text.splitlines() if p.strip()]
                if paths:
                    return paths
        except Exception:
            continue
    return []


async def _fetch_hive_md(session: ClientSession) -> str:
    """Try to read hive.md from the project root via the MCP server."""
    return await _read_file(session, "hive.md")


async def _fetch_patterns(session: ClientSession, patterns_glob: str) -> str:
    # Primary: discover and read pattern files
    try:
        paths = await _find_files(session, patterns_glob)
        if paths:
            parts = []
            for path in paths:
                content = await _read_file(session, path)
                if content:
                    parts.append(content)
            # Only claim success on REAL content. Previously the error strings were
            # truthy, so this returned them and the get_project_context fallback below
            # was never reached — a total priming failure that looked like a success.
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
