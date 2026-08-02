"""Project indexing tool — walks the project, chunks files, inserts into LightRAG.

Connects to the LightRAG MCP server via Streamable HTTP and calls
lightrag_insert for each chunk. Tracks per-file state for incremental
re-indexing so unchanged files are skipped.

State entry format: "<mtime_ns>:<size>|<sha256>|<chunk_count>". The mtime:size
part is a fast pre-check (no file read); when it mismatches the content SHA-256
decides. This makes the state immune to metadata-only churn (git checkout/reset,
touch) that rewrites files without changing their content. Legacy entries
missing "|<sha256>" or "|<chunk_count>" are upgraded in place on the next pass
without re-indexing.

chunk_count exists to make per-file deletion possible without querying LightRAG
at all. Every chunk of a file is sent as its own separate LightRAG "document"
under a deterministic id `doc-sha256(project_id:rel_path:chunk_index)` (see
_chunk_doc_id) AND a deterministic, collision-free file_path (see
_chunk_citation_path) — both matter; see below for why.

Confirmed 2026-08-02, two layers deep:

1. LightRAG (1.5.4) auto-derives an unspecified document's id from
   `normalize_document_file_path(file_path)`, which collapses to the BASENAME
   only (`Path(file_path).name`), dropping the directory. This looked like the
   whole bug at first — but passing an explicit `ids=` does NOT fix it.

2. The real gate is `apipeline_enqueue_documents`' step "3a. Filename-based
   dedup" (lightrag/pipeline.py): before insert, it calls
   `get_existing_doc_by_file_basename(doc_status, file_path)` and rejects the
   NEW document as a duplicate if ANY existing doc_status row already has that
   same basename — regardless of what doc_id was supplied. This is a
   deliberate, hard-coded, basename-is-identity policy in LightRAG itself, not
   an id-derivation quirk we can route around with our own ids.

Verified empirically against a live LightRAG instance (disposable test
project, cleaned up after): two distinct files sharing a basename, and
separately two chunks of one file (same file_path for both), both logged
"Duplicate document detected (filename)" and the second arrival got
`status: failed` with zero chunks in storage — EVEN with distinct explicit
`ids=` on each call. Only making file_path ITSELF basename-unique per
(file, chunk_index) — via _chunk_citation_path, which folds the full
relative path into the final path segment so `Path(...).name` can't drop
it — made both inserts succeed. Real blast radius before this fix: EkamApp
has 39 files named page.tsx, 17 __init__.py, 11 main.py — each group beyond
the first ever indexed was silently dropped, and (independently) every
file with more than one function/class/chunk only ever kept its first chunk.

Trade-off: file_path is also what LightRAG shows as citation source, so a
retrieval citation now reads e.g. "Client__routes__home__page.tsx::chunk0"
instead of a clean "page.tsx" — LightRAG's insert API has no separate
display-only field. Correctness (not silently losing most of a project's
content) outweighs citation prettiness.
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
# Shared with search / read / write / scan — one list, configured per project via
# EXCLUDE_DIRS / EXCLUDE_GLOBS. "backups" and ".hive-index-state" are fail-safe defaults
# in that shared set: backups commonly holds DB dumps (which have held secrets), and
# indexing them would leak the contents into a vector store agents later quote from.
#
# Removed from here: "signoz", "graphify-out", "infra" — those named one project's
# layout inside a project-independent server. Configure them per project instead.
# "infra" was actively harmful as a default: it hid any legitimate infra/ directory.
from .exclusions import EXCLUDE_DIRS as _IGNORE_DIRS, is_excluded
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


def _structure_chunks(data: object, rel: str, file_type: str) -> list[str]:
    """Recursively split a parsed JSON/YAML structure into LightRAG-friendly chunks.

    Drills into dicts/lists until each chunk fits within _CHUNK_SIZE. Every chunk
    carries a 'Section' breadcrumb so LightRAG knows where in the file it came from.
    """
    header = f"File: {rel}\nType: {file_type}"
    chunks: list[str] = []
    _EMPTY_VALUES = frozenset(("{}", "[]", '""', "null", "~"))

    def _emit(obj: object, path_hint: str) -> None:
        text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
        body = f"{header}\nSection: {path_hint}\n\n{text}"
        if len(body) <= _CHUNK_SIZE:
            if text.strip() not in _EMPTY_VALUES:
                chunks.append(body)
            return
        # Too large — drill one level deeper before emitting
        if isinstance(obj, dict):
            for k, v in obj.items():
                _emit(v, f"{path_hint}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _emit(item, f"{path_hint}[{i}]")
        else:
            # Large primitive (base64, minified blob): hard-truncate
            chunks.append(body[:_CHUNK_SIZE])

    root = data if isinstance(data, dict) else {"root": data}
    for k, v in root.items():
        _emit(v, k)
    return chunks


def _json_chunks(path: Path) -> list[str]:
    """Parse any JSON file and emit one structural chunk per logical section."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return _text_chunks(path)
    if not isinstance(data, (dict, list)):
        return _text_chunks(path)
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    return _structure_chunks(data, rel, "json") or _text_chunks(path)


def _yaml_chunks(path: Path) -> list[str]:
    """Parse any YAML file and emit one structural chunk per top-level section."""
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except ImportError:
        return _text_chunks(path)
    except Exception:
        return _text_chunks(path)
    if not isinstance(data, (dict, list)):
        return _text_chunks(path)
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    return _structure_chunks(data, rel, "yaml") or _text_chunks(path)


def _get_chunks(path: Path) -> list[str]:
    """Dispatch to the right chunker based on file extension."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _py_chunks(path)
    if suffix == ".json":
        return _json_chunks(path)
    if suffix in (".yaml", ".yml"):
        return _yaml_chunks(path)
    return _text_chunks(path)


def _file_key(path: Path) -> str:
    """Fast change-detection key using mtime + size — no file read required."""
    s = path.stat()
    return f"{s.st_mtime_ns}:{s.st_size}"


def _sha256(path: Path) -> str:
    """Content hash — used when the fast key mismatches (e.g. after git reset)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chunk_doc_id(project_id: str, rel: str, index: int) -> str:
    """Deterministic per-chunk LightRAG document id.

    Passed explicitly as `ids=` on every insert. NOTE: this alone does NOT
    prevent the basename collision described in the module docstring —
    LightRAG's filename-based dedup rejects by file_path regardless of the id
    supplied. It's still worth passing: it keeps deletion (_delete_stale)
    independent of replicating LightRAG's own id-hash algorithm, which could
    change between versions. Stable across runs: the same (file, chunk
    position) always maps to the same id.
    """
    return "doc-" + hashlib.sha256(f"{project_id}:{rel}:{index}".encode()).hexdigest()


def _chunk_citation_path(rel: str, index: int) -> str:
    """Deterministic, basename-unique file_path for one chunk of one file.

    This is the actual fix for the collision described in the module
    docstring — LightRAG's filename-based dedup keys on `Path(file_path).name`
    (the final path segment only), so a bare relative path loses its directory
    and collides with any other file/chunk sharing a basename. Folding the
    full relative path into that final segment (replacing "/" so nothing is
    lost to Path(...).name) makes every (file, chunk_index) pair unique
    project-wide, regardless of what any two files' real basenames are.
    """
    return f"{rel.replace('/', '__')}::chunk{index}"


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
            # os.walk dir-pruning above only applies EXCLUDE_DIRS (directory NAMES).
            # EXCLUDE_GLOBS (e.g. "training/data/*.jsonl") is file-pattern-scoped and
            # can't prune a directory, so it was never enforced here — confirmed
            # 2026-08-02: agno-hive's own training/eval/** data (98 near-identical
            # eval-case JSON files + a 1.3MB corpus) got indexed into LightRAG despite
            # EXCLUDE_GLOBS being set, because this walk only ever consulted
            # _IGNORE_DIRS. is_excluded() is the single source of truth every other
            # tool (context.py, files.py) already calls; the bootstrap walk was the
            # one path that bypassed it.
            if is_excluded(rel):
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
            s_parts = stored.split("|") if stored else []
            if not force and stored:
                s_fast = s_parts[0]
                s_sha = s_parts[1] if len(s_parts) > 1 else ""
                if s_fast == key:
                    # Fast path: mtime+size unchanged. Lazily upgrade legacy
                    # entries (missing sha and/or chunk_count) — one read, no re-index.
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
                        # Preserve the chunk_count field if present.
                        files_skipped += 1
                        suffix = f"|{s_parts[2]}" if len(s_parts) > 2 else ""
                        new_state[rel] = f"{key}|{s_sha}{suffix}"
                        continue
            # Previous chunk count, used to reconstruct and delete this file's old
            # doc ids before re-inserting (see _chunk_doc_id / _delete_stale). A
            # legacy entry with no recorded count (pre-2026-08-02 state, or the
            # metadata-churn path above which doesn't touch it) means we can't
            # know what old ids to delete — those chunks live on under their
            # OLD basename-derived id until this file changes again, at which
            # point the count is known and cleanup resumes normally. One-time,
            # self-limiting gap; not worse than the status quo before this fix.
            prev_chunk_count = (
                int(s_parts[2]) if len(s_parts) > 2 and s_parts[2].isdigit() else 0
            )
            # was_indexed: file had a previous state entry -> stale docs may
            # exist in LightRAG and must be deleted before re-inserting.
            # force=True implies was_indexed for every file: without this,
            # a force run neither deletes old docs nor re-extracts (identical
            # chunks bounce off content dedupe) — i.e. it silently does nothing.
            to_process.append((p, rel, key, force or bool(stored), prev_chunk_count))
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

                    async def _send_chunk(chunk: str, rel: str, index: int) -> bool:
                        async with insert_sem:
                            try:
                                result = await session.call_tool(
                                    "lightrag_insert",
                                    {
                                        "text": chunk,
                                        "project_id": project_id,
                                        # Must be basename-unique per (file, chunk) — see
                                        # module docstring for why a bare rel path collides.
                                        "file_path": _chunk_citation_path(rel, index),
                                        "doc_id": _chunk_doc_id(project_id, rel, index),
                                    },
                                )
                                return not (result.isError if hasattr(result, "isError") else False)
                            except Exception:
                                return False

                    async def _delete_stale(rel: str, prev_count: int) -> None:
                        """LightRAG indexing is append-only — remove the file's
                        previous chunk docs (by their deterministic ids, see
                        _chunk_doc_id) so stale versions can't win retrieval.
                        Best-effort: a single failed delete shouldn't abort the
                        whole file's re-index."""
                        for i in range(prev_count):
                            try:
                                await session.call_tool(
                                    "lightrag_delete_by_id",
                                    {"doc_id": _chunk_doc_id(project_id, rel, i), "project_id": project_id},
                                )
                            except Exception:
                                pass

                    last_chunk: tuple[str, str, int] | None = None
                    for p, rel, key, was_indexed, prev_chunk_count in to_process:
                        if time.monotonic() - t_start >= time_budget_seconds:
                            budget_exceeded = True
                            break
                        if was_indexed:
                            await _delete_stale(rel, prev_chunk_count)
                        chunks = _get_chunks(p)
                        if chunks:
                            last_chunk = (chunks[-1], rel, len(chunks) - 1)
                        flags = await asyncio.gather(
                            *[_send_chunk(c, rel, i) for i, c in enumerate(chunks)]
                        )
                        chunks_sent += sum(flags)
                        errors += len(flags) - sum(flags)
                        ok = all(flags)
                        if ok:
                            try:
                                new_state[rel] = f"{key}|{_sha256(p)}|{len(chunks)}"
                            except Exception:
                                new_state[rel] = key
                        files_indexed += 1
                        _save_state(project_id, new_state)  # persist after every file

                    # Pipeline kick: LightRAG can leave the final batch of docs
                    # 'pending' indefinitely when they enqueue while a processing
                    # cycle is already running — pending docs only drain on the
                    # next ainsert. Re-send the last chunk under its own file_path
                    # + doc_id: guaranteed dedupe-rejection against the
                    # just-inserted real entry (no new doc), but the insert's
                    # process step picks up everything still pending. Both must
                    # match exactly what the real insert used, or this creates a
                    # fresh, distinct phantom entry instead of colliding with it.
                    if last_chunk is not None and chunks_sent:
                        try:
                            kick_text, kick_rel, kick_index = last_chunk
                            await session.call_tool(
                                "lightrag_insert",
                                {
                                    "text": kick_text,
                                    "project_id": project_id,
                                    "file_path": _chunk_citation_path(kick_rel, kick_index),
                                    "doc_id": _chunk_doc_id(project_id, kick_rel, kick_index),
                                },
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
