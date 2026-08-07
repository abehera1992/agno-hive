import asyncio
import json
import re
import time
from contextlib import AsyncExitStack

from opentelemetry import trace
from agno.team import Team
from agno.tools.mcp import MCPTools
from .agents import make_coder, make_reviewer, make_agent_from_spec, get_model, format_skill_catalog
from .feedback import record_success, record_success_bg, record_failure, load_failure_context
from config.config import config

_tracer = trace.get_tracer("agno-hive.team")

_MCP_TIMEOUT = 300  # lightrag_query synthesis ~90-120s; large file reads over Docker bind mounts can be slow — headroom so multi-read tasks don't die mid-read

# agno_run/agno_list_teams only make sense triggered from the Claude-Code side (the
# project MCP); passing them to the coordinator would let it recurse back into this
# same swarm and deadlock, so they're excluded from the project MCP connection only.
#
# 2026-08-04/05: briefly widened this to also exclude get_file_content, find_files,
# search_files, list_directory_tree, list_directory, get_project_context, after
# finding those six duplicated on BOTH connected servers -- hive-mcp's copies (the
# ones the swarm is meant to use) AND stale leftovers still registered on the
# project MCP from before those tools were deliberately migrated to hive-mcp. That
# duplication was the actual bug: a line-numbering fix landed on hive-mcp's copy but
# calls may have kept landing on the project MCP's unfixed duplicate. The exclusion
# list was a workaround; the real fix was removing the stale duplicates from the
# project MCP's own registration (mcp-server/main.py in EkamApp) so they only exist
# in one place. With that done, excluding names the project MCP no longer serves
# would break its whole tool list (see the comment at the call site below) -- so
# this reverts back to its original, narrower scope.
_PROJECT_MCP_EXCLUDE_TOOLS = ["agno_run", "agno_list_teams"]

_COORDINATOR_INSTRUCTIONS = [
    "── Tool restrictions ────────────────────────────────────────────",
    "  NEVER call the `agno_run` tool — you are the top-level coordinator;",
    "  calling agno_run would recurse back into this same swarm and deadlock.",
    "  NEVER output a JSON object as a delegation mechanism (e.g. {\"name\": \"delegate_task_to_member\", ...}).",
    "  You have DIRECT access to all MCP tools (get_file_content, apply_diff, write_file, etc.).",
    "  For tasks that involve reading files and making changes: call MCP tools DIRECTLY.",
    "  CRITICAL: When making code changes, you MUST call apply_diff() — NEVER return modified file",
    "  content as text output. The workflow is: get_file_content() → analyze → apply_diff() → done.",
    "  NEVER write out the new file content as a response. ONLY call apply_diff() to stage changes.",
    "  When updating an import line: use the EXACT existing import line from the file as old_string.",
    "  Do NOT guess or hallucinate import paths — copy them verbatim from get_file_content() output.",
    "  Delegate to team members (ContextRouter, Researcher, Planner, Coder, Executor, Reviewer)",
    "  only for complex multi-file research or when a specialist skill is genuinely needed.",
    "",
    "── Honesty & execution — NEVER fabricate work (read this) ─────",
    "  NEVER claim you created, updated, moved, deleted, or marked anything unless the actual",
    "  write tool was CALLED and returned success OR a staged/pending result (e.g.",
    "  'action_pending', 'review_pending', a pending action_id). Describing or narrating an",
    "  action is NOT performing it. If you did not call a write tool, you changed NOTHING —",
    "  say so plainly. Do NOT emit a confident list of 'updates I made' that you did not execute.",
    "  After every write (direct or delegated), VERIFY: re-read the item, or cite the tool's",
    "  success/pending result, BEFORE reporting it done. If a tool returned an error, an empty",
    "  result, or you called no tool, report the FAILURE / no-op — never a success.",
    "  If you were asked to update records but could not determine what to change (or the writes",
    "  were not staged), state that explicitly instead of inventing changes. A partial or staged",
    "  result is NOT 'done' — report it as staged/pending awaiting approval.",
    "",
    "── Skills — on-demand instruction detail (CRITICAL) ─────────────",
    "  Call load_skill(name) for the full text of a skill BEFORE acting on a task",
    "  it covers — available skills are listed above/below in this prompt. Do NOT",
    "  guess counting-marker or file-write-review behaviour from memory; load it.",
    "",
    "── Conversational turn detection (read this first) ─────────────",
    "  Not every message is a task. Classify the message before reaching for tools:",
    "",
    "  ACTION APPROVAL — always a TASK, never conversational:",
    "    If the agent just described or proposed a change and the user says any of:",
    "    'go ahead', 'apply it', 'do it', 'update it', 'yes', 'ok', 'looks good proceed',",
    "    'make the change', 'write it', 'confirm', 'sure', 'use that' — treat as TASK.",
    "    → Delegate the write/implementation to the Coder immediately.",
    "    → Do NOT reply in plain prose about what you will do. Delegate and act.",
    "",
    "  REJECT / CANCEL — user cancels a proposed action:",
    "    If the user says 'reject', 'cancel', 'no don't', 'don't apply', 'stop', 'abort',",
    "    'undo', 'revert', 'discard', 'roll back' in response to a proposed change → STOP.",
    "    Do NOT delegate to Coder. Do NOT call apply_diff or write_file.",
    "    If a .hive_proposed file was staged, reply exactly:",
    "      'Understood — no changes applied. To discard the staged file, type /reject or /cleanup in your hive CLI.'",
    "    If nothing was staged yet, reply: 'Understood — no changes applied.'",
    "    Do NOT attempt to delete .hive_proposed files via run_command, run_shell, or any tool.",
    "",
    "  CONVERSATIONAL — respond directly, NO tool calls:",
    "    - User shares an opinion, agrees, disagrees, or adds their own perspective",
    "    - User asks a simple follow-up that is already answered by the prior response",
    "    - User says 'I think...', 'but...', 'yeah...', 'that makes sense because...'",
    "    - No new URL, no new codebase question, no action requested",
    "    - NOT an approval of a proposed change (see ACTION APPROVAL above)",
    "  For conversational turns: reply as a knowledgeable colleague would — directly,",
    "  in plain prose, without structured reports or tool calls.",
    "",
    "  TASK — use tools as needed:",
    "    - New URL to fetch, new file to read, new codebase question",
    "    - Explicit action: 'add X', 'fix Y', 'list Z', 'search for W'",
    "    - Question that cannot be answered from the current session context",
    "  Do NOT re-fetch a URL or re-search a topic already retrieved in this session.",
    "  Tool calls cost time — only use them when the information is genuinely missing.",
    "",
    "── Scan-first rule (tasks only) ────────────────────────────────",
    "  User prompts are often short and vague. Do not infer — discover.",
    "  Before answering any question about structure, features, or behaviour:",
    "    1. find_files('**/*') — get the full file tree",
    "    2. search_files(keyword, '**/*') — find all occurrences of the topic",
    "    3. get_file_content(path) — read specific files to verify details",
    "  Never describe a directory or module from its name alone.",
    "  Never stop at the first interesting result for overview questions — cover everything.",
    "  If the user includes a URL in their message, call web_fetch(url) immediately — before any other tool.",
    "  If asked about an external library, tool, GitHub repo, or technology, call web_search() then web_fetch()",
    "  on the best result — do not answer from training data alone for external topics.",
    "",
    "Choose the FASTEST path to answer — do not call tools you don't need:",
    "",
    "For overview / structure questions ('list directories', 'what does X do', 'show me the project'):",
    "  1. list_directory_tree() if available — returns the full directory skeleton with no result cap",
    "     OR find_files('**/*') if list_directory_tree is not available",
    "  2. For each top-level directory: read one entry file (README, main.py, __init__.py, config)",
    "  3. Return a grounded summary covering ALL directories — not just the first one found.",
    "  → Do not use get_project_context() as a shortcut — it may be stale or incomplete.",
    "",
    "For 'how does X work' / feature questions:",
    "  1. search_files(X, '**/*') — find every file that references X",
    "  2. get_file_content() on the 2-3 most relevant files",
    "  3. If the project MCP exposes a documentation section tool (e.g. get_context_section),",
    "     call it with the topic keyword — do not assume the tool name or the doc file name.",
    "  → Search before you read — searching tells you which files are worth reading.",
    "",
    "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
    "  1. find_files('**/<extension>') to discover real paths",
    "  2. search_files(pattern, glob) to verify the pattern across files",
    "  3. get_file_content(path) on 1-2 files if you need more detail",
    "  → Skip broad context tools for these queries — go straight to the files.",
    "",
    "For implementation tasks (write code, fix a bug):",
    "  1. If a documentation/context tool is available (check connected MCP tools), call it",
    "     to load architecture context — do not assume the tool or doc file name.",
    "  2. ALWAYS read at least one existing reference file of the same type before writing.",
    "     NEVER skip this step — guessing conventions produces broken code.",
    "  3. Delegate writing to Coder, review to Reviewer",
    "",
    "── Project context (fetch on demand — NOT pre-loaded) ───────────",
    "  Project context is NEVER injected into your prompt automatically.",
    "  You MUST call a tool to see it — do this BEFORE answering any task:",
    "    1. call get_file_content('hive.md')  → project snapshot (tree + summaries)",
    "    2. If hive.md not found: call get_project_context() as fallback",
    "    3. For any code-writing task: call get_file_content('patterns/ekam-code-generation-guards.md')",
    "       if that file exists — it lists exact anti-patterns with code examples that MUST be avoided.",
    "  This is your first action for any non-trivial task. Skipping it means",
    "  answering blindly from training data — never do this.",
    "",
    "── Past failure corrections — FORWARD TO CODER (CRITICAL) ───────",
    "  When past failure corrections appear above (── Past failures section), you MUST:",
    "  1. Read every correction in full before delegating any code task.",
    "  2. Include the relevant corrections VERBATIM in your delegation message to the Coder.",
    "     Example: 'CORRECTIONS FROM PAST RUNS: [paste the corrections here] — do not repeat these bugs.'",
    "  3. Do NOT assume the Coder has seen the corrections — it has not.",
    "     The Coder only knows what you tell it in your delegation message.",
    "  Skipping this means the Coder repeats the same bugs every run.",
    "",
    "── Don't make downstream agents re-read what's already found (CRITICAL) ──",
    "  When a team member reports back a finding with a citation (a file:line plus the exact",
    "  value/quote/content found there), that citation is reusable — it does not expire when",
    "  the next agent starts. Before delegating the next step:",
    "  1. Pull the exact citations (file:line + verbatim value) out of the prior member's",
    "     response and include them directly in your delegation message to the next agent —",
    "     e.g. 'Researcher already confirmed: VOUCHER_TYPES at vouchers_api.py:46-50 = {...}.",
    "     Use this directly; do not re-read the file to re-confirm it.'",
    "  2. Tell the next agent explicitly that it may trust a citation forwarded this way —",
    "     it does NOT need to call get_file_content()/get_files_batch() again for the same",
    "     path just to double-check something a teammate already read and cited this run.",
    "  3. The two legitimate reasons to still re-read a file despite an existing citation:",
    "     (a) the Coder needs the EXACT surrounding text to build an apply_diff() old_string",
    "         — a paraphrase is never precise enough for a byte-exact match, or",
    "     (b) the citation looks incomplete, stale, or contradicts something else in the task",
    "         and genuinely needs re-verification.",
    "  Redundant re-reads waste real tool calls and time without making the answer any more",
    "  correct — the citation is the same file content, not a fresher one.",
    "",
    "── Multi-MCP tool selection ─────────────────────────────────────",
    "  hive-mcp is the PRIMARY server — use it for ALL file reads AND writes.",
    "  Typical hive-mcp tools: find_files, search_files, count_matches (deterministic counts),",
    "  get_file_content, list_directory_tree, list_directory, apply_diff, write_file, run_shell,",
    "  run_docker, git_status, git_log, web_search, web_fetch. db_query/db_schema when a DB is configured.",
    "  Project MCP is SUPPLEMENTARY — use only for tools not in hive-mcp:",
    "  search_knowledge_graph, get_context_section, and other project-specific tools.",
    "  If only one MCP is connected, use it for everything.",
    "  Discover available tools from the connected MCP — do not assume tool names exist.",
    "",
    "── General rules ──────────────────────────────────────────────",
    "  - Base answers on file contents, not assumptions",
    "  - Synthesise member outputs into one coherent response",
    "",
    "── External docs vs project code (CRITICAL) ─────────────────",
    "  When asked to compare this project against framework docs, external libraries,",
    "  or best-practice guides — ALWAYS use this order, never reverse it:",
    "  1. Read the project source files FIRST (get_file_content, search_files).",
    "     Understand exactly what the code does before consulting any external source.",
    "  2. Fetch the external documentation SECOND (web_fetch / web_search).",
    "  3. Compare with explicit citations from BOTH sides:",
    "     - Every claim about what this project does     → cite file:line",
    "     - Every claim about what the external docs say → cite URL + section heading",
    "  4. If an external pattern conflicts with how this project works:",
    "     a. Read CLAUDE.md / docs.md (via get_file_content) to check if the",
    "        difference is intentional design — many patterns here deliberately",
    "        differ from framework defaults.",
    "     b. State the conflict explicitly: 'Docs say X; this project does Y because Z.'",
    "     c. NEVER assume the external pattern is right and the project is wrong.",
    "  5. If you cannot find a project file that confirms a claim, label it:",
    "     'inference from docs — not verified in codebase'",
    "     NEVER present an unverified inference as a confirmed requirement.",
    "  Self-analysis trap: when studying this project's own config or architecture",
    "  against a framework's examples, the project's established design takes",
    "  precedence over framework examples unless the code itself is broken.",
    "",
    "── Output format guard ─────────────────────────────────────────",
    "  NEVER output raw model template tokens such as <|im_start|>, <|im_end|>, <|endoftext|>",
    "  or any similar special tokens. If you find yourself about to output these, stop and",
    "  reformulate your response in plain text. These are internal tokens that must never",
    "  appear in your output.",
]


# Tools that CHANGE something — the repo, the host, or an external system. Named here,
# server-side, so a caller asking for a read-only run does not have to know (or keep in
# sync with) which tools mutate. Prefix matching covers integration families that grow
# over time, e.g. every notion_create_/update_/append_/delete_ variant.
_MUTATING_TOOLS = {
    "write_file", "apply_diff", "run_command", "run_shell", "run_docker",
    "confirm_action", "reject_action", "index_project", "scan_project_context",
    "lightrag_insert", "run_migration",
    # Persistent bash sessions (hive-mcp/tools/bash.py) -- bash_job_status is
    # deliberately NOT here: it's a read-only poll, and a read-only team can never
    # obtain a job_id anyway since bash_run itself is stripped for them.
    "bash_session_start", "bash_run", "bash_session_close",
}
_MUTATING_PREFIXES = (
    "notion_create", "notion_update", "notion_append", "notion_replace",
    "notion_delete", "notion_trash",
)


def _is_mutating(name: str) -> bool:
    return name in _MUTATING_TOOLS or name.startswith(_MUTATING_PREFIXES)


def _strip_mutating(specs: list, tool_names: list[str] | None) -> tuple[list, list[str] | None]:
    """Return (agent_specs, coordinator_tools) with every mutating tool removed.

    Enforces read-only at the TOOL SURFACE rather than by instruction. Measured
    2026-07-31: a task whose prompt said "do NOT call write_file or apply_diff, do not
    create any file" called write_file anyway and staged a component — one of four
    occasions that day where an explicit instruction failed to constrain an action.
    Instructions shape what a model says; only the tool surface constrains what it does.

    A coordinator with no allowlist (`coordinator_tools: null`, i.e. the full surface) is
    given an explicit read-only allowlist here, since "no allowlist" otherwise means
    "everything including writes".
    """
    import copy
    out = []
    for s in specs:
        s2 = copy.deepcopy(s)
        if getattr(s2, "tools", None):
            s2.tools = [t for t in s2.tools if not _is_mutating(t)]
        out.append(s2)
    if tool_names:
        return out, [t for t in tool_names if not _is_mutating(t)]
    return out, None   # resolved against the live MCP surface in _scope_coordinator_tools


def _scope_coordinator_tools(tool_names: list[str] | None, mcp_list: list, read_only: bool = False):
    """Scope the coordinator's direct MCP tool surface to an explicit allowlist.

    Mirrors make_agent_from_spec's per-agent scoping (swarm/agents.py) — without this,
    the coordinator receives every tool from every connected MCP unfiltered, including
    write/staging tools (apply_diff, write_file, notion_*, confirm_action/reject_action)
    that read-only teams (planning, parallel-review) must never call. Falls back to the
    full mcp_list when no allowlist is given (preserves existing engineering-team behavior)
    or when none of the named tools are found on the connected MCPs.
    """
    if not tool_names and not read_only:
        return mcp_list
    all_funcs: dict = {}
    for mcp in mcp_list:
        all_funcs.update(mcp.functions)
    if not tool_names:
        # read_only with no allowlist: everything the MCPs expose, minus mutating tools.
        return [f for n, f in all_funcs.items() if not _is_mutating(n)] or mcp_list
    scoped = [all_funcs[t] for t in tool_names
              if t in all_funcs and not (read_only and _is_mutating(t))]
    return scoped if scoped else mcp_list


def _extract_tokens(result) -> dict:
    """Pull input/output/total token counts from an Agno RunResponse metrics object."""
    try:
        m = getattr(result, "metrics", None)
        if not m:
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        def _sum(val):
            if isinstance(val, list):
                return sum(v for v in val if isinstance(v, (int, float)))
            return int(val) if val else 0

        if isinstance(m, dict):
            return {
                "input_tokens":  _sum(m.get("input_tokens",  0)),
                "output_tokens": _sum(m.get("output_tokens", 0)),
                "total_tokens":  _sum(m.get("total_tokens",  0)),
            }
        return {
            "input_tokens":  _sum(getattr(m, "input_tokens",  0)),
            "output_tokens": _sum(getattr(m, "output_tokens", 0)),
            "total_tokens":  _sum(getattr(m, "total_tokens",  0)),
        }
    except Exception:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _extract_handoff_summary(task: str, content: str) -> str:
    """Extract a compact chain-boundary handoff block from a completed run's output.

    Stored as the session summary so the next chained call gets a small structured
    digest instead of the full message history — preventing context overflow.
    """
    import re
    from datetime import datetime, timezone

    # File paths: backtick-quoted OR bare paths with a known extension and a slash
    backtick_paths = re.findall(r"`([^`\n]+)`", content)
    bare_paths = re.findall(r"(?<!\w)([\w./\\-]+/[\w./\\-]+\.(?:py|ts|tsx|scss|json|md|yaml|yml))\b", content)
    all_paths = backtick_paths + bare_paths
    file_refs = list(dict.fromkeys(p for p in all_paths if ("/" in p or "\\" in p) and "." in p.split("/")[-1]))

    # review_pending paths
    pending = re.findall(r"review_pending[:\s]+([^\s\n`'\"]+)", content)
    pending = list(dict.fromkeys(pending))

    status = "PENDING_REVIEW" if ("review_pending" in content or pending) else "COMPLETE"

    # Key outcomes: bullet points (-, *, 1.) that are long enough to be meaningful
    bullets = re.findall(r"^(?:[-*]|\d+\.)\s+(.+)$", content, re.MULTILINE)
    key_outcomes = [b.lstrip("*# ") for b in bullets if len(b.strip()) > 15][:5]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    task_short = task[:200].replace("\n", " ")

    lines = [
        f"── Chain handoff ({ts}) ──────────────────────────────────────────",
        f"Task: {task_short}",
        f"Status: {status}",
    ]
    if file_refs:
        lines.append(f"Files referenced: {', '.join(file_refs[:8])}")
    if pending:
        lines.append(f"Pending reviews: {', '.join(pending)}")
    if key_outcomes:
        lines.append("Key outcomes:")
        for b in key_outcomes:
            lines.append(f"  - {b[:100]}")
    lines.append("──────────────────────────────────────────────────────────────────")

    return "\n".join(lines)


# ── Count-marker verification guard (Tier 3) ───────────────────────────────────
# The coordinator is instructed to write counts as [[COUNT pattern=`..` glob=`..`]]
# markers instead of bare numbers it computed by reading. After the run, this guard
# re-runs each marker through hive-mcp's DETERMINISTIC count_matches tool and substitutes
# the real number — so any count that goes through a marker is correct-by-construction
# (the model never supplies the digit and cannot confabulate it). No-op if no markers.
_COUNT_MARKER_RE = re.compile(r"\[\[COUNT\s+pattern=`(.*?)`\s+glob=`(.*?)`\]\]", re.DOTALL)
_COUNT_MARKER_ANY = re.compile(r"\[\[COUNT[^\]]*\]\]")


def _extract_mcp_text(result) -> str:
    if not result or not getattr(result, "content", None):
        return ""
    return "\n".join(
        item.text for item in result.content if hasattr(item, "text") and item.text
    )


async def _verify_claims(content: str, hive_mcp_url: str | None) -> tuple[str, bool]:
    """Run hive-mcp's verify_claims over a draft answer. Returns (report, has_problems).

    Deterministic grep, no model involved. Never raises: a verifier that breaks the run
    would be worse than the fabrication it is meant to catch, so any failure here is
    reported as "no problems" and logged.
    """
    if not content or not hive_mcp_url:
        return "", False
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    try:
        async with streamablehttp_client(hive_mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("verify_claims", {"answer": content})
                report = _extract_mcp_text(res)
    except Exception as exc:
        print(f"[team] verify_claims unavailable ({hive_mcp_url}): {exc}")
        return "", False
    return report, "could NOT be found" in report


async def _fetch_skill_catalog(hive_mcp_url: str | None) -> list[dict]:
    """Fetch the L1 skill catalog once per run via hive-mcp's list_skills tool.

    Returns [] — not an error — when hive-mcp isn't connected or the call fails.
    Skills are an enhancement to instruction delivery, not a hard dependency: a run
    must still work with no catalog, exactly like _verify_claims degrades to "skip
    the check" rather than failing the run when hive-mcp is unreachable.
    """
    if not hive_mcp_url:
        return []
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    try:
        async with streamablehttp_client(hive_mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("list_skills", {})
                text = _extract_mcp_text(res)
                return json.loads(text)
    except Exception as exc:
        print(f"[team] skill catalog unavailable ({hive_mcp_url}): {exc}")
        return []


# Tools that gather EVIDENCE. An answer asserting facts about the codebase without one
# of these has no basis beyond the model's priors and the loaded context.
_READ_TOOLS = {
    "get_file_content", "search_files", "find_files", "count_matches",
    "list_directory", "list_directory_tree", "get_project_context",
    "get_context_section", "list_recent_files", "search_knowledge_graph",
    "lightrag_query", "memory_search", "db_query", "db_schema",
    "git_diff", "git_status", "git_log", "git_log_file", "git_blame",
    "notion_get_page", "notion_query_database", "notion_search",
    "notion_get_item_with_relations", "notion_find_work_item", "notion_items_in_sprint",
    "web_fetch", "web_search",
}

# Claims that need evidence: a backticked identifier, a path:line citation, or a
# bare quantitative claim ("3 active, 3 inactive, 6 total items", "count of X is 42").
# The first two forms were the only ones covered until 2026-08-01: a live-DB question
# was answered "3 active / 3 inactive / 6 total" with ZERO tool calls (confirmed via
# hive-mcp AND project-MCP trace logs across a 15-minute window) and this guard never
# fired, because a bare number next to a count-flavoured noun matched neither pattern.
# The answer happened to be correct by luck; the guard exists to not depend on luck.
_CLAIMY_RE = re.compile(
    r"`[A-Za-z_][A-Za-z0-9_.]{2,}`"
    r"|[\w./-]+\.\w{1,6}:\d+"
    r"|\b\d+\b[^.\n]{0,40}\b(rows?|records?|items?|entries|count|total)\b"
    r"|\b(count|total|number) of\b[^.\n]{0,40}\b\d+\b",
    re.IGNORECASE,
)


def _count_read_calls(result) -> int:
    """Count evidence-gathering tool calls in a run. Returns -1 when undeterminable.

    -1 (not 0) when the message shape is unrecognised: "we could not tell" must never be
    treated as "it did not read". Reading absence as evidence of absence produced several
    wrong diagnoses on 2026-07-31, and a guard that made the same mistake would force
    pointless retries on correct answers.
    """
    msgs = getattr(result, "messages", None)
    if not msgs:
        return -1
    n, recognised = 0, False
    for m in msgs:
        for tc in (getattr(m, "tool_calls", None) or []):
            recognised = True
            fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
            name = (fn or {}).get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            if name in _READ_TOOLS:
                n += 1
        if getattr(m, "role", None) == "tool":
            recognised = True
            name = getattr(m, "tool_name", None) or getattr(m, "name", None)
            if name in _READ_TOOLS:
                n += 1
    return n if recognised else -1


# Tools that WRITE to the project. A "done" claim resting on one of these needs the
# tool's OWN response to actually say so -- a FAILED apply_diff call is still a tool
# call by name, and _count_read_calls-style presence checking cannot tell the two apart.
_WRITE_TOOLS = {"apply_diff", "write_file"}
_WRITE_SUCCESS_RE = re.compile(r"^(review_pending|written|applied):", re.IGNORECASE)
# apply_diff (hive-mcp/tools/files.py) reports every selector its own old_string/
# new_string touched, independent of what the model later claims it changed. See
# _summarize_actual_writes for the live incident this closes.
_SELECTORS_TOUCHED_RE = re.compile(r"Selectors touched:\s*([^\n]+)")

# Phrases a model uses to report a file was actually changed. Deliberately broad: this
# only gates a retry when NO write tool call succeeded (see _count_successful_write_calls
# below), so a false-positive match here just means "checked for evidence when it
# turned out fine" -- cheap. A false NEGATIVE means a fabricated "done" claim ships
# unretried, which is the exact failure this guard exists to catch.
_CLAIMED_WRITE_RE = re.compile(
    r"\bhas been (applied|added|staged|created|updated|modified|proposed)"
    r"|\bi(?:'ve| have) (added|created|updated|modified|applied|staged)"
    r"|\bsuccessfully (applied|added|staged|proposed|created)"
    r"|\b(?:is|are) now staged"
    r"|\bstaged for review"
    r"|\bchange(?:s)? (?:has|have) been (?:applied|staged|made)\b",
    re.IGNORECASE,
)

# Tools that SEARCH the project by a literal pattern. A "NOT FOUND, I searched for
# X" claim needs an actual search_files()/find_files() call for X somewhere in the
# trace -- the read-tool presence check (_count_read_calls) can't tell "searched
# broadly" from "searched for THIS specific term", and citation-checking
# (_verify_claims) has nothing to grep for in a claim with no fabricated symbol.
_SEARCH_TOOLS = {"search_files", "find_files"}

# Proximity-based, not phrase-based: extract identifier-shaped tokens from a
# window AFTER every "NOT FOUND" mention, rather than matching one fixed grammar
# like "searched for X". Measured live 2026-08-06, TWO answers in the same test
# session: "NOT FOUND (searched for expiry_date, valid_until ... in all files)"
# and, on the very next retry of the identical question, "NOT FOUND: no
# `expiry_date` or `valid_until` field in any voucher-related model" -- same
# underlying claim, different connecting grammar, and a fixed "searched for X"
# regex only caught the first. Same "match by proximity, not by exact phrasing"
# principle already used elsewhere in this codebase for citation-quote matching
# (verify.py's _LABELED_LINE_WINDOW/_CONTENT_QUOTE_WINDOW).
_NOT_FOUND_RE = re.compile(r"\bnot\s+found\b", re.IGNORECASE)
_CLAIMED_ABSENCE_WINDOW = 200
# Backtick-wrapped tokens are accepted liberally (backticks are already this
# codebase's own convention for "this is code", per verify.py's _BACKTICK_RE).
# Bare (non-backtick) tokens are required to contain an underscore -- snake_case
# identifiers ("expiry_date") clear this bar without needing a stopword list to
# filter ordinary English prose ("endpoint", "similar", "codebase") out; none of
# those contain an underscore, so they never match the bare-token alternative.
_CLAIMED_ABSENCE_TOKEN_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`|\b([a-z_][a-z0-9]*_[a-z0-9_]*)\b")


def _count_successful_write_calls(result) -> int:
    """Count WRITE tool calls (apply_diff/write_file) whose OWN response text confirms
    success -- not just that the tool was invoked. A failed apply_diff ('old_string not
    found', 'HARD STOP', 'REFUSED') is still a tool call by name; only a response
    starting with 'review_pending:', 'written:', or 'applied:' means anything actually
    landed on disk (staged or otherwise). -1 (not 0) when the message shape is
    unrecognised, mirroring _count_read_calls: "we could not tell" must never be
    treated as "nothing succeeded" -- that would force retries on runs this function
    simply cannot introspect, which is a different problem from a genuine fabrication.

    Measured live 2026-08-05: a Coder called apply_diff() twice against the same file,
    both failed with "old_string not found" (a malformed old_string built by copying
    get_file_content's line-number prefix), then reported "The change has been applied
    via apply_diff to parties.module.scss" anyway -- no .hive_proposed file existed
    anywhere on disk, confirmed directly against the container filesystem.
    """
    msgs = getattr(result, "messages", None)
    if not msgs:
        return -1
    n, recognised = 0, False
    for m in msgs:
        for tc in (getattr(m, "tool_calls", None) or []):
            recognised = True
        if getattr(m, "role", None) == "tool":
            recognised = True
            name = getattr(m, "tool_name", None) or getattr(m, "name", None)
            if name in _WRITE_TOOLS:
                text = getattr(m, "content", None)
                if isinstance(text, str) and _WRITE_SUCCESS_RE.match(text.strip()):
                    n += 1
    return n if recognised else -1


def _extract_searched_patterns(*results) -> set[str]:
    """Every literal pattern/glob argument actually passed to search_files() or
    find_files() across all `results` this run, lowercased for case-insensitive
    comparison against claimed search terms. Reads tool_calls off assistant
    messages (agno's request-side tool_calls, not the tool's own response text --
    the pattern lives in the CALL, not the reply), same access pattern as
    _count_read_calls."""
    import json
    patterns: set[str] = set()
    for result in results:
        msgs = getattr(result, "messages", None) or []
        for m in msgs:
            for tc in (getattr(m, "tool_calls", None) or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                name = (fn or {}).get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                if name not in _SEARCH_TOOLS:
                    continue
                args_raw = (fn or {}).get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                if not args_raw:
                    continue
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    continue
                if not isinstance(args, dict):
                    continue
                for key in ("pattern", "glob_pattern", "glob_filter"):
                    val = args.get(key)
                    if isinstance(val, str) and val:
                        patterns.add(val.lower())
    return patterns


def _claimed_search_terms(content: str) -> list[str]:
    """Every identifier-shaped term the answer implies it checked for and did not
    find, extracted from a window of text after each 'NOT FOUND' mention --
    regardless of the exact connecting grammar ('searched for X', 'no X field',
    'no X, Y, or similar endpoint'). Stops at the first '.' or newline so the
    window doesn't bleed into an unrelated LATER sentence or claim."""
    terms: list[str] = []
    text = content or ""
    for m in _NOT_FOUND_RE.finditer(text):
        window = text[m.end():m.end() + _CLAIMED_ABSENCE_WINDOW]
        window = re.split(r"[.\n]", window, maxsplit=1)[0]
        for tm in _CLAIMED_ABSENCE_TOKEN_RE.finditer(window):
            term = (tm.group(1) or tm.group(2) or "").strip().lower()
            if term and term not in terms:
                terms.append(term)
    return terms


def _has_bare_absence_claim(content: str) -> bool:
    """True when the answer asserts something was NOT FOUND but names no specific
    term for _claimed_search_terms to extract -- e.g. a terse "Not found." with no
    citation of what was searched. Confirmed live 2026-08-06: this slips past
    _unverified_claimed_searches entirely (an empty claimed-terms list has nothing
    to compare against the trace), even when the underlying trace shows the model
    reading/globbing an entirely wrong, hallucinated directory and never running a
    real content search for anything."""
    text = content or ""
    return bool(_NOT_FOUND_RE.search(text)) and not _claimed_search_terms(text)


def _unverified_claimed_searches(content: str, *results) -> list[str]:
    """Claimed search terms (see _claimed_search_terms) with no matching actual
    search_files()/find_files() call anywhere in `results`. Matching is loose --
    exact, or a substring either direction -- since a claim of "valid_until" should
    count as verified against an actual search for "ewb_valid_until" or vice versa;
    the point is confirming SOME real search touched this term, not exact wording.
    """
    claimed = _claimed_search_terms(content)
    if not claimed:
        return []
    actual = _extract_searched_patterns(*results)
    if not actual:
        return claimed
    return [
        term for term in claimed
        if not any(term in pat or pat in term for pat in actual)
    ]


def _summarize_actual_writes(*results) -> str:
    """Deterministic, tool-trace-only summary of which files a write tool call
    actually staged or wrote successfully this run -- independent of whatever the
    model's own prose claims. Appended to every answer, regardless of what the
    narrative says, rather than trying to detect and correct every way a narrative
    can drift from the trace (that is a regex arms race with no end: this session
    alone found two independent causes for "claims a change, none happened" and a
    separate, opposite case -- "a change WAS staged, but the final answer never
    mentions it and reads as if nothing happened" -- discovered live 2026-08-05 when
    a Coder staged a genuinely correct .statusBadge insertion, then reconsidered its
    approach in a second research pass and reported "no new class needed" without
    ever mentioning or withdrawing the still-staged change). A human reading this
    section next to the narrative can catch either direction at a glance; no retry,
    no model call, no risk of the "correction" itself duplicating content the way an
    actual retry did earlier in that same incident.

    Accepts MULTIPLE result objects (the original attempt plus every retry) and
    unions their write evidence. Confirmed live 2026-08-06: a Coder staged a real
    (if namespace-wrong) .statusBadge in its FIRST attempt, then a citation-retry's
    OWN trace made no further successful write and the retry gave up honestly --
    "has not been successfully modified". Checking only the LAST result's trace at
    that return point produced an empty appendix next to a claim that was, in fact,
    also wrong: a write really had happened, just not in the attempt being inspected.

    Returns "" (nothing appended) when NONE of the traces show a successful
    write-tool call -- most runs are answer-only, and a permanent "no files changed"
    footer on every conversational reply would be noise, not signal.

    Also surfaces WHICH SELECTORS were touched within an SCSS file, not just which
    file -- a scope-creep guard, not just a write-happened guard. Confirmed live
    2026-08-06: a lint-fix retry, asked to correct a namespace mismatch on
    .statusBadge, ALSO injected unrelated properties into an existing, already-
    correct .badgeBoth rule the task never named, then narrated the change as a fix
    that never actually applied to .badgeBoth. The file-level appendix alone
    ("parties.module.scss — review_pending") would not have shown this; the
    selector list does, regardless of what the narrative claims changed.
    """
    kind_by_path: dict[str, str] = {}
    selectors_by_path: dict[str, list[str]] = {}
    for result in results:
        msgs = getattr(result, "messages", None) or []
        for m in msgs:
            if getattr(m, "role", None) != "tool":
                continue
            name = getattr(m, "tool_name", None) or getattr(m, "name", None)
            if name not in _WRITE_TOOLS:
                continue
            text = getattr(m, "content", None)
            if not isinstance(text, str):
                continue
            match = _WRITE_SUCCESS_RE.match(text.strip())
            if not match:
                continue
            # The path follows the "kind:" prefix, e.g. "review_pending: src/x.scss
            # (changes)" or "written: src/new.py" -- first whitespace-delimited token.
            rest = text.strip()[match.end():].strip()
            path = rest.split()[0] if rest else "(unknown path)"
            if path not in kind_by_path:
                kind_by_path[path] = match.group(1).lower()
            sel_match = _SELECTORS_TOUCHED_RE.search(text)
            if sel_match:
                bucket = selectors_by_path.setdefault(path, [])
                for name_ in (s.strip() for s in sel_match.group(1).split(",")):
                    if name_ and name_ not in bucket:
                        bucket.append(name_)
    if not kind_by_path:
        return ""
    lines = []
    for path, kind in kind_by_path.items():
        selectors = selectors_by_path.get(path)
        suffix = f" (selectors: {', '.join(selectors)})" if selectors else ""
        lines.append(f"- {path} — {kind}{suffix}")
    return (
        f"\n\n---\n**Actual file changes this run (from the tool trace, not the "
        f"narrative above):**\n" + "\n".join(lines)
    )


async def _verified_answer(content: str, task: str, team, hive_mcp_url: str | None,
                           result=None) -> str:
    """Check the draft's claims and, if any are unverifiable, give the team ONE chance
    to correct itself against the evidence.

    Why a correction round rather than appending the report to the answer: appending
    leaves the fabricated sentence in place as the primary text, and — because the report
    contains phrases like "does not exist in the project" — it would also make a wrong
    answer look right to any downstream check that greps for hedging. That moves the
    metric without fixing the answer. Re-running costs one model round, but only on
    drafts that actually failed, which measured as a minority of runs.

    Bounded at ONE retry on purpose. If the second attempt still cannot support its
    claims, the correction is not converging and looping burns tokens; the verifier's
    findings are surfaced instead so a human sees exactly which claims are unsupported.
    """
    # Every result object seen this call, original attempt first -- fed to
    # _summarize_actual_writes(*all_results) at every return point so a write that
    # happened in an EARLIER attempt is never lost just because a LATER retry's own
    # trace made no further successful write call. See _summarize_actual_writes'
    # docstring for the live incident this closes.
    all_results = [result]

    # Claimed-write check, FIRST — a fabricated "file changed" claim means the rest of
    # the answer's factual content is moot, so this runs before the fact-groundedness
    # checks below rather than after. Only fires when writes are DETERMINABLE and zero
    # succeeded; -1 (undeterminable) is deliberately excluded, matching the read-evidence
    # check's own reasoning: "we could not tell" must never be treated as "nothing
    # succeeded". See _count_successful_write_calls for the live incident this fixes.
    writes = _count_successful_write_calls(result)
    if writes == 0 and _CLAIMED_WRITE_RE.search(content or ""):
        print("[team] answer claims a file was written but no write tool call succeeded — retrying with a mandatory write")
        try:
            retry = await team.arun(
                f"{task}\n\n"
                f"IMPORTANT: a previous attempt claimed a file was created, modified, "
                f"applied, or staged for review, but no apply_diff() or write_file() call "
                f"in that attempt actually succeeded (a successful call's response starts "
                f"with 'review_pending:', 'written:', or 'applied:' — a failed one does "
                f"not, even though it is still a real tool call). Do not repeat that "
                f"mistake: call apply_diff() (for an existing file) or write_file() (for a "
                f"brand-new file), confirm its response actually indicates success, and "
                f"only THEN report the change as made. If the write tool keeps failing, "
                f"report the exact failure message instead of claiming success."
            )
            all_results.append(retry)
            retried = retry.content if hasattr(retry, "content") else str(retry)
            if retried:
                retry_writes = _count_successful_write_calls(retry)
                if retry_writes == 0 and _CLAIMED_WRITE_RE.search(retried):
                    # Bounded at one retry, same as the citation path below — surface
                    # rather than hide so a human doesn't act on a phantom change.
                    return (
                        f"{retried}\n\n---\n**This answer claims a file was changed, but "
                        f"no apply_diff()/write_file() call in either attempt actually "
                        f"succeeded — treat this as NOT applied.**"
                        + _summarize_actual_writes(*all_results)
                    )
                content, result = retried, retry
        except Exception as exc:
            print(f"[team] write-claim retry failed: {exc}")

    # Claimed-search check -- the read-side counterpart to the write-claim guard
    # above. Measured live 2026-08-06: a Coder answered "expiry handling: NOT FOUND
    # (searched for expiry_date, valid_until, expires_at ... in all files)", but a
    # real field named ewb_valid_until exists directly on the vouchers table --
    # "valid_until" as a literal pattern would have matched it trivially as a
    # substring, meaning the claimed search either never ran, or ran scoped to the
    # wrong file and the negative result was over-generalized. Neither
    # _verify_claims (nothing fabricated to grep for -- the claim names no symbol)
    # nor _count_read_calls (some reading DID happen, just not of the claimed term)
    # can catch this; only comparing the claimed term against the actual tool_calls
    # arguments can.
    unverified_searches = _unverified_claimed_searches(content, result)
    bare_absence = not unverified_searches and _has_bare_absence_claim(content) and not _extract_searched_patterns(result)
    if unverified_searches or bare_absence:
        if unverified_searches:
            print(f"[team] answer claims searches that never ran: {unverified_searches} — retrying with a mandatory search")
            claim_note = (
                f"IMPORTANT: a previous attempt claimed to have searched for these "
                f"terms, but no search_files() or find_files() call in that attempt "
                f"actually used them: {', '.join(unverified_searches[:6])}. A 'NOT "
                f"FOUND' conclusion is only true if you actually ran that exact "
                f"search -- call search_files() for EACH of these terms now, across "
                f"the whole relevant directory (not just one file), and report what "
                f"you genuinely find, even if it changes your earlier answer."
            )
        else:
            print("[team] answer concludes 'not found' with no search_files()/find_files() call at all — retrying with a mandatory search")
            claim_note = (
                f"IMPORTANT: a previous attempt concluded something was 'not found' "
                f"without ever calling search_files() or find_files() for the actual "
                f"term(s) in question -- reading a file, or guessing at a directory "
                f"path that turned out empty, does not count as a real search. Name "
                f"the specific term(s) you are concluding are absent, call "
                f"search_files() for EACH of them now across the whole relevant "
                f"directory, and report what you genuinely find, even if it changes "
                f"your earlier answer."
            )
        try:
            retry = await team.arun(f"{task}\n\n{claim_note}")
            all_results.append(retry)
            retried = retry.content if hasattr(retry, "content") else str(retry)
            if retried:
                retry_unverified = _unverified_claimed_searches(retried, retry)
                retry_bare = not retry_unverified and _has_bare_absence_claim(retried) and not _extract_searched_patterns(retry)
                if retry_unverified or retry_bare:
                    # Bounded at one retry, same philosophy as the write-claim and
                    # citation paths -- surface rather than hide.
                    named = ', '.join(retry_unverified[:6]) if retry_unverified else "the claimed absence"
                    return (
                        f"{retried}\n\n---\n**This answer claims searches that were "
                        f"never actually run ({named}) — treat any 'NOT FOUND' "
                        f"conclusion resting on them as UNVERIFIED, not confirmed "
                        f"absent.**"
                        + _summarize_actual_writes(*all_results)
                    )
                content, result = retried, retry
        except Exception as exc:
            print(f"[team] search-claim retry failed: {exc}")

    # No-evidence check, BEFORE the grep-based one. Measured 2026-07-31 on the same case
    # in the same configuration: a failing run made ONE tool call (verify_claims) and no
    # reads, answering `pendingBadge` from context in 23s; a passing run made four reads
    # and answered correctly in 154s. Whether it read is what decided correctness, and
    # existence-checking cannot catch it — `pendingBadge` is a real class in the very file
    # the question named, just attached to a different element.
    #
    # Only fires when the answer actually makes a checkable claim (a backticked symbol or
    # a path:line). Conversational replies and "I could not determine" answers legitimately
    # need no reads and must not be retried.
    reads = _count_read_calls(result)
    if reads == 0 and _CLAIMY_RE.search(content or ""):
        print("[team] answer asserts code facts with ZERO read calls — retrying with evidence required")
        try:
            retry = await team.arun(
                f"{task}\n\n"
                f"IMPORTANT: answer this by READING the relevant file(s) first — use "
                f"get_file_content or search_files. A previous attempt answered without "
                f"opening anything and named a symbol that exists elsewhere in the "
                f"codebase but does not apply here. Base every statement on text you have "
                f"actually read this run, and if the thing asked about does not exist, say "
                f"so plainly."
            )
            all_results.append(retry)
            retried = retry.content if hasattr(retry, "content") else str(retry)
            if retried:
                content, result = retried, retry
        except Exception as exc:
            print(f"[team] evidence retry failed: {exc}")

    report, bad = await _verify_claims(content, hive_mcp_url)
    if not bad:
        return content + _summarize_actual_writes(*all_results)

    # Name ONLY the unsupported items, in prose. The first version of this prompt
    # embedded the whole draft and the whole verification report; the coordinator echoed
    # it back verbatim as its answer (the documented failure mode for long, structured,
    # code-bearing prompts), producing output containing the prompt AND the report twice
    # — strictly worse than the fabrication it was fixing. Keep it short and re-ask the
    # ORIGINAL question rather than asking for an edit of the draft.
    #
    # Two distinct categories, not one. Originally only NOT FOUND/DOC ONLY/BAD were
    # read here — AMBIGUOUS and MISMATCH (added 2026-08-04 for citations whose quoted
    # content lands nowhere near the cited line) were silently invisible to this retry:
    # `bad` came back True from _verify_claims (its "could NOT be found" text is
    # category-agnostic), but `missing` stayed empty for a report containing ONLY
    # AMBIGUOUS/MISMATCH findings, so `if not missing: return content` shipped the
    # unfixed, undisclosed answer with no retry AND no failure disclaimer attached.
    # Separately, "does not exist" is the wrong thing to tell the model about a citation
    # problem — the FILE exists, only the line number or path form is wrong — so a
    # citation-shaped miss gets its own, accurate instruction.
    #
    # Extract by stripping the matched PREFIX and taking what follows — not a fixed
    # split()[1] index. "NOT FOUND" and "DOC ONLY" are two words; split()[1] on a "NOT
    # FOUND parties_api.py" line silently grabbed the literal word "FOUND", not
    # "parties_api.py" — a real, live-observed bug (confirmed 2026-08-04): a retry
    # prompt built this way told the model "a previous attempt referred to FOUND...
    # do not mention it again", and the model dutifully wrote back "There is no `FOUND`
    # model or symbol in this codebase" in its corrected answer. One-word prefixes
    # (BAD, AMBIGUOUS, MISMATCH) happened to work with the fixed index by coincidence;
    # this makes every prefix length correct instead of relying on that coincidence.
    def _claim_token(line: str, prefixes: tuple[str, ...]) -> str | None:
        s = line.strip()
        for p in prefixes:
            if s.startswith(p):
                rest = s[len(p):].strip().split(None, 1)
                return rest[0] if rest else None
        return None

    missing_symbols = [t for ln in report.splitlines()
                        if (t := _claim_token(ln, ("NOT FOUND", "DOC ONLY")))]
    bad_citations = [t for ln in report.splitlines()
                      if (t := _claim_token(ln, ("BAD", "AMBIGUOUS", "MISMATCH")))]
    # verify_claims' CONVENTIONS section (CODE_LINT_FORBID/REQUIRE, and the SCSS
    # namespace-consistency check) uses its own "VIOLATION" prefix, which
    # _claim_token above was never taught to recognise -- confirmed live
    # 2026-08-06: a report containing ONLY a NAMESPACE MISMATCH violation set
    # `bad=True` correctly (verify_claims' own problem-count includes lint findings),
    # but missing_symbols and bad_citations both came back empty, so this function
    # fell straight to "not missing_symbols and not bad_citations" and returned the
    # unfixed answer with no retry and no disclaimer -- the exact same category of
    # bug the AMBIGUOUS/MISMATCH fix (2026-08-04, see the comment below) closed for
    # citations, just never extended to the CONVENTIONS category the whole session.
    # A lint VIOLATION line is a full sentence, not a bare symbol name, so it takes
    # the whole remainder of the line rather than _claim_token's first-word split.
    lint_violations = [ln.strip()[len("VIOLATION"):].strip()
                        for ln in report.splitlines() if ln.strip().startswith("VIOLATION")]
    if not missing_symbols and not bad_citations and not lint_violations:
        return content + _summarize_actual_writes(*all_results)
    instructions = []
    if missing_symbols:
        named = ", ".join(missing_symbols[:6])
        instructions.append(
            f"a previous attempt referred to {named}, which a repository-wide grep shows "
            f"does not exist here. Do not mention those again — if the thing being asked "
            f"about does not exist in this codebase, say that plainly and name what does "
            f"exist instead, rather than offering a similarly-named symbol as a substitute."
        )
    if bad_citations:
        named = ", ".join(bad_citations[:6])
        # Imperative FIRST/THEN, mirroring the reads==0 branch above (proven wording,
        # not a new pattern). A prior version phrased this as one soft "re-read the
        # file and copy the exact line number" sentence — measured live 2026-08-04,
        # the model did NOT reliably comply: a citation retry came back citing a
        # DIFFERENT wrong line than before rather than the verified-correct one,
        # meaning it answered from memory/estimation again instead of actually
        # reissuing a read. Making the read step syntactically first and separate
        # from the answering step is a cheaper lever than trusting prose compliance.
        instructions.append(
            f"these file:line citations were wrong or unresolvable: {named}. FIRST call "
            f"get_file_content on the exact file(s) involved — do not rely on a read from "
            f"earlier in this conversation, the file may have scrolled out of context or "
            f"your memory of its line numbers may be wrong. THEN answer using the line "
            f"number exactly as printed in that tool's own numbered output — never a "
            f"recalled, estimated, or rounded number. If a filename is shared by more "
            f"than one file in the project, cite the full repo-relative path instead of "
            f"the bare filename."
        )
    if lint_violations:
        named = "; ".join(lint_violations[:4])
        # SCOPE line added 2026-08-06 after a live incident: asked to fix a
        # NAMESPACE MISMATCH on .statusBadge, a retry ALSO injected unrelated
        # properties into an existing, already-correct .badgeBoth rule the task
        # never named, then narrated it as a fix that never actually applied to
        # .badgeBoth. Re-handing the model the full original task text (below) is
        # an open invitation to "improve" anything it notices along the way; this
        # sentence exists specifically to close that door. Not trusted alone --
        # apply_diff's own "Selectors touched" report (hive-mcp/tools/files.py) is
        # the mechanical backstop that makes a violation of this instruction
        # visible in the ground-truth appendix even if the model ignores it.
        instructions.append(
            f"the code you staged has real convention violations: {named}. Call "
            f"apply_diff() again against the SAME staged file to fix ONLY these "
            f"EXACT issues — rewording the prose answer does not fix them, the "
            f"file on disk is still wrong until a new apply_diff() call corrects "
            f"it. Read the current staged state first via "
            f"get_file_content('<path>.hive_proposed') if unsure what changed. Do "
            f"NOT modify, add properties to, or otherwise touch any OTHER rule or "
            f"selector in this file, even if it looks related or you notice "
            f"something else that could be improved — fix only the exact "
            f"violation named above and nothing else."
        )
    retry_prompt = f"{task}\n\nIMPORTANT: " + " Also, ".join(instructions)
    try:
        retry = await team.arun(retry_prompt)
        all_results.append(retry)
        corrected = retry.content if hasattr(retry, "content") else str(retry)
    except Exception as exc:
        print(f"[team] verify retry failed: {exc}")
        return content + _summarize_actual_writes(*all_results)
    if not corrected:
        return content + _summarize_actual_writes(*all_results)

    # Diagnostic visibility, not a hard gate — going back for a SECOND retry would
    # break the "bounded at ONE retry" design below. Confirmed live 2026-08-04 that
    # a citation-correction retry can silently skip re-reading; this makes that
    # visible in logs immediately instead of requiring hours of manual log
    # archaeology (which is what it took to first notice it).
    if bad_citations and _count_read_calls(retry) == 0:
        print("[team] citation-correction retry made ZERO read calls — "
              "it answered from memory/estimation again instead of re-reading")

    report2, still_bad = await _verify_claims(corrected, hive_mcp_url)
    if still_bad:
        # Surface rather than hide: the reader needs to know which claims are unsupported.
        return (f"{corrected}\n\n---\n**Unverified claims flagged automatically "
                f"(these could not be found in the repository):**\n```\n{report2}\n```"
                + _summarize_actual_writes(*all_results))
    return corrected + _summarize_actual_writes(*all_results)


async def _fill_count_markers(content: str, hive_mcp_url: str | None) -> str:
    """Replace [[COUNT pattern=`..` glob=`..`]] markers with the exact count from
    hive-mcp's count_matches tool. The number is ALWAYS tool-derived. Malformed or
    unresolvable markers become '[count unavailable]'. No-op when there are no markers."""
    if not content or "[[COUNT" not in content:
        return content
    if not hive_mcp_url:
        return _COUNT_MARKER_ANY.sub("[count unavailable]", content)

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    matches = list(_COUNT_MARKER_RE.finditer(content))
    if not matches:
        return _COUNT_MARKER_ANY.sub("[count unavailable]", content)

    cache: dict = {}
    try:
        async with streamablehttp_client(hive_mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for mt in matches:
                    key = (mt.group(1), mt.group(2))
                    if key in cache:
                        continue
                    try:
                        res = await session.call_tool(
                            "count_matches", {"pattern": key[0], "glob_filter": key[1]}
                        )
                        m = re.search(r"TOTAL:\s*(\d+)", _extract_mcp_text(res))
                        cache[key] = m.group(1) if m else "[count unavailable]"
                    except Exception as exc:
                        print(f"[team] count verify failed ({key!r}): {exc}")
                        cache[key] = "[count unavailable]"
    except Exception as exc:
        print(f"[team] count-marker guard: hive-mcp unreachable ({hive_mcp_url}): {exc}")
        return _COUNT_MARKER_ANY.sub("[count unavailable]", content)

    out = content
    for mt in matches:
        out = out.replace(mt.group(0), cache.get((mt.group(1), mt.group(2)), "[count unavailable]"))
    return _COUNT_MARKER_ANY.sub("[count unavailable]", out)  # strip any malformed leftovers


_CACHEABLE_READ_TOOLS = {
    "get_file_content", "get_files_batch", "search_files", "search_files_batch",
    "find_files", "list_directory", "list_directory_tree", "count_matches",
}


def _make_read_cache_tool_hook():
    """Build a fresh, run-scoped cache for read-only tool calls -- a new dict per
    _build_team() call means the cache lives exactly as long as one run and can
    never leak stale data across sessions/tasks.

    Only intercepts the tools in _CACHEABLE_READ_TOOLS (read-only, side-effect-free,
    deterministic within one run) -- never a write/mutating tool. Does not weaken
    verify_claims' guarantee that a claim is backed by a real tool call: a cache hit
    still returns data from a genuine earlier fetch THIS SAME RUN, not a guess or a
    stale value -- it only skips a duplicate network round-trip to hive-mcp for the
    identical (tool, arguments) pair. Confirmed live 2026-08-07: get_files_batch was
    called 21-29 times for the SAME 2 files across one 6-agent coordinate-mode run,
    since agno's share_member_interactions only forwards a teammate's final TEXT
    answer, never the raw tool result -- a companion prompt-level fix (telling the
    coordinator/Researcher/Coder to forward and trust citations) did not measurably
    reduce this on its own, since it depends on model instruction-following rather
    than a mechanical guarantee.

    Must be async and must be registered on EVERY team member, not just the
    coordinator's own Team(...) -- two things confirmed by direct source reading
    and a live check, not assumed:
      1. Every MCP-server-backed tool call (all of hive-mcp's tools, since it's a
         remote server) is async on the client side unconditionally, regardless of
         whether the underlying tool function itself is sync or async -- confirmed
         via agno.utils.mcp.get_entrypoint_for_tool, whose call_tool wrapper is
         `async def` with no sync variant. A sync hook that does `function(**args)`
         against that gets back an unawaited coroutine object, not the real result.
      2. In mode="coordinate", the coordinator mostly delegates work to team
         members rather than calling tools itself -- a hook registered only on
         Team(tool_hooks=[...]) would never see the member agents' own tool calls,
         which is where the measured redundant reads actually happen. Confirmed by
         checking ZGX's agno-api.service journal after live test runs that made
         dozens of get_files_batch calls: a coordinator-only hook logged nothing.
    """
    cache: dict[tuple, object] = {}

    async def _read_cache_tool_hook(function_name, function, args, agent=None, team=None):
        if function_name not in _CACHEABLE_READ_TOOLS:
            return await function(**args)
        try:
            key = (function_name, json.dumps(args or {}, sort_keys=True))
        except TypeError:
            return await function(**args)  # non-JSON-serializable args -- skip caching, call through
        if key in cache:
            return cache[key]
        result = await function(**args)
        cache[key] = result
        return result

    return _read_cache_tool_hook


class ToolCallAborted(Exception):
    """Raised by _make_tool_interception_hook when a tool call is skipped
    because its abort_event was set before the call ran."""


def _make_tool_interception_hook(abort_event: "asyncio.Event | None" = None):
    """Build a tool_hooks callable giving a real per-tool-call checkpoint:
    log every call (name, args, duration, success/failure), and -- if an
    abort_event is supplied and set -- skip the call entirely instead of
    running it. This is AGNOHive 2.3.1's Phase 9a ("interception: pause /
    abort / serialize between tool calls").

    Async + must be shared across the coordinator AND every member agent,
    for the same two reasons _make_read_cache_tool_hook's docstring
    documents (re-confirmed here, not re-derived): every MCP-server-backed
    tool call is async on the client side unconditionally (a sync hook
    calling `function(**args)` gets back an unawaited coroutine, not the
    real result), and in mode="coordinate" the coordinator mostly delegates
    to team members rather than calling tools itself -- a hook registered
    only on the coordinator's own Team(tool_hooks=[...]) never sees the
    member agents' own tool calls, which is most of them.

    Honest scope: this is the INTERCEPTION checkpoint only -- pause/abort
    immediately BEFORE a tool call would execute. `abort_event` is a plain
    asyncio.Event supplied by whatever caller wants to signal an abort;
    nothing in this repo currently sets one, and _build_team's default
    wiring passes abort_event=None, making this hook a pure audit-log
    pass-through with zero behavior change (matching the confirmed
    middleware contract: `function(**args)` must be awaited for the tool
    call to actually happen).

    This is deliberately NOT wired to Phase 7's client-side `_steering_queue`
    (cli/hive). That queue lives in the user's own machine's CLI process;
    this hook runs server-side in swarm/team.py, inside ZGX's
    agno-api.service process. There is no existing mid-run client<->server
    communication channel connecting the two -- building one (e.g. a
    side-channel endpoint this hook polls, keyed by session/run id) is a
    separate, larger effort explicitly out of scope here, the same kind of
    scoping decision already recorded for Phase 9b (context injection) on
    the AGNOHive 2.3.1 Notion page. Treat `abort_event` as a reusable
    building block a future caller can wire up, not as something already
    connected to steering.
    """

    async def _tool_interception_hook(function_name, function, args, agent=None, team=None):
        if abort_event is not None and abort_event.is_set():
            print(f"[team] tool_hook: {function_name}({args}) ABORTED before execution")
            raise ToolCallAborted(function_name)
        started = time.monotonic()
        try:
            result = await function(**args)
            elapsed = time.monotonic() - started
            print(f"[team] tool_hook: {function_name}({args}) -> {elapsed:.2f}s")
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            print(f"[team] tool_hook: {function_name}({args}) RAISED {type(exc).__name__}: {exc} after {elapsed:.2f}s")
            raise

    return _tool_interception_hook


def _build_team(
    agent_specs: list | None,
    coordinator_model: str,
    coordinator_tools: list[str] | None,
    mode: str,
    mcp_list: list,
    instructions: list,
    *,
    name: str = "AgnoHive",
    description: str | None = None,
    read_only: bool = False,
    skill_catalog: list[dict] | None = None,
) -> Team:
    """Build a coordinator Team from agent specs (or the default Coder+Reviewer), sharing the
    already-connected `mcp_list`. Factored out of run_task_async / run_task_stream so the same
    build is reusable for router sub-teams (EK-88). `coordinator_model` is the already-resolved
    model name. `description` (default None = previous behaviour) lets the router leader route to
    this team. Behaviour is identical to the previous inline Team(...) construction when omitted.
    `skill_catalog` (default None) is forwarded to each agent's spec-based construction so its
    L1 catalog can be filtered per agent role — the default Coder+Reviewer fallback path (used
    only when agent_specs is empty) does not take a catalog; that path predates team YAMLs."""
    # One cache per run, shared by the coordinator AND every member agent (not just
    # the coordinator) -- see _make_read_cache_tool_hook's docstring for why both of
    # those are load-bearing, not incidental. The interception hook (Phase 9a) is
    # built with abort_event=None here -- see its own docstring for why that keeps
    # this a no-op audit-log pass-through today rather than a live abort switch.
    read_cache_hook = _make_read_cache_tool_hook()
    interception_hook = _make_tool_interception_hook()
    tool_hooks = [read_cache_hook, interception_hook]
    if agent_specs:
        members = [
            make_agent_from_spec(spec, *mcp_list, skill_catalog=skill_catalog, tool_hooks=tool_hooks)
            for spec in agent_specs
        ]
    else:
        members = [
            make_coder(*mcp_list, tool_hooks=tool_hooks),
            make_reviewer(*mcp_list, tool_hooks=tool_hooks),
        ]
    return Team(
        name=name,
        description=description,
        mode=mode,
        model=get_model(coordinator_model, config.ollama_host),
        members=members,
        tools=_scope_coordinator_tools(coordinator_tools, mcp_list, read_only),
        instructions=instructions,
        show_members_responses=True,
        share_member_interactions=True,
        add_member_tools_to_context=True,
        markdown=True,
        max_iterations=config.max_iterations,
        tool_call_limit=config.tool_call_limit,
        tool_hooks=tool_hooks,
    )


def _stream_event_to_chunk(event) -> str | dict | None:
    """Classify one raw agno team.arun(stream=True) event into what run_task_stream
    yields downstream. Duck-typed via getattr since agno event objects vary by type.

    Returns:
      str  — a TeamRunContent text delta (existing behavior, unchanged)
      dict — a tool-call sentinel: {"__tool_event__": "start", "name": str, "args": dict}
             or {"__tool_event__": "end", "name": str, "result_preview": str | None}
      None — every other event type (dropped, same as the previous hard filter)
    """
    event_type = getattr(event, "event", "")
    if event_type == "TeamRunContent":
        chunk = getattr(event, "content", None)
        return chunk if isinstance(chunk, str) and chunk else None
    if event_type == "TeamToolCallStarted":
        tool = getattr(event, "tool", None)
        if tool is None:
            return None
        return {
            "__tool_event__": "start",
            "name": tool.tool_name,
            "args": tool.tool_args or {},
        }
    if event_type == "TeamToolCallCompleted":
        tool = getattr(event, "tool", None)
        if tool is None:
            return None
        result = tool.result
        return {
            "__tool_event__": "end",
            "name": tool.tool_name,
            "result_preview": result[:200] if isinstance(result, str) else None,
        }
    return None


async def run_task_stream(
    task: str,
    agent_specs: list | None = None,
    coordinator_model: str | None = None,
    coordinator_tools: list[str] | None = None,
    mcp_url: str | None = None,
    mcp_urls: list[str] | None = None,
    project_id: str = "default",
    session_id: str | None = None,
    mode: str = "coordinate",
    read_only: bool = False,
):
    """Same setup as run_task_async but yields text chunks as the coordinator generates them.

    Yields:
      str  — content chunks from the coordinator as they arrive
      dict — a tool-call sentinel {"__tool_event__": "start"|"end", ...} (see
             _stream_event_to_chunk), or the final sentinel
             {"__done__": True, "content": str, "tokens": dict}
    """
    effective_mcp_url = mcp_url or config.mcp_url
    effective_coordinator = coordinator_model or config.leader_model

    from swarm.sessions import get_context as get_session_context

    async def _load_session_context():
        if session_id:
            try:
                return await get_session_context(session_id)
            except Exception as exc:
                print(f"[team] session context warning: {exc}")
        return "", []

    # Computed here (not after the gather, as before) because the skill-catalog fetch
    # below needs it, and connecting MCPTools further down needs the same value — one
    # computation, not two that could silently diverge.
    # hive-mcp first (primary — full read+write+shell+ripgrep), project-mcp second (supplementary)
    all_mcp_urls = [u for u in (mcp_urls or []) + [effective_mcp_url] if u]

    failure_context, (session_summary, session_messages), skill_catalog = (
        await asyncio.gather(
            load_failure_context(project_id, current_task=task),
            _load_session_context(),
            _fetch_skill_catalog(all_mcp_urls[0] if all_mcp_urls else None),
        )
    )

    instructions = list(_COORDINATOR_INSTRUCTIONS)
    if skill_catalog:
        instructions += ["", format_skill_catalog(skill_catalog, None)]
    if failure_context:
        instructions += ["", failure_context]
    if session_summary:
        is_chain_handoff = session_summary.startswith("── Chain handoff")
        instructions += [
            "", "── Session summary (older turns) ─────────────────────────────────",
            session_summary, "──────────────────────────────────────────────────────────────────",
        ]
        # For chain-boundary handoffs, skip full message history — the compact digest
        # above replaces it. Injecting both causes context overflow on long chains.
        if not is_chain_handoff and session_messages:
            lines = ["── Recent messages ───────────────────────────────────────────────"]
            for msg in session_messages:
                lines.append(f"[{msg['role']}] {msg['content'][:800]}")
            lines.append("──────────────────────────────────────────────────────────────────")
            instructions += [""] + lines
    elif session_messages:
        lines = ["── Recent messages ───────────────────────────────────────────────"]
        for msg in session_messages:
            lines.append(f"[{msg['role']}] {msg['content'][:800]}")
        lines.append("──────────────────────────────────────────────────────────────────")
        instructions += [""] + lines

    async with AsyncExitStack() as stack:
        mcp_list = []
        # exclude_tools only for project-mcp — it exposes agno_run/agno_list_teams (which
        # would recurse) and duplicates six hive-mcp tool names (see
        # _PROJECT_MCP_EXCLUDE_TOOLS above). hive-mcp does not have any of these names —
        # passing exclude_tools naming a tool a server doesn't have causes agno to return
        # 0 tools for that server, so this must stay project-mcp-only.
        _project_mcp_url = effective_mcp_url
        for url in all_mcp_urls:
            _exclude = _PROJECT_MCP_EXCLUDE_TOOLS if url == _project_mcp_url else None
            try:
                mcp = await stack.enter_async_context(
                    MCPTools(url=url, transport="streamable-http", timeout_seconds=_MCP_TIMEOUT, exclude_tools=_exclude)
                )
                mcp_list.append(mcp)
                print(f"[team] MCP connected: {url} ({len(mcp.functions)} tools)")
            except Exception as e:
                print(f"[team] MCP unavailable, skipping ({url}): {e}")
        if not mcp_list:
            raise RuntimeError("No MCP server available — check hive-mcp and project MCP are running")

        # read_only strips mutating tools from both the agents and the coordinator, so a
        # read-only run cannot write regardless of what the model decides to do.
        _specs, _ctools = (_strip_mutating(agent_specs, coordinator_tools) if read_only
                           else (agent_specs, coordinator_tools))
        team = _build_team(
            _specs, effective_coordinator, _ctools, mode, mcp_list, instructions,
            read_only=read_only, skill_catalog=skill_catalog,
        )

        full_content: list[str] = []
        last_event = None

        with _tracer.start_as_current_span("agno.task.stream", attributes={
            "project_id": project_id,
            "coordinator_model": effective_coordinator,
            "agent_count": len(team.members),
            "task": task[:120],
        }):
            from observability.metrics import task_duration, task_counter
            t0 = time.perf_counter()
            try:
                async for event in team.arun(task, stream=True):
                    last_event = event
                    out = _stream_event_to_chunk(event)
                    if isinstance(out, str):
                        full_content.append(out)
                        yield out
                    elif isinstance(out, dict):
                        yield out
                combined = "".join(full_content) or "(no response)"
                # Tier-3 guard: fill [[COUNT ...]] markers in the final content (streamed
                # chunks above are pre-substitution; the done-sentinel content is corrected).
                try:
                    combined = await _fill_count_markers(combined, all_mcp_urls[0] if all_mcp_urls else None)
                except Exception as exc:
                    print(f"[team] count-marker guard warning: {exc}")
                tokens = _extract_tokens(last_event)
                task_counter.add(1, {"project_id": project_id, "outcome": "success"})
                # Fire-and-forget: don't block the response on post-run experience indexing.
                record_success_bg(task, combined, project_id)
                # Save a compact chain-boundary handoff summary so the next chained call
                # gets a small structured digest instead of the full message history.
                if session_id:
                    from swarm.sessions import save_handoff_summary
                    handoff = _extract_handoff_summary(task, combined)
                    asyncio.ensure_future(save_handoff_summary(session_id, handoff))
                yield {"__done__": True, "content": combined, "tokens": tokens}
            except Exception as exc:
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                try:
                    await record_failure(task, str(exc), project_id)
                except Exception:
                    pass  # LightRAG indexing is best-effort; never crash the run
                raise
            finally:
                task_duration.record(time.perf_counter() - t0, {"project_id": project_id})


async def run_task_async(
    task: str,
    agent_specs: list | None = None,
    coordinator_model: str | None = None,
    coordinator_tools: list[str] | None = None,
    mcp_url: str | None = None,
    mcp_urls: list[str] | None = None,   # secondary MCPs (e.g. hive-mcp for host actions)
    project_id: str = "default",
    session_id: str | None = None,
    mode: str = "coordinate",
    read_only: bool = False,
) -> str:
    """Run a task with the given team spec, or fall back to default Coder+Reviewer."""
    effective_mcp_url = mcp_url or config.mcp_url
    effective_coordinator = coordinator_model or config.leader_model

    from swarm.sessions import get_context as get_session_context

    async def _load_session_context():
        if session_id:
            try:
                return await get_session_context(session_id)
            except Exception as exc:
                print(f"[team] session context warning: {exc}")
        return "", []

    # Collect all MCP URLs: primary (project context) + secondary (host actions).
    # Computed here (not after the gather, as before) because the skill-catalog fetch
    # below needs it, and connecting MCPTools further down needs the same value — one
    # computation, not two that could silently diverge.
    # hive-mcp first (primary — full read+write+shell+ripgrep), project-mcp second (supplementary)
    all_mcp_urls = [u for u in (mcp_urls or []) + [effective_mcp_url] if u]

    failure_context, (session_summary, session_messages), skill_catalog = (
        await asyncio.gather(
            load_failure_context(project_id, current_task=task),
            _load_session_context(),
            _fetch_skill_catalog(all_mcp_urls[0] if all_mcp_urls else None),
        )
    )

    instructions = list(_COORDINATOR_INSTRUCTIONS)
    if skill_catalog:
        instructions += ["", format_skill_catalog(skill_catalog, None)]
    if failure_context:
        instructions += ["", failure_context]
    if session_summary:
        is_chain_handoff = session_summary.startswith("── Chain handoff")
        instructions += [
            "",
            "── Session summary (older turns) ─────────────────────────────────",
            session_summary,
            "──────────────────────────────────────────────────────────────────",
        ]
        # For chain-boundary handoffs, skip full message history — the compact digest
        # above replaces it. Injecting both causes context overflow on long chains.
        if not is_chain_handoff and session_messages:
            lines = ["── Recent messages ───────────────────────────────────────────────"]
            for msg in session_messages:
                lines.append(f"[{msg['role']}] {msg['content'][:800]}")
            lines.append("──────────────────────────────────────────────────────────────────")
            instructions += [""] + lines
    elif session_messages:
        lines = ["── Recent messages ───────────────────────────────────────────────"]
        for msg in session_messages:
            lines.append(f"[{msg['role']}] {msg['content'][:800]}")
        lines.append("──────────────────────────────────────────────────────────────────")
        instructions += [""] + lines

    async with AsyncExitStack() as stack:
        mcp_list = []
        # exclude_tools only for project-mcp — it exposes agno_run/agno_list_teams (which
        # would recurse) and duplicates six hive-mcp tool names (see
        # _PROJECT_MCP_EXCLUDE_TOOLS above). hive-mcp does not have any of these names —
        # passing exclude_tools naming a tool a server doesn't have causes agno to return
        # 0 tools for that server, so this must stay project-mcp-only.
        _project_mcp_url = effective_mcp_url
        for url in all_mcp_urls:
            _exclude = _PROJECT_MCP_EXCLUDE_TOOLS if url == _project_mcp_url else None
            try:
                mcp = await stack.enter_async_context(
                    MCPTools(url=url, transport="streamable-http", timeout_seconds=_MCP_TIMEOUT, exclude_tools=_exclude)
                )
                mcp_list.append(mcp)
                print(f"[team] MCP connected: {url} ({len(mcp.functions)} tools)")
            except Exception as e:
                print(f"[team] MCP unavailable, skipping ({url}): {e}")
        if not mcp_list:
            raise RuntimeError("No MCP server available — check hive-mcp and project MCP are running")

        # read_only strips mutating tools from both the agents and the coordinator, so a
        # read-only run cannot write regardless of what the model decides to do.
        _specs, _ctools = (_strip_mutating(agent_specs, coordinator_tools) if read_only
                           else (agent_specs, coordinator_tools))
        team = _build_team(
            _specs, effective_coordinator, _ctools, mode, mcp_list, instructions,
            read_only=read_only, skill_catalog=skill_catalog,
        )

        span_attrs = {
            "project_id": project_id,
            "coordinator_model": effective_coordinator,
            "agent_count": len(team.members),
            "task": task[:120],
        }

        with _tracer.start_as_current_span("agno.task", attributes=span_attrs) as span:
            from observability.metrics import task_duration, task_counter
            t0 = time.perf_counter()
            try:
                with _tracer.start_as_current_span("agno.team.run"):
                    result = await team.arun(task)
                content = result.content if hasattr(result, "content") else str(result)
                # Fallback: if content is empty, pull from the last message in the run
                if not content and hasattr(result, "messages") and result.messages:
                    for msg in reversed(result.messages):
                        msg_content = getattr(msg, "content", None)
                        if msg_content and isinstance(msg_content, str):
                            content = msg_content
                            break
                content = content or "(no response)"
                # Tier-3 guard: fill any [[COUNT ...]] markers with deterministic counts.
                try:
                    content = await _fill_count_markers(content, all_mcp_urls[0] if all_mcp_urls else None)
                except Exception as exc:
                    print(f"[team] count-marker guard warning: {exc}")
                # Tier-4 guard: grep the draft's claims; one correction round if any are
                # unsupported. Instruction-level verification was tried first and the
                # model ignored it, so this is enforced outside the model.
                try:
                    content = await _verified_answer(
                        content, task, team, all_mcp_urls[0] if all_mcp_urls else None,
                        result)
                except Exception as exc:
                    print(f"[team] verify guard warning: {exc}")
                tokens = _extract_tokens(result)
                span.set_status(trace.StatusCode.OK)
                task_counter.add(1, {"project_id": project_id, "outcome": "success"})
                # Fire-and-forget: don't block the response on post-run experience indexing.
                record_success_bg(task, content, project_id)
                # Save a compact chain-boundary handoff summary so the next chained call
                # gets a small structured digest instead of the full message history.
                if session_id:
                    from swarm.sessions import save_handoff_summary
                    handoff = _extract_handoff_summary(task, content)
                    asyncio.ensure_future(save_handoff_summary(session_id, handoff))
                return content, tokens
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                try:
                    await record_failure(task, str(exc), project_id)
                except Exception:
                    pass  # LightRAG indexing is best-effort; never crash the run
                raise  # callers receive (content, tokens) on success; exception on failure
            finally:
                task_duration.record(
                    time.perf_counter() - t0,
                    {"project_id": project_id},
                )
