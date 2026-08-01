---
name: notion-grounding
description: Rules for creating/reading/updating Notion pages via notion_* tools — required before any Notion write.
---
Notion GROUNDING rules (MANDATORY — read before you write, never guess):

1. NEVER fabricate or guess a Notion page_id. Resolve real ids first:
   notion_find_work_item(query) for a work item (e.g. "Phase 6"),
   notion_items_in_sprint(...) / notion_search() / notion_query_database() for the rest.
2. BEFORE any notion_update_page_props or relation change, call
   notion_get_item_with_relations(page_id) to READ the page's current properties and
   relations. Never set a relation (Parent item 1, Sprint, Work Items) you have not
   just read.
3. Do NOT confuse "Spec" (a doc-link property) with "Parent item 1" (the work-item
   parent). Change a parent only if the task explicitly asks, and only to a page you
   confirmed is a Work Item via notion_get_item_with_relations — never to a
   Spec/doc URL.
4. In notion_update_page_props send ONLY the properties the task names. Do NOT
   re-send Parent item 1 or any relation you were not asked to change (omitted
   properties are left as-is).
5. Never report an item as "orphaned"/missing a value from assumption — read it
   first and report the actual current state.

If a Notion/Google tool returns "action_pending", STOP immediately. Do not call any
other tool. Tell the human the action is staged — they approve via the hive CLI.
Do NOT call confirm_action yourself.
