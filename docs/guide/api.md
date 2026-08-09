← [Back to guide index](README.md) · [Main README](../../README.md)

# 🔌 API Usage

The AGNOHive API server (`python main.py --serve`, port 9001) is the HTTP surface behind the `hive` CLI and the `agno_run` MCP tool.

## Contents
- [Health check](#health-check)
- [Run a task](#run-a-task)
- [Get a plan only (HITL)](#get-a-plan-only-hitl)
- [Clarification requests](#clarification-requests)
- [Session chaining](#session-chaining--carry-context-across-api-calls)
- [Session management endpoints](#session-management-endpoints)
- [Session tree endpoints](#session-tree-endpoints)
- [Feedback (self-improving loop)](#submit-output-feedback-self-improving-loop)

---

## Health check
```bash
curl http://localhost:9001/health
# {"status": "ok", "mcp_url": "http://..."}
```

## Run a task
```bash
curl -X POST http://localhost:9001/run \
  -H "Content-Type: application/json" \
  -d '{
    "task": "What files exist in the project root?",
    "project_id": "EkamApp",
    "team": "engineering",
    "mcp_url": "http://<tailscale-ip>:9000/mcp",
    "mcp_urls": ["http://<tailscale-ip>:9003/mcp"]
  }'
```

## Get a plan only (HITL)
```bash
curl -X POST http://localhost:9001/plan \
  -H "Content-Type: application/json" \
  -d '{"task": "Add rate limiting to login", "project_id": "EkamApp"}'
```

---

## Clarification requests

`/run`, `/plan`, and `/stream` can all return a **clarification request** instead of
(or on `/stream`, in addition to reporting through) the usual completed answer — a
structured signal that the coordinator hit a genuine fork in the road it can't
resolve on its own (a real design choice with more than one valid approach, or a
request that could reasonably mean two different concrete things), as opposed to
something a tool call could have looked up. This is deliberately narrow: most tasks
never trigger it, and it is not used for "I don't know which file to edit" — that's
what the coordinator's own `find_files`/`search_files` tools are for.

`/run` and `/plan` responses gain an optional field:
```json
{
  "result": "...",
  "needs_clarification": {
    "question": "Which caching layer should this use?",
    "options": [
      {"label": "Redis", "description": "Shared across instances, needs infra"},
      {"label": "In-process LRU", "description": "Simpler, per-instance only"}
    ]
  },
  "...": "the rest of RunResponse/PlanResponse is populated as normal"
}
```
2-4 options, each a short label plus one clarifying sentence — the same shape and
constraint as Claude Code's own `AskUserQuestion` tool; this is the same mechanism,
carried over HTTP instead of in-process. `result`/`plan` has the raw block already
stripped out — you'll never see the fenced JSON in the visible text.

`/stream`'s `done` event gains the same field when present:
```
data: {"type": "done", "session": {...}, "needs_clarification": {...} | absent, ...}
```
On `/stream` specifically, the raw fenced block is *not* hidden from the streamed
`chunk` events before the `done` event arrives — only `/run`/`/plan` get a fully
clean strip. The `hive` CLI still renders an interactive picker once the stream
completes; it's a rougher experience on that path, not a broken one.

**Continuing after a clarification**: whatever's calling the API presents
`question`/`options` to the human, then re-calls `/run` (or the next `hive`
prompt) with the chosen option as the task text and the same `session_id` — the
same [session chaining](#session-chaining--carry-context-across-api-calls)
mechanism used everywhere else. The `hive` CLI does this automatically with an
arrow-key picker; a custom client should do the equivalent.

---

## Session chaining — carry context across API calls

Pass `session_id` from a previous `/run` response to resume context in the next call. Without it every call is stateless (`context_size=0`). With it, a compact **chain-boundary handoff digest** (task, files referenced, key outcomes, status) from the previous run is injected into the coordinator — equivalent to staying in REPL mode without the full message-history overhead that can overflow the context window.

The handoff digest is ~20 lines. The coordinator does not receive the full message history when a digest is present — this is what keeps long multi-step chains from exhausting the model's context window.

```bash
# Step 1 — new session; response includes "session": {"session_id": "abc123-..."}
curl -X POST http://localhost:9001/run \
  -d '{"task": "Read businessApi.ts then scaffold emailApi.ts", "project_id": "EkamApp", ...}'

# Step 2 — resume; Coder inherits Researcher output from step 1
curl -X POST http://localhost:9001/run \
  -d '{"task": "Add tabs to page.tsx", "session_id": "abc123-...", "project_id": "EkamApp", ...}'
```

From the `agno_run` MCP tool the session UUID appears on the last result line as `[session: abc123-...]` — pass it as the `session_id` argument on the next call.

---

## Session management endpoints
```bash
curl "http://localhost:9001/sessions?project_id=EkamApp"
curl "http://localhost:9001/sessions/<id>"
curl -X DELETE "http://localhost:9001/sessions/<id>"
curl -X PATCH "http://localhost:9001/sessions/<id>/persist"
```

---

## Session tree endpoints

Every session is a tree (`parent_message_id` chain per message, `current_leaf_id` per session), not a flat log — these three endpoints are what the `hive` CLI's `/tree`, `/branch`, and `--fork` commands call. See [🖥️ CLI Client → Session tree branching](cli.md#session-tree-branching) for the interactive walkthrough.

```bash
# List the full tree — every message, with a server-computed depth for rendering
curl "http://localhost:9001/sessions/<id>/tree"
# {"messages": [{"id": 1, "parent_message_id": null, "role": "user", "content": "...", "created_at": "...", "depth": 0}, ...]}

# Rewind to a message's PARENT and get its content back for edit-and-resubmit
curl -X POST "http://localhost:9001/sessions/<id>/branch" \
  -H "Content-Type: application/json" \
  -d '{"message_id": 5}'
# {"new_leaf_id": 4, "editable_content": "text of message 5"}
# 404 if message_id isn't in this session's tree.
# Branching from a root message (no parent) returns new_leaf_id: null — a valid rewind-to-empty, not an error.

# Copy the session's CURRENT branch into a new, independent session (original untouched)
curl -X POST "http://localhost:9001/sessions/<id>/fork" \
  -H "Content-Type: application/json" \
  -d '{"title": "exploring an alternate approach", "project_id": "EkamApp"}'
# {"session_id": "<new-uuid>"}
# 404 if the source session has no messages to fork.
```

---

## Submit output feedback (self-improving loop)

```bash
# Mark an output as incorrect — correction is injected into next run for this project
curl -X POST http://localhost:9001/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session-id>",
    "task": "write migration for auth.users",
    "project_id": "EkamApp",
    "rating": "bad",
    "notes": "__table_args__ tuple must have dict last, not first — (UniqueConstraint(...), {schema})"
  }'

# Mark an output as correct — stored in LightRAG for future pattern recall
curl -X POST http://localhost:9001/feedback \
  -d '{"task": "...", "project_id": "EkamApp", "rating": "good", "notes": "migration applied cleanly"}'
```

---

**Next:** [🤖 Agents & Teams](agents-and-teams.md) · [🔧 MCP Tools](mcp-tools.md)
