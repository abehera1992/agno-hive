← [Back to guide index](README.md) · [Main README](../../README.md)

# 🔌 API Usage

The AGNOHive API server (`python main.py --serve`, port 9001) is the HTTP surface behind the `hive` CLI and the `agno_run` MCP tool.

## Contents
- [Health check](#health-check)
- [Run a task](#run-a-task)
- [Get a plan only (HITL)](#get-a-plan-only-hitl)
- [Session chaining](#session-chaining--carry-context-across-api-calls)
- [Session management endpoints](#session-management-endpoints)
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
