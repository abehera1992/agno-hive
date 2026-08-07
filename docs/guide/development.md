← [Back to guide index](README.md) · [Main README](../../README.md)

# 🛠️ Development

## Contents
- [Git workflow](#git-workflow)
- [Running tests](#running-tests)
- [Internals: tool-call hooks](#internals-tool-call-hooks)

---

## Git workflow

All file changes are made on Windows, committed, pushed, then pulled on ZGX:

```bash
git -C ~/agno-hive pull   # on ZGX
```

**Never edit files directly on ZGX.**

---

## Running tests

```bash
pytest tests/ -v
```

---

## Internals: tool-call hooks

`swarm/team.py`'s `_build_team` registers two `tool_hooks` callables on the coordinator **and every member agent** (not just the coordinator) — `agno`'s `tool_hooks` kwarg is real middleware wrapping each individual tool call, confirmed via direct source reading, not assumed from docs (which are sparse on this parameter). Two mechanical facts drive how any new hook here must be written:

1. **Every MCP-server-backed tool call is `async` on the client side unconditionally** — a sync hook calling `function(**args)` gets back an unawaited coroutine, not the real result. A hook must be `async def` and must `await function(**args)`.
2. **In `mode="coordinate"` the coordinator mostly delegates rather than calling tools itself.** A hook registered only on the coordinator's own `Team(tool_hooks=[...])` never sees the member agents' own tool calls — which is most of them. Register on both, via the same shared hook instance (`make_agent_from_spec` / `make_coder` / `make_reviewer` all accept and forward `tool_hooks`).

**Current hooks (in order):**

| Hook | Purpose |
|---|---|
| `_make_read_cache_tool_hook()` | Run-scoped cache for read-only tools (`get_file_content`, `search_files`, `find_files`, `list_directory`, `list_directory_tree`, `count_matches`, batch variants). Serves a cached result for an identical `(tool, args)` pair already fetched earlier in the same run, instead of re-fetching. Fixed a real, measured problem: `agno`'s `share_member_interactions` only forwards a teammate's final *text* answer to the next agent, never the raw tool result, so every agent independently re-read the same files (21-29 duplicate calls for 2 files in one live 6-agent run before this fix; 8 total calls after, with the remaining ones genuinely distinct requests). |
| `_make_tool_interception_hook(abort_event=None)` | Per-tool-call checkpoint — audit-logs every call (name, args, duration, success/failure); if a caller-supplied `abort_event` (a plain `asyncio.Event`) is set immediately before a call would run, skips it and raises `ToolCallAborted` instead. `_build_team` currently wires this with `abort_event=None`, making it a pure audit-log pass-through with zero behavior change in production. `abort_event` is a reusable building block for a future pause/abort mechanism, **not currently connected to anything** — see the note below. |

**Known gap, by design:** the CLI's mid-flight steering queue (`cli/hive`'s `_steering_queue` — see [🖥️ CLI Client → Features](cli.md#features)) lives client-side, in the user's own terminal process. `_make_tool_interception_hook`'s checkpoint runs server-side, inside ZGX's `agno-api.service` process. There is no existing mid-run client↔server communication channel connecting the two — building one (e.g. a side-channel endpoint the hook polls, keyed by session/run id) is a separate, larger effort, not built here. Don't assume `abort_event` is live-wired to anything just because the plumbing exists; check `_build_team`'s actual call site before relying on it.

---

← [Back to guide index](README.md)
