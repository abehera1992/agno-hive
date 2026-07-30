"""Pull files from a project into a local dir via its MCP server.

The guards and source live in the PROJECT repo (e.g. EkamApp), not in agno-hive, and
ZGX has no checkout of it. bootstrap.py already solves this by fetching patterns through
the project MCP at task time; this mirrors that so the training corpus and eval cases
can be rebuilt on ZGX without cloning the project.

    # guards (default)
    python -m training.fetch_patterns --out /tmp/ekam-patterns

    # arbitrary source, for eval-case ground truth
    python -m training.fetch_patterns --glob "API/inventory-service/**/*.py" \
                                      --out /tmp/ekam-src
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _text(result) -> str:
    if not result or not getattr(result, "content", None):
        return ""
    return "\n".join(c.text for c in result.content if hasattr(c, "text") and c.text)


async def run(mcp_url: str, glob: str, out: Path, limit: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    async with streamablehttp_client(mcp_url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            listing = _text(await s.call_tool("find_files", {"glob_pattern": glob}))
            paths = [
                ln.strip().lstrip("- ").strip()
                for ln in listing.splitlines()
                if ln.strip() and not ln.strip().endswith("/")
            ]
            paths = [p for p in paths if "." in Path(p).name][:limit]
            print(f"found {len(paths)} file(s) for {glob!r}")
            for p in paths:
                body = _text(await s.call_tool("get_file_content", {"relative_path": p}))
                if not body.strip():
                    print(f"  SKIP (empty): {p}")
                    continue
                # Flatten into the out dir but keep provenance in the filename so an
                # eval case can cite the real repo-relative path.
                dest = out / p.replace("/", "__")
                dest.write_text(body, encoding="utf-8")
                print(f"  {p} -> {dest.name} ({len(body)} chars)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-url", default=os.getenv("AGNO_MCP_URL", "http://100.87.159.86:9000/mcp"))
    ap.add_argument("--glob", default="patterns/**/*.md")
    ap.add_argument("--out", default="/tmp/project-patterns")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    asyncio.run(run(a.mcp_url, a.glob, Path(a.out), a.limit))


if __name__ == "__main__":
    main()
