"""Project context and file-reading tools.

Generic — works with any project layout. Discovers context files
(DOCS.md, README.md, CONTRIBUTING.md) automatically
from PROJECT_ROOT.

CLAUDE.md specifically is excluded from every content-serving path here
(get_project_context, get_file_content, and hive.md's generator in scan.py) — see
_EXCLUDED_ASSISTANT_FILES below. It tells a coding assistant HOW TO BEHAVE in this
repo (workflow rules, delegation policy, tool-usage conventions) — it is not project
documentation, and a swarm agent reading it as if it were project context produces
confused, off-task behavior. Confirmed live 2026-08-09: a cloud model skipped the
documented hive.md/get_project_context flow entirely and called
get_file_content('CLAUDE.md') directly on its own initiative, then answered a "what
shipped" question by pattern-matching against instruction text instead of reading real
source. AGENTS.md/GEMINI.md are NOT excluded (no observed problem with them; don't
extend scope speculatively — add them here only if the same failure mode shows up).
"""
import ast
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from config import PROJECT_ROOT

from .exclusions import EXCLUDE_DIRS, is_excluded, rg_args

# Exclusions live in one place and apply to search, read, write, scan and index alike.
# Project-specific entries come from EXCLUDE_DIRS / EXCLUDE_GLOBS in the project env.
_IGNORE_DIRS = EXCLUDE_DIRS
_RG_EXCLUDES = rg_args()          # already in ["--glob", "!pat", ...] form

_MAX_OUTPUT_CHARS = config.SEARCH_MAX_OUTPUT_CHARS


def _cap(text: str) -> str:
    """Clamp a tool result to _MAX_OUTPUT_CHARS. Applied on EVERY return path, including
    the pure-Python fallback: max_results bounds the number of LINES, not their length,
    and one matching line in a minified bundle or a JSON blob can be megabytes."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return (text[:_MAX_OUTPUT_CHARS]
            + f"\n... TRUNCATED at {_MAX_OUTPUT_CHARS} chars. Narrow `pattern` or pass a "
              f"`glob_filter` such as '**/*.tsx'.")

_CONTEXT_FILES = [
    "DOCS.md", "README.md", "CONTRIBUTING.md",
]

# Basenames refused by get_file_content() and skipped by get_project_context() — see the
# module docstring above for why. Matched by basename (Path(...).name), not full path, so
# a nested per-directory CLAUDE.md is caught the same as one at the project root.
_EXCLUDED_ASSISTANT_FILES = {"CLAUDE.md"}


def _is_ignored(rel: str) -> bool:
    return any(
        p in _IGNORE_DIRS or p.startswith(".")
        for p in rel.replace("\\", "/").split("/")
    )


def _walk_project(root=None):
    """os.walk with ignored-dir pruning."""
    root = root or PROJECT_ROOT
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, str(root))
        for fname in filenames:
            rel = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            yield rel.replace("\\", "/")


def _matches_glob(rel: str, pattern: str) -> bool:
    """Match a relative path against a glob pattern, handling ** correctly."""
    import re as _re
    esc = _re.escape(pattern)
    esc = esc.replace(r"\*\*\/", "(?:.+/)?")
    esc = esc.replace(r"\*\*",   ".*")
    esc = esc.replace(r"\*",     "[^/]*")
    esc = esc.replace(r"\?",     "[^/]")
    return bool(_re.match(f"^{esc}$", rel))


def get_project_context() -> str:
    """
    Return project context by reading DOCS.md, README.md, and
    CONTRIBUTING.md if they exist at the project root.
    Always call this at the start of any task to understand the project conventions.

    Deliberately does NOT include CLAUDE.md — an assistant-instruction file, not
    project documentation. See this module's docstring for why.
    """
    _CAP = 40_000  # per-file cap so a huge DOCS.md can't dominate the context
    parts = []
    for name in _CONTEXT_FILES:
        path = PROJECT_ROOT / name
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > _CAP:
                content = content[:_CAP] + (
                    f"\n\n… [{name} truncated at {_CAP:,} of {len(content):,} bytes — "
                    f"read it directly with get_file_content('{name}') for the rest]"
                )
            parts.append(f"# {name}\n\n{content}")
    if not parts:
        return "No context files found (DOCS.md, README.md, CONTRIBUTING.md)."
    return "\n\n---\n\n".join(parts)


# Files at or below this size are returned whole. Larger files are reduced (skeleton
# for code, head+tail for data) so a single read can never overflow the agent context.
#
# Lowered 200,000 -> 12,000 on 2026-08-23. This threshold governs EXPLORATORY reads
# only: a call with offset/limit is an explicit, bounded range and returns exactly
# what was asked for, checked before this branch. So the only reads affected are
# "give me this whole file", which is where context is actually squandered.
#
# 200,000 was effectively no limit for this codebase. Measured across its 468 Python
# and TypeScript source files:
#     p50 = 2,290    p75 = 5,730    p90 = 9,574    p95 = 15,182    max = 69,525
# Every file sat under the old ceiling, so the reduction path never ran and a single
# architectural-overview task read 309,076 characters of whole files before its budget
# guard stopped it -- after which the run stalled and was killed. Nine distinct
# failures on that one probe, all downstream of loading far more than was needed.
#
# 12,000 sits between p90 and p95: roughly nine files in ten still come back whole,
# and only the top decile is skeletonised -- which is exactly the set doing the damage
# (models.py 31,151, vouchers_api.py 37,113, items_api.py 26,658). Their skeletons run
# 9-21% of full size while keeping what an overview actually needs: imports, class and
# function signatures, decorators, first docstring line.
#
# Nothing is lost, only deferred: the skeleton response names the exact
# get_file_content(path, offset=..., limit=...) call to read any part in full.
#
# RAISED 12,000 -> 40,000 (2026-08-26). "Only deferred" turned out to have a price the
# original reasoning could not have seen, because the thing it spends is a budget that
# did not exist yet.
#
# Measured across battery B8: get_file_content was 206 of 421 tool calls, including 11
# paginated reads of models.py alone. Deferral does not save characters -- the model
# reads the same bytes either way -- it converts them into CALLS, and the per-agent
# tool_call_limit is 50. Three runs (T11, T12, T13b) exhausted that budget on
# legitimate reads and had to be stopped early, while the cumulative read budget sat at
# 52,430 of 300,000 (17%) and vLLM's KV cache at 7%. The resource the 12,000 threshold
# protects was never close to scarce; the one it consumes ran out every time.
#
# What changed since: _MEMBER_READ_CHAR_BUDGET (swarm/team.py, 2026-08-22) caps
# CUMULATIVE loading per member at 300,000 chars. When 12,000 was chosen, per-file size
# was the only brake on context blowout, so it had to be tight. A cumulative cap does
# that job strictly better -- it bounds the total regardless of how the bytes are split
# -- which frees the per-file limit to be sized for call efficiency instead.
#
# 40,000 against the real distribution (474 source files, median 2,304, p95 15,182):
# 470 of 474 come back whole, and the four genuine giants (up to 69,525) still
# skeletonise, so the safety valve is intact. Worst case is ~10K tokens of a 262,144
# window. Every file the battery reads -- business_api.py 21,820, models.py 32,437,
# vouchers_api.py 37,852 -- now returns in one call instead of a skeleton plus five
# pages.
_MAX_FULL_BYTES = 40_000
_CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".kt", ".swift", ".scala",
}


def _py_skeleton(src: str):
    """Structural skeleton of Python source: imports + class/def signatures + the first
    docstring line, with bodies elided. Returns None if the source does not parse."""
    try:
        # Strip a leading UTF-8 BOM before parsing (2026-08-23). ast.parse rejects
        # U+FEFF outright ("invalid non-printable character U+FEFF"), so this returned
        # None for EVERY BOM-prefixed file -- and this project's files carry BOMs.
        # Measured on the four files a T12-style overview reads most:
        #   items_api.py    26,658 -> 5,470 skeleton  (21%)
        #   main.py          6,289 -> 1,322 skeleton  (21%)
        #   models.py       31,151 -> EMPTY   (BOM)
        #   vouchers_api.py 37,113 -> EMPTY   (BOM)
        # The two largest files were exactly the ones falling back to full text, so the
        # size-reduction path was dead where it mattered most and nothing reported it:
        # a None skeleton silently degrades to returning the whole file.
        tree = ast.parse(src.lstrip("﻿"))
    except (SyntaxError, ValueError):
        return None
    lines = src.splitlines()
    # (line_number_or_None, text). Numbers come from the AST nodes, which already carry
    # them -- see _render_skeleton for why they must survive into the output.
    out: list[tuple[int | None, str]] = []

    def push(start: int | None, text: str):
        """Append text, numbering each of its lines from `start` when known."""
        if not text:
            out.append((None, ""))
            return
        for offset, line in enumerate(text.split("\n")):
            out.append((None if start is None else start + offset, line))

    def emit(node, indent):
        pad = "    " * indent
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = ast.get_source_segment(src, node)
            if seg:
                push(node.lineno, seg)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                ds = ast.get_source_segment(src, dec)
                if ds:
                    push(dec.lineno, f"{pad}@{ds}")
            end = node.body[0].lineno - 1 if node.body else node.end_lineno
            end = max(end, node.lineno)  # guard one-line defs
            header = "\n".join(lines[node.lineno - 1:end]).rstrip()
            if header:
                push(node.lineno, header)
            doc = ast.get_docstring(node, clean=False)
            if doc:
                # The docstring's own line, when the first body statement IS it.
                doc_line = node.body[0].lineno if node.body else None
                push(doc_line, f'{pad}    """{doc.strip().splitlines()[0][:120]}"""')
            if isinstance(node, ast.ClassDef):
                members = [c for c in node.body
                           if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                for c in members:
                    emit(c, indent + 1)
                if not members:
                    push(None, f"{pad}    ...")
            else:
                push(None, f"{pad}    ...")   # elided body: no single real line
            out.append((None, ""))
        elif indent == 0 and isinstance(node, (ast.Assign, ast.AnnAssign)):
            seg = ast.get_source_segment(src, node)
            if seg:
                push(node.lineno, seg.splitlines()[0][:200])

    mod_doc = ast.get_docstring(tree, clean=False)
    if mod_doc:
        push(tree.body[0].lineno if tree.body else None,
             f'"""{mod_doc.strip().splitlines()[0][:160]}"""')
    for node in tree.body:
        emit(node, 0)
    return _render_skeleton(out)


def _render_skeleton(items: list[tuple[int | None, str]]) -> str:
    """Render (line_number, text) pairs as cat -n output, matching every other read.

    The skeleton was the ONE read path that returned unnumbered text (2026-08-26).
    Full reads and ranged reads both number their lines; _numbered_lines' own docstring
    explains why -- "without a real number on every line, a model has to count instead
    of copy". The skeleton was never wired into it, and that omission is the root of
    the stall family:

    A Researcher asked to "list every endpoint" got the skeleton, which carried all 13
    `@router` decorators and all 13 signatures -- everything except a citable position
    for any of them. This project REQUIRES exact file:line citations (verify_claims
    flags what it cannot anchor), and `search_files` is the only tool that returns a
    line number per symbol. So the model re-derived them one grep at a time: 44
    search_files calls, its 50-call budget gone in 75 seconds, then agno silently
    refusing further calls while the model re-emitted the same call every ~1.2s until
    the 300s liveness kill. Four such stalls in two batteries, each losing the run
    whole -- 25,000 chars of real findings discarded, 504 returned.

    The header hint made it circular: it told the model to re-read with
    `offset=<line>`, in the one response containing no line numbers to choose from.

    Synthetic lines -- an elided body's `...`, blank separators -- get a blank number
    field rather than a borrowed one. Claiming a line for text that is not at that line
    would recreate the exact miscitation this is meant to prevent.
    """
    parts = []
    for lineno, text in items:
        if not text and lineno is None:
            parts.append("")
        elif lineno is None:
            parts.append(f"{'':>6}\t{text}")
        else:
            parts.append(f"{lineno:>6}\t{text}")
    return "\n".join(parts).strip("\n")


# A name listed one-per-line inside an `export { ... }` block -- the shape RTK Query
# slices use for every generated hook:
#
#     export {
#       useGetVouchersQuery,
#       usePostVoucherMutation,
#     }
#
# Those lines are bare identifiers. They start with no keyword, so every rule in
# _regex_skeleton below skipped them and the skeleton for inventoryApi.ts contained
# ZERO of its five voucher hooks (2026-08-26, verified against the live tool).
#
# That is the whole of the standing T13a/T13b failure. Both variants were asked to list
# the frontend hooks, both got a skeleton with none in it, both found only the 2 hooks
# reachable from page components, and both concluded 6 backend endpoints lacked a
# counterpart when the real number is 4 -- `/post` and `/cancel` DO have hooks
# (usePostVoucherMutation, useCancelVoucherMutation). The model never saw them.
#
# Same defect class as the unnumbered skeleton: hive-mcp eliding the exact thing the
# task asked for, and the answer being blamed for it. symbol_index._ts_index already
# carries this rule for its own export scan; _regex_skeleton simply never got it.
_EXPORT_BLOCK_NAME_RE = re.compile(r"^[A-Za-z_$][\w$]*\s*,?$")

# Opens such a block. BOTH forms matter and only the second appears in this codebase:
#   export {                 -- a plain re-export block
#   export const {           -- destructuring, which is how RTK Query hands back its
#                               generated hooks (`export const { useX, ... } = api;`)
# Assuming the first form was why the initial version of this fix matched nothing --
# inventoryApi.ts:915 is `export const {`, and every hook sits under it.
#
# Deliberately requires the brace to CLOSE the line: `export const foo = {` opens an
# object literal, whose contents are `key: value` pairs, not exported names.
_EXPORT_BLOCK_OPEN_RE = re.compile(r"^export\s+(?:const|let|var)\s*\{\s*$|^export\s*\{\s*$")


def _regex_skeleton(src: str):
    """Best-effort declaration-line skeleton for non-Python code (TS/JS/Go/Java/…)."""
    keep: list[tuple[int | None, str]] = []
    in_export_block = False
    for lineno, ln in enumerate(src.splitlines(), start=1):
        s = ln.strip()
        if not s:
            continue
        # Track `export {` ... `}` so the bare names inside are kept. Only names are
        # matched, so a stray brace or a comment inside the block is still dropped.
        if in_export_block:
            if s.startswith("}"):
                in_export_block = False
            elif _EXPORT_BLOCK_NAME_RE.match(s):
                keep.append((lineno, ln.rstrip()))
            continue
        if _EXPORT_BLOCK_OPEN_RE.match(s):
            in_export_block = True
            keep.append((lineno, ln.rstrip()))
            continue
        if (s.startswith((
                "import ", "export ", "from ", "package ", "func ", "function ",
                "class ", "interface ", "type ", "enum ", "struct ", "trait ",
                "public ", "private ", "protected ", "def ", "module ", "@"))
                or re.match(r"^(export\s+)?(default\s+)?(async\s+)?function\b", s)
                or re.match(r"^(export\s+)?(const|let|var)\s+\w+\s*[:=]", s)
                or re.match(r"^[\w<>,\[\]\s]+\s+\w+\s*\([^)]*\)\s*[:{]?\s*$", s)):
            keep.append((lineno, ln.rstrip()))
    return _render_skeleton(keep) if keep else None


def _numbered_lines(lines: list[str], start: int) -> str:
    """Prefix each line with its 1-based file line number (cat -n style).

    Callers cite `path:line` from what they read here — without a real number on
    every line, a model has to count instead of copy, and silently miscounts.
    Measured live 2026-08-04: without this, an answer citing a docstring's location
    was off by 90-215 lines despite reading the real file content correctly — the
    content was right, there was just nothing to read a line number FROM.
    """
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start=start))


def _find_by_basename(basename: str, max_results: int = 6) -> list[str]:
    """Locate every file in the project with this exact filename, regardless of
    directory. Used by get_file_content()'s not-found fallback below -- a guessed
    path commonly has the right filename but a wrong internal subdirectory (e.g.
    'src/components/user-management/Foo.tsx' guessed vs the real
    'Client/EcommClient-Web/ekamweb/src/components/portal/admin/users/Foo.tsx').
    That's a different failure than find_files()'s GLOB_FALLBACK_PREFIXES handles
    (a missing ROOT prefix on an otherwise-correct relative pattern) -- a basename
    search recovers regardless of how wrong the guessed directory structure is,
    as long as the filename itself is right, which it usually is."""
    import shutil
    rg = shutil.which("rg")
    pattern = f"**/{basename}"
    if rg:
        try:
            return _rg_glob(rg, pattern, max_results)
        except Exception:
            pass
    matches = []
    for p in sorted(PROJECT_ROOT.glob(pattern)):
        rel = p.relative_to(PROJECT_ROOT).as_posix()
        if _is_ignored(rel) or not p.is_file():
            continue
        matches.append(rel)
        if len(matches) >= max_results:
            break
    return matches


def _shared_leading_segments(a: str, b: str) -> int:
    """How many leading directory segments two paths share before diverging --
    e.g. 'Client/EcommClient-Web/ekamweb/src/components/x.tsx' vs
    'Client/EcommClient-Web/ekamweb/src/app/x.tsx' share 4 (Client, EcommClient-Web,
    ekamweb, src) before 'components' vs 'app' diverges. Excludes the filename
    itself from both sides -- only directory structure counts."""
    a_dirs = a.replace("\\", "/").split("/")[:-1]
    b_dirs = b.replace("\\", "/").split("/")[:-1]
    shared = 0
    for x, y in zip(a_dirs, b_dirs):
        if x != y:
            break
        shared += 1
    return shared


def _rank_candidates_by_relevance(guessed_path: str, candidates: list[str]) -> list[str]:
    """Sort get_file_content()'s disambiguation candidates so the one whose directory
    structure most resembles the ORIGINAL (wrong) guess is listed first, not just in
    whatever order _find_by_basename happened to return.

    Confirmed live 2026-08-11: a guess for a real EkamApp web-frontend file, once
    ambiguous across a generic basename (e.g. 'index.tsx', which legitimately exists
    in many unrelated parts of this monorepo), produced a candidate list the model
    then worked through mechanically top-to-bottom -- including the mobile app's own
    unrelated index.tsx -- instead of recognizing which candidate actually matched
    the web-frontend task it was doing. Ranking by shared leading directory segments
    with the original guess (a real project signal already available -- the guess
    itself usually has the RIGHT top-level app/service, just the wrong subdirectory
    beneath it) surfaces the almost-certainly-correct candidate first, deterministically,
    without needing the model to reason about it at all. Ties keep _find_by_basename's
    existing alphabetical order (Python's sort is stable)."""
    return sorted(candidates, key=lambda c: -_shared_leading_segments(guessed_path, c))


def get_file_content(relative_path: str, offset: int = 0, limit: int = 0) -> str:
    """
    Read a file from the project by its path relative to the project root.
    Always read a file before editing it — get the exact content to use as old_string in apply_diff.

    CLAUDE.md is refused (matched by basename, any directory) — it is an
    assistant-instruction file, not project documentation. Use DOCS.md, README.md,
    or get_project_context() for project context instead.

    Large files are NOT dumped whole (that overflows the model context): a file over
    ~200KB returns a structural SKELETON (signatures + docstrings, bodies elided) for code,
    or HEAD+TAIL for data files. Pass offset/limit to read an exact line range of any file.

    Output uses cat -n style line numbers ("   123\tactual content") so file:line
    citations can be copied exactly instead of counted/guessed. Never include the
    line-number prefix itself when passing old_string/new_string to apply_diff —
    match only the actual file content after the tab.

    A guessed path that doesn't exist is NOT a dead end: if exactly one file in the
    project has that same filename elsewhere, it is read automatically (prefixed with
    a NOTE stating the correction — use the corrected path from then on). If several
    files share that filename, their paths are listed so the next call can go straight
    to the right one, instead of falling back to a separate find_files/search_files call.

    Args:
        relative_path: e.g. 'src/api/routes.py', 'package.json', 'docker-compose.yml'
        offset: 0-based first line for a ranged read (default 0 = start of file)
        limit:  number of lines to return from offset (default 0 = to end / whole file)
    """
    basename = Path(relative_path).name
    if basename in _EXCLUDED_ASSISTANT_FILES:
        return (
            f"{basename} is an assistant-instruction file (workflow rules, delegation "
            f"policy, tool-usage conventions for a coding assistant), not project "
            f"documentation — it is excluded from tool access. Use DOCS.md, README.md, "
            f"or get_project_context() for project context instead."
        )
    if is_excluded(relative_path):
        return (
            f"get_file_content blocked: '{relative_path}' is in an excluded path "
            f"(dependency tree, build output, or a project EXCLUDE_DIRS/EXCLUDE_GLOBS "
            f"entry). It is vendored/generated, not part of this project's own source."
        )
    target = PROJECT_ROOT / relative_path
    if not target.exists():
        candidates = _find_by_basename(basename)
        if len(candidates) == 1 and candidates[0] != relative_path:
            corrected = candidates[0]
            note = (
                f"# NOTE: '{relative_path}' not found — '{corrected}' is the only file named "
                f"'{basename}' in the project; reading that instead. Use this exact path from now on.\n"
            )
            return note + get_file_content(corrected, offset, limit)
        if len(candidates) > 1:
            ranked = _rank_candidates_by_relevance(relative_path, candidates)
            return (
                f"File not found: {relative_path}\n"
                f"{len(ranked)} files named '{basename}' exist, sorted by how closely their "
                f"directory matches your guess (most likely match FIRST). Call "
                f"get_file_content() AGAIN right now with the FIRST path below unless you "
                f"have a specific reason to believe a different one is right — copied "
                f"verbatim, do NOT retry '{relative_path}' again, it will keep failing the "
                f"same way:\n"
                + "\n".join(ranked)
            )
        return (
            f"File not found: {relative_path}\n"
            f"No file named '{basename}' exists anywhere in this project. Do NOT retry with a "
            f"different offset/limit — a nonexistent file has no content at any offset, so "
            f"changing those will never produce a different result. Call find_files() or "
            f"search_files() to locate the correct path instead."
        )
    if not target.is_file():
        return f"Not a file: {relative_path}"
    try:
        data = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Could not read {relative_path}: {e}"

    # An EXISTING but EMPTY file must not return an empty string (2026-08-21).
    # _numbered_lines([]) is "", which is byte-identical to what a caller would infer
    # "nothing here" from -- and a model reading an empty result reasons its way to the
    # only explanation it has: the file is missing.
    #
    # Live case: asked to list API/business-service/router/, a run called
    # get_file_content on that package's __init__.py (a real, normal, 0-byte file),
    # got "", and answered "`router/__init__.py` does not exist in the codebase. This
    # is confirmed by multiple attempts to read it, including with progressively larger
    # line limits". All six files were there. It then spent its whole tool_call_limit
    # re-reading a file whose content could never change with offset/limit.
    #
    # Stated before the ranged branch so it wins for every offset/limit combination --
    # the retry loop above is exactly what happens when only the whole-file path says
    # so. Mirrors the "File not found" branch's own anti-retry wording, for the same
    # reason: naming the wrong next action is what stops it.
    if not data:
        return (
            f"# {relative_path} EXISTS and is EMPTY (0 bytes).\n"
            f"# This is NOT 'file not found' — the file is present in the project and has\n"
            f"# no content. Do NOT retry with a different offset/limit: an empty file has\n"
            f"# no content at any offset, so changing those will never return anything\n"
            f"# else. An empty __init__.py, .gitkeep or placeholder module is normal and\n"
            f"# usually means the package/directory exists — use list_directory() or\n"
            f"# find_files() if you need to know what else is beside it."
        )

    # Explicit line-range read — exact and bounded, takes precedence.
    # A ranged read of a file that FITS WHOLE is a page of a one-page book (2026-08-27).
    # The floor below already caught limit=5; the model simply moved to limit=40, which
    # is exactly that floor, and walked an 18,258-byte file in 41 calls -- offsets 100,
    # 130, 170, 210, 250 ... 410 -- for a file one un-ranged call returns entirely.
    # Measured in battery B10: 87 read calls across 8 distinct paths in a single run,
    # 41 of them this one file, and the run died on its 50-call budget.
    #
    # Nothing deduped it because each offset is a different args_key, so the read
    # cache's identical-args stub never applied; and nothing capped it because every
    # call was individually legitimate.
    #
    # Same reasoning as the floor, one step further: serving a superset of what was
    # asked is safe, and here the superset is the whole file, already under the
    # threshold that would have returned it whole anyway. The note matters as much as
    # the content -- the model paginates because it believes more pages exist, so it
    # has to be told plainly that they do not.
    if (offset or limit) and len(data) <= _MAX_FULL_BYTES:
        total = len(data.splitlines())
        return (
            f"# {relative_path} — offset/limit ignored: the whole file is {len(data):,} "
            f"bytes ({total} lines) and fits in one read, so all of it is below.\n"
            f"# There are NO further pages. Do not call this again with another offset "
            f"or limit for this file.\n"
            + _numbered_lines(data.splitlines(), 1)
        )

    if offset or limit:
        lines = data.splitlines()
        start = max(offset, 0)
        # Floor an absurdly small page (2026-08-25). A Researcher asked for limit=5
        # fifteen times in a row on a 906-line file -- offsets 114, 119, 124 ... 184,
        # ~350 chars each -- spending fifteen round-trips on roughly what one read
        # returns. Serving more lines than asked is safe in a way serving fewer is not:
        # the caller gets a superset of what it requested, the extra text is a few
        # hundred characters, and the alternative is a slow walk that also burns the
        # tool-call budget the task needs elsewhere.
        if 0 < limit < _MIN_RANGED_READ_LINES:
            limit = _MIN_RANGED_READ_LINES
        end = start + limit if limit > 0 else len(lines)
        # Past EOF must SAY so (2026-08-23). Previously an out-of-range offset returned
        # the header with an empty body -- 72 characters, no error, no hint -- and an
        # agent paginating a file had no way to know it had run off the end. Live: a
        # Researcher walked business_api.py (about 600 lines) with offset 14500, 15000,
        # 15500 ... 20000 in steps of 500, twelve identical empty responses in a row,
        # then stalled until the liveness watchdog killed the run.
        #
        # Self-inflicted: lowering _MAX_FULL_BYTES made large files return skeletons,
        # and a skeleton response invites exactly this offset/limit pagination. The
        # invitation shipped without the stop sign.
        if start >= len(lines):
            return (
                f"# {relative_path} — offset {start} is PAST THE END OF THE FILE.\n"
                f"# This file has {len(lines)} lines total. There is nothing beyond it,\n"
                f"# and requesting a larger offset will keep returning this message.\n"
                f"# You have already seen everything this file contains — move on, or\n"
                f"# re-read an EARLIER range with offset < {len(lines)}."
            )
        body = _numbered_lines(lines[start:end], start + 1)
        more = "" if end >= len(lines) else (
            f"\n# ── {len(lines) - end} more line(s) below; continue with "
            f"offset={end}, limit={limit or 200} ──"
        )
        at_end = "\n# ── END OF FILE ──" if end >= len(lines) else ""
        return (f"# {relative_path} — lines {start}..{min(end, len(lines))} "
                f"of {len(lines)}\n{body}{more}{at_end}")

    if len(data) <= _MAX_FULL_BYTES:
        return _numbered_lines(data.splitlines(), 1)

    # Oversized file — reduce it so it can't blow the context window.
    ext = target.suffix.lower()
    if ext in _CODE_EXT:
        skel = _py_skeleton(data) if ext == ".py" else _regex_skeleton(data)
        if skel:
            return (
                f"# {relative_path} is {len(data):,} bytes — too large to return whole.\n"
                f"# STRUCTURAL SKELETON below (signatures + docstrings; bodies elided as ...).\n"
                # A concrete limit, not "<n>" (2026-08-25). With no number suggested, a
                # Researcher picked limit=5 and walked this file in FIFTEEN round-trips
                # (offsets 114, 119, 124 ... 184) to retrieve about as much text as one
                # read would have returned. The skeleton invites pagination; it should
                # say what a sensible page looks like.
                f"# Read a specific part with get_file_content('{relative_path}', offset=<line>, limit=200).\n"
                f"# Use limit=200 or more — a small limit means many slow round-trips.\n"
                # Say plainly that the numbers are citable (2026-08-26). Until the
                # skeleton carried line numbers this advice was circular -- "re-read at
                # offset=<line>" in the one response with no line to choose -- and the
                # only way to get one was a grep per symbol, which is precisely what
                # burned the tool budget and stalled the run. The numbers are now real
                # file lines, so neither the grep nor the guesswork is needed.
                f"# The numbers below are REAL file line numbers: cite them directly and\n"
                f"# use them as offsets. Do NOT grep for a symbol that is already listed here.\n\n"
                + skel
            )
    head, tail = data[:8000], data[-4000:]
    return (
        f"# {relative_path} is {len(data):,} bytes — too large to return whole (data/non-code file).\n"
        f"# HEAD (8KB) + TAIL (4KB) shown; use search_files to find specific content,\n"
        f"# or get_file_content('{relative_path}', offset=<line>, limit=<n>) for a line range.\n\n"
        f"===== HEAD =====\n{head}\n\n===== TAIL =====\n{tail}"
    )


async def get_files_batch(paths: list[str]) -> str:
    """
    Read multiple files in ONE tool call, in parallel -- use this instead of several
    separate get_file_content() calls when you already know which files you need
    (e.g. a pattern file + the file you're about to edit + a related test file).

    Each file is read with get_file_content()'s exact same behavior (line numbers,
    skeleton-on-oversized, not-found message) -- this only parallelizes the I/O,
    it does not change what comes back for any individual file.

    Args:
        paths: relative paths, e.g. ['src/api/routes.py', 'src/api/models.py']
    """
    async def _read_one(p: str) -> str:
        return await asyncio.to_thread(get_file_content, p)

    outcomes = await asyncio.gather(*(_read_one(p) for p in paths), return_exceptions=True)
    sections = []
    for p, outcome in zip(paths, outcomes):
        if isinstance(outcome, BaseException):
            sections.append(f"=== {p} ===\nERROR: {outcome}")
        else:
            sections.append(f"=== {p} ===\n{outcome}")
    return _cap("\n\n".join(sections))


async def search_files_batch(pattern: str, glob_filters: list[str], max_results: int = 80) -> str:
    """
    Search the SAME pattern across multiple glob scopes in ONE tool call, in parallel --
    use this instead of several separate search_files() calls when checking a term
    across file types (e.g. '**/*.py' and '**/*.ts') or across multiple directories.

    Args:
        pattern:      Regex or literal string to search for (same as search_files)
        glob_filters: e.g. ['**/*.py', '**/*.ts'] -- one search per glob, in parallel
        max_results:  Max matching lines PER GLOB (default 80, same as search_files)
    """
    async def _search_one(g: str) -> str:
        return await asyncio.to_thread(search_files, pattern, g, max_results)

    outcomes = await asyncio.gather(*(_search_one(g) for g in glob_filters), return_exceptions=True)
    sections = []
    for g, outcome in zip(glob_filters, outcomes):
        if isinstance(outcome, BaseException):
            sections.append(f"=== glob: {g} ===\nERROR: {outcome}")
        else:
            sections.append(f"=== glob: {g} ===\n{outcome}")
    return _cap("\n\n".join(sections))


# When PROJECT_ROOT is a monorepo, short paths like "src/lib/**" are often relative to a
# subdirectory (e.g. a frontend root), not PROJECT_ROOT itself. Try these prefixes in
# order before giving up.
#
# Kept PROJECT-INDEPENDENT: "**" resolves src/lib/** to **/src/lib/** and works for any
# layout. A hardcoded subdirectory path used to sit here, which only helped one repo and
# was dead weight (or a wrong first guess) in every other. Projects needing an explicit
# prefix can set GLOB_FALLBACK_PREFIXES in their env file.
_GLOB_FALLBACK_PREFIXES = config.GLOB_FALLBACK_PREFIXES + ["**"]


def _rg_glob(rg: str, glob_pattern: str, max_results: int) -> list[str]:
    import subprocess
    cmd = [rg, "--files", "--glob", glob_pattern] + _RG_EXCLUDES
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
    )
    if not result.stdout and result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip())
    return sorted([l for l in result.stdout.splitlines() if l.strip()][:max_results])


def find_files(glob_pattern: str, max_results: int = 200) -> str:
    """
    Find files by glob pattern. Returns paths relative to project root.
    Uses ripgrep when available (fast, respects .gitignore); falls back to pathlib.

    Short paths relative to the frontend root (e.g. 'src/lib/**') are automatically
    resolved via fallback prefixes — agents do not need to know the full path.

    Args:
        glob_pattern: e.g. '**/*.py', 'src/**/*.ts', '**/docker-compose*.yml'

    Examples:
        find_files('**/*.py')               → all Python files
        find_files('src/**/*.ts')           → TypeScript in src/ (auto-prefixed if needed)
        find_files('src/lib/api/services/**') → frontend service files (auto-prefixed)
        find_files('**/Dockerfile*')        → all Dockerfiles
        find_files('**/.env*')              → env files
    """
    import shutil
    rg = shutil.which("rg")
    if rg:
        try:
            matches = _rg_glob(rg, glob_pattern, max_results)
            if not matches:
                for prefix in _GLOB_FALLBACK_PREFIXES:
                    matches = _rg_glob(rg, f"{prefix}/{glob_pattern}", max_results)
                    if matches:
                        break
            if not matches:
                return f"No matches for: {glob_pattern}{_did_you_mean_glob(glob_pattern)}"
            return f"{len(matches)} result(s) for '{glob_pattern}':\n" + "\n".join(matches)
        except Exception:
            pass  # fall through to pathlib

    matches = []
    patterns_to_try = [glob_pattern] + [f"{p}/{glob_pattern}" for p in _GLOB_FALLBACK_PREFIXES]
    try:
        for pat in patterns_to_try:
            for p in sorted(PROJECT_ROOT.glob(pat)):
                rel = p.relative_to(PROJECT_ROOT).as_posix()
                if _is_ignored(rel):
                    continue
                matches.append(rel + "/" if p.is_dir() else rel)
                if len(matches) >= max_results:
                    break
            if matches:
                break
    except Exception as e:
        return f"find_files failed: {e}"

    if not matches:
        return f"No matches for: {glob_pattern}"
    return f"{len(matches)} result(s) for '{glob_pattern}':\n" + "\n".join(matches)


def search_files(pattern: str, glob_filter: str = "**/*", max_results: int = 80) -> str:
    """
    Search file contents with a regex pattern. Returns matching lines with path:line: content.
    Uses ripgrep when available (fast, respects .gitignore); falls back to Python re.

    Args:
        pattern:     Regex or literal string to search for
        glob_filter: Restrict to files matching this glob (e.g. '**/*.py', '**/*.ts')
        max_results: Max matching lines to return (default 80)

    Examples:
        search_files('def handle_', '**/*.py')        → Python handler functions
        search_files('import.*React', '**/*.tsx')     → React imports
        search_files('WRITE_REVIEW', '**/*.py')       → env var usages
    """
    import subprocess, shutil
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "-i", "--no-heading", "--max-count", "1"]
        # Only pass glob_filter when it NARROWS. Passing the catch-all "**/*" as an
        # --glob INCLUDE overrides ripgrep's .gitignore handling, so rg descends into
        # node_modules and every other ignored tree. Measured 2026-07-30 on a large repo:
        #   symbol A: with "**/*" 55.0s / 3 matches  -- without 11.8s / 0 matches
        #   symbol B: with "**/*" 45.7s / 611 matches -- without 12.7s / 140 matches
        # Both slower AND wrong: the extra "matches" were all inside vendored dependency
        # code, so an agent checking whether a symbol exists in the PROJECT was handed
        # evidence that it does. The search tool was manufacturing support for the
        # fabrication it is supposed to help catch.
        if glob_filter and glob_filter not in ("**/*", "**", "*"):
            cmd += ["--glob", glob_filter]
        cmd += _RG_EXCLUDES
        cmd.append(pattern)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError(result.stderr.strip())
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            if not lines:
                return f"No matches for: {pattern}"
            # rg output: path:line:content — reformat to path:line: content
            out = []
            for ln in lines[:max_results]:
                parts = ln.split(":", 2)
                if len(parts) == 3:
                    out.append(f"{parts[0]}:{parts[1]}: {parts[2]}")
                else:
                    out.append(ln)
            text = "\n".join(out)
            if len(text) > _MAX_OUTPUT_CHARS:
                text = (text[:_MAX_OUTPUT_CHARS]
                        + f"\n... TRUNCATED at {_MAX_OUTPUT_CHARS} chars "
                          f"({len(lines)} matching files). Narrow `pattern` or pass a "
                          f"`glob_filter` such as '**/*.tsx'.")
            return text
        except subprocess.TimeoutExpired:
            # Do NOT fall through. The Python fallback re-reads the whole tree with no
            # timeout, so a 30s rg timeout escalated into a multi-minute hang — that is
            # what blew the agent's 300s MCP deadline on 2026-07-30 and left it answering
            # from priors instead of the repo. Fail fast so it narrows and retries.
            return (f"search timed out after 30s for pattern: {pattern!r}. "
                    f"Narrow it, or pass a `glob_filter` like '**/*.py' or '**/*.tsx'.")
        except Exception:
            pass  # genuine rg failure (not a timeout) — fall through to Python re

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Invalid regex: {e}"

    results = []
    try:
        for rel in _walk_project():
            if not _matches_glob(rel, glob_filter):
                continue
            fpath = PROJECT_ROOT / rel
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if regex.search(line):
                            results.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(results) >= max_results:
                                return _cap("\n".join(results))
            except Exception:
                continue
    except Exception as e:
        return f"search_files failed: {e}"

    return _cap("\n".join(results)) if results else f"No matches for: {pattern}"


def count_matches(pattern: str, glob_filter: str = "**/*",
                  fixed_string: bool = False, ignore_case: bool = False) -> str:
    """
    Count occurrences of a pattern across files — DETERMINISTIC, computed by ripgrep.

    USE THIS FOR ANY count / total / "how many" / "all" question over file contents.
    NEVER count by reading a file and tallying in your head — that is unreliable and is
    treated as a fabrication. This tool returns the EXACT total (occurrences, matching
    grep -oE ... | wc -l) plus a per-file breakdown. The first line is always
    "TOTAL: <n> ..." so the number can be read/relayed verbatim.

    Args:
        pattern:      regex to count (or a literal string if fixed_string=True).
        glob_filter:  restrict to files matching this glob, e.g. '**/*.py',
                      '**/gst_resolver.py'. Default '**/*' = every file.
        fixed_string: treat pattern as a literal string, not a regex.
        ignore_case:  case-insensitive match.

    Examples:
        count_matches(': *12\\.0', '**/gst_resolver.py')  → occurrences of ': 12.0'
        count_matches('def ', '**/*.py')                  → function-def lines
        count_matches('TODO', '**/*', fixed_string=True)  → literal TODO count
    """
    import subprocess, shutil
    rg = shutil.which("rg")
    if not rg:
        return "count_matches unavailable: ripgrep (rg) not installed in this environment"
    args = [rg, "--count-matches", "--glob", glob_filter] + _RG_EXCLUDES
    if fixed_string:
        args.append("-F")
    if ignore_case:
        args.append("-i")
    args.append(pattern)
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
        )
        # rg exit: 0 = matches, 1 = no matches, 2+ = real error
        if result.returncode not in (0, 1):
            return f"count_matches failed: {result.stderr.strip() or 'rg error'}"
        total = 0
        per_file: list[tuple[str, int]] = []
        for ln in result.stdout.splitlines():
            path, sep, cnt = ln.rpartition(":")
            if not sep:
                continue
            try:
                c = int(cnt)
            except ValueError:
                continue
            total += c
            per_file.append((path, c))
        head = (f"TOTAL: {total} match(es) for pattern {pattern!r} in '{glob_filter}' "
                f"across {len(per_file)} file(s)")
        if not per_file:
            return f"TOTAL: 0 match(es) for pattern {pattern!r} in '{glob_filter}'"
        body = "\n".join(f"  {p}: {c}" for p, c in sorted(per_file, key=lambda x: -x[1])[:50])
        return head + "\n" + body
    except subprocess.TimeoutExpired:
        return "count_matches failed: timed out after 30s"
    except Exception as e:
        return f"count_matches failed: {e}"


def list_directory_tree(max_depth: int = 8) -> str:
    """
    Return the full directory tree of the project up to max_depth levels deep.
    Shows directories only (no individual files) — no result cap.
    Use this for any overview or structure question before drilling into specific directories.
    Prefer this over find_files('**/*') for structure questions — no cap, always complete.

    Args:
        max_depth: How many levels deep to traverse (default 8)

    Examples:
        list_directory_tree()    → full project structure, 8 levels deep
        list_directory_tree(2)   → top 2 levels only

    Default raised 3 -> 8 on 2026-09-02. At 3 this tool was STRUCTURALLY BLIND to the
    frontend routes: Client/EcommClient-Web/ekamweb/src/app is already 5 segments deep,
    and an actual route like .../src/app/(portal)/business/verification-status sits at 8.
    A model asking for the project structure got a tree that stopped four levels above
    the thing it was asked about.

    Battery T11 (2026-09-01) is the cost. Asked to trace a seller document upload, the
    run made 66 tool calls -- 20+ READMEs, docs/frontend.md -- and one
    list_directory_tree(max_depth=3), then answered with
    `src/app/seller/verification-status/page.tsx`. That path does not exist; the real one
    is (portal)/business/verification-status. "verification-status" appears ZERO times in
    that run's entire log, so it was never in any tool result -- the route was produced
    from pattern knowledge because the tree could not show the real one. Four battery runs,
    three T11 failures.

    Measured on this repo before changing it, so the cost is known rather than assumed:

        depth  3 ->  1,028 chars,  72 lines          sees the route dir: NO
        depth  7 ->  2,670 chars, 158 lines          NO
        depth  8 ->  4,038 chars, 213 lines, 3.8s    YES
        depth 10 ->  4,609 chars, 236 lines, 5.5s    YES (saturates)

    ~3 KB and ~3s to stop inventing paths, and the tree saturates by 10 so there is no
    cliff past this. Note this lists DIRECTORIES only at any depth -- it will show the
    real route directory, never page.tsx; citing a file still needs find_files/search_files.
    """
    lines = []

    def _recurse(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(path.iterdir())
        except PermissionError:
            return
        dirs = [c for c in children
                if c.is_dir() and c.name not in _IGNORE_DIRS and not c.name.startswith(".")]
        for d in dirs:
            indent = "  " * (depth - 1)
            lines.append(f"{indent}{d.name}/")
            _recurse(d, depth + 1)

    _recurse(PROJECT_ROOT, 1)
    if not lines:
        return "No directories found."
    return f"Project directory tree (dirs only, {max_depth} levels deep):\n" + "\n".join(lines)


# Ceilings for the tree search in _glob_hint_bases. A service directory sits at
# depth 2 in this layout (API/inventory-service), so 4 covers real cases with room
# to spare while keeping a miss cheap -- and a miss is the common outcome, since
# the search only runs when the configured prefixes already failed.
# Smallest page a ranged read will actually serve. Anything under this costs more in
# round-trips than the lines are worth; 40 is well below a sensible page (the skeleton
# hint suggests 200) and well above the limit=5 walk that motivated it.
_MIN_RANGED_READ_LINES = 40

_HINT_SCAN_MAX_DEPTH = 4
_HINT_SCAN_DIR_BUDGET = 4000


def _prefix_filtered_matches(part: str, siblings: list[str], n: int = 3) -> list[str]:
    """Near-miss candidates that share a beginning with what was asked for.

    difflib alone scores on letter overlap wherever it falls, which is how a
    Researcher looking for `backend/models/parties/` was told "Did you mean:
    krakend?" (live, T4 2026-08-24). `backend` and `krakend` share the subsequence
    `akend`, giving ratio 2*5/(7+7) = 0.714, comfortably past the 0.6 cutoff -- and
    krakend is the API gateway config directory, which has nothing to do with models.

    A suggestion that is confidently wrong is worse than none. The useful half of
    that message ("no 'backend' in ./") was already correct and stands on its own;
    the bad guess only teaches the agent to distrust the hint everywhere it IS right.

    The discriminator is not the ratio but WHERE the overlap sits. A real near-miss
    -- a typo, a plural, an extension -- shares its start:

        routes  -> router      prefix 'rout'   (the 2026-08-23 case)
        models  -> models.py   prefix 'models' (the 2026-08-24 case)
        backend -> krakend     prefix ''       (noise)

    Two characters is deliberately shallow: it keeps short real names workable while
    rejecting a match that shares nothing but its middle. Raising difflib's cutoff
    instead was the obvious alternative and is wrong -- `backend`/`krakend` at 0.714
    scores HIGHER than plenty of legitimate near-misses, so any cutoff that excludes
    it excludes them too.
    """
    import difflib
    candidates = difflib.get_close_matches(part, siblings, n=n * 3, cutoff=0.6)
    low = (part or "").lower()
    kept = [c for c in candidates
            if low[:2] and c.lower().startswith(low[:2])]
    return kept[:n]


def _glob_hint_bases(first_segment: str):
    """Directories a prefix-less pattern's first literal segment could refer to.

    Mirrors find_files' own fallback order so the hint can reach the same files the
    tool itself would have matched: the configured prefixes first, then the "**"
    fallback resolved as an actual tree search. Without this the hint generator is
    strictly less capable than the tool it explains -- see _did_you_mean_glob.

    Shallowest first, capped at 3: a repo can hold several directories of the same
    name (EkamApp has eight `models.py`), and the useful suggestion is the one
    nearest the root, not an exhaustive list.
    """
    bases = []
    for prefix in _GLOB_FALLBACK_PREFIXES:
        if prefix == "**":
            continue
        candidate = PROJECT_ROOT / prefix / first_segment
        if candidate.is_dir():
            bases.append(candidate)
    if bases:
        return bases

    # Bounded breadth-first, NOT PROJECT_ROOT.glob("**/name"). pathlib's ** walks the
    # entire tree before any filter can reject it, so on a repo with node_modules it
    # descends into all of it -- measured at over 180s for seven lookups here, on a
    # helper that runs on the failure path of every find_files. Pruning has to happen
    # during traversal, not after.
    #
    # Breadth-first also gives shallowest-first for free, which is the ordering the
    # caller wants anyway: nearest the root is the useful suggestion.
    found: list[Path] = []
    queue: list[tuple[Path, int]] = [(PROJECT_ROOT, 0)]
    scanned = 0
    while queue and scanned < _HINT_SCAN_DIR_BUDGET and len(found) < 3:
        current, depth = queue.pop(0)
        if depth >= _HINT_SCAN_MAX_DEPTH:
            continue
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        scanned += 1
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name.startswith(".") or entry.name in _IGNORE_DIRS:
                continue
            if entry.name == first_segment:
                found.append(Path(entry.path))
                if len(found) >= 3:
                    break
            queue.append((Path(entry.path), depth + 1))
    return found


def _did_you_mean_glob(glob_pattern: str) -> str:
    """Near-miss suggestion for a glob whose LITERAL directory prefix does not exist.

    Returns "" when there is nothing useful to say.

    Added 2026-08-23 after the same one-token path slip produced something worse than a
    dead end. A Researcher searched `API/business-service/routes/**/verification*.py`
    -- `routes/` plural, when the real directory is `router/` -- got "No matches", and
    concluded in its final answer that **no backend API route for seller verification
    has been implemented**. The route exists: business_admin_api.py:84,
    `@router.post("/verify/{business_id}")`. A wrong directory silently became "the
    feature does not exist", which is far more damaging than a wrong path, because it
    reads as a finding rather than a failure.

    list_directory got this treatment a day earlier; globs did not, and globs are what
    absence claims are usually built on.

    Only the leading LITERAL segments are checked -- everything up to the first segment
    containing a wildcard. A pattern like `**/*.py` has no literal prefix and is
    correctly left alone.
    """
    import difflib

    def _walk(cursor, segments: list[str], walked: list[str]) -> str:
        for part in segments:
            if any(ch in part for ch in "*?["):
                return ""                      # reached the wildcard with everything real
            candidate = cursor / part
            if candidate.exists():
                cursor = candidate
                walked = walked + [part]
                continue
            if not cursor.is_dir():
                return ""
            # Files included, not just directories (fixed 2026-08-23, same day, from a
            # live miss). A glob segment followed by ** must BE a directory, so it is
            # tempting to only offer directories -- but the useful answer is often that
            # the thing is a FILE. Live: find_files('API/business-service/models/**/*.py')
            # matched nothing and stayed silent because the real `models.py` is a flat
            # file and was filtered out of the candidates. "Did you mean: models.py?"
            # is exactly the correction needed, and withholding it left the agent to
            # keep guessing.
            siblings = [p.name for p in cursor.iterdir()
                        if not p.name.startswith(".") and p.name not in _IGNORE_DIRS]
            close = _prefix_filtered_matches(part, siblings)
            if not close:
                # Still "" here, NOT the bare locating message: the caller's next move
                # is to re-resolve this segment under the fallback prefixes, and that
                # is where 'inventory-service/models/**' finds API/inventory-service and
                # produces the good "Did you mean: models.py?" hint. Returning a message
                # from here short-circuits that -- caught live while making this change,
                # having turned a correct hint into a useless one. The bare locating
                # message is emitted once, at the end, only after every resolution has
                # failed.
                return ""
            shown = "/".join(walked) or "."
            return (f" — the path prefix does not exist: no '{part}' in {shown}/. "
                    f"Did you mean: {', '.join(close)}? "
                    f"A glob that matches nothing because its DIRECTORY is wrong is not "
                    f"evidence that the thing you are looking for is absent.")
        return ""

    try:
        raw = (glob_pattern or "").strip().strip("/")
        if not raw or "/" not in raw:
            return ""
        segments = raw.split("/")

        hint = _walk(PROJECT_ROOT, segments, [])
        if hint:
            return hint

        # Resolve the base the way find_files itself does (2026-08-24). find_files
        # retries a failed pattern under each of _GLOB_FALLBACK_PREFIXES, "**"
        # included, so 'inventory-service/router/**/*' resolves happily without the
        # real 'API/' prefix. This helper walked LITERALLY from PROJECT_ROOT, so the
        # same prefix-less pattern died on segment one -- no 'inventory-service' at
        # the root, no top-level sibling close enough to suggest -- and returned "".
        #
        # That asymmetry is the trap, not the missing prefix: prefix-less patterns
        # work often enough that an agent never learns its paths are malformed, and
        # then a genuinely wrong one comes back as a bare dead end with no correction.
        #
        # Live cost (T12, 2026-08-24): a Researcher searched
        # 'inventory-service/models/**/*.py', got "No matches", and reported that the
        # inventory service HAS NO MODELS and that its data models are all defined in
        # business-service. API/inventory-service/models.py is 32,437 bytes and
        # defines 33 SQLAlchemy classes -- Item, ItemCategory, StockLevel, Voucher,
        # Party and the rest. Handed the fully-qualified pattern, this function
        # already produced exactly the right correction ("Did you mean: models.py?");
        # it simply never got the chance.
        first = segments[0]
        if any(ch in first for ch in "*?["):
            return ""
        for base in _glob_hint_bases(first):
            hint = _walk(base, segments[1:],
                         list(base.relative_to(PROJECT_ROOT).parts))
            if hint:
                return hint

        # Nothing resolved and nothing worth suggesting -- say where the trail died
        # anyway. "No matches" alone reads as "the thing is absent"; naming the segment
        # that does not exist says the PATH was wrong, which is a different and far more
        # actionable statement. This is the half that was always correct in T4's
        # "no 'backend' in ./. Did you mean: krakend?" -- worth keeping now that the
        # bad guess beside it is gone.
        if not (PROJECT_ROOT / first).exists():
            return (f" — the path prefix does not exist: no '{first}' in ./. "
                    f"A glob that matches nothing because its DIRECTORY is wrong is not "
                    f"evidence that the thing you are looking for is absent.")
        return ""
    except Exception:
        return ""


def _did_you_mean(relative_path: str) -> str:
    """Near-miss suggestions for a path that does not exist, as a trailing clause.

    Returns "" when there is nothing useful to say, so callers can append it blindly.

    Added 2026-08-22 after two live runs died on one-token path guesses. A coordinator
    asked for `API/inventory-service/routers/__init__.py` -- plural, when the real
    directory is `router/` -- and, given only "Not found:", re-sent the identical
    delegation 54 times until the liveness watchdog killed the run 13 minutes in.
    Another invented `API/seller-service/` outright and burned its budget hunting it.
    A bare "Not found" is a dead end; the model has nothing to correct toward, so it
    guesses again. Naming the real sibling turns the dead end into a correction.

    get_file_content already does this for files. list_directory never did.
    """
    import difflib

    try:
        raw = (relative_path or "").strip().strip("/")
        if not raw:
            return ""
        parts = raw.split("/")
        # Walk down as far as the path IS real, then suggest siblings at the first
        # segment that isn't -- that is where the mistake actually is.
        cursor = PROJECT_ROOT
        for depth, part in enumerate(parts):
            candidate = cursor / part
            if candidate.exists():
                cursor = candidate
                continue
            if not cursor.is_dir():
                return ""
            siblings = [p.name for p in cursor.iterdir()
                        if not p.name.startswith(".") and p.name not in _IGNORE_DIRS]
            close = _prefix_filtered_matches(part, siblings)
            shown = "/".join(parts[:depth]) or "."
            # Same reasoning as the glob variant above: "no 'backend' in ./" is the
            # correction, and it survives a dropped suggestion.
            if close:
                return f" — no '{part}' in {shown}/. Did you mean: " + ", ".join(close) + "?"
            return f" — no '{part}' in {shown}/."
        return ""
    except Exception:
        return ""      # a suggestion is a nicety; never let it break the real answer


def list_directory(relative_path: str = "") -> str:
    """
    List the contents of a directory in the project.
    Useful for exploring unknown project structures.

    Args:
        relative_path: Path relative to project root (default: project root).
                       e.g. 'src/components', 'API/auth-service'
    """
    if relative_path and is_excluded(relative_path):
        return (
            f"list_directory blocked: '{relative_path}' is in an excluded path "
            f"(dependency tree, build output, or a project EXCLUDE_DIRS/EXCLUDE_GLOBS "
            f"entry). It is vendored/generated, not part of this project's own source."
        )
    target = PROJECT_ROOT / relative_path if relative_path else PROJECT_ROOT
    if not target.exists():
        return f"Not found: {relative_path}{_did_you_mean(relative_path)}"
    if not target.is_dir():
        return f"Not a directory: {relative_path}"

    entries = []
    try:
        for item in sorted(target.iterdir()):
            if item.name in _IGNORE_DIRS or item.name.startswith("."):
                continue
            marker = "[DIR] " if item.is_dir() else "[FILE]"
            entries.append(f"{marker} {item.name}")
    except Exception as e:
        return f"list_directory failed: {e}"

    if not entries:
        return f"Empty: {relative_path or '(project root)'}"
    return f"{relative_path or '(project root)'}  ({len(entries)} items):\n" + "\n".join(entries)
