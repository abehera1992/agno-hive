"""Application-owned source discovery for the engineering Workflow (Phase 1.19).

Problem: the Workflow plans work from files the request NAMES. The frozen T11 and T13a battery prompts name none, so the
planner used to refuse them ("the request names no repository file path"). Discovery lets the APPLICATION find the files,
deterministically and with a bounded, inspectable procedure, instead of letting a Coordinator model decide the scope:

  1. LightRAG retrieval: `lightrag_query_context` (retrieval only, no answer generation) in three fixed modes
     (hybrid, local, naive). It returns in seconds and its context carries real repository paths.
  2. Extract file paths from the retrieved context.
  3. Keep only source evidence files (.py/.ts/.tsx/.js/.jsx), dropping migrations, tests and generated/asset files.
  4. VERIFY each candidate exists in the repository (`find_files`, exactly one match required); an unverifiable or
     ambiguous path is dropped, never guessed. A partial path that resolves uniquely is canonicalised.
  5. Rank by how many retrieval modes agree (ties by retrieval rank), with a per-service cap so one service cannot
     flood the selection. Cap the number of files.

Nothing in here knows any acceptance prompt. There is no keyword list, no per-prompt rule and no model call; the only
model-dependent input is the retrieval index's own ranking, which is recorded in the result so a run can be audited.
Every step has a timeout; any failure degrades to "fewer or no files", and an empty result makes the planner refuse the
request truthfully ("NOT ESTABLISHED") rather than invent scope.

The connections are opened BEFORE the Workflow opens its own MCP sessions: opening a second connection to a server the
run already holds open is the step confirmed to hang (swarm/team.py, 2026-08-20).
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

MODES = ("hybrid", "local", "naive")
SOURCE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx")
EXCLUDE_FRAGMENTS = ("/migrations/", "/node_modules/", "/tests/", "/__tests__/", "/__pycache__/", ".test.", ".spec.",
                     "/alembic/", "__init__.py")
DEFAULT_LIMIT = 8
PER_SERVICE_CAP = 3
MAX_VERIFY = 24

_PATH_RE = re.compile(r"(?<![\w./-])((?:[\w()\[\]@.-]+/)+[\w()\[\]@.-]+\.[A-Za-z0-9]{1,8})(?![\w/])")


def extract_candidate_paths(text: str) -> list:
    seen, out = set(), []
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(1).strip(".,;:)")
        if p and p not in seen and "://" not in p and not p.startswith("/"):
            seen.add(p)
            out.append(p)
    return out


def _is_source_candidate(path: str) -> bool:
    low = path.lower()
    return low.endswith(SOURCE_EXT) and not any(f in "/" + low for f in EXCLUDE_FRAGMENTS)


def _service_root(path: str) -> str:
    parts = path.split("/")
    if parts[0] == "API" and len(parts) > 1:
        return "/".join(parts[:2])
    if parts[0] == "Client" and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]


def _text_of(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in (getattr(res, "content", None) or []))


def parse_find_files(text: str) -> list:
    """hive-mcp find_files format: first line "{N} result(s) for '{glob}':", then one repo-relative path per line;
    "No matches for: ..." when empty."""
    t = (text or "").strip()
    if not t or t.startswith("No matches for:"):
        return []
    return [ln.strip() for ln in t.splitlines()[1:] if ln.strip()]


def rank_and_select(per_mode: dict, verified: dict, limit: int, per_service_cap: int = PER_SERVICE_CAP) -> list:
    """Pure: `per_mode` {mode: [candidate paths in retrieval order]}, `verified` {candidate: canonical path | None}.
    Agreement dominates: score = number of retrieval modes that returned the file, ties broken by best rank. A per-service
    cap keeps one service from flooding the selection; it never promotes a lower-agreement file over a higher one that
    still fits under its service's cap. (A first draft spread round-robin across services, which put a one-mode file from a
    fourth service ahead of a three-mode file from a service already represented; found on the discovery probe, before
    any acceptance run.)"""
    score, best_rank = {}, {}
    for mode, paths in per_mode.items():
        for rank, cand in enumerate(paths):
            canon = verified.get(cand)
            if not canon:
                continue
            score.setdefault(canon, set()).add(mode)
            best_rank[canon] = min(best_rank.get(canon, 10**9), rank)
    ordered = sorted(score, key=lambda c: (-len(score[c]), best_rank[c], c))
    used: dict = {}
    picked: list = []
    for c in ordered:
        root = _service_root(c)
        if used.get(root, 0) >= per_service_cap:
            continue
        used[root] = used.get(root, 0) + 1
        picked.append(c)
        if len(picked) >= limit:
            break
    return picked


async def discover(task: str, *, lightrag_url: Optional[str], hive_url: Optional[str], project_id: str,
                   limit: int = DEFAULT_LIMIT, mode_timeout: float = 45.0, verify_timeout: float = 15.0,
                   budget_s: float = 90.0) -> dict:
    """Never raises. Returns {"paths": [...], "reason": str|None, "diagnostics": {...}}."""
    t0 = time.monotonic()
    diag: dict = {"modes": {}, "candidates": 0, "source_candidates": 0, "verified": 0, "dropped_unverifiable": 0,
                  "limit": limit}
    if not lightrag_url:
        return {"paths": [], "reason": "no LightRAG MCP server was available for retrieval", "diagnostics": diag}
    if not hive_url:
        return {"paths": [], "reason": "no hive-mcp server was available to verify retrieved paths", "diagnostics": diag}
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except Exception as e:  # noqa: BLE001
        return {"paths": [], "reason": f"MCP client unavailable ({type(e).__name__})", "diagnostics": diag}

    per_mode: dict = {}

    async def _retrieve():
        async with streamablehttp_client(lightrag_url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for mode in MODES:
                    left = budget_s - (time.monotonic() - t0)
                    if left <= 5:
                        diag["modes"][mode] = {"skipped": "budget exhausted"}
                        continue
                    m0 = time.monotonic()
                    try:
                        res = await asyncio.wait_for(
                            s.call_tool("lightrag_query_context", {"query": task, "project_id": project_id, "mode": mode}),
                            timeout=min(mode_timeout, left))
                        txt = _text_of(res)
                        paths = extract_candidate_paths(txt)
                        per_mode[mode] = [p for p in paths if _is_source_candidate(p)]
                        diag["modes"][mode] = {"seconds": round(time.monotonic() - m0, 1), "chars": len(txt),
                                               "paths": len(paths), "source_paths": len(per_mode[mode])}
                    except Exception as e:  # noqa: BLE001
                        diag["modes"][mode] = {"seconds": round(time.monotonic() - m0, 1), "error": f"{type(e).__name__}: {str(e)[:80]}"}

    try:
        await asyncio.wait_for(_retrieve(), timeout=budget_s)
    except Exception as e:  # noqa: BLE001
        diag["retrieval_error"] = f"{type(e).__name__}: {str(e)[:100]}"
    if not per_mode:
        return {"paths": [], "reason": "LightRAG retrieval returned nothing", "diagnostics": diag}

    cands: list = []
    for paths in per_mode.values():
        for p in paths:
            if p not in cands:
                cands.append(p)
    diag["candidates"] = sum(len(v) for v in per_mode.values())
    diag["source_candidates"] = len(cands)
    # verify the most-agreed candidates first
    freq = {c: sum(c in v for v in per_mode.values()) for c in cands}
    to_check = sorted(cands, key=lambda c: (-freq[c], cands.index(c)))[:MAX_VERIFY]
    verified: dict = {}

    async def _verify():
        async with streamablehttp_client(hive_url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for cand in to_check:
                    try:
                        res = await asyncio.wait_for(s.call_tool("find_files", {"glob_pattern": f"**/{cand}"}), timeout=verify_timeout)
                        hits = [h for h in parse_find_files(_text_of(res)) if ".." not in h and not h.startswith("/")]
                        verified[cand] = hits[0] if len(hits) == 1 else None
                    except Exception:  # noqa: BLE001
                        verified[cand] = None

    try:
        await asyncio.wait_for(_verify(), timeout=60)
    except Exception as e:  # noqa: BLE001
        diag["verify_error"] = f"{type(e).__name__}: {str(e)[:100]}"
    diag["verified"] = sum(1 for v in verified.values() if v)
    diag["dropped_unverifiable"] = sum(1 for v in verified.values() if not v)
    picked = rank_and_select(per_mode, verified, limit)
    diag["elapsed_s"] = round(time.monotonic() - t0, 1)
    if not picked:
        return {"paths": [], "reason": "retrieval found no source file that could be verified to exist", "diagnostics": diag}
    return {"paths": picked, "reason": None, "diagnostics": diag}
