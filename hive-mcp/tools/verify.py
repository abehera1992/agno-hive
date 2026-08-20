"""Deterministic claim checking — grep an answer's factual claims against the repo.

No LLM is involved, which is the entire point: a model cannot be trusted to audit its own
output, and a second model just adds a second thing that can hallucinate. This extracts
the checkable claims from a piece of text and greps for each one.

Why it exists (measured 2026-07-30). main.py already carries a MANDATORY instruction to
cite file+line from code actually read this run. Asked about a symbol that does not
exist, the swarm named a similarly-spelled one that does, invented its behaviour and an
endpoint to match, and returned in 5.6s having called no tool at all. Told explicitly to
grep first, the same swarm answered correctly in 14.3s. So the model verifies when
compelled and not otherwise, and instruction-level fixes have already been tried and
observed to fail. The remaining option is to check the output afterwards.

What it catches, honestly stated:
  * INVENTED SYMBOLS      — fully. A named function is either in the repo or it isn't.
  * WRONG LINE NUMBERS    — fully. Read the line, compare it to the claim.
  * INVENTED PATHS/ROUTES — fully, same as symbols.
  * WRONG-LOCATION QUOTES — fully, when the answer quotes what it claims is AT a cited
    line (a docstring, a literal). The line existing is not enough: content-checking
    reads the real lines around the citation and confirms the quoted text is actually
    there. Measured 2026-08-04: an answer cited `models.py` line 450-497 and quoted the
    docstring "L1 -- Legal entity / PAN-based." (triple-quoted in source) right after
    it -- a real docstring that genuinely exists in the file, just at line 236, not
    450. The old citation check
    passed this cleanly (450 <= 774 total lines, so the line "exists"); it never
    compared what the answer quoted against what is actually on that line.
  * MISATTRIBUTED SYMBOLS — NOT caught for claims with no quoted content. When a real
    single-item function is claimed to handle a batch, the symbol exists and only the
    claim about it is false. Deciding that needs to read intent, which is what a
    reviewer or a human is for. This tool marks the symbol FOUND and says nothing
    about the claim.
  * PROPOSED/NEW CODE — deliberately NOT checked as an existence claim. A symbol
    introduced by a "new/add/create/propose" cue in prose, or found only in a fenced
    code block when nothing was actually staged this task, is reported separately
    under PROPOSED and never counted toward the fabrication verdict — a task whose
    job is "propose the code changes needed" legitimately names things that don't
    exist yet, and that is not the same failure this tool exists to catch.
  * ORM `table.column` CLAIMS — a dotted claim whose joined literal string is not
    found falls back to checking the bare attribute name alone (whole-word, code
    only). If found, reported as SPLIT-FOUND, not NOT FOUND, and not counted toward
    the verdict — the relationship (does THIS table really have THIS column) is not
    verified, same acknowledged limit as MISATTRIBUTED SYMBOLS above, but a real
    attribute existing somewhere is stronger evidence than a joined-string miss alone.
  * hive-mcp'S OWN TOOL NAMES — a backticked mention of one of hive-mcp's registered
    tool names (see _MCP_TOOL_NAMES) is treated as the model naming which tool it
    called, not a code-symbol claim about the target project, and is excluded from
    checking entirely — same treatment as _NOISE.
Treat a clean report as "nothing provably invented", never as "the answer is correct".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import config
from config import PROJECT_ROOT

from .exclusions import rg_args, is_excluded, EXCLUDE_DIRS

# Backticked code spans are where models put symbols they are asserting exist.
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
# path/to/file.ext:123  — the citation form the instructions ask for.
_FILE_LINE_RE = re.compile(r"([A-Za-z0-9_\-./]+\.[A-Za-z0-9]{1,6}):(\d{1,6})")
# Labeled-prose line citations: "**Line:** 389", "line 389", "Line: 389". Measured
# 2026-08-03: a real answer wrote "**File:** `models.py`, **Line:** 389" instead of
# the compact "models.py:389" form -- _FILE_LINE_RE never matches that shape at all,
# so a fabricated line number (off by ~155 lines from the real one) sailed through
# unchecked. Paired with the nearest preceding backticked path below rather than
# requiring one exact label phrasing, since models write this a dozen different ways
# ("in `x` at line N", "File: `x`, Line: N", "`x`, line N") and enumerating every
# phrasing is a losing game; proximity to a real path is the only reliable signal.
# Widened 2026-08-20: "**Lines**: 91–135" (plural, a range) never matched the singular
# form below either -- a live groundedness re-test cited a 3-function-spanning range this
# way and it went unchecked even on a run where verify_claims otherwise ran cleanly. Now
# captures an optional second number so both range endpoints get registered as citations.
#
# The separator class is [\s:*]* (mixed), not [:\s]*\**\s* (ordered). The ordered form
# silently assumed the colon always precedes the bold markers -- true of "**Line:** 389"
# (the 2026-08-03 case it was written for) and false of "**Lines**: 91-135", the exact
# 2026-08-20 case this widening exists to catch. Caught by these tests before the second
# version shipped; the first version deployed that morning would have matched nothing.
_LABELED_LINE_RE = re.compile(
    r"\blines?[\s:*]*(\d{1,6})(?:\s*[-–—]\s*(\d{1,6}))?\b", re.IGNORECASE
)
_BACKTICK_PATH_RE = re.compile(r"`([A-Za-z0-9_\-./]+\.[A-Za-z0-9]{1,6})`")
# How far back (chars) to look for the path a labeled line number belongs to. Wide
# enough to span a markdown table cell or a short sentence, narrow enough that it
# won't grab an unrelated path mentioned several claims earlier.
_LABELED_LINE_WINDOW = 200
# How far FORWARD (chars) to look, after a citation, for the quoted content the
# answer says lives there. Real answers write the citation then the quote right
# after it ("models.py, line 450 -- `\"\"\"docstring\"\"\"`"); this has to be short
# enough that it does not reach past this citation into the NEXT one's own quote.
_CONTENT_QUOTE_WINDOW = 200
# A cited line is considered to match if the quoted content appears within this many
# REAL lines of it -- tolerates citing the class line vs. the docstring one line below
# it, without tolerating the ~90-to-215-line misses actually observed.
_LINE_TOLERANCE = 5
# A bare path (`some/file.ext`) found in the quote-search window is very likely the
# NEXT citation's own path, not content this citation is claiming -- exclude it or
# every citation "quotes" the following one's filename.
_PATHLIKE_RE = re.compile(r"^[A-Za-z0-9_\-./]+\.[A-Za-z0-9]{1,6}$")
# API routes, asserted constantly and invented almost as often. The prefixes come from
# config.ROUTE_PREFIXES because "/api" is a convention, not a rule — a project routing
# under /v1 or /graphql would otherwise have its routes silently skipped, and one routing
# everything under /api would be the only one this check protected. Empty config disables
# route checking; symbols and citations are unaffected.
_ROUTE_RE = (
    re.compile(r"((?:" + "|".join(re.escape(p) for p in config.ROUTE_PREFIXES)
               + r")/[A-Za-z0-9_\-/{}.]+)")
    if config.ROUTE_PREFIXES else None
)
# A bare identifier worth grepping: not prose, not a number.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
# Dotted member expressions (styles.warning, obj.field, mod.CONST). These carry the claim
# just as often as bare names and were being SKIPPED: a fabricated `styles.warning` passed
# a clean verdict because _IDENT_RE rejects the dot. Measured 2026-07-30 on a real miss.
_DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
# Extensions that are documentation, not code. A symbol found ONLY here is not proof the
# code defines it — a pattern file describing `handleSave` in a comment made a fabricated
# function name read as FOUND.
_DOC_EXTS = (".md", ".mdx", ".txt", ".rst", ".adoc")
# An escaped path parameter as it appears after re.escape(): \{id\}, \$\{id\}, :id
_PARAM_RE = re.compile(r"(?:\\$)?\\{[^}]*\\}|:[A-Za-z_][A-Za-z0-9_]*")

# Words that show up in backticks constantly and mean nothing on their own. Grepping
# them wastes a subprocess and returns thousands of hits.
_NOISE = {
    "true", "false", "null", "none", "string", "number", "boolean", "int", "str",
    "float", "bool", "dict", "list", "any", "void", "async", "await", "return",
    "const", "let", "var", "def", "class", "import", "export", "type", "interface",
    "get", "post", "put", "delete", "patch", "data", "error", "result", "value",
}

# Module/package prefixes common enough that `prefix.attribute` inside generated code
# is almost always a legitimate stdlib/framework call, not a project symbol worth
# checking. Without this, csv.writer / io.StringIO / datetime.now flood the report
# with NOT FOUND false positives — observed directly in a live test, 2026-08-01.
_STDLIB_PREFIXES = {
    "os", "sys", "io", "csv", "json", "re", "time", "logging", "subprocess",
    "pathlib", "datetime", "typing", "asyncio", "functools", "itertools",
    "collections",
}

# hive-mcp's OWN registered tool names (see main.py's _tool(...) calls). A backticked
# mention of one of these in an answer's prose is virtually always the model naming
# which tool it called ("verified using `search_files_batch`"), not a claim that a
# symbol by this name exists in the TARGET project being checked — these names live
# in hive-mcp's own source, never the project's. Measured live 2026-08-14:
# `search_files_batch` (a real, registered tool, hive-mcp/tools/context.py:401) was
# reported NOT FOUND because it was grepped against EkamApp's repo, where a hive-mcp
# tool name has no reason to appear. Kept as a hand-maintained set rather than
# imported from main.py: main.py conditionally registers integration tools
# (Notion/DB/migrations) behind env-var gates this module has no reason to satisfy
# just to check an answer's text.
_MCP_TOOL_NAMES = {
    "get_project_context", "get_file_content", "get_files_batch", "find_files",
    "search_files", "search_files_batch", "count_matches", "verify_claims",
    "list_skills", "load_skill", "list_directory", "list_directory_tree",
    "write_file", "apply_diff", "run_command",
    "run_shell", "run_docker", "get_env_info", "check_port", "list_processes",
    "bash_session_start", "bash_run", "bash_session_close", "bash_job_status",
    "bash_job_kill",
    "git_status", "git_log", "git_diff", "git_log_file", "git_blame",
    "index_project", "scan_project_context", "web_search", "web_fetch",
    "confirm_action", "reject_action",
    "notion_search", "notion_get_page", "notion_get_database_schema",
    "notion_query_database", "notion_items_in_sprint", "notion_get_item_with_relations",
    "notion_find_work_item", "notion_create_page", "notion_update_page_props",
    "notion_append_blocks", "notion_append_markdown", "notion_replace_section",
    "notion_update_block", "notion_delete_block", "notion_trash_page",
    "notion_update_content", "run_migration", "db_query", "db_schema",
    "delegate_task_to_member", "delegate_task_to_members", "get_member_information",
    "agno_run", "agno_list_teams", "get_context_section", "list_recent_files",
    "search_knowledge_graph", "get_graph_report",
    "lightrag_query",
}

# A backtick immediately preceded by a negation cue, or immediately followed by an
# explicit non-existence disclaimer, is the model correctly asserting the symbol is
# NOT real -- not claiming it is. verify_claims exists to catch fabricated EXISTENCE
# claims; flagging a correct "X does not exist" disclaimer as a NOT FOUND fabrication
# punishes honesty and can trigger an unnecessary correction retry. Measured live
# 2026-08-05: "Is named `statusBadge` (not `statusBadge.success`, etc., as those do
# not exist)" is a correct, hedged disclaimer that should never have been checked as
# a positive claim in the first place.
_NEGATION_BEFORE_RE = re.compile(
    r"\b(not|isn't|aren't|never|unlike|instead of|rather than|as opposed to)\s*[:,]?\s*$",
    re.IGNORECASE,
)
_NEGATION_AFTER_RE = re.compile(
    r"^\s*[,)]?\s*(does not|doesn't|do not|don't|isn't|aren't)\s+exist\b",
    re.IGNORECASE,
)
_NEGATION_WINDOW = 20


def _is_negated_claim(answer: str, start: int, end: int) -> bool:
    """True if the backticked span answer[start:end] (including the backticks) is
    being asserted NOT to exist, rather than asserted to exist. Checks a short window
    on both sides -- a negation word right before the opening backtick ("not `X`") or
    an explicit non-existence disclaimer right after the closing one ("`X` does not
    exist")."""
    before = answer[max(0, start - _NEGATION_WINDOW):start]
    if _NEGATION_BEFORE_RE.search(before):
        return True
    after = answer[end:end + _NEGATION_WINDOW + 20]
    return bool(_NEGATION_AFTER_RE.match(after))


# A "new/add/propose" cue shortly before a backticked span means the answer is
# describing code to CREATE, not asserting it already exists -- verify_claims exists
# to catch fabricated EXISTENCE claims, and a task whose whole point is "propose the
# code changes needed" will legitimately name symbols that do not exist yet. Measured
# live 2026-08-14: a read-only Phase-1 gap-analysis proposal (EkamApp parties module)
# wrote "Add handlers: `openAddLocationModal`, `handleAddRegistration`,
# `handleAddLocation`" and "Add a `stateOptions` list" -- both plain descriptions of
# NEW code to write -- and every one of those symbols was reported NOT FOUND with a
# "fix the answer before returning it -- this is fabrication" verdict, even though the
# answer's own text explicitly said to ADD them.
#
# Window is wider than negation's (_NEGATION_WINDOW=20) on purpose: a real answer
# states the cue ONCE ("Add handlers:") and then lists several comma-separated
# backticked names after it, not one cue per name -- the 2nd and 3rd names in that
# exact incident sit ~40-65 chars past the word "Add". Widening the window trades a
# small amount of precision (an unrelated "add" mentioned earlier in a long sentence
# could in principle suppress a real check) for closing a false-fabrication verdict
# that otherwise fires on nearly every code-proposal task -- the more common and more
# costly failure of the two, consistent with this file's own acknowledged limits (see
# the module docstring's MISATTRIBUTED SYMBOLS note: this tool has never claimed to
# read intent perfectly).
_PROPOSED_NEW_WINDOW = 90
_PROPOSED_NEW_CUE_RE = re.compile(
    r"\b(add(?:ing|ed|s)?|new|create[sd]?|creating|introduc(?:e|es|ing)|"
    r"propose[sd]?|proposing|required)\b",
    re.IGNORECASE,
)


def _is_proposed_new_claim(answer: str, start: int) -> bool:
    """True if the text shortly before this backticked span frames it as NEW code
    the answer is proposing to add, rather than an assertion that it already exists.
    See _PROPOSED_NEW_CUE_RE's comment for the window-width tradeoff."""
    before = answer[max(0, start - _PROPOSED_NEW_WINDOW):start]
    return bool(_PROPOSED_NEW_CUE_RE.search(before))


_CODE_DOTTED_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b")

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*?\n(.*?)```", re.S)
# Same fences as _FENCE_RE, but also captures the language tag (group 1) so
# _lint_code can tell a ```python block from a ```tsx one — see _COMPONENT_LANGS
# below. A separate regex rather than changing _FENCE_RE itself: _FENCE_RE's
# other caller (_code_idents) expects findall() to return bare code strings, not
# (lang, code) tuples, and there is no reason to touch that path for this fix.
_FENCE_WITH_LANG_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\s*?\n(.*?)```", re.S)
_PROPOSED_SUFFIX = ".hive_proposed"
# REQUIRE rules (e.g. "components must reference styles.x") only make sense for
# actual component files — applying them to every staged file regardless of type
# would flag a staged .py or .scss file for "missing" a React/JSX convention it
# was never going to have. FORBID rules have no such false-positive risk (a
# forbidden JSX pattern simply won't appear in an unrelated file) so those stay
# fully generic across every staged file, matching this tool's project-agnostic
# design elsewhere.
_COMPONENT_EXTS = (".tsx", ".jsx")
# Fenced-code-block language tags treated as "component code" for REQUIRE rules —
# kept narrow to match _COMPONENT_EXTS exactly (a bare ```ts/```js/```typescript/
# ```javascript block is not necessarily a React component, so it stays out,
# same as .ts/.js are excluded from _COMPONENT_EXTS above). An untagged fence
# (```\n...) also stays out — no language info to go on, so it degrades to the
# same non-component treatment.
_COMPONENT_LANGS = ("tsx", "jsx")
# How recent a *.hive_proposed file's mtime has to be to count as "part of this
# task". Generous enough for a genuinely long multi-step run, short enough to
# exclude anything left over from a previous session. Measured live 2026-08-05:
# an unscoped rglob across the whole project picked up a FOUR-DAY-OLD staged
# file from an unrelated earlier session (a GST-compliance edit to
# items_api.py). Its unrelated identifiers (GSTComplianceTask.category_id,
# services.barcode_service, ...) filled essentially the entire _MAX_CLAIMS cap,
# crowding out the actual identifiers from THIS task's own staged file, and the
# resulting verify_claims call -- now checking ~25 mostly-irrelevant symbols
# instead of a handful of relevant ones -- ballooned from the usual tens of
# seconds to 1324s for the whole run. Scoping by recency fixes both the
# correctness problem (wrong file's symbols reported as this task's claims)
# and the performance regression it caused.
_STAGED_FILE_MAX_AGE_SECONDS = 30 * 60


def _staged_files() -> list:
    """Files staged for review (*.hive_proposed) under PROJECT_ROOT, recently enough
    to plausibly belong to the current task rather than a forgotten earlier session.

    Walks with in-place dir pruning (same pattern as index.py's bootstrap walk)
    instead of Path.rglob(), which visits every directory before any filtering
    can happen. Confirmed live 2026-08-05: an unpruned rglob on EkamApp walked
    node_modules (27,405 files) and .venv (14,790 files) on every single
    verify_claims call, since a *.hive_proposed glob still has to inspect every
    file name in every directory to know none of them match. Three calls during
    one test took 183s/205s/233s -- an order of magnitude past the typical
    sub-30s -- with no staged-file pollution to blame (recency scoping below was
    already correctly excluding the one stale leftover file present at the time).
    """
    import time
    try:
        cutoff = time.time() - _STAGED_FILE_MAX_AGE_SECONDS
        out: list = []
        for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
            for filename in filenames:
                if not filename.endswith(_PROPOSED_SUFFIX):
                    continue
                p = Path(dirpath) / filename
                try:
                    rel = p.relative_to(PROJECT_ROOT).as_posix()
                except ValueError:
                    continue
                if is_excluded(rel):
                    continue
                try:
                    if p.stat().st_mtime >= cutoff:
                        out.append(p)
                except OSError:
                    continue
        return out
    except Exception:
        return []


# A module alias from "@use '...' as alias;" -- the name every shared symbol in this
# file is supposed to be referenced through (e.g. "index" in "@use '@/styles/_index'
# as index;").
_SCSS_USE_ALIAS_RE = re.compile(r'@use\s+["\'][^"\']+["\']\s+as\s+([A-Za-z_][A-Za-z0-9_]*)')
# "@use '...' as *;" -- a wildcard import legitimises bare $variable references from
# that module, so a file with one gets none of the checks below.
_SCSS_WILDCARD_USE_RE = re.compile(r'@use\s+["\'][^"\']+["\']\s+as\s+\*')
_SCSS_VAR_TOKEN_RE = re.compile(r'\$[A-Za-z_][A-Za-z0-9_-]*')
# "$foo:" -- a variable's OWN declaration, not a usage. Also collects the set of
# names this file legitimately owns and may reference bare.
_SCSS_VAR_DECL_RE = re.compile(r'(\$[A-Za-z_][A-Za-z0-9_-]*)\s*:')
_SCSS_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SCSS_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _strip_scss_comments(text: str) -> str:
    """Blank out comment content with same-length whitespace (preserving every
    other character's position, in case anything downstream ever needs offsets)
    so a $variable merely MENTIONED in a comment -- documenting a hypothetical
    future variant, describing what NOT to do, anything -- is never treated as a
    real usage needing a namespace prefix. Measured live 2026-08-06: a Coder left
    a genuinely reasonable comment listing hypothetical future .statusBadge
    variants ("* .statusBadge.info { background: $info-bg; color: $info; }"), and
    the bare-variable scan, which does not understand SCSS comment syntax, flagged
    every variable name mentioned inside it as a real compile error alongside the
    one genuine mismatch in the actual, live rule body."""
    text = _SCSS_BLOCK_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)
    return _SCSS_LINE_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)


def _lint_scss_namespace(rel: str, text: str) -> list[str]:
    """Flag a bare $variable in a file whose ONLY imports are named aliases (no
    wildcard) when that variable is not locally declared in the same file -- it
    cannot resolve to anything else.

    Two tiers of confidence, both reported the same way (a compile-time error is a
    compile-time error regardless of which tier caught it):
    - SPECIFIC: the same variable name already appears correctly prefixed
      elsewhere in this file (alias.$var) -- direct proof of the missed prefix.
    - GENERAL: no such evidence exists (the variable is used bare for the first
      time anywhere in the file), but the file still has no wildcard import and no
      local declaration for it, so a bare reference cannot resolve regardless.
      Measured live 2026-08-06: a Coder used bare $info-bg/$info-dark for the FIRST
      time in a file whose only prior variable usages were $success-family names --
      the SPECIFIC check had no "elsewhere" evidence for $info-* to compare
      against, so it stayed silent on a real compile error.

    A prose "match the file's own convention" instruction was tried first (2026-08-05)
    and measured inconsistent live: correct on one run, wrong again on the very next
    run of the identical task. verify_claims' existing CODE_LINT_FORBID/REQUIRE rules
    are project-configured regexes and can't express "consistent with THIS file's own
    usage" -- that needs the file's own content as the reference, which only a
    per-file structural check like this one can do.
    """
    if not rel.endswith((".scss", ".sass")):
        return []
    aliases = sorted(set(_SCSS_USE_ALIAS_RE.findall(text)))
    if not aliases or _SCSS_WILDCARD_USE_RE.search(text):
        return []

    text = _strip_scss_comments(text)  # a variable merely MENTIONED in a comment is not a real usage

    # SPECIFIC evidence: variable name -> alias it's already seen prefixed with.
    known_alias_for: dict[str, str] = {}
    for alias in aliases:
        prefixed_re = re.compile(rf'\b{re.escape(alias)}\.(\$[A-Za-z_][A-Za-z0-9_-]*)')
        for m in prefixed_re.finditer(text):
            known_alias_for.setdefault(m.group(1), alias)

    locally_declared = set(m.group(1) for m in _SCSS_VAR_DECL_RE.finditer(text))

    flagged: dict[str, str | None] = {}
    for m in _SCSS_VAR_TOKEN_RE.finditer(text):
        var = m.group(0)
        if re.match(r"\s*:", text[m.end():m.end() + 3]):
            continue  # this occurrence IS the declaration, not a usage
        if any(text[:m.start()].endswith(a + ".") for a in aliases):
            continue  # already correctly prefixed right here
        if var in locally_declared:
            continue  # this file legitimately owns it
        if var not in flagged:
            flagged[var] = known_alias_for.get(var)

    out: list[str] = []
    for var, alias in sorted(flagged.items()):
        if alias:
            out.append(
                f"NAMESPACE MISMATCH in {rel}: bare {var} used, but this file "
                f"already references it as {alias}.{var} elsewhere — add the "
                f"'{alias}.' prefix here too (a bare reference is likely undefined "
                f"in this file's own scope and will fail to compile)."
            )
        else:
            out.append(
                f"NAMESPACE MISMATCH in {rel}: bare {var} used, but this file only "
                f"imports named modules ({', '.join(aliases)}) with no wildcard "
                f"import and {var} is not declared locally in this file — a bare "
                f"reference cannot resolve and will fail to compile. Prefix it with "
                f"whichever of {', '.join(aliases)} actually exports it."
            )
    return out


def _lint_text(text: str, label: str, check_require: bool) -> list[str]:
    out = []
    for rule in config.CODE_LINT_FORBID:
        pat, _, msg = rule.partition("::")
        try:
            if re.search(pat, text):
                out.append(f"FORBIDDEN pattern {pat!r} in {label} — {msg or 'violates project convention'}")
        except re.error:
            continue
    if check_require:
        for rule in config.CODE_LINT_REQUIRE:
            pat, _, msg = rule.partition("::")
            try:
                if not re.search(pat, text):
                    out.append(f"MISSING required pattern {pat!r} in {label} — {msg or 'required by project convention'}")
            except re.error:
                continue
    return out


def _lint_code(answer: str) -> list[str]:
    """Check fenced code blocks AND currently-staged files against project convention
    rules. Returns violations.

    Existence checking cannot catch a convention breach: emitted Tailwind classes are not
    a nonexistent symbol, they are the wrong system for this repo, and grep has nothing to
    flag. Measured 2026-07-31: a component answer using bare className strings passed
    verification cleanly while being unusable in the project.

    Staged-file scanning added 2026-08-05: the fenced-code-block check above only sees
    code the model chooses to echo back in its prose answer. A write_file() call that
    creates a brand-new file and then summarizes in plain narrative ("I've created the
    party detail page...") never puts the actual code in front of this check at all --
    confirmed live: a new page.tsx used bare Tailwind classNames and shadcn/ui components
    instead of this project's mandatory SCSS-module convention, and verify_claims reported
    zero conventions violations, not because the code was clean but because there was no
    fenced block to scan. Scanning *.hive_proposed directly closes that gap regardless of
    whether the model narrates or pastes the code.
    """
    out: list[str] = []

    # FORBID rules apply to every fenced block regardless of language (same reasoning
    # as _staged_files() below: no false-positive risk). REQUIRE rules (e.g. "components
    # must reference styles.x") are scoped to component-language blocks only — confirmed
    # live 2026-08-09: a pure-backend Python answer with zero frontend code was flagged
    # "MISSING required pattern styles\." because the old check joined every fenced
    # block regardless of language and ran REQUIRE against the lot unconditionally, the
    # exact false positive _staged_files() below was already built to avoid for staged
    # files (see test_require_rule_not_applied_to_non_component_staged_file) — this
    # fence-block path just never got the same treatment.
    blocks = _FENCE_WITH_LANG_RE.findall(answer or "")
    if blocks:
        all_code = "\n".join(code for _lang, code in blocks)
        out += _lint_text(all_code, "the answer's code block(s)", check_require=False)
        component_code = "\n".join(code for lang, code in blocks if lang.lower() in _COMPONENT_LANGS)
        if component_code:
            # check_require=True also re-runs the FORBID rules on this subset, which
            # would duplicate whatever the all_code pass above already reported — keep
            # only the REQUIRE ("MISSING required pattern") violations from this call.
            out += [
                v for v in _lint_text(component_code, "the answer's code block(s)", check_require=True)
                if v.startswith("MISSING required pattern")
            ]

    for path in _staged_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        try:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            rel = str(path)
        is_component = path.name.removesuffix(_PROPOSED_SUFFIX).endswith(_COMPONENT_EXTS)
        out += _lint_text(text, rel, check_require=is_component)
        out += _lint_scss_namespace(rel.removesuffix(_PROPOSED_SUFFIX), text)

    return out


def _code_idents(answer: str) -> list[str]:
    """Dotted attribute-access tokens (item.stock_quantity, Model.field) found INSIDE
    fenced code blocks or currently-staged files — not the prose summary around them.

    verify_claims previously only read backticked spans in prose (see the "no
    checkable claims found" message below), so a fabricated attribute that only
    appeared in the generated code — never restated in backticks in the summary —
    passed every check. Measured 2026-08-01: two independent write tasks both used
    `item.stock_quantity` / `Item.stock_quantity`, a field that does not exist
    anywhere in the project, and neither was caught, because the fabricated name
    never appeared outside the ```python block.

    Staged-file scanning added 2026-08-05, same reasoning and same fix shape as
    _lint_code below: a write_file() task referenced `styles.card`, `styles.fieldRow`,
    and `styles.value` in a brand-new page.tsx, none of which exist in the .module.scss
    it imported (only `.label` and `.badge` are real classes there) -- all three would
    resolve to `undefined` at runtime, rendering completely unstyled. The final answer
    was pure narrative with no code pasted in, so this check had nothing to scan and
    missed all three.
    """
    idents: list[str] = []
    for block in _FENCE_RE.findall(answer or ""):
        for tok in _dotted_idents(block):
            if tok not in idents:
                idents.append(tok)
    for path in _staged_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for tok in _dotted_idents(text):
            if tok not in idents:
                idents.append(tok)

    return idents


def _dotted_idents(text: str) -> list[str]:
    """Dotted attribute-access tokens (item.stock_quantity, Model.field) in `text`.
    Extracted from _code_idents (2026-08-14) so _proposed_code_block_idents below can
    reuse the identical extraction/filtering logic per-fence instead of per-answer."""
    out: list[str] = []
    for m in _CODE_DOTTED_RE.finditer(text):
        # A dotted-looking match immediately preceded by ANOTHER "." is a chained
        # CSS/SCSS class selector (".foo.bar" -- "element needs both classes"),
        # not a dotted member-access expression. Measured live 2026-08-05: staged
        # CSS ".statusBadge.success { ... }" produced a "statusBadge.success"
        # match indistinguishable from a real dotted identifier, and since that
        # exact compound-selector text can never appear anywhere BUT the file
        # currently being staged (it's brand new), it always reports NOT FOUND --
        # flagging correct, newly-introduced CSS as fabrication and triggering an
        # unnecessary correction retry.
        if m.start() > 0 and text[m.start() - 1] == ".":
            continue
        tok = m.group(0)
        left = tok.split(".", 1)[0].lower()
        if left in _STDLIB_PREFIXES or left in _NOISE:
            continue
        if tok not in out:
            out.append(tok)
    return out


# How far back (chars) to look before a fenced code block's opening ``` for a
# new/add/propose cue heading -- generous enough for a markdown heading line plus a
# blank line ("##### Proposed Code Insertion\n\n```tsx"), narrow enough that it
# won't reach back into a PRIOR fence's own trailing prose.
_PROPOSED_FENCE_WINDOW = 200


def _proposed_code_block_idents(answer: str) -> set[str]:
    """Dotted identifiers from a fenced code block in the answer's own prose whose
    IMMEDIATELY PRECEDING text carries a new/add/propose cue (_PROPOSED_NEW_CUE_RE)
    -- e.g. a heading like "Proposed Code Insertion" or "Required State Variables
    (add to ...)" right before the fence. These are the answer's own new-code
    illustration, not an existence claim (see _is_proposed_new_claim's docstring for
    the live incident this closes).

    Deliberately scoped to the answer's fences ONLY, never *.hive_proposed staged
    file content -- a symbol that made it into an actually-staged file is real code
    about to ship and must stay on the strict _code_idents path unconditionally,
    same as before this fix.

    A fence with no such preceding cue (a plain "here is the existing code" snippet)
    is excluded here and stays on the normal strict path via _code_idents -- an
    earlier version of this fix gated on whether ANYTHING was staged anywhere in the
    project, which correctly caught the real incident but also wrongly relabeled a
    plain "show me how X is used" quote of EXISTING code as a new-code proposal,
    since a read-only Q&A call also has no staged file. Checking each fence's own
    preceding text instead of a project-wide toggle fixes both at once.
    """
    idents: set[str] = set()
    for m in _FENCE_RE.finditer(answer or ""):
        window = answer[max(0, m.start() - _PROPOSED_FENCE_WINDOW):m.start()]
        if _PROPOSED_NEW_CUE_RE.search(window):
            idents.update(_dotted_idents(m.group(1)))
    return idents


_MAX_CLAIMS = 25   # subprocess per claim; keep the whole check inside a few seconds


def _rg(pattern: str, fixed: bool = True, glob_filter: str = "",
        whole_word: bool = False) -> list[str]:
    rg = shutil.which("rg")
    if not rg:
        return []
    cmd = [rg, "-n", "--no-heading", "--max-count", "1"]
    if fixed:
        cmd.append("-F")
    if whole_word:
        # Without -w, a claimed `handleSave` matches an unrelated `handleSaveRole` and
        # the fabrication reads as FOUND.
        cmd.append("-w")
    if glob_filter:
        cmd += ["--glob", glob_filter]
    cmd += rg_args()
    cmd.append(pattern)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=str(PROJECT_ROOT), timeout=20)
        return [l for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def _resolve_path(rel_path: str, hint_paths: list[str] | None = None) -> tuple[str | None, int]:
    """Resolve a cited path to a real repo file. Returns (resolved_path, n_candidates).

    Agents cite bare filenames ("someModule.ts:468") far more often than full
    repo-relative paths, and PROJECT_ROOT/"someModule.ts" does not exist — so a correct
    citation was being reported BAD. A false positive is the worst failure this tool can
    have: it teaches agents that the checker is noise, and then the real fabrications get
    ignored too. Resolve by suffix before declaring anything bad.

    hint_paths: other full repo-relative paths already named in the SAME answer (e.g. an
    intro sentence says "defined in `API/inventory-service/models.py`" and every citation
    after it just says "models.py:258"). Measured live 2026-08-04: EkamApp has 8 files
    named models.py, one per service, so a bare "models.py" citation was ALWAYS reported
    AMBIGUOUS even when the answer had already stated, in the same breath, exactly which
    one it meant — and being stuck at AMBIGUOUS meant the citation never even reached the
    line-content check below, so a wrong line number sailed through unverified. If exactly
    one candidate matches a hint the answer already gave for this basename, that is a
    stronger signal than "ambiguous, give up".
    """
    p = PROJECT_ROOT / rel_path
    if p.is_file():
        return rel_path, 1
    rg = shutil.which("rg")
    if not rg:
        return None, 0
    cmd = [rg, "--files"]
    cmd += rg_args()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=str(PROJECT_ROOT), timeout=20)
    except Exception:
        return None, 0
    want = rel_path.replace("\\", "/").lstrip("./")
    cands = [l.replace("\\", "/") for l in r.stdout.splitlines()
             if l.replace("\\", "/").endswith("/" + want) or l.replace("\\", "/") == want]
    if len(cands) == 1:
        return cands[0], 1
    if len(cands) > 1 and hint_paths:
        basename = want.rsplit("/", 1)[-1]
        hinted = {h.replace("\\", "/").lstrip("./") for h in hint_paths
                  if h.replace("\\", "/").endswith("/" + basename)}
        exact = [c for c in cands if c in hinted]
        if len(exact) == 1:
            return exact[0], 1
    return (None, len(cands))


def _read_line(rel_path: str, lineno: int) -> str | None:
    p = PROJECT_ROOT / rel_path
    try:
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i == lineno:
                    return line.rstrip("\n")
    except Exception:
        return None
    return None


_MD_HEADING_RE = re.compile(r"(?:^|\n)[ \t]{0,3}#{1,6}[ \t]")


def _find_nearby_quote(answer: str, pos: int) -> str | None:
    """The quoted content, if any, that a file:line citation is claiming lives there.

    Looks forward from the end of the citation match for the next backticked span --
    the shape real answers use ("models.py, line 450 -- `\"\"\"docstring\"\"\"`"). Skips
    bare paths (almost always the NEXT citation's own filename, not this one's content)
    and anything too short/generic to mean a specific location.

    Also skips across a markdown heading. Measured live 2026-08-05: a correct citation
    ("...models.py:293`") was immediately followed by "\n\n## Introduction History\n
    ...introduced in commit `af635cc`" -- the heading starts a new section about
    something else entirely, but fell inside the search window, so the commit hash
    got paired with the PartyLocation citation and reported a MISMATCH against file
    content for a claim the citation was never making. A heading is section-changed
    prose; a quote past one belongs to whatever comes after it, not to this citation.

    Also skips when the citation itself was written as a single backtick-wrapped
    token ("`inventoryApi.ts:101`"). Measured live 2026-08-05: pos lands right before
    that citation's OWN closing backtick, which the search below would treat as an
    OPENING delimiter and pair with the next unrelated backtick further along,
    capturing plain prose between them as a bogus "quote" -- a real answer's "...or
    `inventoryApi.ts:101` exists in the codebase -- the `getParty` endpoint..." had
    the prose "exists in the codebase -- the" reported as MISMATCH content, when
    nothing about that citation was ever quoting anything.
    """
    if answer[pos:pos + 1] == "`":
        return None
    window = answer[pos: pos + _CONTENT_QUOTE_WINDOW]
    m = _BACKTICK_RE.search(window)
    if not m:
        return None
    if _MD_HEADING_RE.search(window[:m.start()]):
        return None
    quoted = m.group(1).strip()
    if len(quoted) < 4 or quoted.lower() in _NOISE or _PATHLIKE_RE.match(quoted):
        return None
    return quoted


def _read_window(rel_path: str, center_line: int, span: int) -> str:
    """Real file text within `span` lines of center_line, for a location check."""
    p = PROJECT_ROOT / rel_path
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    lo = max(0, center_line - 1 - span)
    hi = min(len(lines), center_line - 1 + span)
    return "\n".join(lines[lo:hi])


# Detects a stuck retry loop: the exact same answer text checked twice in a row,
# with nothing revised in between. Module-level and intentionally coarse, mirroring
# hive-mcp/tools/files.py's _last_failed_call breaker for apply_diff. Measured
# 2026-08-01: a live task called verify_claims 3 times on the byte-identical draft
# answer (interleaved with re-reading files that never changed what got submitted),
# ~50s each, before the run was cancelled — verify_claims itself never told the
# caller that re-checking unchanged text is pointless.
_last_checked_answer: str | None = None
_repeat_count = 0


def verify_claims(answer: str, glob_filter: str = "") -> str:
    """
    Check an answer's factual claims against the repository. Read-only, no approval.

    Run this on your OWN answer before returning it whenever you have named a symbol,
    a file:line, or an API route. It greps for each claim and reports what does not
    exist. If something comes back NOT FOUND, correct the answer — do not ship it.

    Args:
        answer:      The text to check (your drafted answer, or another agent's).
        glob_filter: Optional glob to narrow the search, e.g. '**/*.tsx'. Leave empty
                     to search the whole project.

    Limits: proves EXISTENCE, not correctness. A symbol that exists but does not do what
    the answer claims will pass — this cannot read intent. When a citation is quoted
    ("`path`, line N -- `quoted text`"), the quoted text's actual location IS checked
    against line N — but a bare, unquoted citation still only proves the line exists.
    """
    global _last_checked_answer, _repeat_count
    if not answer or not answer.strip():
        return "verify_claims: nothing to check (empty answer)."

    if answer == _last_checked_answer:
        _repeat_count += 1
    else:
        _last_checked_answer = answer
        _repeat_count = 0

    if _repeat_count >= 1:
        _last_checked_answer = None  # reset — a later distinct check isn't blocked
        _repeat_count = 0
        # Deliberately contains "could NOT be found", the exact phrase
        # swarm/team.py's _verify_claims uses to classify a report as "bad"
        # ("could NOT be found" in report). That orchestrator-level guard calls
        # this tool up to twice — an initial check, then a recheck of a correction
        # round — and if the correction genuinely changes nothing, its second call
        # lands here. It must still see this as bad: the underlying claims were
        # never actually fixed, only re-submitted, so treating a stuck repeat as
        # "verified good" would silently drop the fabrication disclaimer the
        # orchestrator would otherwise attach.
        return (
            "verify_claims STOPPED: this exact answer text was already checked and "
            "its claims could NOT be found in the project — checking it again "
            "unchanged will not help. Either (a) revise the answer based on the "
            "previous report's findings — read more of the codebase and change "
            "what you are claiming, or (b) if you cannot find a grounded answer "
            "after that, say so plainly instead of re-submitting the same draft."
        )

    # ── collect candidate claims ──────────────────────────────────────────────
    idents: list[str] = []
    # Symbols the answer itself frames as new/proposed code -- reported separately
    # below, never counted toward the fabrication verdict. See _is_proposed_new_claim
    # and the have_staged comment just below for the two ways a symbol lands here.
    proposed_idents: list[str] = []
    for m in _BACKTICK_RE.finditer(answer):
        span = m.group(1)
        tok = span.strip().rstrip("()").strip()
        if (_IDENT_RE.match(tok) or _DOTTED_RE.match(tok)) and tok.lower() not in _NOISE:
            if tok in _MCP_TOOL_NAMES:
                continue
            if _is_negated_claim(answer, m.start(), m.end()):
                continue
            if _is_proposed_new_claim(answer, m.start()):
                if tok not in idents and tok not in proposed_idents:
                    proposed_idents.append(tok)
                continue
            if tok not in idents:
                idents.append(tok)
    # Fenced-code-block identifiers whose block sits right after a new/add/propose
    # heading (e.g. "Proposed Code Insertion") go to proposed_idents too -- see
    # _proposed_code_block_idents' docstring. Everything else from _code_idents
    # (a plain code quote with no such heading, or anything from a *.hive_proposed
    # staged file, which that helper never scans) keeps the original strict path.
    proposed_fence_idents = _proposed_code_block_idents(answer)
    for tok in _code_idents(answer):
        if tok in idents or tok in proposed_idents:
            continue
        if tok in proposed_fence_idents:
            proposed_idents.append(tok)
        else:
            idents.append(tok)

    file_lines: list[tuple[str, int]] = []
    content_claims: dict[tuple[str, int], str] = {}
    for m in _FILE_LINE_RE.finditer(answer):
        path, num = m.group(1), int(m.group(2))
        pair = (path, num)
        if pair not in file_lines:
            file_lines.append(pair)
        if pair not in content_claims:
            quoted = _find_nearby_quote(answer, m.end())
            if quoted:
                content_claims[pair] = quoted

    # Labeled prose citations ("**File:** `x`, **Line:** 389") that never use the
    # compact path:line form above, and so never matched _FILE_LINE_RE at all. Pair
    # each labeled line number with the nearest preceding backticked path within a
    # bounded window — see _LABELED_LINE_RE's comment for why proximity, not a fixed
    # phrasing, is the matching strategy.
    backtick_paths = list(_BACKTICK_PATH_RE.finditer(answer))
    for m in _LABELED_LINE_RE.finditer(answer):
        line_start = m.start()
        nearest = None
        for p in backtick_paths:
            if p.start() >= line_start:
                break  # only paths mentioned BEFORE this line-number claim count
            if line_start - p.end() <= _LABELED_LINE_WINDOW:
                nearest = p
        if nearest is None:
            continue
        line_nums = [int(m.group(1))]
        if m.group(2):
            line_nums.append(int(m.group(2)))
        for num in line_nums:
            pair = (nearest.group(1), num)
            if pair not in file_lines:
                file_lines.append(pair)
            if pair not in content_claims:
                quoted = _find_nearby_quote(answer, m.end())
                if quoted:
                    content_claims[pair] = quoted

    routes: list[str] = []
    if _ROUTE_RE is not None:
        for r in _ROUTE_RE.findall(answer):
            r = r.rstrip(".,;)")
            # Skip file paths. "/api/services/inventory/inventoryApi.ts" was being
            # extracted out of a repo path and checked as if it were an HTTP route.
            last = r.rsplit("/", 1)[-1]
            if "." in last and not last.endswith("}"):
                continue
            if r not in routes:
                routes.append(r)

    if not (idents or file_lines or routes or proposed_idents) and not _lint_code(answer):
        return ("verify_claims: no checkable claims found (no backticked symbols, "
                "code-block attribute references, file:line citations, API routes, "
                "or convention violations).")

    out: list[str] = ["verify_claims — deterministic grep of the claims in this answer", ""]
    problems = 0
    # DOC ONLY items are tracked SEPARATELY from problems (2026-08-15, T1e live
    # incident, engineering team) -- see the DOC ONLY branch below for why they must
    # never count toward the fabrication verdict.
    doc_only_count = 0

    # ── symbols ───────────────────────────────────────────────────────────────
    if idents:
        out.append(f"SYMBOLS ({len(idents[:_MAX_CLAIMS])} checked):")
        for tok in idents[:_MAX_CLAIMS]:
            dotted = "." in tok
            hits = _rg(tok, fixed=True, glob_filter=glob_filter, whole_word=not dotted)
            code_hits = [h for h in hits
                         if not h.split(":", 1)[0].lower().endswith(_DOC_EXTS)]
            if not code_hits:
                # Dotted `owner.attribute` claims (a table/column, a class/field, a
                # struct/property -- this tool has no idea which, and does not need
                # to) never appear as one literal joined string whenever the
                # framework or language declares the "owner" and the "attribute" in
                # two separate statements rather than one inline expression -- true
                # of most ORMs and struct/schema definitions across most languages,
                # not any one of them specifically, which is why this fallback
                # parses none of that syntax and only ever looks at the identifier
                # AFTER the last dot. Confirmed live 2026-08-14 on one concrete case
                # (an EkamApp SQLAlchemy model, table name and column declared on
                # separate lines): `item_categories.sku_prefix` genuinely existed
                # (ItemCategory.sku_prefix, models.py:129) and the answer's claim was
                # correct, but `rg -F` for the joined string found nothing and this
                # tool reported real, correct code as fabrication. Before declaring
                # NOT FOUND, fall back to the bare attribute name alone (whole-word,
                # code only) -- if THAT exists, this is the same MISATTRIBUTED
                # SYMBOLS blind spot the module docstring already accepts (proves
                # existence, not the claimed relationship) rather than an invented
                # symbol, so it is reported distinctly and does not count toward the
                # fabrication verdict.
                #
                # Gated on `not code_hits`, not `not hits` -- a live re-test the SAME
                # day this fallback shipped found the gap: CLAUDE.md itself came to
                # quote the literal string `item_categories.sku_prefix` (documenting
                # this exact incident), which made `hits` non-empty on the very next
                # run and skipped the fallback entirely, landing on DOC ONLY instead
                # of ever trying SPLIT-FOUND. A doc-only hit on the joined string is
                # exactly as uninformative about the real relationship as no hit at
                # all, so both cases now reach the same fallback.
                if dotted:
                    attr = tok.rsplit(".", 1)[-1]
                    attr_hits = _rg(attr, fixed=True, glob_filter=glob_filter, whole_word=True)
                    attr_code_hits = [h for h in attr_hits
                                       if not h.split(":", 1)[0].lower().endswith(_DOC_EXTS)]
                    if attr_code_hits:
                        out.append(
                            f"  SPLIT-FOUND {tok:36s} <-- \"{attr}\" exists in code "
                            f"({attr_code_hits[0][:70]}) but not joined as this exact "
                            f"dotted string; the table/class-attribute relationship "
                            f"itself is not verified"
                        )
                        continue
                if hits:
                    # NOT counted toward `problems` (2026-08-15, T1e live incident,
                    # engineering team): a symbol that appears in real project
                    # documentation is NOT fabrication just because this grep-based
                    # check cannot also confirm it as one literal string in code --
                    # it may be constructed dynamically (an f-string table name, a
                    # loop-generated identifier), or the documentation may name a
                    # convention/pattern rather than a source-literal symbol at all.
                    # Confirmed live: a real, correctly-cited documentation hit
                    # (patterns/ekam-frontend.md:1109, symbol
                    # inventory.party_module_settings) was counted as a `problems`
                    # hit here, which set _verify_claims' `bad=True` and drove
                    # swarm/team.py's one-shot correction retry to instruct the
                    # model "does not exist here... do not mention it again" --
                    # the model then discarded its own correct citation and
                    # answered that an entire real, documented pattern "does not
                    # exist". A DOC ONLY hit is evidence the thing IS real
                    # (documented), just not code-grep-confirmed -- the opposite of
                    # NOT FOUND, and must never carry the same "fabrication, fix
                    # before returning" verdict language.
                    doc_only_count += 1
                    out.append(f"  DOC ONLY   {tok:38s} <-- appears only in documentation, "
                               f"not in code: {hits[0][:70]} (this is a real citation, not "
                               f"fabrication -- the exact string was not independently "
                               f"confirmed in code, which can happen for dynamically "
                               f"constructed identifiers or documented conventions)")
                else:
                    problems += 1
                    out.append(f"  NOT FOUND  {tok:38s} <-- does not exist in the project")
                continue
            out.append(f"  FOUND      {tok:38s} {code_hits[0][:90]}")
        out.append("")

    # ── proposed / new code ──────────────────────────────────────────────────
    if proposed_idents:
        out.append(
            f"PROPOSED ({len(proposed_idents[:_MAX_CLAIMS])} shown) — the answer itself "
            f"frames these as new code to add, not a claim they already exist. Not "
            f"counted toward the verdict below:"
        )
        for tok in proposed_idents[:_MAX_CLAIMS]:
            out.append(f"  NEW        {tok}")
        out.append("")

    # ── file:line citations ───────────────────────────────────────────────────
    if file_lines:
        out.append(f"CITATIONS ({len(file_lines[:_MAX_CLAIMS])} checked):")
        all_paths_in_answer = [m.group(1) for m in _BACKTICK_PATH_RE.finditer(answer)]
        for path, num in file_lines[:_MAX_CLAIMS]:
            resolved, n_cands = _resolve_path(path, hint_paths=all_paths_in_answer)
            if resolved is None:
                problems += 1
                if n_cands > 1:
                    # Ambiguous is NOT fabrication — say so, or the tool cries wolf.
                    out.append(f"  AMBIGUOUS  {path}:{num} <-- {n_cands} files share that "
                               f"name; cite a repo-relative path")
                else:
                    out.append(f"  BAD        {path}:{num} <-- no such file in the project")
                continue
            line = _read_line(resolved, num)
            if line is None:
                problems += 1
                out.append(f"  BAD        {resolved}:{num} <-- file exists but has no line {num}")
            else:
                out.append(f"  LINE {num:<6} {resolved}")
                out.append(f"             | {line.strip()[:100]}")
                quoted = content_claims.get((path, num))
                if quoted is not None:
                    if quoted in _read_window(resolved, num, _LINE_TOLERANCE):
                        out.append(f"             content verified within {_LINE_TOLERANCE} lines")
                    else:
                        problems += 1
                        # {resolved}:{num} as the SECOND token, matching BAD/AMBIGUOUS's
                        # shape — swarm/team.py's retry-prompt builder extracts
                        # line.split()[1] as "the unsupported thing" by that convention;
                        # putting the word "quoted" there instead (an earlier version of
                        # this line did) made every MISMATCH invisible to the retry loop.
                        out.append(
                            f"  MISMATCH   {resolved}:{num} <-- quoted {quoted[:60]!r} not "
                            f"found within {_LINE_TOLERANCE} lines; citation and quoted "
                            f"content do not point at the same place"
                        )
        out.append("")

    # ── routes ────────────────────────────────────────────────────────────────
    if routes:
        out.append(f"ROUTES ({len(routes[:_MAX_CLAIMS])} checked):")
        for r in routes[:_MAX_CLAIMS]:
            # A router usually declares only a SUFFIX of the full URL — a gateway or an
            # app-level prefix supplies the rest — so the whole path rarely appears
            # verbatim in source. Probe progressively shorter suffixes and stop at the
            # first that matches, keeping {params} intact.
            #
            # Measured 2026-07-30, which is why it is a suffix walk and not a segment:
            #   single trailing segment  -> matched unrelated code (a common word)
            #   two-segment suffix       -> correctly absent for a fabricated route,
            #                               correctly present for a real one
            #   params stripped          -> correctly-cited real routes went missing
            # Requiring >= 2 segments avoids blessing a route because one common word in
            # it appears somewhere in the repo.
            # Path params are written three ways for the same route -- {id} in docs,
            # ${id} in a JS template literal, :id in some routers -- so a literal probe
            # for "items/{id}" misses a real "items/${id}". Probe the param as a regex
            # wildcard instead of a fixed string; a real route then matches whichever
            # form the source uses. Measured 2026-07-30: the literal form reported a
            # genuine endpoint as NOT FOUND, which is the false positive that teaches
            # agents to ignore this tool.
            segs = [s for s in r.split("/") if s]
            probe, hits = None, []
            for start in range(len(segs) - 1):
                cand = "/".join(segs[start:])
                pat = _PARAM_RE.sub(r"[^/\"'`]+", re.escape(cand))
                found = _rg(pat, fixed=False, glob_filter=glob_filter)
                if found:
                    probe, hits = cand, found
                    break
                if probe is None:
                    probe = cand   # report the most specific probe tried
            if hits:
                out.append(f"  PLAUSIBLE  {r:44s} (segment {probe!r} found)")
            else:
                problems += 1
                out.append(f"  NOT FOUND  {r:44s} <-- no trace of segment {probe!r}")
        out.append("")

    lint = _lint_code(answer)
    if lint:
        out.append(f"CONVENTIONS ({len(lint)} violation(s)):")
        for v in lint:
            out.append(f"  VIOLATION  {v}")
        out.append("")
        problems += len(lint)

    if problems:
        out.append(f"VERDICT: {problems} claim(s) could NOT be found in the project. "
                   f"Fix the answer before returning it — a NOT FOUND symbol or a BAD "
                   f"citation is fabrication, not a near miss.")
        if doc_only_count:
            out.append(f"NOTE: {doc_only_count} additional DOC ONLY item(s) above are NOT "
                       f"part of this verdict and do NOT need to be fixed or retracted — "
                       f"they are real citations found in project documentation, just not "
                       f"independently confirmed as a literal string in code.")
    elif doc_only_count:
        # A report containing ONLY DOC ONLY items (no real problems) must read as a
        # clean pass, not a fabrication warning -- this is the exact shape of the T1e
        # incident this whole section exists to fix. "could NOT be found" (the literal
        # substring swarm/team.py's _verify_claims checks for) must NOT appear here.
        out.append(f"VERDICT: no fabricated claims found. {doc_only_count} claim(s) are "
                   f"DOC ONLY (see above) — real documentation citations that a literal "
                   f"code grep could not independently confirm, which is expected for "
                   f"dynamically-constructed identifiers or documented conventions. This "
                   f"is not fabrication and does not need to be fixed.")
    else:
        out.append("VERDICT: every checked claim exists in the project. NOTE: this "
                   "proves existence only. It does NOT confirm the symbol does what the "
                   "answer says it does.")
    return "\n".join(out)
