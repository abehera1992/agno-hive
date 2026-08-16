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

Default --mcp-url points at hive-mcp (AGNO_SYSTEM_MCP_URL), NOT the project MCP
(AGNO_MCP_URL) -- confirmed live 2026-08-16 while regenerating the training corpus:
find_files/get_file_content were removed from EkamApp's own MCP server in commit
2e0fbe1 (2026-08-04, generic file-I/O tools deliberately migrated to hive-mcp to
decouple client-machine file I/O from any one project) but this script's default
was never updated, so it silently returned "found 0 file(s)" for every call since
then instead of erroring -- find_files against a server that doesn't register that
tool name returns an empty result, not a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _text(result) -> str:
    if not result or not getattr(result, "content", None):
        return ""
    return "\n".join(c.text for c in result.content if hasattr(c, "text") and c.text)


def _strip_line_numbers(text: str) -> str:
    """Undo hive-mcp's get_file_content cat -n formatting ("   123\\tcontent").

    Confirmed live 2026-08-16: this broke patterns_md.py's guard parser silently --
    _GUARD_RE anchors on a literal '## ' at the start of a line, and every line here
    now starts with a right-aligned number + tab instead, so 0 guards ever matched.
    Split on the FIRST tab only, per hive-mcp's own get_file_content docstring
    ("match only the actual file content after the tab") -- a line whose real
    content itself contains a tab must not lose anything past the first one.
    """
    return "\n".join(
        ln.split("\t", 1)[1] if "\t" in ln else ln
        for ln in text.splitlines()
    )


async def run(mcp_url: str, glob: str, out: Path, limit: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    async with streamablehttp_client(mcp_url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            listing = _text(await s.call_tool("find_files", {"glob_pattern": glob}))
            # hive-mcp's find_files always prefixes its reply with a header line --
            # "N result(s) for '<glob>':" (hive-mcp/tools/context.py) -- before the
            # actual paths. Confirmed live 2026-08-16: that header line does not end
            # in "/" and, because the glob pattern itself contains slashes, Path(...)
            # .name on the WHOLE header string still resolves to something containing
            # a "." (the tail of the glob), so it silently passed the old "looks like
            # a file" filter and got written to disk as a bogus pattern file. Drop the
            # header explicitly rather than trying to heuristically detect it.
            lines = listing.splitlines()
            if lines and re.match(r"^\d+ result\(s\) for ", lines[0]):
                lines = lines[1:]
            paths = [
                ln.strip().lstrip("- ").strip()
                for ln in lines
                if ln.strip() and not ln.strip().endswith("/")
            ]
            paths = [p for p in paths if "." in Path(p).name][:limit]
            print(f"found {len(paths)} file(s) for {glob!r}")
            for p in paths:
                body = _strip_line_numbers(
                    _text(await s.call_tool("get_file_content", {"relative_path": p}))
                )
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
    ap.add_argument("--mcp-url", default=os.getenv("AGNO_SYSTEM_MCP_URL", "http://100.87.159.86:9003/mcp"))
    ap.add_argument("--glob", default="patterns/**/*.md")
    ap.add_argument("--out", default="/tmp/project-patterns")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    asyncio.run(run(a.mcp_url, a.glob, Path(a.out), a.limit))


if __name__ == "__main__":
    main()
