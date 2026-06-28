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

# Block types that carry editable rich_text (so notion_update_block can rewrite their text).
_RICH_TEXT_BLOCKS = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout", "code",
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _headers(version: str = _NOTION_VERSION) -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_API_KEY}",
        "Notion-Version": version,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, body: dict | None = None, version: str = _NOTION_VERSION) -> dict:
    import httpx
    resp = httpx.request(
        method, f"{_NOTION_BASE}{path}",
        headers=_headers(version),
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
            page_id = result.get("id")
            # Append any blocks beyond the 100-block create cap (rich markdown_content).
            if page_id and args.get("markdown_content"):
                _append_in_batches(page_id, _markdown_to_blocks(args["markdown_content"])[100:])
            return f"notion page created: {result.get('url', page_id)}"

        if tool == "append_markdown":
            block_id = _clean_id(args["block_id"])
            n = _append_in_batches(block_id, _markdown_to_blocks(args["markdown"]))
            return f"notion: appended {n} markdown block(s) to {args['block_id']}"

        if tool == "update_page_props":
            page_id = _clean_id(args["page_id"])
            props   = _build_update_properties(args["page_id"], args["properties"])
            result  = _request("PATCH", f"/pages/{page_id}", {"properties": props})
            set_names = ", ".join(props.keys()) or "(nothing)"
            return f"notion page updated ({set_names}): {result.get('url', result.get('id'))}"

        if tool == "trash_page":
            page_id  = _clean_id(args["page_id"])
            archived = not args.get("restore", False)
            result   = _request("PATCH", f"/pages/{page_id}", {"archived": archived})
            state    = "restored" if not archived else "trashed"
            return f"notion page {state}: {result.get('url', result.get('id'))}"

        if tool == "append_blocks":
            block_id = _clean_id(args["block_id"])
            children = _coerce_blocks(args["children"])
            body: dict = {"children": children}
            if args.get("after"):
                body["after"] = _clean_id(args["after"])
            _request("PATCH", f"/blocks/{block_id}/children", body)
            return f"notion: appended {len(children)} block(s) to {args['block_id']}"

        if tool == "update_block":
            block_id = _clean_id(args["block_id"])
            blk      = _request("GET", f"/blocks/{block_id}")
            btype    = blk.get("type")
            inner: dict = {}
            if args.get("text") is not None:
                if btype not in _RICH_TEXT_BLOCKS:
                    return (f"notion update_block: block type '{btype}' has no editable text "
                            f"(only {', '.join(sorted(_RICH_TEXT_BLOCKS))})")
                inner["rich_text"] = _rich(args["text"])
            if args.get("checked") is not None and btype == "to_do":
                inner["checked"] = bool(args["checked"])
            if not inner:
                return "notion update_block: nothing to update — pass text and/or checked"
            _request("PATCH", f"/blocks/{block_id}", {btype: inner})
            return f"notion: updated {btype} block {args['block_id']}"

        if tool == "delete_block":
            block_id = _clean_id(args["block_id"])
            if args.get("restore"):
                _request("PATCH", f"/blocks/{block_id}", {"archived": False})
                return f"notion: restored block {args['block_id']}"
            _request("DELETE", f"/blocks/{block_id}")
            return f"notion: deleted (trashed) block {args['block_id']}"

        if tool == "update_content":
            old_str, new_str = args["old_str"], args["new_str"]
            blocks: list[dict] = []
            _collect_blocks(args["page_id"], blocks)
            matches = []
            for b in blocks:
                bt = b.get("type")
                if bt not in _RICH_TEXT_BLOCKS:
                    continue
                md = _rich_to_md(b.get(bt, {}).get("rich_text", []))
                if old_str in md:
                    matches.append((b, bt, md))
            if not matches:
                return "notion update_content: old_str not found in any block — no change made"
            if len(matches) > 1:
                return (f"notion update_content: AMBIGUOUS — old_str matched {len(matches)} blocks; "
                        "make old_str longer/unique so it identifies exactly one block")
            b, bt, md = matches[0]
            _request("PATCH", f"/blocks/{_clean_id(b['id'])}",
                     {bt: {"rich_text": _rich(md.replace(old_str, new_str))}})
            return f"notion: update_content replaced text in {bt} block {_clean_id(b['id'])}"

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


def notion_get_page(page_id: str, max_lines: int = 600) -> str:
    """
    Read a Notion page's FULL content — paginates ALL top-level blocks (not just the first
    page of 25) and renders table rows, so long pages (roadmaps, rate tables) are read
    end-to-end. Read-only — no approval required. Each block line shows its real
    `(block_id: <hex>)` — pass that id to notion_update_block / notion_delete_block to
    rewrite or remove that exact block (never fabricate or guess a block id).

    Args:
        page_id:   Notion page ID (32-char hex, UUID format, or last segment of page URL).
        max_lines: Safety cap on output lines (default 600).
    """
    try:
        clean  = _clean_id(page_id)
        page   = _request("GET", f"/pages/{clean}")
        title  = _extract_title(page)
        url    = page.get("url", "")
        lines  = [f"notion page: {title}  ({url})"]
        cursor = None
        while len(lines) < max_lines:
            path = f"/blocks/{clean}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = _request("GET", path)
            for b in data.get("results", []):
                lines.append("  " + _block_summary(b))
                # Expand table blocks — render each row's cells (table_row children).
                if b.get("type") == "table" and b.get("has_children"):
                    rows = _request("GET", f"/blocks/{b['id']}/children?page_size=100")
                    for row in rows.get("results", []):
                        if row.get("type") == "table_row":
                            cells = row.get("table_row", {}).get("cells", [])
                            text = " | ".join(
                                "".join(t.get("plain_text", "") for t in cell) for cell in cells
                            )
                            lines.append("      " + text)
                if len(lines) >= max_lines:
                    lines.append("  … (truncated at max_lines)")
                    break
            if not data.get("has_more") or len(lines) >= max_lines:
                break
            cursor = data.get("next_cursor")
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


def notion_query_database(
    database_id: str,
    filter: dict | str | None = None,
    sorts: list | str | None = None,
    page_size: int = 50,
) -> str:
    """
    Query a database and LIST / FILTER its rows — e.g. "all Work Items in a given sprint",
    "every Bug with Status = Open". Read-only — no approval required. This is the right tool for
    reporting questions; do NOT page through rows one by one.

    Each result row includes its `(page_id: <hex>)` — pass that id straight to
    notion_update_page_props / notion_trash_page to act on a row you found here (do NOT use the
    display id like "EK-16"; that is not a page id).

    Args:
        database_id: Notion database ID (32-char hex, UUID, or last URL segment).
        filter:      Optional Notion filter object (or JSON string). Examples:
                       relation: {"property": "Sprint", "relation": {"contains": "<page-id-or-url>"}}
                       select:   {"property": "Type", "select": {"equals": "Story"}}
                       status:   {"property": "Status", "status": {"equals": "Done"}}
                       compound: {"and": [ {...}, {...} ]}   (also supports "or")
                     For a relation filter, the value must be a page id (use _clean_id form or a URL).
        sorts:       Optional list of {"property": "<name>", "direction": "ascending"|"descending"}.
        page_size:   Max rows to return (default 50, max 100).
    """
    try:
        body: dict = {"page_size": max(1, min(int(page_size), 100))}
        f = _as_dict(filter)
        if f:
            # coerce a relation filter's contains value (URL -> id) for convenience
            rel = f.get("relation") if isinstance(f, dict) else None
            if isinstance(rel, dict) and "contains" in rel:
                rel["contains"] = _extract_id(rel["contains"])
            body["filter"] = f
        s = _as_list(sorts)
        if s:
            body["sorts"] = s
        data    = _request("POST", f"/databases/{_clean_id(database_id)}/query", body)
        results = data.get("results", [])
        if not results:
            return "notion: query returned 0 rows"
        more  = " (more available — raise page_size or paginate)" if data.get("has_more") else ""
        lines = [f"notion: {len(results)} row(s){more}:"]
        for r in results:
            lines.append("  " + _format_row(r))
        return "\n".join(lines)
    except Exception as e:
        return f"notion_query_database failed: {e}"


def notion_items_in_sprint(
    database_id: str,
    sprint_id: str,
    status: str | None = None,
    exclude_status: str | None = None,
    sprint_property: str = "Sprint",
    status_property: str = "Status",
) -> str:
    """
    List every work item in a sprint — filtered by the Sprint RELATION, with full pagination.
    Read-only — no approval required.

    Purpose-built so you only pass a sprint id: this tool builds the nested relation filter and
    pages through ALL results for you (the two steps to get wrong by hand). Prefer this over
    notion_query_database for any "what is in / what is left in sprint X" question. Do NOT list
    the whole database and filter by eye — relation values are omitted from row output on purpose.

    Each row includes its `(page_id: <hex>)` for use with notion_update_page_props / notion_trash_page.

    Args:
        database_id:     Work-items database id (hex / UUID / URL, or a data-source/collection id).
        sprint_id:       The sprint PAGE id or URL — matched against each item's Sprint relation.
        status:          Optional — keep only rows whose Status equals this (e.g. "In progress").
        exclude_status:  Optional — drop rows whose Status equals this (e.g. "Done" → what's left).
        sprint_property: Relation property linking an item to its sprint (default "Sprint").
        status_property: Status property name (default "Status").
    """
    try:
        db         = _clean_id(database_id)
        rel_target = _extract_id(sprint_id)
        base = {"page_size": 100,
                "filter": {"property": sprint_property, "relation": {"contains": rel_target}}}
        rows, cursor, total, resolved = [], None, 0, False
        while True:
            body = dict(base)
            if cursor:
                body["start_cursor"] = cursor
            try:
                data = _request("POST", f"/databases/{db}/query", body)
            except Exception:
                if resolved:
                    raise
                db, resolved = _resolve_database_id(database_id), True  # accept a data-source id
                data = _request("POST", f"/databases/{db}/query", body)
            page = data.get("results", [])
            total += len(page)
            for r in page:
                sp = r.get("properties", {}).get(status_property, {})
                st = _prop_value(sp) if sp else None
                if status and st != status:
                    continue
                if exclude_status and st == exclude_status:
                    continue
                rows.append(r)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        if total == 0:
            return (f"notion: 0 items in sprint {rel_target} — check database_id "
                    f"and that '{sprint_property}' is the relation to the sprint")
        filt = (f" with {status_property}={status}" if status
                else f" (excluding {status_property}={exclude_status})" if exclude_status else "")
        lines = [f"notion: {len(rows)} of {total} sprint item(s){filt}:"]
        for r in rows:
            lines.append("  " + _format_row(r))
        return "\n".join(lines)
    except Exception as e:
        return f"notion_items_in_sprint failed: {e}"


# ── Write tools (staged when WRITE_REVIEW=true) ───────────────────────────────

def notion_create_page(
    parent_id: str,
    title: str,
    properties: dict | str | None = None,
    content: str = "",
    parent_type: str = "database_id",
    markdown_content: str = "",
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
        parent_type: 'database_id' (default) or 'page_id'. For 'database_id' the value may be a
                     database id OR a data-source/collection id — the latter is resolved to its
                     database automatically.
        markdown_content: Optional RICH body in Notion-flavored markdown — headings (#/##/###),
                     paragraphs, bullet/number lists, fenced code blocks, block quotes, dividers,
                     tables, with inline **bold** / `code` / [label](url). Converted to Notion
                     blocks. Use this (not `content`) for technical docs / multi-section pages.
    """
    props = _as_dict(properties)
    args = {
        "parent_id": parent_id, "parent_type": parent_type,
        "title": title, "content": content, "properties": props,
        "markdown_content": markdown_content,
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


def notion_append_markdown(block_id: str, markdown: str) -> str:
    """
    Append RICH content to a Notion page/block from Notion-flavored markdown — headings (#/##/###),
    paragraphs, bullet/number lists, fenced code blocks, block quotes, dividers, tables, with inline
    **bold** / `code` / [label](url). Use this to write technical docs / multi-section bodies (the
    rich counterpart to notion_append_blocks, which only takes plain paragraphs). Batches over the
    100-block API cap automatically. Requires human approval when WRITE_REVIEW is enabled.

    Args:
        block_id: ID of the page (or block) to append to.
        markdown: The markdown body to render as Notion blocks.
    """
    if WRITE_REVIEW:
        n = len(_markdown_to_blocks(markdown))
        return _stage_action(
            "notion", "append_markdown",
            f"Append {n} markdown block(s) to {block_id[:8]}...",
            {"block_id": block_id, "markdown": markdown},
        )
    return _execute("append_markdown", {"block_id": block_id, "markdown": markdown})


def notion_trash_page(page_id: str, restore: bool = False) -> str:
    """
    Move a Notion page (e.g. a work item) to trash — i.e. remove it from the board / database.
    Set restore=True to bring a trashed page back. Requires human approval when WRITE_REVIEW is
    enabled. This is the delete verb for board CRUD; note trashing a row also drops it from any
    database query results.

    Args:
        page_id: ID of the page to trash (or restore).
        restore: False (default) trashes the page; True restores it from trash.
    """
    if WRITE_REVIEW:
        verb = "Restore" if restore else "Trash"
        return _stage_action(
            "notion", "trash_page",
            f"{verb} page {page_id[:8]}...",
            {"page_id": page_id, "restore": bool(restore)},
        )
    return _execute("trash_page", {"page_id": page_id, "restore": bool(restore)})


def notion_update_block(block_id: str, text: str | None = None, checked: bool | None = None) -> str:
    """
    Edit an EXISTING block's text IN PLACE — e.g. rewrite a roadmap line, tick a checkbox.
    This is the counterpart to notion_append_markdown (which only adds NEW blocks): use this
    when the user asks to "update / change / fix" text that is already on the page. Requires
    human approval when WRITE_REVIEW is enabled.

    Get the target block_id from notion_get_page (each line shows its block) or from the
    /blocks/<id>/children listing. Works on text-bearing blocks: paragraph, heading_1/2/3,
    bulleted_list_item, numbered_list_item, to_do, toggle, quote, callout, code.

    Args:
        block_id: ID of the existing block to edit.
        text:     New text — Notion-flavored inline markdown (**bold**, `code`, [label](url))
                  is parsed. Replaces the block's entire text. Omit to leave text unchanged
                  (e.g. when only toggling `checked`).
        checked:  For to_do blocks only — True/False to set the checkbox state.
    """
    if WRITE_REVIEW:
        bits = []
        if text is not None:
            bits.append(f"text -> '{text[:40]}'")
        if checked is not None:
            bits.append(f"checked={bool(checked)}")
        return _stage_action(
            "notion", "update_block",
            f"Update block {block_id[:8]}... ({'; '.join(bits) or 'no-op'})",
            {"block_id": block_id, "text": text, "checked": checked},
        )
    return _execute("update_block", {"block_id": block_id, "text": text, "checked": checked})


def notion_delete_block(block_id: str, restore: bool = False) -> str:
    """
    Delete (trash) an EXISTING block — e.g. remove a stale roadmap line or a duplicated
    section. Set restore=True to bring a trashed block back. Requires human approval when
    WRITE_REVIEW is enabled.

    Get the block_id from notion_get_page or the /blocks/<id>/children listing. Deleting a
    block also removes its children. To replace text rather than remove it, prefer
    notion_update_block.

    Args:
        block_id: ID of the block to trash (or restore).
        restore:  False (default) trashes the block; True restores it from trash.
    """
    if WRITE_REVIEW:
        verb = "Restore" if restore else "Delete"
        return _stage_action(
            "notion", "delete_block",
            f"{verb} block {block_id[:8]}...",
            {"block_id": block_id, "restore": bool(restore)},
        )
    return _execute("delete_block", {"block_id": block_id, "restore": bool(restore)})


def notion_update_content(page_id: str, old_str: str, new_str: str) -> str:
    """Reliable in-place search/replace on a Notion page — the safe alternative to
    notion_update_block.

    Finds the ONE block whose rendered markdown CONTAINS `old_str`, replaces `old_str` →
    `new_str` inside it, and patches that block. Because it matches the FULL block text (not a
    'starts-with' prefix), it never edits the wrong block when several blocks share a prefix
    across sections. `old_str` must identify exactly ONE block: if it matches zero or more than
    one block the call is rejected (make `old_str` longer / more unique). Searches nested
    children (sub-bullets) too. `new_str` may carry inline markdown (**bold** / `code` /
    [label](url)). Requires human approval when WRITE_REVIEW is enabled.

    Args:
        page_id:  Notion page id (or the last segment of a page URL).
        old_str:  Exact text (as rendered markdown) to find within a single block.
        new_str:  Replacement text (inline markdown supported).
    """
    if WRITE_REVIEW:
        return _stage_action(
            "notion", "update_content",
            f"Search/replace one block on page {_clean_id(page_id)[:8]}...",
            {"page_id": page_id, "old_str": old_str, "new_str": new_str},
        )
    return _execute("update_content", {"page_id": page_id, "old_str": old_str, "new_str": new_str})


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
    bid   = _clean_id(block.get("id", ""))
    tag   = f"[{btype}] (block_id: {bid})" if bid else f"[{btype}]"
    return f"{tag} {text}" if text else tag


def _prop_value(p: dict):
    """Extract a compact human-readable value from a Notion property object."""
    t = p.get("type")
    v = p.get(t)
    if v is None:
        return None
    if t in ("select", "status"):
        return v.get("name")
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in v) or None
    if t in ("number", "checkbox", "url", "email", "phone_number"):
        return v
    if t == "date":
        return v.get("start")
    if t == "relation":
        return f"{len(v)} linked" if v else None
    if t == "people":
        return f"{len(v)} people" if v else None
    if t == "unique_id":
        num = v.get("number")
        return f"{v.get('prefix')}-{num}" if num is not None else None
    if t in ("rich_text", "title"):
        return "".join(x.get("plain_text", "") for x in v) or None
    return None


def _format_row(page: dict) -> str:
    """One compact line per row, including the row's page_id so it can be updated directly:
    [EK-79] Title (trashed?) (page_id: <hex>) | Prop=val  Prop=val ..."""
    props = page.get("properties", {})
    title = _extract_title(page)
    page_id = _clean_id(page.get("id", ""))
    uid, parts = "", []
    for name, p in props.items():
        ptype = p.get("type")
        # Skip the title (shown separately) and relations: a relation renders as "N linked",
        # which models misread as a value (e.g. "Sprint=1 linked" read as "Sprint 1"). The row's
        # relation membership is already implied by the query filter; use notion_get_page for detail.
        if ptype in ("title", "relation"):
            continue
        val = _prop_value(p)
        if val is None or val == "" or val is False:
            continue
        if ptype == "unique_id":
            uid = f"[{val}] "
            continue
        parts.append(f"{name}={val}")
    flag = " (trashed)" if page.get("in_trash") or page.get("archived") else ""
    idpart = f" (page_id: {page_id})" if page_id else ""
    return f"{uid}{title}{flag}{idpart}" + (" | " + "  ".join(parts) if parts else "")


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
        # Array-valued props are cleared with [], not null (Notion 400s on e.g. {"relation": null}).
        if ptype in ("relation", "multi_select", "people", "files", "rich_text", "title"):
            return {ptype: []}
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


def _resolve_database_id(maybe_id: str) -> str:
    """A `database_id` parent that isn't actually a database may be a data-source / collection
    id (the 2022-06-28 API can't use that as a database parent). Look up its parent database
    via the 2025-09-03 `data_sources` endpoint so callers can pass EITHER id. Returns the
    original id if it can't be resolved."""
    cid = _clean_id(maybe_id)
    try:
        ds = _request("GET", f"/data_sources/{cid}", version="2025-09-03")
        db = (ds.get("parent") or {}).get("database_id")
        if db:
            return _clean_id(db)
    except Exception:
        pass
    return cid


def _build_create_payload(
    parent_id: str,
    parent_type: str,
    title: str,
    content: str = "",
    properties: dict | None = None,
    markdown_content: str = "",
) -> dict:
    payload: dict = {"parent": {parent_type: parent_id}}
    if parent_type == "database_id":
        db_id = _clean_id(parent_id)
        try:
            schema, title_name = _get_database_schema(db_id)
        except Exception:
            db_id = _resolve_database_id(parent_id)  # accept a data-source/collection id too
            schema, title_name = _get_database_schema(db_id)
        payload["parent"] = {"database_id": db_id}
        payload["properties"] = _build_properties(schema, title_name, title, properties)
    else:
        payload["properties"] = {
            "title": {"title": [{"type": "text", "text": {"content": title}}]}
        }
    children: list = []
    if markdown_content:
        children = _markdown_to_blocks(markdown_content)
    elif content:
        children = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
        }]
    if children:
        # Notion caps children at 100 per create; the rest are appended after (see _execute).
        payload["children"] = children[:100]
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


# ── Markdown -> Notion blocks ─────────────────────────────────────────────────
# Converts Notion-flavored markdown (headings, paragraphs, bullet/number lists, code fences,
# block quotes, dividers, tables, with inline **bold** / `code` / [link](url)) into Notion block
# objects, so write tools can author rich pages instead of plain paragraphs.

_NOTION_LANGS = {
    "py": "python", "js": "javascript", "ts": "typescript", "tsx": "typescript",
    "jsx": "javascript", "sh": "shell", "bash": "shell", "yml": "yaml",
    "dockerfile": "docker", "text": "plain text", "txt": "plain text", "": "plain text",
}
_NOTION_LANG_SET = {
    "python", "javascript", "typescript", "shell", "bash", "json", "yaml", "sql", "markdown",
    "plain text", "go", "rust", "java", "c", "c++", "c#", "html", "css", "diff", "docker",
    "graphql", "php", "ruby", "kotlin", "swift", "scala", "xml", "toml", "ini", "powershell",
}
_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")
_MAX_RT = 1900  # Notion caps a single rich_text content at 2000 chars


def _rt(content: str, **ann) -> list[dict]:
    """One or more rich_text items for `content`, chunked under the 2000-char cap."""
    content = content or ""
    items = []
    for j in range(0, max(len(content), 1), _MAX_RT):
        chunk = content[j:j + _MAX_RT]
        item = {"type": "text", "text": {"content": chunk}}
        if ann:
            item["annotations"] = ann
        items.append(item)
    return items


def _rich(text: str) -> list[dict]:
    """Parse inline markdown (**bold**, `code`, [label](url)) into Notion rich_text items."""
    if not text:
        return []
    out: list[dict] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            out += _rt(part[2:-2], bold=True)
        elif part.startswith("`") and part.endswith("`"):
            out += _rt(part[1:-1], code=True)
        elif part.startswith("[") and "](" in part and part.endswith(")"):
            label = part[1:part.index("]")]
            url = part[part.index("](") + 2:-1]
            out.append({"type": "text", "text": {"content": label, "link": {"url": url}}})
        else:
            out += _rt(part)
    return out


def _rich_to_md(rich: list) -> str:
    """Inverse of _rich: render a Notion rich_text array back to inline markdown, so a
    search/replace can match the same **bold** / `code` / [label](url) form _rich produces."""
    out = []
    for t in rich or []:
        txt = t.get("plain_text", "")
        ann = t.get("annotations", {}) or {}
        link = (t.get("text") or {}).get("link")
        if link and link.get("url"):
            txt = f"[{txt}]({link['url']})"
        elif ann.get("code"):
            txt = f"`{txt}`"
        elif ann.get("bold"):
            txt = f"**{txt}**"
        out.append(txt)
    return "".join(out)


def _collect_blocks(parent_id: str, acc: list, max_blocks: int = 2000) -> None:
    """Recursively collect ALL descendant blocks under a page/block id (so nested sub-bullets
    are reachable — notion_get_page only renders top level + table rows)."""
    cursor = None
    while len(acc) < max_blocks:
        path = f"/blocks/{_clean_id(parent_id)}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = _request("GET", path)
        for b in data.get("results", []):
            acc.append(b)
            if b.get("has_children") and b.get("type") not in ("child_page", "child_database", "table"):
                _collect_blocks(b["id"], acc, max_blocks)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _markdown_to_blocks(md: str) -> list[dict]:
    """Convert Notion-flavored markdown into a list of Notion block objects."""
    lines = (md or "").split("\n")
    blocks: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        s = raw.strip()

        if s.startswith("```"):                                   # code fence
            lang = s[3:].strip().lower()
            lang = _NOTION_LANGS.get(lang, lang if lang in _NOTION_LANG_SET else "plain text")
            code: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i]); i += 1
            i += 1
            blocks.append({"object": "block", "type": "code",
                           "code": {"rich_text": _rt("\n".join(code)), "language": lang}})
            continue

        if s.startswith("|") and i + 1 < n and set(lines[i + 1].strip()) <= set("|-: "):  # table
            header = _split_row(s)
            width = len(header)
            i += 2
            rows = [header]
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i].strip())); i += 1
            tr = [{"type": "table_row",
                   "table_row": {"cells": [_rich(c) for c in (r + [""] * width)[:width]]}}
                  for r in rows]
            blocks.append({"object": "block", "type": "table",
                           "table": {"table_width": width, "has_column_header": True,
                                     "has_row_header": False, "children": tr}})
            continue

        if s in ("---", "***", "___"):                            # divider
            blocks.append({"object": "block", "type": "divider", "divider": {}}); i += 1; continue

        m = re.match(r"(#{1,3})\s+(.*)", s)                        # heading 1-3
        if m:
            lvl = len(m.group(1))
            blocks.append({"object": "block", "type": f"heading_{lvl}",
                           f"heading_{lvl}": {"rich_text": _rich(m.group(2))}}); i += 1; continue

        if s.startswith(">"):                                      # quote
            blocks.append({"object": "block", "type": "quote",
                           "quote": {"rich_text": _rich(s[1:].strip())}}); i += 1; continue

        if re.match(r"[-*+]\s+", s):                               # bulleted list
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich(re.sub(r"^[-*+]\s+", "", s))}})
            i += 1; continue

        if re.match(r"\d+\.\s+", s):                               # numbered list
            blocks.append({"object": "block", "type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": _rich(re.sub(r"^\d+\.\s+", "", s))}})
            i += 1; continue

        if not s:                                                 # blank
            i += 1; continue

        blocks.append({"object": "block", "type": "paragraph",                # paragraph
                       "paragraph": {"rich_text": _rich(s)}}); i += 1
    return blocks


def _append_in_batches(block_id: str, blocks: list) -> int:
    """Append blocks to a page/block in batches of 100 (Notion's per-request cap). Returns count."""
    clean = _clean_id(block_id)
    total = 0
    for k in range(0, len(blocks), 100):
        batch = blocks[k:k + 100]
        if batch:
            _request("PATCH", f"/blocks/{clean}/children", {"children": batch})
            total += len(batch)
    return total
