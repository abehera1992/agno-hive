"""Index EkamApp codebase into LightRAG via MCP (no direct filesystem access needed).

Usage:
    python3.12 index_via_mcp.py               # incremental — only changed files
    python3.12 index_via_mcp.py --force       # re-index everything
    python3.12 index_via_mcp.py --progress    # show live progress bar (TTY)
    python3.12 index_via_mcp.py --force --progress

Large files are automatically pre-split at logical boundaries (class/def for Python,
export/function for TS) before insertion so the LLM worker never times out on
oversized chunks.  Each part gets a "File: path (part N/M)" header so LightRAG
knows the parts are siblings.  The file hash is computed on the FULL content, so
changing one line re-indexes all parts together.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL    = "http://100.87.159.86:9000/mcp"
PROJECT_ID = "ekam"
STATE_FILE = Path.home() / ".agno-hive" / "index-state" / f"{PROJECT_ID}.json"

GLOBS = [
    "API/**/*.py",
    "Client/EcommClient-Web/ekamweb/src/**/*.ts",
    "Client/EcommClient-Web/ekamweb/src/**/*.tsx",
    "Client/EcommClient-Web/ekamweb/src/**/*.scss",
    "patterns/**/*.md",
    "mcp-server/**/*.py",
    "CLAUDE.md",
    "DOCS.md",
]

SKIP_CONTAINS = ["node_modules", "__pycache__", ".next", "dist/", ".venv"]
SKIP_EXT = {".lock", ".log", ".ico", ".png", ".jpg", ".svg", ".pyc"}


# ── helpers ──────────────────────────────────────────────────────────────────

def should_skip(path: str) -> bool:
    p = path.lower()
    return any(s in p for s in SKIP_CONTAINS) or any(p.endswith(e) for e in SKIP_EXT)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct)
    return "█" * filled + "░" * (width - filled)


# ── file splitter ─────────────────────────────────────────────────────────────

# Files larger than this get pre-split before ainsert() to avoid LLM timeouts.
# LightRAG's internal chunk size is ~4 800 chars; 8 000 → at most 2 chunks/insert.
MAX_INSERT_CHARS = 8_000

# Regex patterns marking "start of a new top-level definition" per language.
_BOUNDARY: dict[str, re.Pattern] = {
    ".py":  re.compile(r"^(class |def |async def )", re.MULTILINE),
    ".ts":  re.compile(r"^(export |class |function )", re.MULTILINE),
    ".tsx": re.compile(r"^(export |class |function )", re.MULTILINE),
    ".scss": re.compile(r"^(\.|#|@mixin |@include |\/\/)", re.MULTILINE),
}


def split_for_insert(path: str, content: str) -> list[str]:
    """Return a list of ainsert-ready strings for *path*/*content*.

    Files within MAX_INSERT_CHARS → single element (no overhead).
    Larger files → split at language-aware boundaries; each part tagged
    "File: path (part N/M)" so LightRAG links them as siblings.
    """
    if len(content) <= MAX_INSERT_CHARS:
        return [f"File: {path}\n\n{content}"]

    ext = Path(path).suffix.lower()
    pattern = _BOUNDARY.get(ext)

    # Collect split-point positions: every position where a top-level def begins.
    # Fallback (no pattern / .md etc.): split at blank lines.
    if pattern:
        split_points = [m.start() for m in pattern.finditer(content)]
    else:
        split_points = [m.start() for m in re.finditer(r"\n\n+", content)]

    # Always start from 0; remove duplicates and sort.
    split_points = sorted({0} | set(split_points))

    # Greedily merge consecutive segments until we would exceed MAX_INSERT_CHARS,
    # then flush.  If a single segment is already larger, hard-cut at MAX_INSERT_CHARS.
    parts: list[str] = []
    buf_start = 0
    buf_end = 0

    for sp in split_points[1:] + [len(content)]:
        segment_len = sp - buf_start
        incremental = sp - buf_end  # chars we'd add to the current buffer

        if buf_end > buf_start and (buf_end - buf_start) + incremental > MAX_INSERT_CHARS:
            # Flush current buffer before this segment
            chunk = content[buf_start:buf_end].strip()
            if chunk:
                parts.append(chunk)
            buf_start = buf_end

        buf_end = sp

    # Flush remainder
    tail = content[buf_start:].strip()
    if tail:
        parts.append(tail)

    # Hard-cut any part that is still over the limit (no boundaries found inside it)
    final: list[str] = []
    for part in parts:
        if len(part) <= MAX_INSERT_CHARS:
            final.append(part)
        else:
            # Brute-force split at newlines
            pos = 0
            while pos < len(part):
                end = pos + MAX_INSERT_CHARS
                if end < len(part):
                    nl = part.rfind("\n", pos, end)
                    end = nl if nl > pos else end
                final.append(part[pos:end].strip())
                pos = end

    final = [p for p in final if len(p.strip()) >= 30]
    n = len(final)
    return [f"File: {path} (part {i + 1}/{n})\n\n{part}" for i, part in enumerate(final)]


# ── progress display ──────────────────────────────────────────────────────────

class Progress:
    """4-line live display when interactive; plain log lines when backgrounded."""

    LINES = 4  # number of lines the live display occupies

    def __init__(self, total: int, label: str, interactive: bool):
        self.total = total
        self.label = label
        self.interactive = interactive
        self.indexed = self.skipped = self.errors = 0
        self.inserts = 0   # total ainsert() calls (>= indexed when files are split)
        self.current = ""
        self._start = time.time()
        self._file_times: list[float] = []
        self._file_t0 = 0.0
        self._rendered_once = False

    def start_file(self, path: str) -> None:
        self.current = path
        self._file_t0 = time.time()

    def finish(self, action: str, parts: int = 1) -> None:
        dt = time.time() - self._file_t0
        if action == "indexed":
            self.indexed += 1
            self.inserts += parts
            self._file_times.append(dt)
        elif action == "skipped":
            self.skipped += 1
        else:
            self.errors += 1
        self._render(action)

    def _render(self, action: str) -> None:
        done = self.indexed + self.skipped + self.errors
        pct = done / self.total if self.total else 0
        elapsed = time.time() - self._start
        avg = sum(self._file_times) / len(self._file_times) if self._file_times else 0
        eta = avg * (self.total - done) if avg else 0

        if self.interactive:
            bar = _bar(pct)
            insert_tag = f"  ({self.inserts} inserts)" if self.inserts > self.indexed else ""
            lines = [
                f"[{self.label}]  {bar}  {pct:5.1%}  {done}/{self.total} files",
                f"  current : {self.current[-65:]}",
                f"  indexed : {self.indexed}{insert_tag}   skipped: {self.skipped}   errors: {self.errors}",
                f"  elapsed : {_fmt_dur(elapsed)}   eta: ~{_fmt_dur(eta)}",
            ]
            if self._rendered_once:
                sys.stdout.write(f"\033[{self.LINES}A")
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            self._rendered_once = True
        else:
            sym = {"indexed": "+", "skipped": "~", "error": "!"}.get(action, "?")
            print(
                f"[{done:>4}/{self.total}  {pct:5.1%}] {sym} {self.current[-70:]}",
                flush=True,
            )

    def summary(self) -> None:
        elapsed = time.time() - self._start
        print(f"\n{'='*60}", flush=True)
        insert_note = f"  inserts:{self.inserts}" if self.inserts > self.indexed else ""
        print(
            f"Done — indexed:{self.indexed}{insert_note}  skipped:{self.skipped}  "
            f"errors:{self.errors}  elapsed:{_fmt_dur(elapsed)}  project:{PROJECT_ID}",
            flush=True,
        )


# ── MCP helpers ───────────────────────────────────────────────────────────────

async def mcp_find(session: ClientSession, glob: str) -> list[str]:
    r = await session.call_tool("find_files", {"glob_pattern": glob, "max_results": 200})
    raw = r.content[0].text if r.content else ""
    return [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.startswith("[") and not line.startswith("Error")
    ]


async def mcp_read(session: ClientSession, path: str) -> str:
    r = await session.call_tool("get_file_content", {"relative_path": path})
    return r.content[0].text if r.content else ""


# ── main ──────────────────────────────────────────────────────────────────────

async def index(force: bool, show_progress: bool) -> None:
    from lightrag_mcp.rag import get_rag

    state = {} if force else load_state()
    new_state: dict[str, str] = {}

    interactive = show_progress and sys.stdout.isatty()

    print(f"[init] connecting to {MCP_URL}", flush=True)
    async with streamablehttp_client(MCP_URL) as (rd, wr, _):
        async with ClientSession(rd, wr) as session:
            await session.initialize()

            # Phase 1: scan all globs to get the full file list before indexing
            print("[scan] collecting file list …", flush=True)
            all_paths: list[str] = []
            for glob in GLOBS:
                paths = await mcp_find(session, glob)
                if not paths and "*" not in glob:
                    paths = [glob]
                eligible = [p for p in paths if not should_skip(p)]
                all_paths.extend(eligible)
                print(f"  {glob:<55} → {len(eligible)} files", flush=True)

            total = len(all_paths)
            mode = "force reindex" if force else "incremental"
            print(f"[scan] {total} files  ({mode})", flush=True)

            # Phase 2: set up LightRAG
            rag = get_rag(PROJECT_ID)
            await rag.initialize_storages()
            print("[init] LightRAG storages ready\n", flush=True)

            prog = Progress(total=total, label=f"indexing {PROJECT_ID}", interactive=interactive)

            # Phase 3: read + insert
            for path in all_paths:
                prog.start_file(path)
                try:
                    content = await mcp_read(session, path)
                    if not content or len(content.strip()) < 30:
                        prog.finish("skipped")
                        continue

                    h = sha256(content)
                    new_state[path] = h

                    if not force and state.get(path) == h:
                        prog.finish("skipped")
                        continue

                    parts = split_for_insert(path, content)
                    for part in parts:
                        await rag.ainsert(part)
                    prog.finish("indexed", parts=len(parts))

                except Exception as e:
                    print(f"\n  [ERR] {path}: {e}", flush=True)
                    prog.finish("error")

    # Persist hashes — merge with prior state so unchanged files keep their hash
    merged = {**state, **new_state}
    save_state(merged)
    print(f"State saved → {STATE_FILE}", flush=True)

    prog.summary()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index EkamApp into LightRAG via MCP")
    parser.add_argument("--force", action="store_true", help="Re-index all files (ignore hash cache)")
    parser.add_argument("--progress", action="store_true", help="Show live progress bar (auto-detects TTY)")
    args = parser.parse_args()
    asyncio.run(index(force=args.force, show_progress=args.progress))
