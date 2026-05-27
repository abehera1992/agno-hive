"""Notion integration tools.

Auth: NOTION_API_KEY environment variable (integration token from notion.so/my-integrations).
Reads pass through immediately. Writes are staged for human review when WRITE_REVIEW=true.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from config import WRITE_REVIEW
from tools.integrations import _stage_action, register_executor

_NOTION_VERSION = "2022-06-28"
_NOTION_BASE    = "https://api.notion.com/v1"


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_API_KEY}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, body: dict | None = None) -> dict:
    import httpx
    resp = httpx.request(
        method, f"{_NOTION_BASE}{path}",
        headers=_headers(),
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ── Execute dispatcher (registered with the integration registry) ─────────────

def _execute(tool: str, args: dict) -> str:
    """Execute a confirmed Notion write operation."""
    try:
        if tool == "create_page":
            result = _request("POST", "/pages", _build_create_payload(**args))
            return f"notion page created: {result.get('url', result.get('id'))}"

        if tool == "update_page_props":
            page_id    = args["page_id"]
            properties = args["properties"]
            result     = _request("PATCH", f"/pages/{page_id}", {"properties": properties})
            return f"notion page updated: {result.get('url', result.get('id'))}"

        if tool == "append_blocks":
            block_id  = args["block_id"]
            body: dict = {"children": args["children"]}
            if args.get("after"):
                body["after"] = args["after"]
            result = _request("PATCH", f"/blocks/{block_id}/children", body)
            count  = len(result.get("results", []))
            pos    = f" after {args['after'][:8]}" if args.get("after") else ""
            return f"notion: appended {count} block(s) to {block_id}{pos}"

        if tool == "create_database":
            result = _request("POST", "/databases", args["payload"])
            return f"notion database created: {result.get('url', result.get('id'))}"

        return f"unknown notion tool: {tool}"
    except Exception as e:
        return f"notion execute failed ({tool}): {e}"


register_executor("notion", _execute)


# ── Read tools (no approval required) ────────────────────────────────────────

def notion_search(query: str, filter_type: str = "") -> str:
    """
    Search Notion pages and databases by title or content.
    Read-only — no approval required.

    Args:
        query:       Search terms
        filter_type: Optional — 'page' or 'database' to narrow results
    """
    body: dict = {"query": query, "page_size": 10}
    if filter_type in ("page", "database"):
        body["filter"] = {"value": filter_type, "property": "object"}
    try:
        data    = _request("POST", "/search", body)
        results = data.get("results", [])
        if not results:
            return f"notion: no results for '{query}'"
        lines = [f"notion search — {len(results)} result(s) for '{query}':"]
        for r in results:
            obj   = r.get("object", "?")
            rid   = r.get("id", "?")
            url   = r.get("url", "")
            title = _extract_title(r)
            lines.append(f"  [{obj}] {title}  id={rid}  {url}")
        return "\n".join(lines)
    except Exception as e:
        return f"notion_search failed: {e}"


def notion_get_page(page_id: str) -> str:
    """
    Read a Notion page's properties and top-level blocks.
    Read-only — no approval required.

    Args:
        page_id: Notion page ID (32-char hex, UUID format, or last segment of page URL)
    """
    try:
        clean  = _clean_id(page_id)
        page   = _request("GET", f"/pages/{clean}")
        blocks = _request("GET", f"/blocks/{clean}/children?page_size=25")
        title  = _extract_title(page)
        url    = page.get("url", "")
        lines  = [f"notion page: {title}  ({url})"]
        for b in blocks.get("results", []):
            lines.append("  " + _block_summary(b))
        return "\n".join(lines)
    except Exception as e:
        return f"notion_get_page failed: {e}"


# ── Write tools (staged when WRITE_REVIEW=true) ───────────────────────────────

def notion_create_page(
    parent_id: str,
    title: str,
    content: str = "",
    parent_type: str = "database_id",
) -> str:
    """
    Create a new Notion page inside a database or as a child of another page.
    Requires human approval when WRITE_REVIEW is enabled.

    Args:
        parent_id:   ID of the parent database or page
        title:       Page title
        content:     Optional plain-text paragraph added as the first block
        parent_type: 'database_id' (default) or 'page_id'
    """
    if WRITE_REVIEW:
        return _stage_action(
            "notion", "create_page",
            f"Create page '{title}' in {parent_type.replace('_id', '')} {parent_id[:8]}...",
            {"parent_id": parent_id, "parent_type": parent_type,
             "title": title, "content": content},
        )
    return _execute("create_page", {
        "parent_id": parent_id, "parent_type": parent_type,
        "title": title, "content": content,
    })


def notion_update_page_props(page_id: str, properties: dict) -> str:
    """
    Update properties of an existing Notion page (title, status, date, etc.).
    Requires human approval when WRITE_REVIEW is enabled.

    Args:
        page_id:    ID of the page to update
        properties: Notion properties object (same shape as the API)
                    e.g. {"Status": {"select": {"name": "Done"}}}
    """
    if WRITE_REVIEW:
        prop_names = ", ".join(properties.keys())
        return _stage_action(
            "notion", "update_page_props",
            f"Update page {page_id[:8]}... — set {prop_names}",
            {"page_id": page_id, "properties": properties},
        )
    return _execute("update_page_props", {"page_id": page_id, "properties": properties})


def notion_append_blocks(block_id: str, blocks: list, after_block_id: str = "") -> str:
    """
    Append content blocks to a Notion page or block.
    Requires human approval when WRITE_REVIEW is enabled.

    Args:
        block_id:       ID of the parent page or block to append to
        blocks:         List of Notion block objects
                        e.g. [{"object":"block","type":"paragraph",
                                "paragraph":{"rich_text":[{"type":"text","text":{"content":"..."}}]}}]
        after_block_id: Optional ID of an existing child block to insert after.
                        Omit (or pass empty string) to append at the end.
    """
    pos_note = f" after {after_block_id[:8]}..." if after_block_id else ""
    if WRITE_REVIEW:
        return _stage_action(
            "notion", "append_blocks",
            f"Append {len(blocks)} block(s) to {block_id[:8]}...{pos_note}",
            {"block_id": block_id, "children": blocks, "after": after_block_id},
        )
    return _execute("append_blocks", {"block_id": block_id, "children": blocks, "after": after_block_id})


# ── Internal helpers ──────────────────────────────────────────────────────────

def _clean_id(notion_id: str) -> str:
    return notion_id.replace("-", "")


def _extract_title(obj: dict) -> str:
    props = obj.get("properties", {})
    for key in ("title", "Name", "Title"):
        if key in props:
            rich = props[key].get("title", [])
            return "".join(t.get("plain_text", "") for t in rich)
    return obj.get("id", "untitled")


def _block_summary(block: dict) -> str:
    btype = block.get("type", "unknown")
    inner = block.get(btype, {})
    rich  = inner.get("rich_text", [])
    text  = "".join(t.get("plain_text", "") for t in rich)[:80]
    return f"[{btype}] {text}" if text else f"[{btype}]"


def _build_create_payload(
    parent_id: str,
    parent_type: str,
    title: str,
    content: str = "",
) -> dict:
    payload: dict = {
        "parent": {parent_type: parent_id},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
    }
    if content:
        payload["children"] = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": content}}]
            },
        }]
    return payload
