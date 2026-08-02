← [Back to guide index](README.md) · [Main README](../../README.md)

# 🚀 Running AGNOHive

## Contents
- [Start the servers](#start-the-servers)
- [Run a task](#run-a-task)
- [Index a codebase into LightRAG](#index-a-codebase-into-lightrag)

---

## Start the servers

### 1. LightRAG MCP Server (ZGX)
```bash
python main.py --serve-lightrag
# → Streamable HTTP on 0.0.0.0:9002  (/mcp endpoint)
```

### 2. AGNOHive API Server (ZGX)
```bash
python main.py --serve
# → FastAPI on 0.0.0.0:9001
```

### 3. hive-mcp (client machine)
```bash
docker compose -f docker-compose.hive.yml up -d
```

## Run a task

### Single task (CLI)
```bash
python main.py "How does authentication work in this project?"
```

### Interactive loop
```bash
python main.py
```

> For the full CLI reference (sessions, REPL, review mode, write-review flow), see **[🖥️ CLI Client Guide](cli.md)**.

---

## Index a codebase into LightRAG

Two paths — choose based on whether ZGX has direct filesystem access to the project.

### Option A — hive-mcp bootstrap (primary path)

Runs on the client machine, no ZGX filesystem access needed:

```powershell
$env:AGNO_PROJECT = "ekam"
hive --bootstrap                          # index all source files
hive --bootstrap --force                  # full reindex from scratch
hive --bootstrap --glob "Client/**/*.ts"  # scoped to a specific directory + extension
```

**State file:** `PROJECT_ROOT/.hive-index-state/{project_id}.json` — entries are
`"<mtime_ns>:<size>|<sha256>|<chunk_count>"`. The fast mtime+size key is checked first (no file
read); on mismatch the content SHA-256 decides, so metadata-only churn
(`git reset --hard`, checkout, `touch`) never triggers a re-index. `chunk_count` lets a
changed file's old chunk docs be found and deleted by their own deterministic ids before
re-inserting, without querying LightRAG at all. Legacy entries missing either field upgrade
in place on the next pass.

**Excluded automatically:** `node_modules`, `.next`, `dist`, `build`, `signoz`, `graphify-out`, `infra`, `backups` (DB dumps — never index), `.hive-index-state`, hidden dirs, binaries, certs (`.pem`, `.key`, `.crt`). Secret/dump files are skipped by **filename pattern** (extension matching can't catch `.env`): any `*.env` / `.env.*` (except `.env.example`), `.npmrc`, `.pypirc`, and SQL files named `backup`/`dump` — while schema/migration/init SQL stays indexed. Project-specific excludes (`EXCLUDE_DIRS` / `EXCLUDE_GLOBS` env vars) are honored by the walk as well as by search/read/write.

**Throughput** (2026-06-12): chunk inserts run **6-concurrent** over the shared MCP
session, matched by LightRAG-side tuning (`max_parallel_insert=6`,
`llm_model_max_async=6` to match `OLLAMA_NUM_PARALLEL=6`,
`entity_extract_max_gleaning=0`, `embedding_batch_num=32`, dynamic `num_ctx`
8K/32K) — roughly 4–6× faster than the old serial pipeline.

**Version correctness & document identity** (updated 2026-08-02): every chunk of a file is
sent under a deterministic, **basename-unique** `file_path` and explicit `doc_id` — folding
the full relative path + chunk index into the citation path before LightRAG's own basename
extraction can collapse two different files (or two chunks of the same file) into the same
identity. Without this, LightRAG's hard-coded filename-dedup rule silently drops any document
sharing a basename with an already-indexed one — verified against a live instance, and the
reason a bare relative path is never passed as `file_path` directly. Re-indexing a changed
file first deletes its previous chunk docs by their own reconstructed ids
(`lightrag_delete_by_id`) — LightRAG indexing is otherwise append-only and stale versions
could win retrieval. Each run ends with a pipeline kick (a dedupe-rejected re-send of the
last chunk, under its own id) so LightRAG never leaves the final batch sitting in `pending`.
State load fails loud (never silently resets to `{}` and truncates progress); saves are atomic.

<details>
<summary><b>⚠️ Trade-off: citation display</b></summary>

Because `file_path` doubles as both LightRAG's citation-display value and its dedup identity
key, a retrieval citation now reads e.g. `Client__routes__home__page.tsx::chunk0` instead of
a clean `page.tsx`. LightRAG's insert API has no separate display-only field — correctness
(not silently losing most of a project's content to basename collisions) wins that trade.

</details>

**Per-project isolation:** each `project_id` gets its own Qdrant collections, AGE
graph, and PostgreSQL `workspace` (set via the per-instance `LightRAG(workspace=…)`
constructor — **`POSTGRES_WORKSPACE` must never be set**, it overrides the
per-instance value and cross-contaminates projects). Queries carry a grounding
guard (answer from retrieved evidence, say "not found" for unsupported claims) —
and should be phrased neutrally, since RAG can confirm a leading question's
false presupposition.

### Option B — ZGX-side direct indexer

Use when ZGX has direct filesystem access to the repo:

```bash
python main.py --index --path /path/to/repo --project-id myproject
python main.py --index --path /path/to/repo --project-id myproject --force
```

State file: `~/.agno-hive/index-state/{project_id}.json`

---

**Next:** [🖥️ CLI Client Guide](cli.md) · [🔌 API Usage](api.md)
