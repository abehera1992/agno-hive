"""Project indexing tool — walks the project, chunks files, inserts into LightRAG.

Connects to the LightRAG MCP server via Streamable HTTP and calls
lightrag_insert for each chunk. Tracks per-file state for incremental
re-indexing so unchanged files are skipped.

State entry format: "<mtime_ns>:<size>|<sha256>". The mtime:size part is a
fast pre-check (no file read); when it mismatches the content SHA-256 decides.
This makes the state immune to metadata-only churn (git checkout/reset, touch)
that rewrites files without changing their content. Legacy entries without the
"|<sha256>" suffix are upgraded in place on the next pass without re-indexing.
"""
import ast
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import PROJECT_ROOT

_STATE_DIR  = Path(os.getenv("HIVE_INDEX_STATE_DIR", str(PROJECT_ROOT / ".hive-index-state")))
_CHUNK_SIZE = 4000   # chars for non-Python files
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".next", "dist", "build",
    ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", "coverage",
    ".tox", ".eggs", "signoz", "graphify-out", "infra",
    # backups holds DB dumps (incl. Phase secrets DB) — must never be indexed
    "backups", ".hive-index-state",
}
_SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".bin", ".exe", ".dll", ".so", ".dylib", ".zip",
    ".tar", ".gz", ".pdf", ".lock", ".pem", ".key", ".crt", ".cer", ".p12", ".pfx",
    ".tsbuildinfo",
}
# Secret-bearing filenames — extension matching can't catch these (".env" has
# no pathlib suffix). Think in filename PATTERNS, not exact names: phase.env,
# prod.env, .xcode.env all carry env values. .env.example documents shape only.
_SKIP_FILENAMES = {".npmrc", ".pypirc"}

# Dependency lock files — huge, near-zero semantic value, pure noise in the
# entity graph (a single package-lock.json is ~176 chunks). Their extension is
# .json/.yaml/.toml so _SKIP_EXTS can't catch them; match by exact filename.
_SKIP_LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "pdm.lock", "cargo.lock", "composer.lock", "gemfile.lock",
    "podfile.lock", "packages.lock.json",
}


def _is_skippable_file(filename: str) -> bool:
    """True if the file should never be indexed (secret OR noise lock file)."""
    return filename.lower() in _SKIP_LOCKFILES or _is_secret_file(filename)


def _is_secret_file(filename: str) -> bool:
    lower = filename.lower()
    if lower in _SKIP_FILENAMES:
        return True
    if lower.endswith(".example"):
        return False  # .env.example etc. — shape, not values
    # Any *.env or .env.* file: ".env", "phase.env", ".xcode.env", ".env.local"
    if lower == ".env" or lower.endswith(".env") or lower.startswith(".env."):
        return True
    # SQL DUMPS only (backup.sql, phase-db-backup.sql, *_dump.sql) — schema,
    # migration, and init SQL are useful code and stay indexed.
    if lower.endswith(".sql") and ("backup" in lower or "dump" in lower):
        return True
    return False


# ── File chunking ─────────────────────────────────────────────────────────────

def _py_chunks(path: Path) -> list[str]:
    """Extract module docstring + each function/class as a separate chunk."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree   = ast.parse(source)
    except Exception:
        return _text_chunks(path)

    chunks = []
    rel = path.relative_to(PROJECT_ROOT).as_posix()

    # Module-level docstring
    if (ast.get_docstring(tree) or "").strip():
        chunks.append(f"File: {rel}\nType: module\n\n{ast.get_docstring(tree)}")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        try:
            segment = ast.get_source_segment(source, node) or ""
        except Exception:
            continue
        if len(segment) < 30:
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        doc  = ast.get_docstring(node) or ""
        header = f"File: {rel}\nType: {kind}\nName: {node.name}"
        if doc:
            header += f"\nDocstring: {doc}"
        chunks.append(f"{header}\n\n{segment[:_CHUNK_SIZE]}")

    return chunks if chunks else _text_chunks(path)


def _text_chunks(path: Path) -> list[str]:
    """Split any text file into fixed-size chunks with a file/type header."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    rel  = path.relative_to(PROJECT_ROOT).as_posix()
    ext  = path.suffix.lstrip(".") or "text"
    header = f"File: {rel}\nType: {ext}\n\n"
    chunks = []
    for i in range(0, len(text), _CHUNK_SIZE):
        chunk = text[i : i + _CHUNK_SIZE]
        if chunk.strip():
            chunks.append(header + chunk)
    return chunks


def _file_key(path: Path) -> str:
    """Fast change-detection key using mtime + size — no file read required."""
    s = path.stat()
    return f"{s.st_mtime_ns}:{s.st_size}"


def _sha256(path: Path) -> str:
    """Content hash — used when the fast key mismatches (e.g. after git reset)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── State tracking ────────────────────────────────────────────────────────────

def _load_state(project_id: str) -> dict:
    f = _STATE_DIR / f"{project_id}.json"
    if not f.exists():
        return {}  # legitimately empty — first run for this project
    # An EXISTING state file that fails to read/parse must NOT silently reset
    # to {}. Doing so makes the pass start from empty and _save_state then
    # truncates all prior progress (observed 2026-06-12: a transient read
    # during concurrent container ops left 346/632 files tracked while
    # LightRAG kept all 632). Retry briefly, then fail loud so the caller
    # aborts/retries instead of corrupting state.
    last_exc = None
    for _ in range(3):
        try:
            return json.loads(f.read_text())
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.2)
    raise RuntimeError(
        f"state file {f} exists but could not be read after retries "
        f"(refusing to reset and truncate): {last_exc}"
    )


def _save_state(project_id: str, state: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: serialize to a temp file then replace, so a reader never
    # observes a half-written (corrupt) state file mid-save.
    target = _STATE_DIR / f"{project_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, target)


# ── Main tool ─────────────────────────────────────────────────────────────────

async def index_project(
    project_id: str,
    lightrag_url: str,
    glob_filter: str = "**/*",
    force: bool = False,
    time_budget_seconds: int = 240,
) -> str:
    """
    Index the project into LightRAG for semantic search and knowledge graph queries.

    Walks the project directory, chunks files (Python files are parsed with AST
    for function/class-level granularity; all other files use text windows).
    Skips unchanged files unless force=True: fast mtime+size check first, then
    content SHA-256 when metadata changed — so git checkout/reset alone never
    triggers a re-index.
    Connects to the LightRAG MCP server at lightrag_url via Streamable HTTP
    using a single shared session (not one connection per chunk).

    State is saved after every file so interrupted runs resume from where they
    left off. When time_budget_seconds is reached the tool returns a "Partial"
    result — call again (without force) to continue from saved state.

    Args:
        project_id:          Namespace for this project in LightRAG.
        lightrag_url:        LightRAG MCP server URL.
        glob_filter:         Glob to restrict which files are indexed.
        force:               Re-index all files even if unchanged.
        time_budget_seconds: Stop after this many seconds and return partial
                             result (default 240). Caller loops until "Done".
    """
    import time
    t_start = time.monotonic()

    state     = {} if force else _load_state(project_id)
    new_state = dict(state)

    files_seen = files_skipped = chunks_sent = errors = files_indexed = 0

    # Match files against the full glob pattern (supports directory prefixes like
    # "Client/**/*.ts"). PurePosixPath.match() handles ** correctly so
    # "Client/**/*.ts" only matches files under Client/, not signoz/.
    # _file_pat is a fast filename-only pre-filter to skip obvious non-matches cheaply.
    import fnmatch as _fnmatch
    from pathlib import PurePosixPath
    _file_pat = glob_filter.split("/")[-1] if "/" in glob_filter else glob_filter
    _dir_scoped = bool(glob_filter.split("/")[0].strip("*"))  # True when glob starts with a real dir name

    # Collect candidates — os.walk with in-place dir pruning avoids descending into
    # node_modules / .next / __pycache__ etc., which Path.glob visits before filtering.
    to_process: list[tuple[Path, str, str]] = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT, topdown=True):
        # Prune ignored + hidden dirs so os.walk never descends into them
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _IGNORE_DIRS and not d.startswith(".")
        )
        for filename in filenames:
            if not _fnmatch.fnmatch(filename, _file_pat):
                continue
            if _is_skippable_file(filename):
                continue
            p = Path(dirpath) / filename
            if p.suffix.lower() in _SKIP_EXTS:
                continue
            try:
                rel = p.relative_to(PROJECT_ROOT).as_posix()
            except ValueError:
                continue
            # Full glob match — enforces directory prefix (e.g. "Client/**/*.ts"
            # must not match "signoz/foo.ts"). PurePosixPath.match() supports **.
            if _dir_scoped and not PurePosixPath(rel).match(glob_filter):
                continue
            files_seen += 1
            try:
                key = _file_key(p)
            except Exception:
                continue
            stored = state.get(rel) or ""
            if not force and stored:
                s_fast, _, s_sha = stored.partition("|")
                if s_fast == key:
                    # Fast path: mtime+size unchanged. Lazily upgrade legacy
                    # entries (no sha) to the dual format — one read, no re-index.
                    files_skipped += 1
                    if not s_sha:
                        try:
                            new_state[rel] = f"{key}|{_sha256(p)}"
                        except Exception:
                            pass
                    continue
                if s_sha:
                    # mtime/size changed — check whether content actually did.
                    try:
                        cur_sha = _sha256(p)
                    except Exception:
                        continue
                    if cur_sha == s_sha:
                        # Metadata-only churn (git reset, touch): re-key, skip.
                        files_skipped += 1
                        new_state[rel] = f"{key}|{s_sha}"
                        continue
            # was_indexed: file had a previous state entry -> stale docs may
            # exist in LightRAG and must be deleted before re-inserting.
            # force=True implies was_indexed for every file: without this,
            # a force run neither deletes old docs nor re-extracts (identical
            # chunks bounce off content dedupe) — i.e. it silently does nothing.
            to_process.append((p, rel, key, force or bool(stored)))
    to_process.sort(key=lambda x: x[1])  # stable alphabetical order

    budget_exceeded = False

    if to_process:
        try:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
            # Ensure trailing slash — httpx (used by streamablehttp_client) does not
            # follow POST 307 redirects, so /mcp must already be the canonical path.
            _lr_url = lightrag_url.rstrip("/") + "/"
            # Open ONE session for all inserts — avoids per-chunk connection overhead
            async with streamablehttp_client(_lr_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Concurrent chunk inserts: LightRAG's pipeline parallelism
                    # (max_parallel_insert / llm_model_max_async) is wasted when
                    # chunks arrive strictly serially over the MCP session.
                    insert_sem = asyncio.Semaphore(4)

                    async def _send_chunk(chunk: str, rel: str) -> bool:
                        async with insert_sem:
                            try:
                                result = await session.call_tool(
                                    "lightrag_insert",
                                    {"text": chunk, "project_id": project_id, "file_path": rel},
                                )
                                return not (result.isError if hasattr(result, "isError") else False)
                            except Exception:
                                return False

                    async def _delete_stale(rel: str) -> None:
                        """LightRAG indexing is append-only — remove the file's
                        previous docs so stale versions can't win retrieval.
                        Best-effort: older lightrag servers lack the tool."""
                        try:
                            await session.call_tool(
                                "lightrag_delete_by_file",
                                {"file_path": rel, "project_id": project_id},
                            )
                        except Exception:
                            pass

                    last_chunk: tuple[str, str] | None = None
                    for p, rel, key, was_indexed in to_process:
                        if time.monotonic() - t_start >= time_budget_seconds:
                            budget_exceeded = True
                            break
                        if was_indexed:
                            await _delete_stale(rel)
                        chunks = _py_chunks(p) if p.suffix == ".py" else _text_chunks(p)
                        if chunks:
                            last_chunk = (chunks[-1], rel)
                        flags = await asyncio.gather(*[_send_chunk(c, rel) for c in chunks])
                        chunks_sent += sum(flags)
                        errors += len(flags) - sum(flags)
                        ok = all(flags)
                        if ok:
                            try:
                                new_state[rel] = f"{key}|{_sha256(p)}"
                            except Exception:
                                new_state[rel] = key
                        files_indexed += 1
                        _save_state(project_id, new_state)  # persist after every file

                    # Pipeline kick: LightRAG can leave the final batch of docs
                    # 'pending' indefinitely when they enqueue while a processing
                    # cycle is already running — pending docs only drain on the
                    # next ainsert. Re-send the last chunk: guaranteed
                    # dedupe-rejection (no new doc), but the insert's process
                    # step picks up everything still pending.
                    if last_chunk is not None and chunks_sent:
                        try:
                            await session.call_tool(
                                "lightrag_insert",
                                {"text": last_chunk[0], "project_id": project_id, "file_path": last_chunk[1]},
                            )
                        except Exception:
                            pass
        except Exception as e:
            errors += 1
            _save_state(project_id, new_state)
            return (
                f"Indexing project '{project_id}' → {lightrag_url}\n"
                f"Error connecting to LightRAG: {e}"
            )

    _save_state(project_id, new_state)

    remaining = len(to_process) - files_indexed
    status = (
        f"Partial — {remaining} files remaining — run again to continue."
        if budget_exceeded
        else "Done — project knowledge is now queryable via lightrag_query."
    )

    return "\n".join([
        f"Indexing project '{project_id}' → {lightrag_url}",
        f"Files scanned:   {files_seen}",
        f"Files skipped:   {files_skipped}  (unchanged)",
        f"Files indexed:   {files_indexed}",
        f"Chunks sent:     {chunks_sent}",
        f"Errors:          {errors}",
        status,
    ])
