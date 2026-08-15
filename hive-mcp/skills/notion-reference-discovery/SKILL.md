---
name: notion-reference-discovery
description: How to resolve a task's reference to a named Notion page/spec/doc — notion_search then notion_get_page, never a guessed local file path like NOTION/<title>.md. Read-side discovery, distinct from notion-grounding (write safety).
---
NOTION rule: If the task references a Notion page, spec, or doc by
name/title ("the Notion page X", "per the spec Y"), and
notion_search/notion_get_page are available (check connected MCP tools):
call notion_search(title) to find the page id, then notion_get_page(page_id)
to read its full content. Do NOT guess a local file path like
"NOTION/<title>.md" or "docs/<title>.md" — that path does not exist and
get_file_content() will return "File not found". Confirmed live 2026-08-14:
the former Planner role (since merged into Researcher) tried
get_file_content() against a fabricated NOTION/<title>.md path twice, got
"File not found" both times, then had to stop the whole run and ask the user
to paste the content directly — notion_search + notion_get_page avoid this
entirely when connected. If neither tool is available, say so plainly and
ask for the content rather than guessing a path.
