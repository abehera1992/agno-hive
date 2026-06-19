"""Notion integration tools.

Auth: NOTION_API_KEY environment variable (integration token from notion.so/my-integrations).
Reads pass through immediately. Writes are staged for human review when WRITE_REVIEW=true.

Property handling: write tools accept SIMPLE values (e.g. Status="Done", Area="Platform",
a relation as a page id/url). The tool fetches the target database schema and coerces each
value into the correct Notion property shape (title / rich_text / select / status /
multi_select / number / checkbox / date / url / relation / people). This means the LLM never
has to know whether a field is a select vs status vs relation.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from config import WRITE_REVIEW
from tools.integrations import _stage_action, register_executor

_NOTION_VERSION = "2022-06-28"
_NOTION_BASE    = "https://api.notion.com/v1"

# Keys that mark a value as already being a Notion property/object (passthrough).
_NOTION_PROP_KEYS = (
    "title", "rich_text", "select", "status", "multi_select", "number", "checkbox",
    "date", "url", "email", "phone_number", "people", "relation", "files",
)


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
            page_id = _clean_id(args["page_id"])
            props   = _build_update_properties(args["page_id"], args["properties"])
            result  = _request("PATCH", f"/pages/{page_id}", {"properties": props})
            set_names = ", ".join(props.keys()) or "(nothing)"
            return f"notion page updated ({set_names}): {result.get('url', result.get('id'))}"

        if tool == "append_blocks":
            block_id = _clean_id(args["block_id"])
            children = _coerce_blocks(args["children"])
            body: dict = {"children": children}
            if args.get("after"):
                body["after"] = _clean_id(args["after"])
            _request("PATCH", f"/blocks/{block_id}/children", body)
            return f"notion: appended {len(children)} block(s) to {args['block_id']}"

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


def notion_get_database_schema(database_id: str) -> str:
    """
    List a Notion database's property names + types (and select/status option names).
    Read-only — no approval required. Use this to learn valid field/option names before
    creating or updating rows.

    Args:
        database_id: Notion database ID (32-char hex, UUID, or last URL segment)
    """
    try:
        data  = _request("GET", f"/databases/{_clean_id(database_id)}")
        props = data.get("properties", {})
        lines = [f"notion database schema — {len(props)} properties:"]
        for name, p in props.items():
            ptype = p.get("type", "?")
            opts  = ""
            if ptype in ("select", "status", "multi_select"):
                names = [o.get("name") for o in p.get(ptype, {}).get("options", [])]
                if names:
                    opts = "  options: " + ", ".join(n for n in names if n)
            lines.append(f"  {name} : {ptype}{opts}")
        return "\n".join(lines)
    except Exception as e:
        return f"notion_get_database_schema failed: {e}"


# ── Write tools (staged when WRITE_REVIEW=true) ───────────────────────────────

def notion_create_page(
    parent_id: str,
    title: str,
    properties: dict | str | None = None,
    content: str = "",
    parent_type: str = "database_id",
) -> str:
    """
    Create a new Notion page (a database row, or a child of another page).
    Requires human approval when WRITE_REVIEW is enabled.

    Args:
        parent_id:   ID of the parent database (default) or page.
        title:       Page title (goes to the database's title property automatically).
        properties:  Optional dict of OTHER fields as SIMPLE values — the tool maps each to
                     the correct Notion type from the database schema. Examples:
                       {"Type": "Task", "Status": "In progress", "Stage": "In-flight",
                        "Area": "Platform", "Priority": "P2",
                        "Sprint": "https://app.notion.com/p/<sprint-id>",
                        "Parent item": "<epic-page-id-or-url>"}
                     - select/status: pass the option name as a string.
                     - relation: pass a page id/url (or a list of them).
                     - number: pass a number; checkbox: true/false; date: "YYYY-MM-DD".
                     Unknown field names are skipped (so a typo won't 400 the whole call).
        content:     Optional plain-text paragraph added as the first block.
        parent_type: 'database_id' (default) or 'page_id'.
    """
    props = _as_dict(properties)
    args = {
        "parent_id": parent_id, "parent_type": parent_type,
        "title": title, "content": content, "properties": props,
    }
    if WRITE_REVIEW:
        extra = (" + " + ", ".join(props.keys())) if props else ""
        return _stage_action(
            "notion", "create_page",
            f"Create page '{title}' in {parent_type.replace('_id', '')} {parent_id[:8]}...{extra}",
            args,
        )
    return _execute("create_page", args)


def notion_update_page_props(page_id: str, properties: dict | str) -> str:
    """
    Update properties of an existing Notion page (database row).
    Requires human approval when WRITE_REVIEW is enabled.

    Pass SIMPLE values — the tool reads the database schema and coerces each to the right
    Notion type (so you do NOT need to know select vs status vs relation):
        {"Status": "Done", "Stage": "Delivered", "Priority": "P1",
         "Sprint": "https://app.notion.com/p/<sprint-id>"}
    - select/status -> option name string; relation -> page id/url (or list);
      number -> number; checkbox -> bool; date -> "YYYY-MM-DD"; pass null to clear a field.
    Unknown field names are skipped.

    Args:
        page_id:    ID of the page (row) to update.
        properties: Dict of field name -> simple value.
    """
    props = _as_dict(properties)
    if WRITE_REVIEW:
        prop_names = ", ".join(props.keys())
        return _stage_action(
            "notion", "update_page_props",
            f"Update page {page_id[:8]}... — set {prop_names}",
            {"page_id": page_id, "properties": props},
        )
    return _execute("update_page_props", {"page_id": page_id, "properties": props})


def notion_append_blocks(block_id: str, blocks: list | str, after_block_id: str = "") -> str:
    """
    Append content blocks to a Notion page or block.
    Requires human approval when WRITE_REVIEW is enabled.

    Args:
        block_id:       ID of the parent page or block to append to.
        blocks:         Simplest form — a list of plain strings; each becomes its own
                        paragraph block, e.g. ["First line", "Second line"]. You may also pass
                        full Notion block dicts for advanced formatting; both are accepted.
        after_block_id: Optional ID of an existing child block to insert after.
                        Omit (or pass empty string) to append at the end.
    """
    blocks = _as_list(blocks)
    pos_note = f" after {after_block_id[:8]}..." if after_block_id else ""
    if WRITE_REVIEW:
        return _stage_action(
            "notion", "append_blocks",
            f"Append {len(blocks)} block(s) to {block_id[:8]}...{pos_note}",
            {"block_id": block_id, "children": blocks, "after": after_block_id},
        )
    return _execute("append_blocks", {"block_id": block_id, "children": blocks, "after": after_block_id})


# ── Internal helpers ──────────────────────────────────────────────────────────

def _as_dict(value) -> dict:
    """Coerce a value into a dict. LLM tool-calls often arrive as a JSON STRING for nested
    object params (the agno/ollama path stringifies dicts) — parse those transparently."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(value) -> list:
    """Coerce a value into a list — accepts a list, a JSON-encoded list/object string, or a
    plain string (wrapped as a single-element list)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") or s.startswith("{"):
            try:
                parsed = json.loads(s)
                return parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                return [value]
        return [value] if s else []
    return [value]


def _clean_id(notion_id: str) -> str:
    return str(notion_id).replace("-", "")


def _extract_id(value) -> str:
    """Extract a 32-char hex Notion id from a page id, UUID, or full page URL."""
    s = str(value).replace("-", "")
    runs = re.findall(r"[0-9a-fA-F]{32}", s)
    return runs[-1] if runs else s


def _extract_title(obj: dict) -> str:
    props = obj.get("properties", {})
    for key, p in props.items():
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p.get("title", []))
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


def _get_database_schema(database_id: str) -> tuple[dict, str]:
    """Return ({prop_name: prop_type}, title_property_name) for a database."""
    data   = _request("GET", f"/databases/{_clean_id(database_id)}")
    props  = data.get("properties", {})
    schema = {name: p.get("type") for name, p in props.items()}
    title_name = next((n for n, t in schema.items() if t == "title"), "title")
    return schema, title_name


def _coerce_property(ptype: str, value):
    """Map a simple Python value into the Notion property object for the given type."""
    # Already a Notion property object — pass through unchanged.
    if isinstance(value, dict) and any(k in value for k in _NOTION_PROP_KEYS):
        return value
    if value is None:
        return {ptype: None}

    if ptype == "title":
        return {"title": [{"type": "text", "text": {"content": str(value)}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}
    if ptype == "select":
        return {"select": {"name": str(value)}}
    if ptype == "status":
        return {"status": {"name": str(value)}}
    if ptype == "multi_select":
        vals = value if isinstance(value, list) else [value]
        return {"multi_select": [{"name": str(v)} for v in vals]}
    if ptype == "number":
        return {"number": float(value)}
    if ptype == "checkbox":
        return {"checkbox": bool(value)}
    if ptype == "date":
        return {"date": value if isinstance(value, dict) else {"start": str(value)}}
    if ptype == "url":
        return {"url": str(value)}
    if ptype == "email":
        return {"email": str(value)}
    if ptype == "phone_number":
        return {"phone_number": str(value)}
    if ptype == "people":
        vals = value if isinstance(value, list) else [value]
        return {"people": [{"object": "user", "id": _extract_id(v)} for v in vals if v]}
    if ptype == "relation":
        vals = value if isinstance(value, list) else [value]
        return {"relation": [{"id": _extract_id(v)} for v in vals if v]}
    # Fallback — store as rich text rather than 400.
    return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}


def _build_properties(schema: dict, title_name: str, title, properties) -> dict:
    out: dict = {}
    if title is not None and title_name:
        out[title_name] = {"title": [{"type": "text", "text": {"content": str(title)}}]}
    for name, value in _as_dict(properties).items():
        ptype = schema.get(name)
        if ptype is None:
            continue  # unknown property name — skip so a typo doesn't fail the whole write
        out[name] = _coerce_property(ptype, value)
    return out


def _build_create_payload(
    parent_id: str,
    parent_type: str,
    title: str,
    content: str = "",
    properties: dict | None = None,
) -> dict:
    payload: dict = {"parent": {parent_type: parent_id}}
    if parent_type == "database_id":
        schema, title_name = _get_database_schema(parent_id)
        payload["properties"] = _build_properties(schema, title_name, title, properties)
    else:
        payload["properties"] = {
            "title": {"title": [{"type": "text", "text": {"content": title}}]}
        }
    if content:
        payload["children"] = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
        }]
    return payload


def _build_update_properties(page_id: str, properties: dict | None) -> dict:
    """Coerce simple values into typed Notion properties using the page's database schema."""
    page   = _request("GET", f"/pages/{_clean_id(page_id)}")
    db_id  = page.get("parent", {}).get("database_id")
    if db_id:
        schema, _ = _get_database_schema(db_id)
    else:
        schema = {n: p.get("type") for n, p in page.get("properties", {}).items()}
    out: dict = {}
    for name, value in _as_dict(properties).items():
        ptype = schema.get(name)
        if ptype is None:
            continue
        out[name] = _coerce_property(ptype, value)
    return out


def _coerce_blocks(blocks) -> list:
    """Accept a string, a list of strings, or a list of block dicts -> list of block objects."""
    if isinstance(blocks, str):
        blocks = [blocks]
    out: list = []
    for b in (blocks or []):
        if isinstance(b, str):
            out.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": b}}]},
            })
        elif isinstance(b, dict):
            out.append(b)
    return out
