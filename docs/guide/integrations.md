← [Back to guide index](README.md) · [Main README](../../README.md)

# 🔗 External Platform Integrations

hive-mcp can connect to external platforms (Notion, Google, etc.) using API keys. All platform writes go through the same human-approval gate as file writes.

## Contents
- [Activating integrations](#activating-integrations)
- [Approval flow](#approval-flow)
- [Adding a new platform](#adding-a-new-platform)
- [Available platforms](#available-platforms)

---

## Activating integrations

Set the relevant env var before starting hive-mcp:

```bash
# Notion — integration token from notion.so/my-integrations
export NOTION_API_KEY=secret_xxxx

docker compose -f docker-compose.hive.yml pull
docker rm -f hive-mcp
docker compose -f docker-compose.hive.yml up -d
```

## Approval flow

```
agent calls notion_create_page(parent_id, title, ...)
  └─ WRITE_REVIEW=true
       ├─ writes .hive_pending_actions/<id>.json to project volume
       └─ returns "action_pending: notion/create_page — ..."
              ↓
       agent STOPS and tells the user the action is staged
              ↓
       hive CLI detects new .json files in .hive_pending_actions/
              ↓
       CLI shows action summary + arrow-key selector:
         ❯ confirm  — POST /actions/confirm → platform API call executes
           reject   — discard
           skip     — decide later
```

Read operations (`notion_search`, `notion_get_page`) pass through immediately — no approval required.

## Adding a new platform

1. Create `hive-mcp/tools/integrations/<platform>.py`
2. Implement `_execute(tool, args)` and call `register_executor("<platform>", _execute)` at module level
3. Add read and write tool functions — write tools call `_stage_action()` when `WRITE_REVIEW=true`
4. Guard activation in `hive-mcp/main.py` with the env var check
5. Add the env var to `hive-mcp/config.py` and `docker-compose.hive.yml`

## Available platforms

| Platform | Env var | Read tools | Write tools |
|---|---|---|---|
| Notion | `NOTION_API_KEY` | `notion_search`, `notion_get_page` (prints each `(block_id: …)`), `notion_get_database_schema`, `notion_query_database` | `notion_create_page` (+`markdown_content`), `notion_update_page_props`, `notion_append_blocks`, `notion_append_markdown`, `notion_update_content` (in-place search/replace), `notion_update_block`, `notion_delete_block`, `notion_trash_page` |

> **Notion write tools take simple values.** Pass `properties` as a plain dict
> (`{"Status": "Done", "Area": "Platform", "Sprint": "<page-url>"}`); the tool reads the database
> schema and maps each value to the correct Notion type (select / status / relation / number /
> date / …). Relations accept a page id or URL. Nested params passed as JSON strings are parsed
> automatically. See `hive-mcp/README.md` for the full per-tool reference.

---

**Next:** [🛠️ Development](development.md)
