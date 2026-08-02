← [Back to guide index](README.md) · [Main README](../../README.md)

# ⚙️ Setup

Everything needed to get ZGX and a client machine talking to each other.

## Contents
- [ZGX prerequisites](#zgx-prerequisites)
- [Ollama models](#ollama-models)
- [Client machine prerequisites](#client-machine-prerequisites)
- [Installation (ZGX)](#installation-zgx)
- [Infrastructure setup (ZGX)](#infrastructure-setup-zgx)
- [Client setup: hive-mcp](#client-setup-hive-mcp)
- [Configuration (.env)](#configuration-env)

---

## ZGX prerequisites

- Ubuntu / Linux with Python 3.12+
- Miniforge or standard venv
- Ollama running natively (for GPU access)
- Docker + Docker Compose (for Qdrant and PostgreSQL/AGE)
- Tailscale

## Ollama models

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
# Ollama (running natively on ZGX)
OLLAMA_HOST=http://<zgx-ip>:11434

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
