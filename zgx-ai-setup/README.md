# zgx-ai-setup — AGNOHive inference stack on the ZGX (GB10)

Reproducible capture of the local-inference serving stack that runs AGNOHive on the
**NVIDIA GB10 (DGX Spark, `sm_121`, 121 GB unified memory)** workstation "ZGX". Before
this folder, every piece below was a hand-typed `docker run` / `nohup` on the box with
no recovery path — if ZGX were wiped, the stack had to be rebuilt from memory. This
folder is that memory.

## Architecture (ALL-MoE — the production layout)

```
agno swarm + LightRAG
        |  OpenAI-compatible
        v
LiteLLM  :4000   (gateway — model-name routing, request_timeout)
        |
        v
vLLM coordinator :8003   Qwen3-Coder-30B-A3B-Instruct-FP8  (served as qwen3-coder-30b)
                         util 0.6, 64K ctx — the WHOLE roster runs here (coordinator,
                         all workers, ContextRouter/Executor, and LightRAG's LLM)
vLLM embeddings  :8002   Qwen3-Embedding-0.6B (1024-dim, always resident)

AGNO API :9001  +  LightRAG MCP :9002   (systemd --user services)
Ollama   :11434                          (fallback backend — see Reversibility)
```

The dense `qwen2.5-coder:32b` was dropped after an A/B showed it ~5–7× slower per token
than the 30B A3B MoE on the GB10 with no quality edge, so the entire roster collapses onto
the single resident 30B (`swarm/agents.py _VLLM_MODEL_MAP` maps every tag → `qwen3-coder-30b`).
vLLM continuous-batching serves the concurrent agent + LightRAG load on the one model
(measured: 8 concurrent requests in ~17 s, no queueing).

## Components & ports

| Component | Port | Started by | Notes |
|---|---|---|---|
| vLLM coordinator (30B) | 8003 | `docker-compose.yml` | resident, `--restart unless-stopped` |
| vLLM embeddings | 8002 | `docker-compose.yml` | resident |
| LiteLLM gateway | 4000 | `docker-compose.yml` | host network |
| AGNO API (swarm) | 9001 | `systemd/agno-api.service` | self-healing |
| LightRAG MCP | 9002 | `systemd/lightrag.service` | self-healing |
| llama-swap (optional) | 9100 | host binary | diverse-roster variant only |
| dedicated 32B (optional) | 8004 | ad-hoc | diverse-roster variant only |

## Prerequisites

- **vLLM image:** `timothystewart6/vllm-gb10:latest` (a prebuilt `sm_121` + CUDA 13 vLLM
  for the GB10 — stock `pip install vllm` does NOT cover `sm_121`).
- **NVIDIA Container Toolkit + CDI** enabled, so GPUs attach via `--device nvidia.com/gpu=all`
  (no `sudo`, no `--gpus`). The compose `devices: [nvidia.com/gpu=all]` mirrors this.
- **`HF_TOKEN`** — a HuggingFace **read** token, exported in the environment. Needed to pull
  the FP8 weights on first container start. NEVER commit it. Weights cache at
  `~/.cache/vllm-hf` (volume-mounted) so subsequent starts are fast.
- Python env for the services: `~/miniforge3/envs/zgx` (the systemd units call it by full path).

## Bring-up (cold start / disaster recovery)

```bash
# 1. vLLM servers + LiteLLM gateway (the 30B reloads ~5 min on cold start)
export HF_TOKEN=<your-hf-read-token>
docker compose -f docker-compose.yml up -d
#    wait until: curl -s -o /dev/null -w '%{http_code}' http://localhost:8003/health  -> 200

# 2. systemd --user services (self-healing, boot-persistent)
export XDG_RUNTIME_DIR=/run/user/$(id -u)
cp systemd/agno-api.service systemd/lightrag.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now agno-api.service lightrag.service

# 3. (optional, diverse-roster variant only) llama-swap pool:
#    cd ~/llama-swap && ./llama-swap -config <this>/llama-swap-config.yaml -listen :9100
```

## After a reboot / power outage (GPU CDI spec recovery)

On a cold boot the vLLM containers may come back **Exited (255)** with
`CDI device injection failed: unresolvable CDI devices nvidia.com/gpu=all`. The driver is
fine — the cause is that the NVIDIA **CDI spec** (which maps `nvidia.com/gpu=all` to the GPU
device nodes) was only written to `/var/run/cdi`, and that path is **tmpfs — wiped on every
reboot**. Docker then has nothing to resolve the device against.

**Permanent fix (one-time, needs sudo):** regenerate the spec into the *persistent*
`/etc/cdi` so it survives future reboots:

```bash
sudo mkdir -p /etc/cdi && sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
docker start vllm-coord vllm-embed          # existing containers — replays original run config
```

`docker start` (not recreate) replays each container's original `docker run` config, so no
flags need re-typing. The 30B reloads ~3–5 min on cold start (`embed` is ready in ~30 s);
watch `docker logs -f vllm-coord` for `Loading safetensors checkpoint shards` → ready, then
re-check the health table above.

Once `/etc/cdi/nvidia.yaml` exists it persists across reboots, so subsequent power cycles
should bring the containers back automatically (they carry `--restart unless-stopped`).
Quick triage if a future reboot still fails:

```bash
nvidia-smi -L                 # driver/GPU healthy?  ("N/A" memory column is a GB10 quirk, not an error)
ls /etc/cdi/ /var/run/cdi/    # is a CDI spec present anywhere?
nvidia-ctk cdi generate --format=yaml | head   # does generation still work? (no root needed for dry-run)
```

> Networking note: ZGX and the dev PC are reached over **Tailscale**, whose `100.x` IPs are
> stable across LAN changes — a network switch does **not** require any config edits here.
> SSH/MCP to ZGX uses the MagicDNS hostname `zgx-32df` (= `100.96.86.82`); avoid pinning the
> LAN IP (`192.168.x.x`), which changes per network.

## Memory budget (121 GB unified)

vLLM **reserves `gpu_memory_utilization × 121 GB` up front** per server (weights + KV
cache), held even at 0 % usage. Steady state: coordinator (util 0.6 ≈ 72 GB) + embed
(util 0.05 ≈ 6 GB) + base ≈ **~85 GB used, ~35 GB free**. `max-model-len 65536` on the
coordinator (KV ≈ 431K tokens) is sized for agents' large prompts and LightRAG's big
hybrid-query context. Trim `gpu_memory_utilization` if you add a resident model.

## Reversibility — the `INFERENCE_BACKEND` flag (Ollama fallback)

Dual-pathway is permanent. `~/agno-hive/.env` carries `INFERENCE_BACKEND=ollama|vllm`,
read at startup by `lightrag_mcp/rag.py`, `config.py`, and `api/server.py`. Both code
paths stay live; flip + restart to switch. Ollama (`:11434`) keeps all 5 models on disk
(`qwen3-coder:30b`, `qwen2.5-coder:32b/7b`, `llama3.1:8b`, `qwen3-embedding:0.6b`).

To roll back to Ollama:
```bash
docker stop vllm-coord vllm-embed              # free GPU memory first
sed -i 's/^INFERENCE_BACKEND=.*/INFERENCE_BACKEND=ollama/' ~/agno-hive/.env
systemctl --user restart agno-api lightrag
# ... and to return: flip back to vllm, `docker start vllm-coord vllm-embed`, restart services
```

> **Caveat (verified 2026-06-27):** the Ollama code path is intact (the migration did not
> break it), BUT Ollama loads `qwen3-coder:30b` at its **256K-context default → ~104 GB**,
> which starves delegation workers and makes the full swarm crawl. For a usable rollback,
> cap the Ollama context (`OLLAMA_CONTEXT_LENGTH` env on the Ollama service, or a model
> `num_ctx`) to ~16–32K before relying on it. (Tracked: EK-121 / EK-124.)

## Management & logs

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status|restart|stop agno-api|lightrag
journalctl --user -u agno-api -f          # logs (NOT the old ~/agno-hive/*.log)
docker logs -f vllm-coord                  # vLLM
# Update an MCP-server image change: docker rm -f <c> && docker compose up -d <c> (restart won't pull)
```

> These files mirror what is live on ZGX. After changing the running stack on the box,
> update the matching file here so this folder never drifts from reality.
