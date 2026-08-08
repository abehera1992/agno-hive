← [Back to guide index](README.md) · [Main README](../../README.md)

# ☁️ Cloud Model Providers

## Contents
- [Who this is for](#who-this-is-for)
- [How it works](#how-it-works)
- [Enabling it](#enabling-it)
- [Hybrid teams — mixing local and cloud on one roster](#hybrid-teams--mixing-local-and-cloud-on-one-roster)
- [Adding a provider](#adding-a-provider)
- [Admin API — editing routes at runtime](#admin-api--editing-routes-at-runtime)
- [Safety guardrails](#safety-guardrails)
- [Cost and rate limits](#cost-and-rate-limits)
- [LiteLLM infra notes](#litellm-infra-notes)
- [Open questions](#open-questions)

---

## Who this is for

AGNOHive is an open-source agentic toolkit, not a single-deployment internal tool. Running 16B–50B-class models locally at good quality needs hardware most users don't have — a GB10-class workstation or an equivalent multi-GPU rig. Cloud provider support removes that barrier: OpenAI, Anthropic/Claude, Google Gemini, Perplexity, and HuggingFace Inference are all available as a per-agent choice, routed through the same LiteLLM gateway AGNOHive already uses for local vLLM.

**This is purely additive.** The default `engineering`/`planning`/`parallel-review`/`sprint-master` teams, `INFERENCE_BACKEND=ollama` (the default), and the ZGX vLLM/Ollama stack are completely unaffected whether or not you ever touch this feature.

## How it works

**Updated 2026-08-08 — routing moved from hardcoded Python to a DB-backed registry.** Every agent's model still resolves through one function, `get_model()` (`swarm/agents.py`), but the routing table itself is no longer a hardcoded `_CLOUD_ALIASES` set in that file. Two SQL tables (`swarm/db.py`) are the source of truth:

- **`model_catalog`** — one row per known model id: `kind` (`local`/`cloud`), `provider`, `vllm_served_as` (the local ALL-MoE consolidation override), `requires_cloud_gate`, `active`.
- **`team_role_models`** — the DEFAULT model for a (team, role) pair, consulted by `api/server.py`'s `_load_team()` only when a team YAML's `model:`/`coordinator_model:` field is omitted. A YAML value, when present, always wins. **As of 2026-08-08, none of the 4 shipped teams (`engineering`, `planning`, `parallel-review`, `sprint-master`) declare `model:` at all** — every role's model is fully DB-managed by default; see [Agents & Teams](agents-and-teams.md#agent-roster) for the current values and the [Admin API](#admin-api--editing-routes-at-runtime) for how to change one.

Both tables live in whichever database `config.database_url` points at — **SQLite by default** (`data/agnohive.db`, zero setup), or Postgres/MySQL/anything SQLAlchemy has a dialect for. See [Engine-agnostic storage](#engine-agnostic-storage-sqlite-by-default) below.

`get_model()` itself is a synchronous hot path (called on every agent construction) and never queries the DB directly — it reads an in-process cache (`swarm/model_routing.py`), loaded once per process via `ensure_cache_loaded()` (at FastAPI startup, and defensively before every task run so the plain `main.py` CLI path — which never runs the FastAPI startup event — is covered too):

```
teams/*.yaml (model: <id>, optional — falls back to team_role_models when omitted)
        │
        ▼
swarm/agents.py get_model(id, host)
        │
        ▼
swarm/model_routing.py get_route(id)  ── reads the in-process cache only, no DB call
        │
        ├─ row found, requires_cloud_gate=True?  ──yes──▶ ALLOW_CLOUD_MODELS set?
        │                                                    │            │
        │                                                   yes           no → raise, loudly
        │                                                    ▼
        │                                          OpenAILike → LiteLLM gateway (:4000)
        │                                                    │
        │                                          zgx-ai-setup/litellm-config.yaml
        │                                                    │
        │                                ┌───────────────────┼───────────────────┬──────────────┐
        │                                ▼                   ▼                   ▼              ▼
        │                            OpenAI              Anthropic            Gemini      Perplexity / HF
        │
        └─ no row / row inactive / kind="local" ──▶ existing INFERENCE_BACKEND branch (vLLM or Ollama), unchanged
```

The cache is populated from the DB, seeding sane defaults into an empty `model_catalog` on first load — a fresh deployment gets working local-model routing (the ZGX consolidation mapping) and the 5 cloud aliases below without a manual seed step. Writes to the DB (via the [admin API](#admin-api--editing-routes-at-runtime)) do **not** take effect for already-running agents until `POST /admin/model-routes/reload` is called — by design, so a routing change is a deliberate, visible action, not something that silently changes mid-run.

### Engine-agnostic storage — SQLite by default

`config.database_url` (env `DATABASE_URL`) controls where `model_catalog`/`team_role_models` (and, since the same addendum standardized the whole app, `chat_sessions`/`session_messages`/`failure_log` too) actually live:

- **Unset (default)** — a local SQLite file, `data/agnohive.db`. No server to run, no credentials to configure — the literal goal of "ships ready to run when a user downloads agno-hive."
- **Set to a Postgres/MySQL/etc. URL** — e.g. `postgresql+psycopg://user:pass@host:5432/db` — same schema, same code, different engine.
- **Unset, but the legacy `POSTGRES_URI` env var IS set** (ZGX's existing deployment) — that value is reused automatically, so an upgrade needs no `.env` change. `POSTGRES_URI` itself is unaffected/unchanged — it's still separately read by LightRAG's own graph storage (`docs/guide/setup.md`); `DATABASE_URL` is App storage's own, distinct setting.

Plain vendor DSN forms (`sqlite:///...`, `postgresql://...`) are accepted and silently upgraded to the async-driver form SQLAlchemy needs (`sqlite+aiosqlite://`, `postgresql+psycopg://`) — you don't need to know that convention to configure this.

## Enabling it

1. **Set real API keys** where the LiteLLM container reads them (`zgx-ai-setup/docker-compose.yml`'s `litellm` service `environment:` block, or your own `.env`) — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `PERPLEXITYAI_API_KEY`, `HUGGINGFACE_API_KEY`. Unset is fine for any provider you don't use — that route just fails at call time, it doesn't block startup or affect local routes.
2. **Review `zgx-ai-setup/litellm-config.yaml`'s "Cloud providers" section** — the model ids there (`anthropic/claude-sonnet-4-5-20250929`, `openai/gpt-4o`, etc.) are examples; verify against each provider's current docs and adjust before real use.
3. **Set `ALLOW_CLOUD_MODELS=true`** wherever `agno-api.service` runs (ZGX's `.env`, or your own deployment's env). This is a deliberate, global opt-in gate — `get_model()` raises a clear `RuntimeError` for any cloud-aliased model if it's unset, rather than silently falling back to local or silently sending a request off-network. Default is `false`; every existing deployment is unaffected until this is explicitly flipped.
4. **Set a cloud alias as any agent's `model:` field** in whichever team YAML you're actually using — see below, there's no separate "cloud team" file to opt into.

## Hybrid teams — mixing local and cloud on one roster

There is no dedicated "cloud team" file, and no requirement that a team be all-local or all-cloud. `get_model()` resolves each agent's model independently — a team YAML can mix providers freely, one line per agent:

```yaml
# teams/engineering.yaml
agents:
  - name: Researcher
    model: sonar-pro-cloud       # cloud — native web-search grounding
  - name: Coder
    model: qwen2.5-coder:32b     # stays local — see the privacy note below
  - name: Reviewer
    model: claude-sonnet-cloud   # cloud — stronger reasoning for review
```

Requires the same `ALLOW_CLOUD_MODELS=true` gate as any other cloud use — it's a global switch, not per-team. `hive --team engineering "some task"` (or `agno_run(..., swarm_team="engineering")`) runs the mixed roster exactly like any other team; there's nothing special to invoke.

**Why edit your real team file directly instead of keeping a separate reference copy:** an earlier version of this feature shipped a parallel `teams/engineering-cloud.yaml` demonstrating the same per-agent mixing shown above. It was removed (2026-08-08) — the mixing pattern needs no dedicated file to prove it works, and a second copy of the roster risked drifting out of sync with the real `engineering.yaml`'s tuned `instructions:` blocks every time those were refined. Edit the team you actually run.

## Adding a provider

1. Add a `model_list` entry to `zgx-ai-setup/litellm-config.yaml`'s "Cloud providers" section — pick an alias with **no colon** (so it can't be mistaken for an Ollama-style local tag), point `litellm_params.model` at LiteLLM's `<provider>/<real-model-id>` form, and `api_key` at `os.environ/<YOUR_VAR>`.
2. Add a row for that same alias to `model_catalog` — either via a one-time addition to `swarm/model_routing.py`'s `_CLOUD_MODELS` seed list (only takes effect on a *fresh* deployment, since the seed only runs against an empty table), or, for an existing deployment, via `POST /admin/model-routes` (see below).
3. Add the matching `*_API_KEY` env var to `zgx-ai-setup/docker-compose.yml`'s `litellm` service.
4. Reference the alias in a team YAML's `model:`/`coordinator_model:` field, or set it as that role's DB default via `POST /admin/model-routes/teams`.
5. Call `POST /admin/model-routes/reload` if you added the row via the admin API (step 2 or step 4) — the in-process cache doesn't see a DB write until reload runs.

`tests/test_get_model_cloud.py`, `tests/test_model_routing.py`, and `tests/test_admin_model_routes_api.py` cover the routing/gating/admin logic — run them after any change here.

## Admin API — editing routes at runtime

`model_catalog`/`team_role_models` are editable through the same FastAPI app agno-hive already runs (`api/server.py`) — no separate admin service. Same trust boundary as every other endpoint (unauthenticated, reached only over Tailscale):

| Endpoint | Purpose |
|---|---|
| `GET /admin/model-routes` | List every `model_catalog` row |
| `POST /admin/model-routes` | Create a new model (`model_id`, `kind`, `provider`, `vllm_served_as`, `requires_cloud_gate`, `active`) — 409 on a duplicate `model_id` |
| `PATCH /admin/model-routes/{model_id}` | Update only the fields supplied — 404 if unknown, 400 if nothing supplied |
| `DELETE /admin/model-routes/{model_id}` | Remove a model — 409 if any `team_role_models` row still references it (the FK enforces this; remove those first) |
| `GET /admin/model-routes/teams` | List every `team_role_models` default |
| `POST /admin/model-routes/teams` | Create or replace the default for a `(team_name, role_name)` pair — 409 if `model_id` isn't in `model_catalog` |
| `DELETE /admin/model-routes/teams/{team_name}/{role_name}` | Remove a default |
| `POST /admin/model-routes/reload` | **Required after any write above** — re-reads the DB into `get_model()`'s in-process cache and returns the actual diff (`{"model_catalog": {"added": [...], "removed": [...], "changed": [...]}, "team_role_models": {...}}`), so a mistake is visible immediately instead of a bare 200 |

A write that isn't followed by `/reload` has no effect on any agent — this is deliberate, not a bug: routing changes are explicit, not silently live mid-run.

## Safety guardrails

**Privacy.** Any agent reading real project source via MCP tools (`get_file_content`, `search_files`, …) sends that content to a third party as part of the prompt once it's routed to a cloud provider — a genuine data-exposure decision, never a silent default. The `ALLOW_CLOUD_MODELS` opt-in gate is the actual guardrail (raises loudly, not a silent fallback) — since teams can mix providers per-agent (see above), it's a per-role decision on YOUR part which agents read real source AND go to a cloud provider; code-reading roles (Coder, Researcher) are the ones to think hardest about before routing to cloud for a proprietary codebase.

**`README.md`'s "100% local, no cloud API calls" tagline** describes the default configuration — it's accurate for every deployment that doesn't explicitly opt into this feature, and remains false only for a run that names a cloud-aliased model with `ALLOW_CLOUD_MODELS=true` set.

## Cost and rate limits

Per-user subscription-tier awareness (tracking what plan/limit a given provider API key is on) is **explicitly out of scope** — providers enforce this authoritatively themselves, and there's no uniform way to query it across 5 different providers. Two narrower, in-scope things instead:

- **Operator-side spend ceiling, independent of the provider's own plan** — LiteLLM's built-in per-key `max_budget`/`budget_duration` (LiteLLM's own feature, not something AGNOHive builds) lets you cap spend regardless of what the underlying plan allows — the same mitigation `TOOL_CALL_LIMIT`/`MAX_ITERATIONS` already provide against a runaway coordinator loop, now with real financial stakes once cloud-routed.
- **Graceful degradation on rate-limit/quota rejection** — `swarm/team.py`'s `_cloud_provider_error_message()` catches `openai.RateLimitError` (the exception type AGNOHive's own process actually sees — LiteLLM talks OpenAI protocol back to agno's `OpenAILike` client, so this is `openai.*`, not `litellm.*`, which only exists inside the separately-running gateway process) and turns it into a clear message — *"Cloud model provider hit its rate limit or quota — retry shortly, or switch this agent to another provider/local model"* — instead of an opaque failure. One catch covers all 5 providers, since LiteLLM normalizes their distinct error shapes into this one common form before it reaches AGNOHive.

## LiteLLM infra notes

Since 2026-08-08, `litellm` runs on the default docker-compose bridge network, not `network_mode: host` — it now holds real cloud provider API keys and makes outbound internet calls, which doesn't need host-level network access and is unnecessary exposure for a credential-holding service. Local vLLM containers are reached by service name (`http://vllm-coord:8000`, etc.) on the shared compose network; the ad-hoc `:8004`/`:8005` boxes (not defined in this compose file) are reached via `host.docker.internal` (resolvable via the `extra_hosts: host-gateway` entry — this is not automatic on native Linux Docker Engine the way it is on Docker Desktop).

Recommended next steps, not yet done: prefer LiteLLM's own "virtual keys" (internal tokens mapped to the real provider keys server-side) over configuring the raw provider keys directly, so a leaked/misused key is revocable and never the actual provider credential. Keep one LiteLLM instance on ZGX rather than splitting local/cloud routing across two — local-vLLM routing has a real reason to stay near the GPU boxes (latency, same host); cloud routing doesn't have that constraint but also doesn't justify the operational complexity of a second instance unless ZGX uptime becomes an actual bottleneck for cloud-routed agents specifically.

## Open questions

Not resolved by design, only by testing against real cloud credentials:

1. **Tool-calling reliability per provider** — does LiteLLM's OpenAI-shape translation lose fidelity on any provider's native tool-calling protocol versus using agno's own native class for that provider directly (`agno.models.anthropic.Claude`, `agno.models.google.Gemini`, etc. — all already installed, confirmed present under `agno/models/`)? If a specific provider's reliability suffers, that provider can be special-cased to bypass LiteLLM using its native agno class, while everything else stays on the unified gateway path.
2. **Is a tool-call-format fixup layer needed for any cloud provider** the way `OllamaToolFix` exists for specific local Ollama models? Only discoverable by running real multi-tool-call tasks against each provider.
3. **MCP tool-surface behavior across providers** (verbosity, over-calling, malformed args) — same, only discoverable by running Coder/Reviewer-shaped tasks against each provider on realistic work.

---

**Next:** [🤖 Agents & Teams](agents-and-teams.md) · [🔌 API Usage](api.md)
