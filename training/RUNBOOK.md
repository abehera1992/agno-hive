# Phase 2.5 → 4 Runbook (ZGX)

All commands are copy-pasteable on ZGX. Two rules that are not negotiable:

- **Training runs in `zgx-train` only.** Installing unsloth into `zgx` bumps torch out
  from under vLLM (risk #3, already materialised once).
- **Nothing is promoted without the gate exiting 0.** Stage D removed production A/B,
  so the offline gate is the only signal before a checkpoint swap.

---

## Phase 2.5 — pre-flight (NO maintenance window; stack stays up)

### 2.5.1 Expand the corpus — this is a blocker, not a nicety

v1 had **33** preference pairs, only **2** about citation — the axis Phase 3 targets.
The synthetic source fixes that; ground truth is read off disk, never invented.

```bash
cd ~/agno-hive && git pull origin main

# refresh project patterns over the project MCP (no checkout needed)
~/miniforge3/envs/zgx/bin/python -m training.fetch_patterns --out /tmp/ekam-patterns

# build v2 WITH citation pairs
~/miniforge3/envs/zgx/bin/python -m training.build_dataset \
  --out training/data/v2.jsonl \
  --patterns /tmp/ekam-patterns \
  --project ekam \
  --citation-root /home/abehera1992/agno-hive \
  --citation-per-file 3
```

Expect ≈ **540 records / 195 preference pairs** (162 citation). Verify ground truth:

```bash
~/miniforge3/envs/zgx/bin/python - <<'PY'
import json, pathlib
rows=[json.loads(l) for l in open('training/data/v2.jsonl',encoding='utf-8')]
syn=[r for r in rows if r['source']=='synthetic_citation' and r['meta']['shape']=='with_excerpt']
ok=sum(1 for r in syn if r['meta']['symbol'] in
       (pathlib.Path('/home/abehera1992/agno-hive')/r['meta']['file'])
       .read_text(encoding='utf-8').splitlines()[r['meta']['true_line']-1])
print(f"ground truth: {ok}/{len(syn)} correct")   # must be N/N
PY
```

**Stop if this is not N/N.** A wrong `chosen` line trains the model to cite wrongly.

### 2.5.2 Expand eval cases 6 → 15–20

The gate refuses any axis with `< min_cases_per_axis` (4). Current n: A=1, B=3, C=2, D=2
— **three axes are below the floor**, so the gate cannot pass today regardless of training.

Target distribution: **A ≥ 4, B ≥ 5, C ≥ 6, D ≥ 5**.

Axis C template (evidence present → exact line required):

```bash
cat > ~/agno-hive/training/eval/cases/C3-router-signature.json <<'JSON'
{
  "id": "C3-router-signature",
  "kind": "citation",
  "provenance": "EK-269 changed the GET /items signature on 2026-07-30 — recent change, so stale weights show up.",
  "scorers": ["grounding", "citation"],
  "prompt": "Numbered excerpt from API/inventory-service/router/items_api.py:\n\n   206|    category: Optional[str] = None,\n   207|    category_id: Optional[UUID] = None,\n   208|    item_type: Optional[str] = None,\n   209|    status_filter: Literal[\"active\", \"inactive\", \"all\"] = Query(\"active\", alias=\"status\"),\n   210|    tags: Optional[str] = None,\n\nWhich line declares the parameter whose wire-level name is `status`?",
  "required_facts": ["status_filter", "alias"],
  "forbidden_facts": ["active_only", "bool"],
  "correct_lines": [209]
}
JSON
```

Axis B template (no evidence → restraint required):

```bash
cat > ~/agno-hive/training/eval/cases/B4-similar-items-field.json <<'JSON'
{
  "id": "B4-similar-items-field",
  "kind": "grounding",
  "provenance": "EK-271/272 added similar_items to ItemResponse on 2026-07-30. Post-cutoff, so confident answers are fabrication.",
  "scorers": ["grounding", "citation"],
  "prompt": "In EkamApp, what type is the `similar_items` field on the ItemResponse Pydantic class, and on what line is it declared?",
  "required_facts": ["read", "file"],
  "forbidden_facts": ["list[str]", "str]", "definitely", "certainly"],
  "correct_lines": [96]
}
JSON
```

**Author the rest against changes made after the model's cutoff** (EK-269→272:
`status_filter`, `similar_items`, the pg_trgm migration, `AGNO_FAILURE_CONTEXT_LIMIT`).
Those are the only prompts where stale-weight fabrication is detectable.

### 2.5.3 Re-baseline on the widened set

> **The served WEIGHTS are the only thing that distinguishes a baseline run from a
> candidate run.** The command below is byte-identical to the candidate command apart
> from `--out`/`--label`: both point at `:8003` and ask for `local-shared`, which is a
> stable alias whose backing checkpoint has been swapped at least four times
> (granite4.1:30b → qwen3-coder:30b → Qwen3-30B-A3B-Instruct-2507-FP8 → qwen3-30b-hive-v2-fp8).
>
> So **check what `:8003` is actually serving before running this**, or the output is a
> candidate measurement wearing the baseline filename:
>
> ```bash
> docker inspect vllm-coord --format '{{range .Mounts}}{{.Source}}{{println}}{{end}}'
> ```
>
> Done wrong on 2026-08-31: this command was run while v2 was served, producing
> candidate scores in `baseline.json` — the file `gate.py` compares every future
> candidate against. Nothing errored; the numbers simply meant something other than
> their filename. An untracked file also leaves no trace when overwritten, which is why
> the eval outputs are now committed (see 2.5.4).

```bash
~/miniforge3/envs/zgx/bin/python -m training.eval.harness \
  --base-url http://localhost:8003/v1 --model local-shared \
  --out training/eval/baseline.json \
  --label "untuned Qwen3-30B-A3B-Instruct-2507-FP8 (baseline, 15-20 cases)"
```

The 6-case baseline is **superseded** — do not compare a widened candidate against it.

### 2.5.4 Commit the eval output

Eval results are tracked in git (`training/eval/*.json`). They are small (~25 KB), they
are the only record of how a given checkpoint scored, and leaving them untracked made it
impossible to answer "did a baseline ever exist?" after one was overwritten.

Two things to know when reading them:

* **Scores move between identical runs.** Two runs of the same 44 cases against the same
  served model, minutes apart, gave `C 93.3% → 86.7%` and `D 21.4% → 28.6%`. At n=14-15
  one case flipping is ~7 points, so treat any single-run C/D delta under ~10 points as
  noise. A/B/E were stable across both.
* **Axis D's scorer requires literal strings the prompt never states.** D-guard10 gives a
  rule about `old_string` uniqueness, says "write code that follows this rule", then
  requires the output to contain `db.add(invite)` and `db.refresh(invite)` — names that
  appear only in the guard's original example. Cases whose required string IS in the
  prompt pass; cases whose isn't, fail. D is not a clean measure of rule-following until
  that is fixed.

---

## Phase 3a — fetch the base model (NO window; stack stays up)

```bash
# pre-checks — need ~153 GB total (61 BF16 + 61 merged + 31 FP8)
df -h /home | tail -1
free -g | head -2

mkdir -p /home/abehera1992/models
HF_TOKEN=$(grep -E '^HF_TOKEN=' ~/agno-hive/.env | cut -d= -f2-) \
~/miniforge3/envs/zgx-train/bin/python - <<'PY'
from huggingface_hub import snapshot_download
p = snapshot_download(
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
    local_dir="/home/abehera1992/models/Qwen3-30B-A3B-Instruct-2507",
    max_workers=8,
    allow_patterns=["*.safetensors","*.json","*.txt","*.model"],  # skip .bin duplicates
)
print("downloaded ->", p)
PY

du -sh /home/abehera1992/models/Qwen3-30B-A3B-Instruct-2507   # expect ~57-62 GB
```

Download over Tailscale takes a while — **run it before the window opens**, not inside it.

Dry-run the recipe with no weights loaded:

```bash
cd ~/agno-hive && ~/miniforge3/envs/zgx-train/bin/python -m training.train \
  --config training/config/qwen3-30b.yaml --dry-run
```

---

## Phase 3b — the maintenance window (hive IS DOWN)

### a) Graceful shutdown

```bash
# tell anyone watching, then drain
curl -s http://localhost:9001/health

# stop the API first so no new task starts mid-shutdown
systemctl --user stop agno-api.service

# free the GPU: coord (~72 GB), extract (~11 GB), embed (~7 GB)
docker stop vllm-coord vllm-extract vllm-embed

# STOP THIS BEFORE TRAINING STARTS -- not optional. Its "stale inference" detector
# fires `docker restart vllm-coord` after 25 CONTINUOUS minutes of GPU util >=88%,
# with zero awareness of whether that util is stuck vLLM inference or a legitimate
# training run -- and QLoRA training sits at 90-96% util for its whole multi-hour
# duration. Found live 2026-08-16: caught with ~9-10 min to spare before it would
# have restarted vllm-coord mid-training, reopening production traffic capability
# while the window was still supposed to be in effect and risking GPU/unified-memory
# contention corrupting the run. Full detail: DOCS.md "zgx-thermal-watchdog.service".
systemctl --user stop zgx-thermal-watchdog.service

# confirm ~110 GB is actually free before starting
free -g | head -2
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

Do **not** `docker rm` them — `docker start` restores the same config afterwards.

### b) Train

```bash
cd ~/agno-hive
nohup ~/miniforge3/envs/zgx-train/bin/python -m training.train \
  --config training/config/qwen3-30b.yaml > /tmp/train.log 2>&1 &
TRAIN_PID=$!
tail -f /tmp/train.log
```

**Attach the training thermal guard immediately — not optional.** Stopping
`zgx-thermal-watchdog.service` above removes ALL its thermal protection, not just the
stale-inference trigger that made stopping it necessary — that watchdog can only ever
`docker start/stop/restart` `vllm-coord`/`extract`/`embed`, so it was never able to
protect against heat from the training process itself even when running, and its
emergency tier would make things WORSE mid-training (starts vllm-coord back up on an
already-overheating box). Confirmed live 2026-08-16: the first Qwen3.8-27B training
attempt drove ZGX hot enough that the box auto-shut off outright, with zero monitoring
in place and no graceful handling of the training process. Full incident + design
rationale: `DOCS.md` "zgx-thermal-watchdog.service" (second incident note).

```bash
nohup ~/bin/training-thermal-guard.sh "$TRAIN_PID" \
  > /tmp/thermal_guard_stdout.log 2>&1 < /dev/null &
disown
# log: /tmp/training_thermal_guard.log
# SIGTERM at 90C sustained (2 polls), SIGKILL at 95C -- watches the training PID
# directly, exits on its own once that PID is no longer running (script lives on
# ZGX at ~/bin/, same location convention as thermal-watchdog.sh, not in this repo)
```

Watch for: `peak GPU` (expect ~25–35 GB) and the loss trend. If peak approaches 100 GB,
kill it — something is not in 4-bit.

### c) Merge to BF16

```bash
~/miniforge3/envs/zgx-train/bin/python -m training.export.merge \
  --config training/config/qwen3-30b.yaml \
  --out /home/abehera1992/models/merged/worker-v1-bf16
```

### d) Offline eval of the candidate

Serve the **merged BF16** on a spare port (8005) so the real coordinator config is untouched:

```bash
# --entrypoint vllm is REQUIRED. docker-compose.yml sets entrypoint: ["vllm"], but a
# bare `docker run` does NOT inherit it — without the flag this fails instantly with
# `Error: No such command '/model'` (hit for real on 2026-07-30).
docker run -d --name vllm-candidate --device nvidia.com/gpu=all \
  --entrypoint vllm \
  -v /home/abehera1992/agno-hive/checkpoints/qwen3-30b-hive-v2-merged-bf16:/model:ro \
  -p 8005:8000 timothystewart6/vllm-gb10:latest \
  serve /model --served-model-name candidate \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --gpu-memory-utilization 0.55 --max-model-len 32768 --host 0.0.0.0 --port 8000

# wait for HTTP 200, then score it
~/miniforge3/envs/zgx/bin/python -m training.eval.harness \
  --base-url http://localhost:8005/v1 --model candidate \
  --out training/eval/candidate.json --label "worker-v1 merged BF16 candidate"
```

> BF16 needs ~61 GB resident — this is why it runs **inside** the window with the
> production containers down, not alongside them.

### e) Gate

```bash
# NEVER read $? straight after a pipe — it reports the LAST command's status, not the
# gate's. Piping through `tail` on 2026-07-30 printed "GATE exit=0" for a run that had
# actually exited 1 (DO NOT PROMOTE). A promotion decision read off the wrong exit code
# is the most dangerous mistake available in this runbook.
~/miniforge3/envs/zgx/bin/python -m training.eval.gate \
  --config training/config/qwen3-30b.yaml \
  --baseline training/eval/baseline.json \
  --candidate training/eval/candidate.json 2>&1 | tee /tmp/gate_v2.log
GATE_RC=${PIPESTATUS[0]}          # <- the GATE's status, not tee's
echo "gate exit=$GATE_RC"         # 0 = promote, 1 = do not promote
[ "$GATE_RC" -eq 0 ] || echo ">>> DO NOT QUANTISE <<<"
```

Enforces C ≥ 0.80, B ≥ 0.85, A ≥ 0.98, D ≥ 0.98, **plus** no regression >2 pts on any
axis, **plus** ≥4 cases per axis.

### f) Restore service — ALWAYS, pass or fail

```bash
docker rm -f vllm-candidate
docker start vllm-embed && sleep 5 && docker start vllm-extract vllm-coord
sleep 240
curl -s -o /dev/null -w "vllm-coord HTTP %{http_code}\n" http://localhost:8003/v1/models
systemctl --user start agno-api.service
curl -s http://localhost:9001/health

# Re-arm the thermal watchdog stopped at the start of Phase 3b -- it does not
# restart itself. Skipping this leaves the GPU with no thermal protection at all
# until the next person happens to notice.
systemctl --user start zgx-thermal-watchdog.service
systemctl --user status zgx-thermal-watchdog.service
```

---

## Phase 4 — promotion (only if the gate exited 0)

```bash
# archive the current FP8 for rollback BEFORE overwriting anything
cp -r ~/.cache/vllm-hf/models--Qwen--Qwen3-30B-A3B-Instruct-2507-FP8 \
      /home/abehera1992/models/rollback-fp8-$(date +%F)

# quantise merged BF16 -> FP8, then point the compose `serve` line at it,
# recreate vllm-coord (~4 min), and watch one real workload.
```

Rollback = swap the `serve` line back and recreate. Keep the archived checkpoint until
the new one has survived a full day of real traffic.
