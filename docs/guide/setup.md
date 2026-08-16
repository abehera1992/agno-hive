← [Back to guide index](README.md) · [Main README](../../README.md)

# ⚙️ Setup

Everything needed to get ZGX and a client machine talking to each other.

## Contents
- [ZGX prerequisites](#zgx-prerequisites)
- [Model serving: Ollama or vLLM](#-model-serving-ollama-or-vllm)
  - [Option A — Ollama](#option-a--ollama-default)
  - [Option B — vLLM + LiteLLM](#option-b--vllm--litellm)
  - [Switching backends](#-switching-backends)
- [Client machine prerequisites](#client-machine-prerequisites)
- [Installation (ZGX)](#installation-zgx)
- [Infrastructure setup (ZGX)](#infrastructure-setup-zgx)
- [Client setup: hive-mcp](#client-setup-hive-mcp)
- [Configuration (.env)](#configuration-env)

---

## ZGX prerequisites

- Ubuntu / Linux with Python 3.12+
- Miniforge or standard venv
- Docker + Docker Compose (for Qdrant, PostgreSQL/AGE, and optionally vLLM + LiteLLM)
- Tailscale
- **Either** Ollama running natively (for GPU access) **or** an NVIDIA GPU reachable via the Container Device Interface (CDI) for vLLM — see below

## 🧠 Model serving: Ollama or vLLM

AGNOHive supports **two interchangeable inference backends** — pick one, or set up both and switch per session. Both serve the same agent roster; which one is active is controlled by a single env var (see [Switching backends](#-switching-backends)).

| | Ollama | vLLM + LiteLLM |
|---|---|---|
| **Best for** | Simplicity, diverse per-role models, no GPU sharing math | Higher throughput, longer context, continuous batching, one resident model serving the whole roster |
| **Roster shape** | Diverse — a different model per agent role | Consolidated — the whole roster maps onto one resident coordinator model (`vllm_served_as` in the `model_catalog` DB table, see [☁️ Cloud Model Providers](cloud-models.md#how-it-works)) |
| **Setup** | `ollama pull ...` | `docker compose -f zgx-ai-setup/docker-compose.yml up -d` |

### Option A — Ollama (default)

Pull before first run:

```bash
ollama pull qwen3-coder:30b        # engineering Coordinator — non-thinking A3B MoE
ollama pull qwen2.5-coder:32b      # Researcher + Planner + Coder + Reviewer
ollama pull qwen2.5-coder:7b       # planning/parallel-review coordinator + LightRAG extraction
ollama pull llama3.1:8b            # ContextRouter + Executor + session compaction
ollama pull qwen3-embedding:0.6b   # LightRAG embeddings (1024-dim)
```

All agents run local Ollama models. Set any model via env var (e.g. `CODER_MODEL=qwen2.5-coder:32b`) or in `teams/*.yaml`.

<details>
<summary><b>📋 Model compatibility notes (click to expand)</b></summary>

> **Active roster (2026-06-12):** 5 models, ~47 GB resident. `ibm/granite4.1:30b` and `qwen3:30b-a3b` were deleted (35 GB freed) — re-pull granite only if a coordinator rollback is needed.
>
> **ARM64 GB10 model compatibility:**
> - `qwen3-coder:30b` — ✅ **Current engineering coordinator.** Non-thinking A3B MoE. Same-task A/B (full pipeline): 84s grounded (read every service) vs granite4.1:30b 217s (hallucinated purposes from dir names) vs qwen3:30b-a3b thinking 318s. ~2.6× faster than granite with better grounding.
> - `qwen2.5-coder:32b` — ✅ Researcher/Planner/Coder/Reviewer. Reliable tool use. (Was unusable as *coordinator* on Ollama 0.24 — CUDA crash after ~14 min — but that was the old build; untested as coordinator since 0.30.6.)
> - `qwen2.5-coder:7b` — ✅ planning + parallel-review coordinator, and LightRAG extraction (better code entities than llama3.1:8b; ~30% slower but worth it).
> - `llama3.1:8b` — ✅ ContextRouter + Executor + session compaction.
> - **Thinking models are NOT suitable for swarm roles:** `qwen3:30b-a3b` burns ~1K hidden reasoning tokens/turn; `gemma4:26b-a4b` returns EMPTY content under tight `num_predict`. `/no_think` and `think:false` do not disable cleanly. Use non-thinking variants only.
> - Retired: `deepseek-r1`, `gemma3:27b`, `mixtral:8x7b` (HTTP 400 on tool calls); `devstral:24b`, `mistral-small3.1:24b`, `lfm2:24b`, `nemotron3:33b` (tool-calling/orchestration failures, 2026-05).
>
> **MoE-on-GB10 segfaults FIXED on Ollama 0.30.6 (retested 2026-06-11):** `qwen3:30b-a3b` and `gemma4:26b-a4b` both ran warmup + sustained + 4-parallel inference with zero crashes — the old 0.24-era `cuda_v12` libs didn't list GB10's `cc=1210`; `cuda_v13` does. The MoE instability entries above are obsolete; the only reason those two aren't in the roster is their *thinking* behaviour, not stability. See [Ollama Upgrade & GB10 Compatibility](../../DOCS.md#ollama-upgrade--gb10-compatibility-20260607) in DOCS.md.

</details>

### Option B — vLLM + LiteLLM

A GB10 (ARM64) serving stack: one resident vLLM coordinator handles the **entire** agent roster (continuous batching, no model-swap cost), a small embedding server backs LightRAG, and an on-demand extraction server is started only while indexing. [LiteLLM](https://github.com/BerriAI/litellm) fronts all three behind one OpenAI-compatible gateway so agno never needs to know which port a model lives on.

**Prerequisites:** NVIDIA GB10 GPU reachable via CDI (`--device nvidia.com/gpu=all`, no sudo needed), and a [Hugging Face](https://huggingface.co/) **read token** (weights are pulled from the Hub on first start).

```bash
export HF_TOKEN=hf_xxxxxxxxxxxx     # read-token from huggingface.co/settings/tokens
docker compose -f zgx-ai-setup/docker-compose.yml up -d

# Verify
curl http://localhost:4000/v1/models        # LiteLLM gateway
docker logs vllm-coord --tail 20            # watch for "Uvicorn running"
```

| Service | Hugging Face model | Served as | Port | GPU mem | Always on? |
|---|---|---|---|---|---|
| `vllm-coord` | [`Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8) | `local-shared` | 8003 | 0.6 | ✅ yes — serves the whole roster |
| `vllm-embed` | [`Qwen/Qwen3-Embedding-0.6B`](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | — | 8002 | 0.05 | ✅ yes — LightRAG query embeddings |
| `vllm-extract` | [`RedHatAI/Meta-Llama-3.1-8B-Instruct-FP8`](https://huggingface.co/RedHatAI/Meta-Llama-3.1-8B-Instruct-FP8) | `llama3.1-8b` | 9100 | 0.1 | ❌ **indexing-only** — start before `hive --bootstrap`, stop after (`docker start/stop vllm-extract`) to free ~13 GB |
| `litellm` | — (gateway, no weights) | — | 4000 | — | ✅ yes |

<details>
<summary><b>📋 Notes (click to expand)</b></summary>

> **`vllm-coord`'s served name (`local-shared`) can be an alias for a fine-tuned checkpoint.** The compose file above serves the stock HF base directly; a production deployment may instead mount a locally fine-tuned/requantized checkpoint under the same served name (e.g. an Unsloth QLoRA+ORPO fine-tune merged and FP8-requantized) so `litellm-config.yaml` and the `model_catalog` DB table's `vllm_served_as` column need no change either way. Check `docker inspect vllm-coord` (or the `command:` line in `zgx-ai-setup/docker-compose.yml`) to see what's actually mounted on a given box — `serve <hf-repo-id>` means the stock base; `serve /path/to/local/checkpoint` means a fine-tune. The alias is named `local-shared` rather than after any specific model (renamed 2026-08-16 from `qwen3-coder-30b`, itself a name inherited from a model that stopped being served back on 2026-07-25) precisely so it never needs renaming again on the next swap or fine-tune promotion — and `docker-compose.yml`'s own committed state drifted from the real live mount once already because of this, so keep the two in sync going forward.
>
> **ALL-MoE consolidation:** unlike Ollama (which keeps a diverse roster warm — different models for different roles), the vLLM stack collapses the entire roster onto the one resident coordinator (`vllm_served_as` in the `model_catalog` DB table — AGNOHive 2.3.2 addendum, 2026-08-08; was a hardcoded `_VLLM_MODEL_MAP` dict in `swarm/agents.py` before this). This trades per-role model diversity for continuous-batching throughput and one large resident KV cache, since the dense 32B Ollama models measured 5–7× slower per token on GB10 with no measurable code-quality edge over the MoE coordinator.
>
> **`vllm-extract` is indexing-only.** A LightRAG *query* uses the coordinator (`local-shared`) for keyword extraction + synthesis, not `vllm-extract` — that server is only the entity-extraction LLM used while indexing (`hive --bootstrap`). Leaving it resident wastes ~13 GB of unified memory for zero benefit outside indexing runs.
>
> **Context window:** `vllm-coord --max-model-len` is tuned for the fixed per-run injection (`hive.md` + `patterns/**/*.md` ≈ 56K tokens) plus real multi-tool tasks — see `zgx-ai-setup/docker-compose.yml` for the current value and the reasoning in its comments.

</details>

### 🔀 Switching backends

One env var controls which backend every agent uses — no code changes:

```env
INFERENCE_BACKEND=ollama   # default — OllamaToolFix, diverse per-role roster
INFERENCE_BACKEND=vllm     # OpenAI-compatible client against the LiteLLM gateway

# Only read when INFERENCE_BACKEND=vllm:
VLLM_GATEWAY_URL=http://localhost:4000/v1   # default; override if LiteLLM runs elsewhere
```

Both stacks can be installed side by side — Ollama running natively and the vLLM containers stopped (or vice versa) — and you flip between them by restarting the AGNOHive API server with a different `INFERENCE_BACKEND` value:

```bash
# Switch to vLLM for this session
INFERENCE_BACKEND=vllm python main.py --serve

# Switch back to Ollama
INFERENCE_BACKEND=ollama python main.py --serve
```

On ZGX (systemd), set `INFERENCE_BACKEND` in the service's environment file and `systemctl --user restart agno-api.service` — see [🚀 Running AGNOHive](running.md).

## Client machine prerequisites

- Docker (for hive-mcp)
- **Tailscale** — mandatory; ZGX and client machines must be on the same Tailscale network
- Python 3.8+ (for the `hive` CLI — stdlib only, no pip install needed)

---

## Installation (ZGX)

```bash
git clone <repo-url> ~/agno-hive
cd ~/agno-hive
pip install -r requirements.txt
```

---

## Infrastructure setup (ZGX)

Start Qdrant and PostgreSQL/AGE via Docker:

```bash
docker compose -f docker/docker-compose.zgx.yml up -d

# Verify
docker ps --filter "name=agno-"
curl http://localhost:6333/healthz
docker exec agno-postgres-age psql -U agno -d agno_graph -c "SELECT * FROM ag_catalog.ag_graph;"
```

---

## Client setup: hive-mcp

hive-mcp is a Docker container that runs on your local machine and gives AGNOHive host-level access via Tailscale.

```bash
# Copy the compose file into your project directory
cp /path/to/agno-hive/hive-mcp/docker-compose.hive.yml .

# Pull and start
docker compose -f docker-compose.hive.yml up -d

# Verify
docker ps --filter "name=hive-mcp"
```

ZGX reaches it via your Tailscale IP: `http://<your-tailscale-ip>:9000/mcp`

The `hive` CLI auto-detects your Tailscale IP — no manual URL configuration needed.

### 🔎 Enabling web search

Add `WEB_SEARCH_ENABLED=true` to the container to give agents access to `web_search` and `web_fetch`:

```bash
# docker run
docker run -d --name hive-mcp ... -e WEB_SEARCH_ENABLED=true ghcr.io/abehera1992/hive-mcp:latest

# docker compose — set in shell or .env before running
WRITE_REVIEW=true WEB_SEARCH_ENABLED=true docker compose -f docker-compose.hive.yml up -d
```

When enabled, agents will:
- **Auto-fetch any URL** the user shares in a prompt
- **Read GitHub repos** (README + metadata) when a repo URL or name is mentioned
- **Search DuckDuckGo** when asked about unfamiliar libraries, tools, or technologies
- **Chain search → fetch** — find the best result, then read the full page for grounded answers

Uses the client machine's network. No API key required.

---

## Configuration (.env)

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Minimum required `.env` on ZGX:

```env
# Inference backend — see "Model serving: Ollama or vLLM" above
INFERENCE_BACKEND=ollama                     # or "vllm"

# Ollama (only read when INFERENCE_BACKEND=ollama)
OLLAMA_HOST=http://<zgx-ip>:11434

# vLLM + LiteLLM (only read when INFERENCE_BACKEND=vllm)
VLLM_GATEWAY_URL=http://localhost:4000/v1
HF_TOKEN=hf_xxxxxxxxxxxx                     # read-token, needed once to pull weights

# Client project MCP server (Streamable HTTP, /mcp endpoint)
MCP_URL=http://<project-host>:9000/mcp

# Storage (Docker on ZGX)
QDRANT_URL=http://localhost:6333
POSTGRES_URI=postgresql://agno:agno@localhost:5432/agno_graph

# PostgreSQL individual vars (required by LightRAG)
POSTGRES_USER=agno
POSTGRES_PASSWORD=agno
POSTGRES_DATABASE=agno_graph
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# App storage — sessions, the feedback log, model routing (AGNOHive 2.3.2 addendum).
# A DIFFERENT concern from POSTGRES_URI above (that one's for LightRAG's graph
# storage). Unset by default: without it, agno-hive uses a local SQLite file
# (data/agnohive.db) — zero setup, works the moment you clone the repo. Set this
# to point the app's own storage at Postgres/MySQL/anything SQLAlchemy supports
# instead — e.g. reusing the same Postgres instance as above:
# DATABASE_URL=postgresql+psycopg://agno:agno@localhost:5432/agno_graph
# If DATABASE_URL is unset but POSTGRES_URI (above) IS set, that value is reused
# automatically — an existing ZGX deployment needs no .env change on upgrade.
# DATABASE_URL=

# LightRAG MCP (Streamable HTTP, port 9002)
LIGHTRAG_MCP_PORT=9002
LIGHTRAG_MCP_URL=http://localhost:9002/mcp
LIGHTRAG_LLM_MODEL=qwen2.5-coder:7b
LIGHTRAG_EMBED_MODEL=qwen3-embedding:0.6b
LIGHTRAG_EMBED_DIM=1024

# Observability → SigNoz (optional, omit to disable)
OTEL_EXPORTER_OTLP_ENDPOINT=http://<signoz-host>:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_RESOURCE_ATTRIBUTES=service.name=agno-hive,deployment.environment=dev
```

---

**Next:** [🚀 Running AGNOHive](running.md)
