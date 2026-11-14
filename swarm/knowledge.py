"""Shared, application-controlled Knowledge Preparation (Phase 1.20).

One service, called by BOTH engines at the top of a new top-level execution, before any agent starts:

    dossier = await prepare_run_knowledge(task, project_id=..., lightrag_url=..., hive_url=..., run_key=...)

It queries LightRAG through the existing integration (`swarm.team._repo_lightrag_query`, retrieval-only
`lightrag_query_context`) -- no model decides whether this happens, and no second LightRAG client exists. The raw
response is parsed STRUCTURALLY (entities, relationships, chunks with their exact `File:` / `Lines:` headers), deduplicated
across retrieval modes, ranked for relevance to the task, and condensed into a compact Knowledge Dossier. Agents are given a
budgeted brief rendered from the dossier; raw retrieval JSON is never pasted into a prompt and nothing is cut at an
arbitrary character boundary (items are dropped whole, lowest rank first).

The dossier is a LEAD, never evidence. Acceptance does not depend on it: a file it names is a candidate, a file an agent
finds and reads on its own is equally valid evidence, and an empty dossier never fails a task. When retrieval is not fully
successful the APPLICATION appends an explicit instruction to investigate independently with the agent's other tools.

Statuses: SUCCESS, PARTIAL, EMPTY, UNAVAILABLE, TIMEOUT, ERROR. This function never raises.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

SUCCESS, PARTIAL, EMPTY, UNAVAILABLE, TIMEOUT, ERROR = "SUCCESS", "PARTIAL", "EMPTY", "UNAVAILABLE", "TIMEOUT", "ERROR"
MODES = ("hybrid", "local", "naive")
SOURCE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx")
DOC_EXT = (".md", ".rst", ".txt")
EXCLUDE_FRAGMENTS = ("/migrations/", "/node_modules/", "/tests/", "/__tests__/", "/__pycache__/", ".test.", ".spec.", "/alembic/", "__init__.py")

BRIEF_CHARS = int(os.getenv("HIVE_KNOWLEDGE_BRIEF_CHARS", "3500"))          # budget of the brief given to each Researcher execution
COORD_CHARS = int(os.getenv("HIVE_KNOWLEDGE_COORD_CHARS", "1200"))          # budget of the Coordinator's block
BUDGET_S = float(os.getenv("HIVE_KNOWLEDGE_BUDGET_S", "45"))                 # whole preparation, retrieval + verification
MAX_SOURCE_FILES = int(os.getenv("HIVE_KNOWLEDGE_MAX_FILES", "10"))
MAX_DOC_FILES, MAX_ENTITIES, MAX_RELS = 3, 8, 8

_STOP = frozenset("""about above after again all also and any are because been before being between both but can could did does doing
down during each few for from further had has have having her here hers him his how into its itself just more most nor not now off once
only other our out over own same she should some such than that the their them then there these they this those through too under until
very was were what when where which while who whom why will with would you your which each every named name end file files function
functions service services involved module modules list audit endpoints tables hooks""".split())


def emit(event: str, **kw) -> None:
    """One structured log line per event (existing '[component] KEY=value' convention). Never logs raw retrieval content."""
    print("[knowledge] " + event + " " + " ".join(f"{k}={v}" for k, v in kw.items() if v is not None), flush=True)


@dataclass
class CandidateFile:
    path: str
    kind: str                       # "source" | "doc"
    score: float = 0.0
    modes: list = field(default_factory=list)
    chunks: int = 0
    lines: str = ""                 # line range of the best chunk, as retrieved
    role: str = ""                  # verbatim first meaningful line of the best chunk (not generated)
    verified: Optional[bool] = None   # True exists / False missing (dropped) / None could not be checked


@dataclass
class KnowledgeDossier:
    run_key: str
    task_sha: str
    project_id: str
    status: str
    reason: str = ""
    retrieved_at: float = 0.0
    queries: list = field(default_factory=list)
    modes: dict = field(default_factory=dict)             # mode -> {outcome, seconds, chars}
    files: list = field(default_factory=list)             # CandidateFile as dicts, source first, ranked
    entities: list = field(default_factory=list)          # {"name","type","summary"}
    relationships: list = field(default_factory=list)     # {"a","b","summary"}
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    fallback_required: bool = False

    def source_files(self) -> list:
        return [f for f in self.files if f["kind"] == "source"]


# ------------------------------------------------------------------------------------------ parsing
_HDR = {"entities": re.compile(r"^Knowledge Graph Data \(Entity\)", re.M), "relationships": re.compile(r"^Knowledge Graph Data \(Relationship\)", re.M),
        "chunks": re.compile(r"^Document Chunks", re.M), "refs": re.compile(r"^Reference Document List", re.M)}
_NOTE_RE = re.compile(r"── Note: (\d+) namespace\(s\) failed[^:]*?[-—]+\s*treat this result as incomplete:\s*(.*?)\s*──", re.S)
_CHUNK_HEAD = re.compile(r"^File:\s*(?P<file>.+?)\s*\nType:\s*(?P<type>.*?)\s*\nLines:\s*(?P<lines>[\d\-–]+)\s*\n", re.S)
_SEP = "<SEP>"


def _json_lines(block: str) -> tuple:
    out, bad = [], 0
    for ln in block.splitlines():
        s = ln.strip()
        if s.startswith("{"):
            # any line that opens a JSON object must be a complete, valid one; otherwise it is malformed (never silently ignored)
            try:
                v = json.loads(s)
                out.append(v) if isinstance(v, dict) else None
                bad += 0 if isinstance(v, dict) else 1
            except Exception:  # noqa: BLE001
                bad += 1
    return out, bad


def parse_context(text: str) -> dict:
    """Structural parse of one lightrag_query_context response. Pure."""
    text = text or ""
    pos = {}
    for k, rx in _HDR.items():
        m = rx.search(text)
        if m:
            pos[k] = m.start()
    order = sorted(pos.items(), key=lambda kv: kv[1])
    sect = {}
    for i, (k, start) in enumerate(order):
        end = order[i + 1][1] if i + 1 < len(order) else len(text)
        sect[k] = text[start:end]
    ents, b1 = _json_lines(sect.get("entities", ""))
    rels, b2 = _json_lines(sect.get("relationships", ""))
    chs, b3 = _json_lines(sect.get("chunks", ""))
    chunks = []
    for c in chs:
        content = str(c.get("content", ""))
        m = _CHUNK_HEAD.match(content)
        if m:
            chunks.append({"file": m.group("file").strip(), "type": m.group("type").strip(), "lines": m.group("lines"),
                           "body": content[m.end():].lstrip("\n"), "ref": c.get("reference_id")})
    failed = []
    for m in _NOTE_RE.finditer(text):
        failed.append(m.group(2).strip())
    return {"entities": [e for e in ents if isinstance(e, dict) and e.get("entity")], "relationships": [r for r in rels if isinstance(r, dict) and r.get("entity1")],
            "chunks": chunks, "malformed": b1 + b2 + b3, "failed_namespaces": failed, "sections": sorted(sect), "chars": len(text)}


# ------------------------------------------------------------------------------------------ ranking helpers
def task_terms(task: str) -> list:
    seen, out = set(), []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", task or ""):
        lw = w.lower()
        if lw not in _STOP and lw not in seen:
            seen.add(lw)
            out.append(lw)
    return out


def _overlap(terms: list, text: str) -> float:
    if not terms:
        return 0.0
    low = (text or "").lower()
    return sum(1 for t in terms if t in low) / len(terms)


def _first_sentence(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "").split(_SEP)[0]).strip()
    cut = re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0]
    return (cut if len(cut) <= n else cut[: n - 1].rstrip() + "…")


def _kind(path: str) -> Optional[str]:
    low = "/" + path.lower()
    if any(f in low for f in EXCLUDE_FRAGMENTS):
        return None
    if low.endswith(SOURCE_EXT):
        return "source"
    if low.endswith(DOC_EXT):
        return "doc"
    return None


def _role_line(body: str, n: int = 110) -> str:
    """The first line of the best chunk that reads as a description of the file's responsibility (a docstring or a prose
    sentence), verbatim. Code-looking lines are skipped; if nothing qualifies the role is left empty rather than invented."""
    for ln in (body or "").splitlines():
        s = ln.strip()
        if s.lower().startswith("docstring:"):
            s = s[len("docstring:"):].strip()
        else:
            s = s.lstrip("#*-> `").strip()
        if len(s) < 20 or len(s.split()) < 4:
            continue
        letters = sum(ch.isalpha() or ch == " " for ch in s) / len(s)
        if letters < 0.85 or any(tok in s for tok in ("{", "}", ";", "=>", "()", "=", "<", ">")) or s.endswith((",", ")", "(")):
            continue
        return s if len(s) <= n else s[: n - 1].rstrip() + "…"
    return ""


def build_dossier(task: str, parsed_by_mode: dict, *, run_key: str, project_id: str, modes_meta: dict, status: str, reason: str,
                  warnings: list, brief_chars: int = BRIEF_CHARS) -> KnowledgeDossier:
    """Pure: merge the per-mode parses into one deduplicated, ranked dossier."""
    terms = task_terms(task)
    raw = {"entities": 0, "relationships": 0, "chunks": 0}
    ent: dict = {}
    rel: dict = {}
    files: dict = {}
    for mode, p in parsed_by_mode.items():
        raw["entities"] += len(p["entities"]); raw["relationships"] += len(p["relationships"]); raw["chunks"] += len(p["chunks"])
        for e in p["entities"]:
            k = str(e["entity"]).strip().lower()
            ent.setdefault(k, {"name": e["entity"], "type": e.get("type", ""), "desc": str(e.get("description", "")), "modes": set()})["modes"].add(mode)
        for r in p["relationships"]:
            k = (str(r["entity1"]).lower(), str(r.get("entity2", "")).lower())
            rel.setdefault(k, {"a": r["entity1"], "b": r.get("entity2", ""), "desc": str(r.get("description", "")), "modes": set()})["modes"].add(mode)
        for c in p["chunks"]:
            kind = _kind(c["file"])
            if kind is None:
                continue
            f = files.setdefault(c["file"], {"path": c["file"], "kind": kind, "modes": set(), "chunk_keys": set(), "best": None, "text": ""})
            f["modes"].add(mode)
            f["chunk_keys"].add((c["lines"], hashlib.md5(c["body"][:200].encode("utf-8", "replace")).hexdigest()[:8]))
            f["text"] += " " + c["body"][:400]
            ov = _overlap(terms, c["body"])
            if f["best"] is None or ov > f["best"][0]:
                f["best"] = (ov, c["lines"], c["body"])
    degree: dict = {}
    for r in rel.values():
        for n in (r["a"], r["b"]):
            degree[str(n).lower()] = degree.get(str(n).lower(), 0) + 1
    cands = []
    for f in files.values():
        ov_path = _overlap(terms, f["path"]); ov_text = _overlap(terms, f["text"])
        score = 2.0 * len(f["modes"]) + 0.5 * min(len(f["chunk_keys"]), 4) + 3.0 * max(ov_path, ov_text) + (0.0 if f["kind"] == "source" else -1.5)
        b = f["best"] or (0, "", "")
        cands.append(CandidateFile(path=f["path"], kind=f["kind"], score=round(score, 3), modes=sorted(f["modes"]), chunks=len(f["chunk_keys"]),
                                   lines=b[1], role=_role_line(b[2])))
    cands.sort(key=lambda c: (-c.score, c.path))
    src = [c for c in cands if c.kind == "source"][:MAX_SOURCE_FILES + 4]       # a few spare: verification may drop some
    docs = [c for c in cands if c.kind == "doc"][:MAX_DOC_FILES]
    ents = sorted(ent.values(), key=lambda e: (-(2.0 * _overlap(terms, e["name"] + " " + e["desc"]) + degree.get(e["name"].lower(), 0) * 0.25 + 0.3 * len(e["modes"])), e["name"]))[:MAX_ENTITIES]
    keep = {e["name"].lower() for e in ents}
    rels = sorted(rel.values(), key=lambda r: (-((2.0 if (r["a"].lower() in keep and r["b"].lower() in keep) else 0.0) + 2.0 * _overlap(terms, r["a"] + " " + r["b"] + " " + r["desc"]) + 0.3 * len(r["modes"])), r["a"], r["b"]))[:MAX_RELS]
    d = KnowledgeDossier(run_key=run_key, task_sha=hashlib.sha256((task or "").encode("utf-8", "replace")).hexdigest()[:16], project_id=project_id, status=status,
                         reason=reason, retrieved_at=time.time(), queries=[{"mode": m, "query_chars": len(task or "")} for m in modes_meta], modes=modes_meta,
                         files=[asdict(c) for c in src + docs], entities=[{"name": e["name"], "type": e["type"], "summary": _first_sentence(e["desc"], 140)} for e in ents],
                         relationships=[{"a": r["a"], "b": r["b"], "summary": _first_sentence(r["desc"], 150)} for r in rels if len(_first_sentence(r["desc"], 150)) >= 25], warnings=list(warnings))
    d.stats = {"raw_chars_by_mode": {m: v.get("chars", 0) for m, v in modes_meta.items()}, "raw_chars_total": sum(v.get("chars", 0) for v in modes_meta.values()),
               "entities_raw": raw["entities"], "entities_unique": len(ent), "relationships_raw": raw["relationships"], "relationships_unique": len(rel),
               "chunks_raw": raw["chunks"], "files_unique_source": sum(1 for c in cands if c.kind == "source"), "files_unique_doc": sum(1 for c in cands if c.kind == "doc"),
               "retained": {"source_files": len(src), "doc_files": len(docs), "entities": len(ents), "relationships": len(rels)}}
    return d


# ------------------------------------------------------------------------------------------ rendering
FALLBACK_TEXT = (
    "The application's retrieval did not return complete project knowledge for this request. That does NOT mean the repository lacks the relevant "
    "implementation -- do not conclude absence from it, and do not stop. Investigate independently with your own tools: use search_files to locate "
    "relevant source files (try the request's key domain terms, route and symbol names), read the actual source with get_file_content rather than "
    "relying on search snippets, follow imports, calls and references to establish the full behaviour, discover files beyond any listed here, use "
    "your other permitted code-search, database and MCP tools where useful, record file:line evidence from files you read, and say so plainly in "
    "your answer if the missing retrieval limits what you could establish.")
GUIDANCE_TEXT = (
    "These are LEADS from the project's index, ranked by relevance -- not proof and not a checklist. Verify against the real source by reading it. "
    "You are expected to search and read beyond this list; evidence from files you find yourself is just as valid.")


def _fit(lines: list, budget: int) -> list:
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            break
        out.append(ln)
        used += len(ln) + 1
    return out


def render_brief(d: Optional[KnowledgeDossier], audience: str = "researcher", budget: Optional[int] = None) -> str:
    """Compact, labelled application context. Items are dropped whole, lowest rank first; the header and the fallback/guidance
    text (which the application must always deliver) are never dropped."""
    if d is None:
        return ""
    budget = budget or (BRIEF_CHARS if audience == "researcher" else COORD_CHARS)
    head = [f"KNOWLEDGE PREPARATION (run by the application before you started -- you did not and cannot call it): status={d.status}"
            + (f" -- {d.reason}" if d.reason else "")]
    tail = [FALLBACK_TEXT if (d.fallback_required or d.status != SUCCESS) else GUIDANCE_TEXT]
    body: list = []
    src = [f for f in d.files if f["kind"] == "source" and f.get("verified") is not False]
    docs = [f for f in d.files if f["kind"] == "doc"]
    if audience == "researcher":
        if src:
            body.append("Candidate source files (ranked):")
            body += [f"  - {f['path']}" + (f" (lines {f['lines']})" if f.get("lines") else "") + (f" -- {f['role']}" if f.get("role") else "") for f in src[:MAX_SOURCE_FILES]]
        if d.relationships:
            body.append("Relationships the index knows about:")
            body += [f"  - {r['a']} -> {r['b']}: {r['summary']}" for r in d.relationships]
        if d.entities:
            body.append("Related entities:")
            body += [f"  - {e['name']} ({e['type']}): {e['summary']}" for e in d.entities]
        if docs:
            body.append("Project docs that may help (docs are context, not source evidence):")
            body += [f"  - {f['path']}" for f in docs]
    else:
        if src:
            body.append("Candidate starting files (the Researcher receives these with context): " + ", ".join(f["path"] for f in src[:8]))
    if d.warnings:
        body.append("Retrieval notes: " + "; ".join(d.warnings)[:200])
    fixed = "\n".join(head) + "\n" + "\n".join(tail)
    room = max(0, budget - len(fixed) - 2)
    return "\n".join(head + _fit(body, room) + tail)


def delegation_note(d: Optional[KnowledgeDossier]) -> str:
    return render_brief(d, "researcher")


# ------------------------------------------------------------------------------------------ preparation
def _cache_path(run_key: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"agnohive-knowledge-{re.sub(r'[^A-Za-z0-9_-]', '', run_key)[:64]}.json")


def load_cached(run_key: Optional[str]) -> Optional[KnowledgeDossier]:
    """A barren-retry spawns a NEW worker for the SAME top-level request; it must not retrieve again."""
    if not run_key:
        return None
    try:
        with open(_cache_path(run_key), "r", encoding="utf-8") as fh:
            return KnowledgeDossier(**json.load(fh))
    except Exception:  # noqa: BLE001
        return None


def save_cached(d: KnowledgeDossier) -> None:
    if not d.run_key:
        return
    try:
        tmp = _cache_path(d.run_key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(d), fh)
        os.replace(tmp, _cache_path(d.run_key))
    except Exception:  # noqa: BLE001
        pass


def drop_cached(run_key: Optional[str]) -> None:
    if run_key:
        try:
            os.unlink(_cache_path(run_key))
        except OSError:
            pass


def parse_find_files(text: str) -> list:
    t = (text or "").strip()
    if not t or t.startswith("No matches for:"):
        return []
    return [ln.strip() for ln in t.splitlines()[1:] if ln.strip()]


async def _verify_files(hive_url: Optional[str], d: KnowledgeDossier, budget_s: float) -> None:
    """Mark each candidate source file as existing / missing / unverifiable. A missing one is dropped at render time."""
    cands = [f for f in d.files if f["kind"] == "source"]
    if not cands or not hive_url:
        return
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def go():
        async with streamablehttp_client(hive_url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for f in cands:
                    try:
                        res = await asyncio.wait_for(s.call_tool("find_files", {"glob_pattern": f"**/{f['path']}"}), timeout=10)
                        txt = "\n".join(getattr(c, "text", "") for c in (getattr(res, "content", None) or []))
                        hits = [h for h in parse_find_files(txt) if ".." not in h and not h.startswith("/")]
                        f["verified"] = True if hits else False
                        if len(hits) == 1:
                            f["path"] = hits[0]                  # canonical repo path
                    except Exception:  # noqa: BLE001
                        f["verified"] = None
    try:
        await asyncio.wait_for(go(), timeout=max(5.0, budget_s))
    except Exception:  # noqa: BLE001
        pass
    d.files = [f for f in d.files if f.get("verified") is not False]
    keep_src, n = [], 0
    for f in d.files:
        if f["kind"] == "source":
            n += 1
            if n > MAX_SOURCE_FILES:
                continue
        keep_src.append(f)
    d.files = keep_src


def classify(outcomes: dict, parsed: dict, *, has_data: bool) -> tuple:
    """(status, reason, warnings) from the per-mode retrieval outcomes."""
    vals = list(outcomes.values())
    warnings = []
    for p in parsed.values():
        for note in p.get("failed_namespaces", []):
            if "global memory" in note.lower() and "no results" in note.lower():
                continue                                     # the global-memory namespace being empty is routine, not an incompleteness
            warnings.append(("retrieval note: " + note)[:160])
        if p.get("malformed"):
            warnings.append(f"{p['malformed']} malformed retrieval item(s) skipped")
    warnings = list(dict.fromkeys(warnings))
    bad = [m for m, o in outcomes.items() if o in ("timeout", "error", "unavailable", "no_url")]
    if has_data:
        if bad or any("retrieval note" in w for w in warnings):
            return PARTIAL, (f"retrieval incomplete (modes failed: {','.join(bad)})" if bad else "retrieval reported an incomplete namespace"), warnings
        return SUCCESS, "", warnings
    if all(o in ("unavailable", "no_url") for o in vals):
        return UNAVAILABLE, "the LightRAG service could not be reached", warnings
    if all(o == "timeout" for o in vals):
        return TIMEOUT, "retrieval exceeded its deadline", warnings
    if any(o == "error" for o in vals) or any(o == "timeout" for o in vals):
        return ERROR, "retrieval failed or returned unusable data", warnings
    # every mode answered; none produced usable content. Text that cannot be parsed (malformed items, or none of the expected
    # sections) is UNUSABLE data (ERROR); a well-formed response with nothing in it is a genuine EMPTY.
    if parsed and any(p.get("malformed") or not p.get("sections") for p in parsed.values()):
        return ERROR, "retrieval returned malformed or unparseable data", warnings
    return EMPTY, "retrieval completed but found no useful project knowledge", warnings


async def prepare_run_knowledge(task: str, *, project_id: str, lightrag_url: Optional[str], hive_url: Optional[str] = None,
                                run_key: Optional[str] = None, budget_s: float = BUDGET_S, caller: str = "") -> KnowledgeDossier:
    """THE shared preparation. Never raises. Idempotent per `run_key` (a retried worker of the same top-level request reuses it)."""
    cached = load_cached(run_key)
    if cached is not None and cached.task_sha == hashlib.sha256((task or "").encode("utf-8", "replace")).hexdigest()[:16]:
        emit("KNOWLEDGE_DOSSIER_REUSED", run=run_key, reason="same top-level request (worker retry)", status=cached.status, caller=caller)
        return cached
    t0 = time.monotonic()
    key = run_key or "-"
    emit("KNOWLEDGE_PREPARATION_STARTED", run=key, project=project_id, task_chars=len(task or ""), modes=",".join(MODES), caller=caller)
    outcomes, meta, parsed = {}, {}, {}
    try:
        from swarm import team as T
        deadline = time.monotonic() + max(5.0, budget_s - 8)

        async def one(mode):
            m0 = time.monotonic()
            try:
                outcome, text = await T._repo_lightrag_query(task, project_id, lightrag_url, mode=mode, use_context_only=True, deadline=deadline)
            except Exception as e:  # noqa: BLE001
                outcome, text = "error", ""
                emit("KNOWLEDGE_PREPARATION_FAILED", run=key, mode=mode, error=f"{type(e).__name__}")
            outcomes[mode] = outcome
            meta[mode] = {"outcome": outcome, "seconds": round(time.monotonic() - m0, 1), "chars": len(text or "")}
            if outcome == "usable":
                parsed[mode] = parse_context(text)
        await asyncio.gather(*(one(m) for m in MODES))
    except Exception as e:  # noqa: BLE001
        emit("KNOWLEDGE_PREPARATION_FAILED", run=key, error=f"{type(e).__name__}: {str(e)[:80]}")
        for m in MODES:
            outcomes.setdefault(m, "error"); meta.setdefault(m, {"outcome": "error", "seconds": 0, "chars": 0})
    has_data = any(p["chunks"] or p["entities"] or p["relationships"] for p in parsed.values())
    status, reason, warnings = classify(outcomes, parsed, has_data=has_data)
    try:
        d = build_dossier(task, parsed, run_key=key, project_id=project_id, modes_meta=meta, status=status, reason=reason, warnings=warnings)
        if status in (SUCCESS, PARTIAL) and not d.source_files() and not d.files:
            d.status, d.reason = EMPTY, "retrieval returned content but no usable project source or doc file"
        await _verify_files(hive_url, d, max(5.0, budget_s - (time.monotonic() - t0)))
        d.fallback_required = d.status != SUCCESS or not d.source_files()
        d.stats["brief_chars_researcher"] = len(render_brief(d, "researcher"))
        d.stats["brief_chars_coordinator"] = len(render_brief(d, "coordinator"))
        d.stats["elapsed_s"] = round(time.monotonic() - t0, 1)
    except Exception as e:  # noqa: BLE001 -- never let preparation break an execution
        emit("KNOWLEDGE_PREPARATION_FAILED", run=key, error=f"build {type(e).__name__}: {str(e)[:80]}")
        d = KnowledgeDossier(run_key=key, task_sha="", project_id=project_id, status=ERROR, reason="dossier construction failed", retrieved_at=time.time(), modes=meta, fallback_required=True)
    if d.status in (SUCCESS, PARTIAL):
        emit("KNOWLEDGE_PREPARATION_COMPLETED", run=key, status=d.status, files=len(d.source_files()), entities=len(d.entities), relationships=len(d.relationships),
             raw_chars=d.stats.get("raw_chars_total"), brief_chars=d.stats.get("brief_chars_researcher"), elapsed_s=d.stats.get("elapsed_s"))
        emit("KNOWLEDGE_DOSSIER_CREATED", run=key, status=d.status, source_files=len(d.source_files()), unique_entities=d.stats.get("entities_unique"),
             unique_relationships=d.stats.get("relationships_unique"), raw_chars=d.stats.get("raw_chars_total"), brief_chars=d.stats.get("brief_chars_researcher"))
    elif d.status == EMPTY:
        emit("KNOWLEDGE_PREPARATION_EMPTY", run=key, reason=d.reason, elapsed_s=d.stats.get("elapsed_s"))
    else:
        emit("KNOWLEDGE_PREPARATION_FAILED", run=key, status=d.status, reason=d.reason, elapsed_s=d.stats.get("elapsed_s"))
    if d.fallback_required:
        emit("KNOWLEDGE_FALLBACK_ACTIVATED", run=key, status=d.status, reason=d.reason or "no usable candidate source files", agents="independent investigation instructed")
    save_cached(d)
    return d


def dossier_from_dict(x: Optional[dict]) -> Optional[KnowledgeDossier]:
    try:
        return KnowledgeDossier(**x) if x else None
    except Exception:  # noqa: BLE001
        return None


def tool_event(tool: str, args, ok: bool, run: str = "-", where: str = "") -> None:
    """Agent tool activity classified for the audit: independent discovery, file reads, and any (unexpected) agent LightRAG call."""
    name = str(tool or "")
    if name.startswith("lightrag_query"):
        emit("AGENT_LIGHTRAG_CALL", run=run, tool=name, where=where, note="UNEXPECTED: agents have no LightRAG query tool")
    elif name in ("search_files", "search_files_batch", "find_files", "list_directory", "list_directory_tree", "count_matches"):
        emit("AGENT_INDEPENDENT_DISCOVERY", run=run, tool=name, ok=ok, where=where)
    elif name in ("get_file_content", "get_files_batch"):
        emit("AGENT_FILE_READ", run=run, tool=name, ok=ok, where=where)
