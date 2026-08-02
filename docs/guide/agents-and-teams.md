← [Back to guide index](README.md) · [Main README](../../README.md)

# 🤖 Agents & Teams

## Contents
- [Agent roster](#agent-roster)
- [Teams](#teams)
- [Sprint Master — delivery-board PM team](#sprint-master--delivery-board-pm-team)

---

## Agent roster

| Agent | Model | Role |
|---|---|---|
| Coordinator (engineering) | `qwen3-coder:30b` | Routes tasks, delegates to agents, synthesises results |
| Coordinator (planning / parallel-review) | `qwen2.5-coder:7b` | Lightweight coordinator for read-only teams |
| ContextRouter | `llama3.1:8b` | Picks the right memory/search backend |
| Researcher | `qwen2.5-coder:32b` | Reads and summarises the codebase |
| Planner | `qwen2.5-coder:32b` | Breaks tasks into ordered steps |
| Coder | `qwen2.5-coder:32b` | Implements features and fixes |
| Executor | `llama3.1:8b` | Runs commands and validates results |
| Reviewer | `qwen2.5-coder:32b` | Reviews code for correctness and security |

All models are configurable via `.env` or `teams/*.yaml`. Models are swappable without code changes — the YAML spec drives which model each agent uses.

> **Coordinator model:** `qwen3-coder:30b` (non-thinking A3B MoE) — selected 2026-06-12 after a same-task A/B on the full pipeline: 84s with every service actually read, vs `ibm/granite4.1:30b` at 217s (purposes hallucinated from directory names) and `qwen3:30b-a3b` (thinking) at 318s. ~2.6× faster than granite with better grounding, and stable on GB10 ARM64 under Ollama 0.30.6. Granite is retained as a rollback option (deleted from Ollama; re-pull if needed). The qwen3-coder XML tool-call format is handled by `OllamaToolFix` format 6.

---

## Teams

| Team | Agents | Mode | Used for |
|---|---|---|---|
| `engineering` | All 6 (default) | `coordinate` | Full implementation tasks |
| `planning` | ContextRouter + Researcher + Planner | `coordinate` | HITL plan review via `POST /plan` |
| `parallel-review` | Researcher + SecurityReviewer + PerformanceReviewer | `collaborate` | Read-only parallel analysis — all agents run simultaneously |
| `sprint-master` | BacklogResearcher + StoryWriter | `coordinate` | Backlog/sprint work-item CRUD on the connected work-tracking platform — board management only, never code |
| `router` | _(virtual — no agents of its own)_ | classify → dispatch | Auto-routes a task to the best team above (`engineering` / `parallel-review` / `sprint-master` / `planning`); read-only grounding/verification probes route to `engineering` |

Create a new team by adding a YAML file in `teams/` — no code changes needed. Set `mode: collaborate` in the YAML to run all agents in parallel, or override per-request via the `mode` field in `POST /run`.

**`router`** is a *virtual* team (not a YAML file). `swarm_team="router"` runs a one-shot classifier (`ROUTER_CLASSIFIER_MODEL`, default `qwen3-coder:30b`) that reads the candidate teams' descriptions, picks the single best one, then dispatches the task to that team via the normal path — the response reports `team: router:<chosen>`. This **classifier-then-dispatch** design deliberately avoids agno's route-mode nested-team delegation, which is unreliable over ollama (`delegate_task_to_member` is intermittently emitted as plain text). A single team delegating to its own agents is reliable. Routing needs a strong model — `llama3.1:8b` mis-routes (it just picks the longest description), `qwen3-coder:30b` routes accurately.

---

## Sprint Master — delivery-board PM team

`sprint-master` (added 2026-06-19) is a **generic, platform-agnostic PM team** for delivery tracking. It reads the board schema + current state, then creates, updates, and nests work items (epics, features, stories, tasks, bugs) on whatever work-tracking platform is connected — Notion today, Jira/Linear later (each is just a new `hive-mcp` integration). It is **board CRUD only**: no agent holds `apply_diff` / `write_file` / `run_command`.

- **Coordinator:** `qwen3-coder:30b` (escalated from `qwen2.5-coder:7b`, which emitted unparseable delegation calls). Holds the board read **and** write tools, scoped via `coordinator_tools` so it never gets code/shell tools. `WRITE_REVIEW`, not tool restriction, gates every write.
- **Agents:** BacklogResearcher + StoryWriter (`qwen2.5-coder:32b`).
- **Property handling:** pass `properties` as a plain dict of simple values (`{"Status": "Done", "Sprint": "<page-url>"}`); the platform tool coerces each to the right type from the schema. Relations (Sprint, parent) take a page id or URL and must be set **in the same create call**.
- **Project board facts** (data-source IDs, field/option names, native sub-item nesting) live in the *project's* repo — e.g. EkamApp `docs/delivery-board.md` — which the team reads, so the team stays project-agnostic.

```bash
hive --project ekam --team sprint-master "mark Feature EK-42 done"
hive --project ekam --team sprint-master "add a Story 'wire up X' under epic EK-12 in the current sprint"
# from Claude Code / Claude Desktop:
#   agno_run("add a Story under EK-12 in the current sprint", swarm_team="sprint-master")
```

---

**Next:** [🔧 MCP Tools](mcp-tools.md) · [🔗 Integrations](integrations.md)
