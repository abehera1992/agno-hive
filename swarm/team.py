import asyncio
import json
import os
import re
import time
from contextlib import AsyncExitStack, suppress

from opentelemetry import trace
from pydantic import BaseModel, Field
from agno.run.team import TeamRunOutput
from agno.team import Team
from agno.tools import tool as agno_tool
from agno.tools.mcp import MCPTools
from agno.utils.string import url_safe_string
from .agents import (
    make_coder, make_reviewer, make_agent_from_spec, get_model, format_skill_catalog,
    update_session_state,
)
from .feedback import record_success, record_success_bg, record_failure, load_failure_context
from . import model_routing, team_config
from config.config import config

_tracer = trace.get_tracer("agno-hive.team")

# T6 root-caused 2026-08-18: this was equal to config.liveness_silence_threshold_s
# (300s default), so a genuinely hung MCP tool call (confirmed live: agent called
# verify_claims with the full ~6.7KB final answer as its `answer` argument; hive-mcp's
# own docker logs show ZERO trace of that request ever arriving -- the hang is
# client-side, before any bytes reach the wire) never got a chance to time out and
# raise its own catchable exception (_make_tool_interception_hook's `await
# function(**args)` would log "RAISED ... after Ns"). Instead the coordinator sat
# silent for the full 300s and the cruder, outer liveness watchdog (api/server.py,
# polling activity["last_call_at"] on its own clock) always won the race by a second
# or two, SIGKILLing the whole worker with a content-free "no tool call or new stream
# content for over 300s" 504 -- discarding whatever diagnostic value the MCP client's
# own TimeoutError would have carried. Live evidence (task kn7ohwq3h): heartbeat showed
# "277s since last tool call (last: verify_claims)" at 18:29:12, then the liveness kill
# fired at 18:29:13 -- one second later, with no RAISED log ever printed in between.
# Lowered to 180s so a stuck tool call fails cleanly, with a real logged exception,
# well before the 300s liveness kill would otherwise silently eat the whole window.
# See test_mcp_timeout_has_headroom_before_liveness_kill for the invariant this relies on.
_MCP_TIMEOUT = 180  # lightrag_query synthesis ~90-120s; large file reads over Docker bind mounts can be slow — headroom so multi-read tasks don't die mid-read

# 2026-08-19 (T6 follow-up, found live watching task koi6p1bkd): _MCP_TIMEOUT above
# only governs the AGENT's own MCPTools instance (the persistent session every
# real tool call goes through) -- it does NOT cover _verify_claims/_fetch_skill_catalog/
# _fill_count_markers below, each of which opens its OWN separate, one-shot
# streamablehttp_client(hive_mcp_url) connection with no timeout at all, protected
# only by a broad `except Exception` that a hung await never reaches. Live-caught
# via py-spy mid-run: MainThread idle in select() -- the exact same "waiting on a
# socket that will never deliver another byte" signature as the original T6 hang --
# during what turned out to be a verify_claims-shaped quiet stretch. That specific
# run went on to complete successfully (not a confirmed repeat of the hang -- a
# single idle snapshot proves nothing was running at that instant, not that it was
# stuck forever), but the underlying gap is real regardless: these three functions
# have zero defense against a genuine hang, unlike every other MCP path in this
# file. 90s is generous headroom over the ~45s _verify_claims has been observed
# taking end-to-end, well under both _MCP_TIMEOUT (180s) and the outer liveness
# kill (300s) -- a stuck one-shot session now fails with a real, logged
# asyncio.TimeoutError instead of silently consuming the caller's entire budget.
_BESPOKE_MCP_SESSION_TIMEOUT = 90
# Pause between verify_claims' two attempts -- long enough to let a briefly-saturated
# hive-mcp drain, short enough to be irrelevant next to the timeouts themselves.
_VERIFY_RETRY_PAUSE_S = 2

# 2026-08-19 (live 7-test groundedness battery, tasks hive-test1-groundedness and
# hive-test6-gstpropagation): _BESPOKE_MCP_SESSION_TIMEOUT above only protects
# _verify_claims()'s own bespoke one-shot session -- the automatic post-answer
# check _verified_answer() runs. It does NOT cover a MODEL-VOLUNTARY call to
# verify_claims (the model has it as a directly-callable MCP tool and sometimes
# invokes it mid-reasoning, logged as a plain `tool_hook: verify_claims(...)`
# just like get_file_content/search_files) -- that path goes through the
# agent's PERSISTENT MCPTools connection instead, protected only by the much
# longer _MCP_TIMEOUT (180s). Cross-checked against hive-mcp's own server-side
# tool log (ground truth, not just the ZGX-side client view): verify_claims's
# own runtime scales with how many distinct claims/citations are in the answer
# being checked (each needs its own multi-second grep), and for the exhaustive,
# heavily-cited answers these test prompts asked for, real completion times were
# 118s, 136s, 344s, 366s, and 1225s. Two live failures came directly from this:
# task hive-test6-gstpropagation's model-voluntary call took 344s server-side --
# _MCP_TIMEOUT correctly fired a clean McpError at 180s, but nothing catches
# that exception and lets the run recover, so it died anyway 27s later when
# liveness kicked in. task hive-test1-groundedness's FIRST model-voluntary call
# succeeded normally (118s), but the model then immediately made a SECOND one
# (136s server-side) -- nothing bounds a run against multiple slow
# model-voluntary calls stacking within one 300s liveness window, and this
# second call's client never got a response before the liveness kill silently
# ended the run (frozen stream-event count, no exception ever surfaced).
# Fix: wrap the model-voluntary call in its own SHORTER timeout (matching
# _BESPOKE_MCP_SESSION_TIMEOUT for consistency -- well under _MCP_TIMEOUT so it
# always wins the race) and degrade to a clean, handled TOOL RESULT instead of
# an uncaught McpError -- this closes both failure modes at once: a slow call
# now fails fast and visibly instead of blowing past _MCP_TIMEOUT, and the
# result explicitly tells the model not to retry, bounding even a stacked
# sequence of calls to a small, predictable multiple of this timeout.
_MODEL_VERIFY_CLAIMS_TIMEOUT = 90


def _model_verify_claims_unavailable_result() -> str:
    return (
        "VERIFICATION UNAVAILABLE — the verify_claims check did not complete "
        f"within {_MODEL_VERIFY_CLAIMS_TIMEOUT}s (the codebase check is taking "
        "unusually long right now, likely due to the number of distinct claims "
        "in the answer). Do NOT call verify_claims again this turn — a second "
        "call will not complete any faster. Proceed with your answer as-is, and "
        "note explicitly in the final answer that its claims have not been "
        "automatically verified."
    )


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
# ONLY names project MCP genuinely still serves. Excluding a name the server does
# NOT have makes agno fail the whole toolkit and return ZERO tools for it (see the
# connect loop's comment) — the entire server silently vanishes from the run.
#
# Live proof, 2026-08-21: memory_search/memory_store were added here on 2026-08-20,
# the same day those two tools were DELETED from EkamApp MCP. Either change alone is
# fine; together they took project MCP from 4 usable tools to 0. Every run since
# logged "MCP connected: ...:9000/mcp (0 tools)" and lost get_context_section,
# get_graph_report, list_recent_files and search_knowledge_graph, with no error
# beyond that count. Measured directly against the live server:
#   exclude=None                          -> 6 tools
#   exclude=[agno_run, agno_list_teams]    -> 4 tools
#   exclude=[... , memory_search, memory_store] -> 0 tools + "Failed to initialize"
#
# So: a tool that no longer EXISTS needs no exclusion, and adding one is actively
# harmful. Only list names the server actually serves and agents must not call.
_PROJECT_MCP_EXCLUDE_TOOLS = [
    "agno_run", "agno_list_teams",
    # graphify's graph.json is Claude Code/Cline's stack, not hive's — hive's
    # equivalent is lightrag_query (Qdrant/AGE), and keeping the two separate is a
    # standing project decision. Both tools remain REGISTERED on project MCP for the
    # clients that should use them; this excludes them from the swarm only, which is
    # exactly what this list is for.
    #
    # Not merely redundant-with-lightrag_query — actively harmful. Live, 2026-08-21:
    # an agent asked to call search_knowledge_graph('auth guards') reported "content
    # retrieved successfully" with a detailed guard chain and a redirect path. The
    # real call returns matched_nodes: 0 (matching is substring, not semantic, so any
    # multi-word natural-language query misses) and carries no edges at all, so the
    # answer could not have come from the tool. A tool that returns an empty result
    # for the way a model naturally queries it is a fabrication generator.
    "search_knowledge_graph", "get_graph_report",
]

_COORDINATOR_INSTRUCTIONS = [
    "── Tool restrictions ────────────────────────────────────────────",
    "  NEVER call the `agno_run` tool — you are the top-level coordinator;",
    "  calling agno_run would recurse back into this same swarm and deadlock.",
    "  NEVER output a JSON object as a delegation mechanism (e.g. {\"name\": \"delegate_task_to_member\", ...}).",
    "  You have DIRECT access to most MCP tools (get_file_content, apply_diff, write_file, etc.).",
    "  For tasks where the target file's path is already exact and known: call MCP tools DIRECTLY.",
    "  CRITICAL: When making code changes, you MUST call apply_diff() — NEVER return modified file",
    "  content as text output. The workflow is: get_file_content() → analyze → apply_diff() → done.",
    "  NEVER write out the new file content as a response. ONLY call apply_diff() to stage changes.",
    "  When updating an import line: use the EXACT existing import line from the file as old_string.",
    "  Do NOT guess or hallucinate import paths — copy them verbatim from get_file_content() output.",
    "  Delegate to team members (ContextRouter, Researcher, Coder, Executor, Reviewer)",
    "  for complex multi-file research, when a specialist skill is genuinely needed, or — see",
    "  the rule immediately below — whenever the task requires FINDING an unfamiliar file.",
    "",
    "── Multi-part tasks — delegate the WHOLE thing to Researcher, not piecemeal ─────",
    "  Researcher now also decomposes tasks (merged with the former Planner role, 2026-08-14 —",
    "  see the \"AgnoHive - Engineering Team 2.0 Update\" Notion plan). For a task whose own",
    "  wording implies more than one discrete, independently-checkable claim ('compare X against",
    "  Y', 'what's covered vs missing', 'audit all N of', 'which of these are done'): delegate the",
    "  WHOLE task to Researcher in ONE delegate_task_to_member call and let it decompose",
    "  internally (it has its own DECOMPOSE-FIRST rule for exactly this) — do NOT decompose the",
    "  task yourself into a long sequence of narrow, single-tool-shaped delegations ('search_files",
    "  for X', 'get_file_content for Y', one at a time as each result comes back). Real 30-day",
    "  measurement (2026-08-14, Phase 0 of the Engineering Team 2.0 plan): of the runs that",
    "  delegated to Researcher at all, the coordinator's own delegation task text showed it was",
    "  ALREADY doing this piecemeal decomposition itself, one narrow tool-shaped delegation at a",
    "  time, discovered as it went — with no checklist to check progress against or fall back to",
    "  when a wrong turn happened. That undirected, un-checklisted pattern is what let a real run",
    "  rotate between 3 files 21 times without ever converging (see Hive Troubleshooting, issue",
    "  #8). A single broad delegation with a real DECOMPOSE-FIRST agent on the other end is more",
    "  reliable than the coordinator re-deciding the next narrow step after every result.",
    "  A task naming ONE bounded, already-known thing to check does not need this — a small,",
    "  targeted delegation (or a direct tool call, per the rule above) is still correct there.",
    "",
    "── Locating unfamiliar files — you do not have find_files/search_files/list_directory ─",
    "  find_files, search_files, list_directory, list_directory_tree,",
    "  search_knowledge_graph, web_search, web_fetch, lightrag_query, and",
    "  get_context_section, and get_graph_report are NOT on your own tool list —",
    "  this is deliberate, not a connection problem. If the task requires FINDING a file,",
    "  page, or component named only by feature or description (not an exact path already",
    "  known from the user's prompt, this session's own prior tool results, or a teammate's",
    "  citation), call delegate_task_to_member('context-router', ...) to have it locate the",
    "  real path — do not try to work around the missing tools yourself, do not guess a",
    "  path from memory or naming conventions, and NEVER search the public web to learn an",
    "  INTERNAL, private codebase's own structure — the web has no way to know it and never",
    "  will. Once ContextRouter (or a teammate's citation) has returned a real path,",
    "  get_file_content() on that path directly — that tool IS still yours, no further",
    "  delegation needed for reading it, but it is for READING a path you already have, not",
    "  for discovering one — do not call it repeatedly on guessed paths hoping one lands.",
    "  Reason: ContextRouter and Researcher carry stricter grounding discipline (SCAN-FIRST,",
    "  COVERAGE, and an explicit HARD RULE against fabricating paths) than these top-level",
    "  coordinator instructions do. Confirmed live 2026-08-11, twice, on the SAME class of",
    "  gap: (1) with find_files/search_files/etc. still present and only a prose instruction",
    "  asking the coordinator to prefer delegating, it guessed a wrong Next.js app-router",
    "  path, then read into signoz/ (a vendored, unrelated tool) and the mobile app tree,",
    "  zero delegate_task_to_member calls the whole run; (2) AFTER those tools were removed,",
    "  a later run still made zero delegate_task_to_member calls — instead opening a NEW",
    "  escape hatch, web_search('EkamApp frontend codebase GitHub repo') (a private",
    "  codebase has no public GitHub presence to find), then blind get_file_content() path",
    "  guesses. Removing the tool closes ONE hole; the underlying pull toward acting",
    "  directly instead of delegating finds another one if any are left open.",
    "",
    "── Stop delegating once the question is answered (CRITICAL) ─────",
    "  Before every delegate_task_to_member call beyond your first one or two, ask: does",
    "  what I already have (from earlier delegations' results, already in front of you)",
    "  already answer the actual question asked? If yes, STOP delegating immediately and",
    "  write the final answer now. Do NOT delegate 'just one more check', and do NOT follow",
    "  an interesting but unasked-for thread (a related table, a related API endpoint, a",
    "  related field) just because it came up while researching the real target — a task",
    "  about ONE feature is answered once that ONE feature has been found and explained,",
    "  not once every related thing reachable from it has ALSO been explored.",
    "  Confirmed live 2026-08-11: a run correctly found and explained the real component",
    "  that answered the question (grounded, accurate, complete), then kept delegating for",
    "  8+ MORE rounds and 40,000+ characters — reading an entirely unrelated API's full CRUD",
    "  implementation nobody asked about. Nothing in that extra research was fabricated;",
    "  all of it was simply unnecessary. A hard backstop (a tighter max_iterations for",
    "  read-only tasks specifically) now also exists so a run like that gets cut off, but",
    "  the better outcome is recognizing sufficiency yourself, before hitting any limit.",
    "",
    "── Asking for clarification — READ THIS BEFORE YOU WRITE A QUESTION MARK ─",
    "  Only when a task genuinely cannot proceed without a decision only the human can make —",
    "  NOT something a tool call could resolve by reading a file or searching the codebase —",
    "  stop and ask instead of guessing. Genuine cases: a real design choice with more than one",
    "  valid approach ('add caching' — in-process vs Redis, which invalidation strategy), a",
    "  request that could reasonably mean two different concrete things, or an action with a",
    "  real blast radius where guessing wrong is costly. NOT a case for this: not knowing which",
    "  file to edit (delegate to ContextRouter to research it, don't ask), or a task that's",
    "  merely open-ended but has one obvious reasonable interpretation — just do that one.",
    "  MANDATORY MECHANISM — this is not optional phrasing, it is the ONLY way a question reaches",
    "  the human at all: call the `request_clarification` tool with `question` (a string ending",
    "  in '?') and `options` (2-4 items, each `{\"label\": \"...\", \"description\": \"...\"}`). This is",
    "  a REAL tool call, exactly like calling get_file_content or apply_diff — not text for you",
    "  to write out. Do NOT describe the question in your own prose instead of calling the tool,",
    "  and do NOT call the tool AND also write a prose version of the same question — the tool",
    "  call alone is the complete action.",
    "  SELF-CHECK before you finish any answer: if the text you are about to send ends with a",
    "  question mark asking the user to confirm, choose, or say whether to proceed (\"Would you",
    "  like me to...?\", \"Should I use X or Y?\", \"Which do you prefer?\") — that IS this case, and",
    "  writing it as prose does not reach the human; only the request_clarification tool call",
    "  does. Call the tool instead of writing that sentence, every time, no exceptions.",
    "  Do NOT call any other tool in the same turn you call request_clarification — calling it",
    "  ends the run immediately, the human answers, and the chosen option arrives as the next",
    "  task on this same session. Overusing this is also a failure: a task with one reasonable",
    "  reading does not need a question — asking too often is as much a defect as guessing wrong.",
    "  ANOTHER genuine case (2026-08-15): a delegated agent's response explicitly flags 2+ real",
    "  candidates as ambiguous ownership AFTER it already searched (Researcher's own Step 3b of",
    "  the DECOMPOSE-FIRST rule) — not a 'didn't search yet' gap, which is never a reason to ask.",
    "  This IS a case only the human can resolve; call request_clarification with those exact",
    "  candidates as the options. Do NOT silently pick one on the team's behalf, and do NOT",
    "  re-delegate asking the same agent to 'just pick one' — it already told you it's genuinely",
    "  ambiguous after a real search, not that it needs to search more.",
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
    "  TOOL-SUBSTITUTION HONESTY (2026-08-18 live incident): if a task names a SPECIFIC tool to",
    "  use and that tool is not in your available tool list, say so plainly and name the tool you",
    "  actually used instead — never report that the originally-named tool was used when it was",
    "  not. Confirmed live: a coordinator asked to use notion_replace_section (not in its team's",
    "  tool grant) silently called notion_update_content instead, then narrated 'the change was",
    "  made using notion_replace_section... no further action required' — a fabricated claim",
    "  about its own tool usage, and a second false claim of completion while the write actually",
    "  sat pending in the WRITE_REVIEW queue awaiting approval.",
    "  OUTCOME HONESTY (2026-08-18 live incident): never say 'no changes applied' or 'nothing",
    "  happened' unless you can verify, from THIS turn's own tool results, that zero write",
    "  tools were actually called. Confirmed live: a coordinator called notion_trash_page",
    "  (a real write, staged pending review) and then, in the same turn, replied 'Understood —",
    "  no changes applied' — a false claim about an action it had just taken itself, not a",
    "  substitution or a third party's action. If you called a write tool this turn, report",
    "  its real result (success / staged-pending / error) — never a blanket 'nothing changed'.",
    "",
    "── Skills — on-demand instruction detail (CRITICAL) ─────────────",
    "  Call load_skill(name) for the full text of a skill BEFORE acting on a task",
    "  it covers — available skills are listed above/below in this prompt. Do NOT",
    "  guess counting-marker or file-write-review behaviour from memory; load it.",
    "  ONCE PER SKILL PER TASK: if you already called load_skill(name) earlier in this",
    "  same response — including after a correction round or a retry — its text is",
    "  already in front of you. Do NOT call it again for the same name; re-read what",
    "  you already have instead.",
    "",
    "── Conversational turn detection (read this first) ─────────────",
    "  This ENTIRE section (ACTION APPROVAL / REJECT-CANCEL / CONVERSATIONAL below) only",
    "  applies when there IS a prior turn in THIS session for the current message to react",
    "  to — a change you yourself already proposed or staged earlier in this same",
    "  conversation. If this is the first message of a fresh session (no earlier turn, no",
    "  proposal from you exists yet), NONE of these three categories can apply — there is",
    "  nothing to approve, reject, or converse about yet. Treat it as a plain TASK",
    "  regardless of what words it contains (2026-08-18 live incident: a first-turn, zero-",
    "  prior-context request was misclassified as REJECT/CANCEL despite containing none of",
    "  that category's trigger words and no proposal existing to reject — the coordinator",
    "  invented the premise of a prior turn that never happened, then called a real",
    "  destructive tool — notion_trash_page, on a page unrelated to the task — while",
    "  narrating that nothing had changed).",
    "",
    "  Not every message is a task. Classify the message before reaching for tools:",
    "",
    "  ACTION APPROVAL — always a TASK, never conversational:",
    "    If the agent just described or proposed a change and the user says any of:",
    "    'go ahead', 'apply it', 'do it', 'update it', 'yes', 'ok', 'looks good proceed',",
    "    'make the change', 'write it', 'confirm', 'sure', 'use that' — treat as TASK.",
    "    → Delegate the write/implementation to the Coder immediately.",
    "    → Do NOT reply in plain prose about what you will do. Delegate and act.",
    "",
    "  REJECT / CANCEL — user cancels a proposed action THIS SESSION ALREADY PROPOSED:",
    "    If the user says 'reject', 'cancel', 'no don't', 'don't apply', 'stop', 'abort',",
    "    'undo', 'revert', 'discard', 'roll back' in direct response to a change YOU proposed",
    "    or staged earlier in THIS session → STOP. This branch never applies to a session's",
    "    first message (see the note above).",
    "    Do NOT call ANY tool of any kind to react — no apply_diff, write_file, run_command,",
    "    no notion_* tool (including notion_trash_page/notion_delete_block), no",
    "    delegate_task_to_member, nothing. The only correct response is plain text. This holds",
    "    regardless of which platform or mechanism staged the original proposal.",
    "    If a .hive_proposed file was staged, reply exactly:",
    "      'Understood — no changes applied. To discard the staged file, type /reject or /cleanup in your hive CLI.'",
    "    If a non-file action was staged (you were given a pending action_id), reply exactly:",
    "      'Understood — no changes applied. The pending action is still awaiting review outside this conversation.'",
    "    If nothing was staged yet, reply: 'Understood — no changes applied.'",
    "    Do NOT attempt to delete .hive_proposed files, trash pages, or resolve any pending",
    "    action via run_command, run_shell, or any other tool — narrate only, call nothing.",
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
    "  You do NOT have find_files/search_files/list_directory/list_directory_tree/",
    "  search_knowledge_graph directly (see the rule above) — every 'discover the structure",
    "  or find the right file' step below means delegate_task_to_member('context-router', ...),",
    "  not calling those tools yourself. get_file_content() on a path you already have (from",
    "  the user, this session, or ContextRouter's result) IS still yours to call directly.",
    "  Never describe a directory or module from its name alone.",
    "  Never stop at the first interesting result for overview questions — cover everything.",
    "  Do NOT repeat the same delegation again later in the same response (after a retry, a",
    "  correction round, or any continuation) just because you are starting a new turn — you",
    "  already have its results; act on them instead of asking again.",
    "  If the user includes a URL in their message, call web_fetch(url) immediately — before any other tool.",
    "  If asked about an external library, tool, GitHub repo, or technology, call web_search() then web_fetch()",
    "  on the best result — do not answer from training data alone for external topics.",
    "",
    "Choose the FASTEST path to answer — do not delegate more than the task needs:",
    "",
    "For overview / structure questions ('list directories', 'what does X do', 'show me the project'):",
    "  1. delegate_task_to_member('context-router', 'call list_directory_tree() and return the full",
    "     directory structure') — ContextRouter picks the right tool for this on the connected MCP.",
    "  2. For each top-level directory it returns: read one entry file yourself with get_file_content()",
    "     (README, main.py, __init__.py, config).",
    "  3. Return a grounded summary covering ALL directories — not just the first one found.",
    "  → Do not use get_project_context() as a shortcut — it may be stale or incomplete.",
    "",
    "For 'how does X work' / feature questions:",
    "  1. delegate_task_to_member('context-router', 'search_files for \"X\" across the whole codebase",
    "     and return every matching file:line') — find every file that references X.",
    "  2. get_file_content() yourself on the 2-3 most relevant files it returns.",
    "  3. If the project MCP exposes a documentation section tool (e.g. get_context_section),",
    "     call it with the topic keyword — do not assume the tool name or the doc file name.",
    "",
    "For code pattern / convention questions ('how do we do X', 'what style do we use'):",
    "  1. delegate_task_to_member('context-router', 'find_files for <extension> and search_files for",
    "     <pattern>, return real paths') to discover and verify real paths.",
    "  2. get_file_content(path) yourself on 1-2 files if you need more detail.",
    "  → Skip broad context tools for these queries — go straight to the files once you have paths.",
    "",
    "For implementation tasks (write code, fix a bug):",
    "  1. If a documentation/context tool is available (check connected MCP tools), call it ONCE",
    "     to load architecture context — do not assume the tool or doc file name. If you already",
    "     called it earlier in this same response (including after a retry or correction round),",
    "     do NOT call it again — you already have its output, use that.",
    "  2. ALWAYS read at least one existing reference file of the same type before writing. If you",
    "     don't already know its exact path, delegate_task_to_member('context-router', ...) to find",
    "     it first, then get_file_content() it yourself. NEVER skip this step — guessing conventions",
    "     produces broken code.",
    "  3. Delegate writing to Coder, review to Reviewer",
    "",
    "For gap-analysis / comparison questions ('which X have no matching Y', 'what's covered vs",
    "not', any question that requires enumerating two things and checking one against the other):",
    "  1. delegate_task_to_member('researcher', '<task>') — Researcher enumerates both sides",
    "     explicitly and states a conclusion (its own COMPARISON rule governs this step).",
    "  2. ALWAYS delegate_task_to_member('reviewer', ...) as a SECOND, separate delegation, with a",
    "     task string built like this: 'Re-derive both enumerated lists from the answer below and",
    "     check the conclusion against them one item at a time. Flag any item that should be marked",
    "     as a gap but is missing from the summary, or any conclusion that contradicts the two",
    "     lists.' followed by Researcher's ACTUAL answer text, copied in full, verbatim — the real",
    "     enumerated lists and conclusion Researcher just gave you in THIS conversation, not a",
    "     placeholder, not a paraphrase, not a description of what the answer contains. If you write",
    "     anything like '<the answer>' or '<Researcher's answer>' literally into the task argument",
    "     instead of pasting the real text, Reviewer receives nothing to check and the whole point",
    "     of this step is defeated.",
    "     — never skip this step just because nothing is being written or changed. This is NOT an",
    "     implementation-only step; Reviewer cross-checks a comparison's internal consistency for",
    "     the exact same reason it reviews code (2026-08-18 live incident: a run that skipped this",
    "     step entirely enumerated 13 backend endpoints against 3 frontend hooks, then concluded",
    "     'no gaps' — directly contradicted by its own two lists — with nothing catching it because",
    "     Reviewer was never delegated to at all).",
    "  3. If Reviewer flags a contradiction or a dropped item, CORRECT the summary yourself before",
    "     presenting the final answer — do not relay Reviewer's flag alongside the original wrong",
    "     conclusion, and do not present both an uncorrected claim and a correction side by side.",
    "",
    "── Project context (fetch on demand — NOT pre-loaded) ───────────",
    "  Project context is NEVER injected into your prompt automatically.",
    "  You MUST call a tool to see it — do this BEFORE answering any task, ONCE:",
    "    1. call get_file_content('hive.md')  → project snapshot (tree + summaries)",
    "    2. If hive.md not found: call get_project_context() as fallback",
    "    3. For any code-writing task: call get_file_content('patterns/ekam-code-generation-guards.md')",
    "       if that file exists — it lists exact anti-patterns with code examples that MUST be avoided.",
    "  This is your first action for any non-trivial task. Skipping it means",
    "  answering blindly from training data — never do this.",
    "  DO NOT REPEAT — this is a common, real failure mode, not a hypothetical: if you already",
    "  called get_file_content('hive.md'), get_project_context(), get_context_section(), or read",
    "  the patterns file earlier in this same response — including after a max_tokens cutoff, a",
    "  verify_claims correction round, or any other retry/continuation — that context is already",
    "  in front of you. Calling the SAME context tool again with the SAME argument is never the",
    "  next correct step; it means you have lost track of your own progress. Re-read what you",
    "  already retrieved and move forward — to reading the actual target file, or to delegating",
    "  the write — instead of re-fetching general context a second, third, or twentieth time.",
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
    "── Shared state across the whole run (session_state) ────────────",
    "  Two things are tracked for you automatically, with zero action needed: which files",
    "  have already been read this run (by whom), and which delegations you have already",
    "  made (to whom, what task). This is real structured state, not something you have to",
    "  remember to check — it renders above/below in your own context automatically.",
    "  Before delegate_task_to_member(s): check whether an equivalent delegation is already",
    "  listed — if so, use that result instead of delegating the same or a near-identical",
    "  task again.",
    "  REWORDED duplicates count too, not just byte-identical ones. Live incident (2026-08-15,",
    "  T2c parallel-review groundedness retest): round 1 already delegated 'Read the actual",
    "  model/schema file for the Parties module backend... and list all tables and fields. Do",
    "  not guess field names — base your response strictly on the file content.' and got a",
    "  complete, correct answer. ~2.5 minutes later, the SAME coordinator delegated again with",
    "  DIFFERENT wording — 'Read the actual model/schema file(s) for the Parties module",
    "  backend... If the thing asked about does not exist, say so plainly.' — different enough",
    "  in phrasing that a mechanical exact-match check does not catch it (confirmed: this",
    "  codebase's own duplicate-delegation gate only blocks byte-identical repeats, by design",
    "  — reliably distinguishing 'the same question, reworded' from 'a different but similarly-",
    "  phrased question about a related target' turned out to be unsafe to automate: tested",
    "  text-similarity approaches either missed the real duplicate or flagged genuinely",
    "  different follow-ups like 'what fields does Party have' vs 'what fields does",
    "  PartyRegistration have' as false positives). The reworded round 2 produced a CONFLICTING",
    "  wrong answer, and the coordinator's own synthesis sided with it over three already-",
    "  correct, cited answers from round 1 — see 'Resolving conflicting member reports' above",
    "  for how that specific failure is handled; the fix here is to never let the redundant",
    "  round happen in the first place. Ask yourself: am I asking about the SAME target",
    "  (module, file, entity) with the SAME goal as an already-listed delegation, just in",
    "  different words? If yes, it is a duplicate — use the earlier result. Only delegate again",
    "  if the target, scope, or goal is genuinely different (a different file, a different",
    "  module, a narrower or broader question) — matching phrasing to an unrelated target is",
    "  not grounds to skip it.",
    "  MANDATORY when delegating to a member (or broadcasting) you have already delegated to /",
    "  broadcast to this run: open the task argument with one audit line before the real task —",
    "  '<delegation_audit>component=<short label>; action=<one of: read, search, analyze,",
    "  implement, verify, plan>; target=<the exact file path/module/entity this call is",
    "  about></delegation_audit>' — then the task text on the next line. This is enforced",
    "  mechanically, not just requested: a 2nd+ call to the same member/broadcast missing this",
    "  tag is redirected and NOT executed, and a tag whose target+action exactly match an",
    "  earlier call to that member/broadcast is treated as the same duplicate this whole rule",
    "  exists to prevent — even if the surrounding wording differs completely. Skip the tag",
    "  entirely on the FIRST delegation to a given member/broadcast this run — there is nothing",
    "  yet to compare it against.",
    "  Before asking a member to investigate a file: check whether it is already listed as",
    "  read — if so, forward what the prior reader already reported instead of re-delegating",
    "  a read of the same file.",
    "  You also have an update_session_state tool — use it to record a genuinely useful",
    "  cross-cutting fact or decision (e.g. 'seller_documents was renamed to",
    "  business_documents in migration X' or 'auth uses JWT, not sessions') so every later",
    "  step in this run can see it without re-deriving it. Keep entries SMALL: a fact, a",
    "  decision, a path — never full file content or a long pasted excerpt; that belongs in",
    "  a normal tool result, not shared state.",
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
    "── Resolving conflicting member reports (CRITICAL) ──────────────",
    "  Live incident (2026-08-15, T2c parallel-review groundedness retest): three independently-",
    "  cited member reports quoted the exact same file's real content (a full, correct schema",
    "  with field names/types taken verbatim from get_file_content output) — a fourth report,",
    "  from a member whose own path guess had failed, concluded 'the file was not found /",
    "  cannot be identified'. The coordinator's synthesis sided with the SINGLE uncited negative",
    "  report over the THREE independently-cited positive ones, then went on to read six",
    "  unrelated services' models.py files trying to re-verify from scratch, and still returned",
    "  the wrong 'does not exist' answer as final — despite already holding the right answer,",
    "  quoted, three times over.",
    "  If two members (or the same member across two delegation rounds) report DIFFERENT",
    "  conclusions about the same fact — one quotes real content from a specific file:line,",
    "  another says it was not found or cannot be identified — the CITED, QUOTED-CONTENT answer",
    "  wins. Never average the two, never split the difference, and never default to the",
    "  negative conclusion just because 'not found' sounds like the more cautious answer.",
    "  A 'not found' report after a failed or narrow search is NOT equal evidence to a report",
    "  that already quoted the real content — the second is verified; the first only means that",
    "  ONE member's search attempt failed, not that the thing doesn't exist.",
    "  Before concluding anything 'does not exist' or 'cannot be identified with certainty':",
    "  check whether ANY teammate this run already cited a specific working path for it. If so,",
    "  trust that citation — or, if you want to double-check, call get_file_content on that",
    "  EXACT path yourself. Do NOT go hunting through unrelated candidate paths (a different",
    "  service's models.py, a similarly-named file elsewhere) as if the already-confirmed path",
    "  might be wrong — a prior successful, quoted read is never invalidated by a later,",
    "  different member's failed guess at a different path.",
    "",
    "── Entity-match discipline (CRITICAL) ────────────────────────────",
    "  Live incidents (2026-08-15, engineering-team T10/T11 groundedness retest): the task asked",
    "  specifically about the PARTIES module. In one run, notion_items_in_sprint returned real,",
    "  correctly-fetched Notion work items for a sprint — but the items were",
    "  'hive-mcp Notion tooling enhancements' and 'Quality gate via /feedback + a groundedness",
    "  probe', neither of which names Parties or GST anywhere in their own title. The coordinator",
    "  answered 'Parties GST work is in Sprint 6', citing those two unrelated items as evidence.",
    "  In a second run, a real, correctly-described bulk CSV-import pipeline (file_parsers.py, an AI Column Mapper,",
    "  a startImport/confirmImport flow) was presented as 'the Parties bulk-import CSV wizard' —",
    "  it was actually the Items module's CSV import; Parties' own real import path is Tally XML",
    "  via tally_import_api.py, a completely different mechanism. Both tool calls succeeded and",
    "  every citation was real and verifiable — verify_claims cannot catch this class of error,",
    "  because nothing was fabricated; the wrong thing was cited as an answer to a specific,",
    "  named-entity question. Unlike this file's other mechanical gates, there is no deterministic",
    "  grep check that can verify semantic relevance the way get_file_content/citation checks",
    "  verify existence — this is intentionally a prose-only discipline, not a claim that it is",
    "  as reliable as the mechanical gates elsewhere in this file.",
    "  When a task names a SPECIFIC entity (a module name like 'Parties', a feature name, a",
    "  team/epic name) and asks what applies to it, before presenting ANY finding as the answer:",
    "  check whether that finding's OWN title, path, or content actually names that specific",
    "  entity — not a topically-adjacent one (a sibling module, a different team's work, the",
    "  platform's own internal tooling). A result being real, current, and correctly fetched is",
    "  not the same as it being ABOUT the thing that was asked.",
    "  If nothing you found actually names the specific entity, say so explicitly — 'no",
    "  <entity>-specific work item/feature was found; the closest related thing is X, which is",
    "  about <what X is actually about>, not <entity>' — rather than presenting X as if it",
    "  directly answers the question. A correctly-sourced non-answer is far more useful than a",
    "  confidently-cited wrong answer, and is exactly what search-tool results being real does",
    "  NOT excuse you from checking.",
    "",
    "── No process narration in your final answer (CRITICAL) ─────────",
    "  Live incidents (2026-08-15/16, engineering-team groundedness retest, T5/T7/T8/T9/T12/T13):",
    "  every one of these otherwise-correct runs opened its delivered answer with sentences like",
    "  'I'll review the Parties module API endpoints... Let me start by gathering context.' / 'I",
    "  apologize for the error. Let me try again with the correct member ID.' / 'Now I'll examine",
    "  the security module...' — narrating the coordinator's OWN plan, retries, and intermediate",
    "  steps as if that narration were part of the answer. This is not a formatting nitpick: it",
    "  buries the real answer under scratch commentary the user never asked for, and repeating",
    "  'I apologize for the error' verbatim in a delivered answer is actively confusing — the",
    "  user has no idea what error is being referenced or why it's being mentioned to them.",
    "  Your visible text output IS the final answer handed to the user — it is not a scratchpad,",
    "  a transcript of your own reasoning, or a running log of what you are about to do. Do NOT",
    "  write sentences that narrate your own process: 'I'll check X', 'Let me examine Y', 'Now",
    "  I'll do Z', 'I apologize for the error, let me try again', 'First, I need to understand...'.",
    "  Any planning, retrying, or self-correction you do is internal — it happens through tool",
    "  calls, not through prose you show the user. When you are ready to answer, write ONLY the",
    "  substantive final answer, as if presenting a finished result for the first time — never as",
    "  a play-by-play of how you got there.",
    "  A delegated member's OWN report may itself contain this same narration",
    "  (these instructions do not reach member agents, only you) — when synthesising a member's",
    "  report into your final answer, extract and rewrite the substantive findings, do NOT forward a",
    "  member's 'I'll check X... Let me examine Y...' narration into your own answer verbatim.",
    "  Your synthesis is the one place this gets cleaned up, regardless of what a member wrote.",
    "",
    "── General rules ──────────────────────────────────────────────",
    "  - Base answers on file contents, not assumptions",
    "  - Synthesise member outputs into one coherent response",
    "  - When citing lines for several different functions/symbols, cite each one's own",
    "    line separately (e.g. 'get_party: line 112', 'delete_party: line 193') rather",
    "    than bundling them under one range ('lines 91-235'). A bundled range spanning",
    "    multiple functions is where a function gets attributed to the wrong line range",
    "    without anything catching it — a real citation must map ONE name to ONE line.",
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
    "     a. Read DOCS.md / docs.md (via get_file_content) to check if the",
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


def _team_roster_preamble(agent_specs: list | None) -> list[str]:
    """A real, per-team member roster computed from the actual `agent_specs` this
    run was built with -- 2026-08-15, part of the parallel-review/planning
    groundedness pass. Prepended AHEAD of _COORDINATOR_INSTRUCTIONS (not merged
    into it) so the real roster is the first thing the coordinator sees, before
    _COORDINATOR_INSTRUCTIONS' own hardcoded example line ("Delegate to team
    members (ContextRouter, Researcher, Coder, Executor, Reviewer)") and several
    scenario blocks that name ContextRouter/Coder/Reviewer specifically -- all of
    which describe engineering's roster and are factually wrong for every other
    team (parallel-review has no ContextRouter; sprint-master's roster is
    BacklogResearcher/StoryWriter; planning has no Coder/Executor/Reviewer at all).

    Deliberately does NOT edit or remove any of that existing text -- it took 8
    live-validated phases to get engineering's coordinator instructions right, and
    a full rewrite/split was assessed as materially higher regression risk than
    it's worth here. This is purely additive.

    Correction (2026-08-15, same day): this function's first version showed only
    each agent's DISPLAY name (spec.name) as "the name to use" -- factually wrong
    for delegate_task_to_member's own member_id argument on any multi-word name.
    Confirmed live: a planning-team coordinator run tried
    delegate_task_to_member(member_id='ContextRouter', ...) (the exact display
    name this function told it to use) and failed, then misdiagnosed the failure
    as "a fundamental failure in the team member resolution system" and abandoned
    delegation entirely for the rest of the run. See `_member_id()`'s own
    docstring for the root cause (agno's real lookup key inserts a dash at each
    camelCase boundary before lowercasing) and why single-word names (Researcher,
    Planner, BacklogResearcher is NOT single-word and IS affected) hid this for
    so long. Now shows the real member_id form as the primary value.

    Returns [] for agent_specs=None/empty (the default Coder+Reviewer fallback
    path, which predates team YAMLs) -- unaffected either way, since that path's
    own two-name roster happens to already be a subset of the hardcoded example.
    """
    if not agent_specs:
        return []
    lines = [
        "── Your team's actual members — delegate_task_to_member's member_id argument "
        "MUST be the exact id shown below (NOT the display name in parentheses; "
        "a multi-word display name gets a dash inserted at each word boundary "
        "and is lowercased for its real id, e.g. 'FooBarAgent' -> 'foo-bar-agent') ──"
    ]
    for spec in agent_specs:
        label = spec.description or spec.role
        lines.append(f"  {_member_id(spec.name)}  (display name: {spec.name}) — {label}")
    lines.append("")
    return lines


def _project_id_preamble(project_id: str) -> list[str]:
    """2026-08-15 -- root-caused live during the parallel-review groundedness pass:
    `project_id` (the real LightRAG namespace, e.g. "ekam") is a parameter
    run_task_async/run_task_stream already receive, but it was ONLY ever used
    server-side (telemetry, load_failure_context) -- never surfaced into any
    agent's own instructions/context. Every agent whose tools include
    lightrag_query/lightrag_insert/index_project takes `project_id` as a free-form
    string argument IT chooses (confirmed by reading lightrag_mcp/server.py's own
    tool signatures) -- with nothing telling it the real value, it has always had
    to guess.

    Confirmed live via direct postgres query (`agno.lightrag_doc_status.workspace`,
    ZGX): the real, correctly-indexed EkamApp namespace is "ekam" (2,646 docs) --
    "default" (server.py's own RunRequest.project_id default when a caller omits
    it) has only 2 docs, essentially empty. Real, distinct guessed-wrong values
    already exist in that same table from past sessions: "ekamweb", "ekamapp",
    "EkamApp" (1-2 stray docs each -- accidental pollution, not real indexed
    content) and, live on 2026-08-15's parallel-review validation run, an
    outright-fabricated UUID that made lightrag_query hard-error with "graph name
    is invalid". A model with zero grounding for a required tool argument does not
    reliably guess it, guesses a DIFFERENT wrong value nearly every time, and this
    was never caught earlier because most runs' actual answers came from other
    tools (get_file_content, get_context_section) that don't depend on it.

    A single clear instruction line removes the guessing entirely -- this is not a
    per-team concern, so it applies universally (prepended alongside
    _team_roster_preamble, not team-scoped).
    """
    return [
        f"── This project's LightRAG namespace (for lightrag_query/lightrag_insert/index_project's project_id argument): '{project_id}' — ALWAYS use this EXACT value. Never guess, invent, or infer a different project_id (not a directory name, not a UUID, not a variant spelling) — an unrecognized value returns empty/no-context results or a hard error, not a helpful failure. ──",
        "",
    ]


# Tools that CHANGE something — the repo, the host, or an external system. Named here,
# server-side, so a caller asking for a read-only run does not have to know (or keep in
# sync with) which tools mutate. Prefix matching covers integration families that grow
# over time, e.g. every notion_create_/update_/append_/delete_ variant.
_MUTATING_TOOLS = {
    "write_file", "apply_diff", "run_command", "run_shell", "run_docker",
    "confirm_action", "reject_action", "index_project", "scan_project_context",
    "lightrag_insert", "run_migration",
    # Persistent bash sessions + background jobs (hive-mcp/tools/bash.py) --
    # bash_job_status is deliberately NOT here: it's a read-only poll, and a
    # read-only team can never obtain a job_id anyway since bash_run itself is
    # stripped for them.
    "bash_session_start", "bash_run", "bash_session_close", "bash_job_kill",
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
    # `is not None`, NOT truthiness (fixed 2026-08-21). An EXPLICITLY EMPTY allowlist is
    # a deliberate disarm and must survive read_only stripping; only an ABSENT one means
    # "resolve against the live MCP surface". Under the old truthy test, [] fell through
    # to None and _scope_coordinator_tools took its unrestricted branch.
    #
    # This silently voided engineering's coordinator disarm (coordinator_tools: [],
    # 2026-08-20) for EVERY read_only run -- which is every question-answering call.
    # Measured 2026-08-21 by logging the resolved surface: 25 tools, including
    # get_file_content, list_processes, check_port, db_query and db_schema -- the exact
    # tools that disarm removed. The YAML said [], _load_team returned [], the worker
    # payload carried [], and _build_team produced [] when called directly; only the
    # read_only path in between turned it into None, so every static check passed.
    #
    # Fourth instance of empty-vs-absent in this file, and the costliest: the same
    # conflation was fixed in _scope_coordinator_tools (early return for []) and twice
    # in api/server.py's _load_team (per-agent tools:, coordinator_tools:).
    if tool_names is not None:
        return out, [t for t in tool_names if not _is_mutating(t)]
    return out, None   # absent, not empty -- resolved against the live MCP surface


# Discovery tools the coordinator must never call directly -- always resolved out of
# its own tool surface by _scope_coordinator_tools below, regardless of allowlist or
# read_only. get_file_content is deliberately NOT in this set: an already-known exact
# path (from the user's prompt, a prior citation this session, or ContextRouter's own
# delegated result) should still be read directly, no wasted round-trip through
# delegation for that case -- only the DISCOVERY step (finding a path you don't have
# yet) is forced through ContextRouter.
#
# Confirmed live 2026-08-11 that a prose-only instruction was not sufficient here: a
# _COORDINATOR_INSTRUCTIONS block telling the coordinator to prefer
# delegate_task_to_member for "find this unfamiliar file" tasks had ZERO measured
# effect on a live retest -- identical direct-tool-call pattern (35 direct calls, 0
# delegate_task_to_member) as the run before the instruction existed. This module
# already has the relevant lesson on record from a different incident
# (_strip_mutating's docstring, 2026-07-31): "Instructions shape what a model says;
# only the tool surface constrains what it does." This is the tool-surface version of
# the same fix, applied to discovery instead of writes.
#
# web_search/web_fetch added the same day, same incident class: after find_files/
# search_files/etc. were removed, a later retest (still zero delegate_task_to_member
# calls -- confirmed via journalctl grep, not assumed) had the coordinator open a
# NEW escape hatch instead of delegating: web_search('EkamApp frontend codebase
# GitHub repo') and web_fetch -- searching the PUBLIC web for a private, internal
# codebase's own structure, which can never return anything useful, before falling
# back to blind get_file_content() path-guessing. web_search/web_fetch are genuinely
# needed for EXTERNAL research (verifying a library name, checking docs) -- but that
# capability isn't lost by removing them here, only moved: ContextRouter and
# Researcher both carry the same tools in teams/engineering.yaml, so external
# research still happens, just delegated like discovery already is.
#
# lightrag_query/get_context_section added 2026-08-15 -- same incident class,
# found live on `planning` (which lists both in its own coordinator_tools:
# allowlist, so neither was previously blocked by this set at all): a run that
# should have delegated to Researcher instead had the coordinator call
# lightrag_query 3 times directly and produce the whole answer itself, never
# reaching Researcher/ContextRouter/Planner or planning's own Notion tools (only
# the MEMBER agents have notion_search/notion_get_page, not the coordinator) --
# and a separate parallel-review run had the coordinator call get_context_section
# ~25 times directly across topics with zero relevance to the task (seller, buyer,
# docker, krakend, delivery-board) instead of ever delegating. Both tools are
# semantic-search/reference lookups, the same category as search_knowledge_graph
# already excluded above -- this closes the same gap for the two tools that were
# missed the first time this set was built.
_COORDINATOR_DISCOVERY_TOOLS = {
    "find_files", "search_files", "list_directory", "list_directory_tree",
    "search_knowledge_graph", "web_search", "web_fetch",
    "lightrag_query", "get_context_section",
    # get_graph_report added 2026-08-15, same live validation pass -- missed when
    # get_context_section was added right above it. Same graphify/knowledge-graph
    # reference-lookup category; live-observed the coordinator calling it directly
    # (repeatedly, ~40s apart, heading toward another 300s liveness auto-kill)
    # immediately after get_context_section's own direct calls in the same run.
    "get_graph_report",
}
# Notion READ/discovery tools added 2026-08-15, same incident class as the set
# above, found live on `engineering` (which -- unlike parallel-review/planning --
# has NO coordinator_tools: allowlist at all in teams/engineering.yaml, so
# _scope_coordinator_tools' no-allowlist branch handed the coordinator every tool
# from every connected MCP except _COORDINATOR_DISCOVERY_TOOLS, Notion included):
# asked a sprint-lookup question, the coordinator directly hand-built raw
# notion_query_database relation filters instead of delegating -- 5 failed
# attempts (one with a dropped dash mid-UUID, a plausible model transcription
# slip, not the root cause) before it stumbled onto the purpose-built
# notion_items_in_sprint tool with no run budget left to synthesize a final
# answer. teams/sprint-master.yaml's BacklogResearcher carries the exact
# instruction that would have prevented this ("SPRINT QUESTIONS -- USE THE
# PURPOSE-BUILT TOOL: ... you do NOT look up the sprint id yourself") -- but
# that's member-agent YAML instruction text, invisible to the coordinator, which
# has no equivalent guidance of its own. Same fix shape as lightrag_query/
# get_context_section above: these are reference-lookup/discovery tools, not
# writes (notion_create_page/notion_update_page_props/etc. deliberately NOT
# included -- engineering's coordinator legitimately writes Notion content as
# part of the project's own delivery-board-sync workflow, gated by WRITE_REVIEW
# same as any other write tool; only the READ/query side needs to be forced
# through a properly-instructed member).
#
# SUPERSEDED 2026-08-18 (see _NOTION_WRITE_EXEMPT_TEAMS below): the rationale
# above -- "engineering legitimately writes Notion content" -- was the
# original design intent, but two live incidents the same day (a coordinator
# with no coordinator_tools: allowlist calling notion_trash_page against an
# unrelated real sprint page, then, after that specific tool was blocked,
# calling notion_create_page instead with a hallucinated parent database id)
# showed engineering's coordinator reaching for delivery-board writes with
# none of sprint-master's guardrails (no notion_find_work_item lookup, no
# schema-confirmed property values) for a task that never asked for a Notion
# write at all. Notion WRITE access is now sprint-master-only, structurally
# enforced below -- engineering keeps only the READ/discovery tools in
# _NOTION_DISCOVERY_TOOLS above (still forced through delegation, unchanged).
# Kept as a SEPARATE set, not merged into _COORDINATOR_DISCOVERY_TOOLS directly,
# because sprint-master is a real, deliberate exception: its own coordinator_tools
# comment documents that direct (non-delegated) coordinator access to these exact
# read tools was a TESTED FIX for a prior incident ("a read-only coordinator could
# not complete writes... delegation to the worker was unreliable -- it thrashed
# ~400s then gave up"). Blanket-adding these to _COORDINATOR_DISCOVERY_TOOLS would
# silently strip them from sprint-master too (`_keep()` applies even to a team's
# own explicit coordinator_tools: allowlist, by design -- see lightrag_query's own
# comment above), regressing a different, already-fixed problem while fixing this
# one. _scope_coordinator_tools takes a `team_name` and exempts exactly this set
# for `_NOTION_DISCOVERY_EXEMPT_TEAMS` instead.
_NOTION_DISCOVERY_TOOLS = {
    "notion_search", "notion_get_page", "notion_get_database_schema",
    "notion_query_database", "notion_items_in_sprint",
    "notion_get_item_with_relations", "notion_find_work_item",
}
_NOTION_DISCOVERY_EXEMPT_TEAMS = {"sprint-master"}

# Every Notion WRITE tool (create/update/append/replace/delete/trash — same
# families as _MUTATING_PREFIXES' notion_* entries) is blocked from the
# coordinator's direct tool surface for every team EXCEPT sprint-master, the
# one team with a real, board-CRUD-aware coordinator (schema-confirmed
# property values, notion_find_work_item lookups before writing, the
# WRITE_REVIEW gate) built for this. Applied in ALL THREE branches of
# _scope_coordinator_tools below -- no-allowlist, read-only, AND explicit
# allowlist -- so a future accidental grant of a Notion write tool to a non-
# sprint-master team's coordinator_tools: (YAML or DB) is still structurally
# blocked, not just today's no-allowlist gap.
#
# Two live incidents, same day (2026-08-18), same underlying tendency:
# engineering.yaml has no coordinator_tools: allowlist at all (unrestricted),
# so its coordinator got every Notion write tool "for free." Given a plain,
# first-turn, zero-prior-context task that never asked for any Notion write
# (a "look up and summarize this page" request), it called notion_trash_page
# against a real, unrelated, completed sprint page with 24+ linked work
# items -- narrating "no changes applied" while the trash action sat
# genuinely staged in WRITE_REVIEW. A first fix scoped only to
# notion_trash_page held (that specific tool was never called again on
# retest) but the coordinator routed around it via notion_create_page
# instead -- creating a new delivery-board work item nested under that same
# sprint page, with a parent_id that did not even match the real, documented
# Work Items database id. Both were caught and rejected before any write
# landed, purely because WRITE_REVIEW held -- the gap was the coordinator
# attempting an unrequested write at all, and no per-tool block closes that
# for certain, since there was no reason to believe a third Notion write tool
# wouldn't be reached for next. Blocking the whole family, not just the one
# tool that happened to be used first, is the actual fix.
_NOTION_WRITE_PREFIXES = (
    "notion_create", "notion_update", "notion_append", "notion_replace",
    "notion_delete", "notion_trash",
)
_NOTION_WRITE_EXEMPT_TEAMS = {"sprint-master"}


def _is_notion_write(name: str) -> bool:
    return name.startswith(_NOTION_WRITE_PREFIXES)


def _scope_coordinator_tools(
    tool_names: list[str] | None, mcp_list: list, read_only: bool = False,
    team_name: str | None = None,
):
    """Scope the coordinator's direct MCP tool surface to an explicit allowlist, and
    always exclude _COORDINATOR_DISCOVERY_TOOLS (see its own docstring above).

    Mirrors make_agent_from_spec's per-agent scoping (swarm/agents.py) — without this,
    the coordinator receives every tool from every connected MCP unfiltered, including
    write/staging tools (apply_diff, write_file, notion_*, confirm_action/reject_action)
    that read-only teams (planning, parallel-review) must never call. Falls back to the
    full mcp_list when no allowlist is given and none of _COORDINATOR_DISCOVERY_TOOLS
    filtering leaves anything left (should not happen in practice — every connected MCP
    exposes far more than these 5 tools).

    Every branch resolves to individual functions (via mcp.functions) rather than
    returning raw toolkit objects, so the discovery-tool filter can apply uniformly —
    this previously differed for the "no allowlist, not read_only" branch, which
    returned mcp_list unfiltered; confirmed via mcp.functions already being reliably
    populated by the time this runs (the read_only and explicit-allowlist branches have
    depended on it since 2026-07-31 with no reported gap).

    `team_name` (default None, matching every pre-2026-08-15 caller byte-for-byte)
    exempts `_NOTION_DISCOVERY_TOOLS` from the discovery-block for teams listed in
    `_NOTION_DISCOVERY_EXEMPT_TEAMS` — currently just `sprint-master`, whose own
    coordinator_tools comment documents that direct (non-delegated) access to these
    exact read tools was a deliberate, tested fix for a different prior incident.
    See _NOTION_DISCOVERY_TOOLS's own comment for why this is a separate exemption
    rather than simply not adding those tools to _COORDINATOR_DISCOVERY_TOOLS at all.

    ALL THREE branches also always exclude a Notion WRITE tool
    (`_is_notion_write`) unless `team_name in _NOTION_WRITE_EXEMPT_TEAMS`
    (currently just `sprint-master`) — see that set's own comment. Unlike
    `_NOTION_DISCOVERY_TOOLS`'s exemption, this one applies even to an
    explicit `coordinator_tools:` allowlist naming the tool by name: Notion
    write access is sprint-master-only by design now, not something any
    other team's YAML/DB grant can re-enable.
    """
    # An EXPLICIT empty allowlist means "this coordinator calls no MCP tool at all" --
    # pure orchestration, every tool call delegated to a member that owns the tool.
    # Distinguished from `None` (no allowlist configured -> everything minus the
    # discovery blocklist), which is what `if not tool_names` below still handles.
    #
    # This distinction has to be made HERE, ahead of everything else, because all three
    # branches below end in `scoped or mcp_list` -- a fallback that exists so a typo'd
    # allowlist naming only unknown tools fails OPEN rather than leaving the coordinator
    # toolless. That fallback is right for a misconfiguration and exactly wrong for a
    # deliberate empty list: without this early return, `coordinator_tools: []` would
    # hand the coordinator every tool from every connected MCP, silently doing the
    # opposite of what it says.
    #
    # Safe because the coordinator is never actually toolless: _build_team appends
    # request_clarification/update_session_state unconditionally, and agno adds
    # delegate_task_to_member(s) + get_member_information itself in
    # agno/team/_tools.py (`_tools.append(delegate_task_func)`), independent of `tools=`.
    if tool_names is not None and len(tool_names) == 0:
        return []

    all_funcs: dict = {}
    for mcp in mcp_list:
        all_funcs.update(mcp.functions)

    notion_exempt = team_name in _NOTION_DISCOVERY_EXEMPT_TEAMS
    notion_write_exempt = team_name in _NOTION_WRITE_EXEMPT_TEAMS

    def _keep(name: str) -> bool:
        if not notion_write_exempt and _is_notion_write(name):
            return False
        if name not in _COORDINATOR_DISCOVERY_TOOLS and name not in _NOTION_DISCOVERY_TOOLS:
            return True
        return notion_exempt and name in _NOTION_DISCOVERY_TOOLS

    if not tool_names and not read_only:
        scoped = [f for n, f in all_funcs.items() if _keep(n)]
        return scoped or mcp_list
    if not tool_names:
        # read_only with no allowlist: everything the MCPs expose, minus mutating tools.
        scoped = [f for n, f in all_funcs.items() if not _is_mutating(n) and _keep(n)]
        return scoped or mcp_list
    scoped = [all_funcs[t] for t in tool_names
              if t in all_funcs and not (read_only and _is_mutating(t)) and _keep(t)]
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


def _cloud_provider_error_message(exc: Exception) -> "str | None":
    """AGNOHive 2.3.2: graceful degradation when a cloud-routed agent's model call is
    rate-limited or over quota. agno-hive talks to LiteLLM over the OpenAI protocol
    (agno's OpenAILike is built on the `openai` package), so this surfaces client-side
    as `openai.RateLimitError` (HTTP 429) -- NOT a `litellm.*` exception, since that
    type only exists inside the separately-running LiteLLM proxy process, not in this
    one. LiteLLM's own job is normalizing every provider's distinct error shape into
    this one common form before it ever reaches agno-hive, so this single check covers
    OpenAI/Anthropic/Gemini/Perplexity/HuggingFace alike -- one catch, not five
    provider-specific parsers. Also fires for local vLLM/Ollama backends that happen
    to raise the same exception type (harmless -- the message is generic enough to
    still be accurate, and the distinction is not worth a second branch).

    Returns a clear, actionable message if this looks like a rate-limit/quota
    rejection, else None (caller re-raises the original exception unchanged)."""
    try:
        import openai
    except ImportError:
        return None
    if isinstance(exc, openai.RateLimitError):
        return (
            "Cloud model provider hit its rate limit or quota — retry shortly, "
            f"or switch this agent to another provider/local model. (original error: {exc})"
        )
    return None


_HANDOFF_EXCERPT_MAX = 5       # how many of the most recent read-tool results to keep
_HANDOFF_EXCERPT_CHARS = 800   # per-excerpt cap -- same truncation convention run_task_async
                                # already uses for session_messages injection (msg['content'][:800])


def _extract_tool_excerpts(messages) -> list[tuple[str, str]]:
    """The most recent read-tool results from a run's message history -- (tool_name,
    truncated content) pairs, oldest-to-newest of the last _HANDOFF_EXCERPT_MAX kept.

    Deterministic, no LLM involved -- consistent with this codebase's existing stance
    on the exact same tradeoff (see hive-mcp/tools/verify.py's own module docstring:
    "a model cannot be trusted to audit its own output"), applied here to session
    handoffs instead of claim verification. Reuses _READ_TOOLS (the same set
    _count_read_calls already keys off) rather than defining a second overlapping
    list -- a write tool's result ("applied: src/foo.tsx") is not evidence worth
    carrying forward the same way a read result is.
    """
    out: list[tuple[str, str]] = []
    for m in messages or []:
        if getattr(m, "role", None) != "tool":
            continue
        name = getattr(m, "tool_name", None) or getattr(m, "name", None)
        if name not in _READ_TOOLS:
            continue
        content = getattr(m, "content", None)
        if not isinstance(content, str) or not content.strip():
            continue
        out.append((name, content[:_HANDOFF_EXCERPT_CHARS]))
    return out[-_HANDOFF_EXCERPT_MAX:]


def _extract_handoff_summary(task: str, content: str, final_run_output=None) -> str:
    """Extract a compact chain-boundary handoff block from a completed run's output.

    Stored as the session summary so the next chained call gets a small structured
    digest instead of the full message history — preventing context overflow.

    Widened 2026-08-14 (session/context-overflow pipeline, part #1): the original
    version only ever regexed the coordinator's rendered FINAL ANSWER text -- file
    paths in backticks, up to 5 short bullets. Confirmed live: a chained call working
    from that alone had no memory of the actual file content or schema field names a
    prior turn had already read, only a list of file PATHS -- it had to re-search and
    re-read everything from scratch before it could even start the actual requested
    work, wasting most of its own time budget, then stalled before producing an
    answer. final_run_output (the real TeamRunOutput, when passed) adds real excerpts
    from the run's own read-tool results via _extract_tool_excerpts -- see that
    function's docstring for why this stays deterministic rather than a second LLM
    summarization pass. final_run_output=None (the default) keeps the original
    file-path/bullet-only behavior for any caller that doesn't have it.
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
    excerpts = _extract_tool_excerpts(getattr(final_run_output, "messages", None))
    if excerpts:
        lines.append("Recent tool results (most recent last):")
        for name, text in excerpts:
            lines.append(f"  [{name}] {text}")
    lines.append("──────────────────────────────────────────────────────────────────")

    return "\n".join(lines)


# ── Clarification requests (structured "I need a decision from the user") ──────
# Original design (2026-08-09): the coordinator is an LLM producing free text, so
# it was instructed to emit a fenced ```needs_clarification block and the block
# was regex-extracted from the final answer text. Live testing that same day
# showed this fails intermittently -- the model sometimes expresses the exact
# right judgment (a genuine decision point) but ends its answer with a plain
# prose question instead of remembering the fenced-block convention, and the
# question is then silently lost since nothing else looks at raw prose for it.
#
# 2026-08-10: replaced with a REAL tool call. request_clarification (below) is a
# genuine tool on the coordinator's own tool list -- calling it is a first-class
# action the model is already reliably trained to decide on (it calls MCP tools
# correctly dozens of times per run), not a text formatting convention it has to
# remember on top of everything else it's generating. stop_after_tool_call=True
# halts the run the instant it's called, and the tool's own (Pydantic-validated)
# arguments ARE the question/options -- no text parsing involved. See
# _extract_clarification_from_tools, the new primary extraction path.
#
# _extract_clarification (the original regex-over-text approach) stays below as
# a fallback for the rare case the model reverts to the old fenced-block habit
# from training data instead of calling the tool -- cheap insurance, no cost if
# never hit. Confirmed live 2026-08-09: this whole mechanism does NOT fix a
# separate failure mode where an open-ended "add an endpoint, figure out the
# pattern yourself" task caused the Coder to narrate intent in a loop without
# ever calling a write tool (there was no genuine ambiguity there, just an
# unfinished task) -- that's out of scope for this mechanism either way.
_CLARIFICATION_RE = re.compile(
    r"```needs_clarification\s*\n(.*?)\n```", re.DOTALL
)


class ClarificationOption(BaseModel):
    """One option the coordinator can offer via the request_clarification tool."""

    label: str = Field(..., description="Short display text for this option (1-5 words).")
    description: str | None = Field(None, description="One clarifying sentence about this option.")


@agno_tool(stop_after_tool_call=True)
async def request_clarification(question: str, options: list[ClarificationOption]) -> str:
    """Ask the human a structured question with 2-4 predefined options, for a genuine decision
    point only the human can resolve -- not something a tool call could look up. Calling this
    ends the run immediately; do not call any other tool or write anything else in this turn.

    Args:
        question: The question to ask. Must end with a question mark.
        options: 2-4 options, each a short label plus one clarifying sentence.
    """
    return "Question presented to the user; the run has ended to wait for their choice."


def _extract_clarification_from_tools(result) -> dict | None:
    """Pull a request_clarification tool call's own arguments off a completed agno run --
    the primary clarification-extraction path since 2026-08-10 (see the block comment
    above). `result` is whatever team.arun() returned (or the final stream event, which
    carries the same `.tools` shape) -- duck-typed via getattr since both TeamRunOutput
    and streaming's last event expose `.tools: list[ToolExecution]`.

    Validates the same shape _extract_clarification enforces (non-empty question, 2-4
    options each with a non-empty label) so a malformed call degrades to "no clarification
    requested" rather than crashing or forwarding garbage -- same fail-safe posture as
    every other post-run guard in this file. Returns the first valid match if the
    coordinator somehow called it more than once (should not happen given
    stop_after_tool_call halts the run on the first call).
    """
    tools = getattr(result, "tools", None) or []
    for t in tools:
        if getattr(t, "tool_name", None) != "request_clarification":
            continue
        args = getattr(t, "tool_args", None) or {}
        question = args.get("question")
        options = args.get("options")
        if not isinstance(question, str) or not question.strip():
            continue
        if not isinstance(options, list) or not (2 <= len(options) <= 4):
            continue
        cleaned_options = []
        valid = True
        for opt in options:
            if not isinstance(opt, dict):
                valid = False
                break
            label = opt.get("label")
            if not isinstance(label, str) or not label.strip():
                valid = False
                break
            cleaned_options.append({"label": label, "description": opt.get("description")})
        if not valid:
            continue
        return {"question": question, "options": cleaned_options}
    return None


def _extract_clarification(content: str) -> tuple[str, dict | None]:
    """Pull a ```needs_clarification fenced JSON block out of the coordinator's
    final answer, if present. Returns (content_with_block_removed, clarification)
    where clarification is None if no block was found OR the block was malformed
    (fail-safe: a bad block is treated as "no clarification requested", never a
    crash — same posture as every other post-run guard in this file).

    Expected shape inside the fence:
        {"question": "...", "options": [{"label": "...", "description": "..."}, ...]}
    2-4 options, matching the same constraint Claude Code's own AskUserQuestion
    tool uses for the human-facing analog of this mechanism.
    """
    match = _CLARIFICATION_RE.search(content)
    if not match:
        return content, None

    stripped = (content[:match.start()] + content[match.end():]).strip()
    try:
        payload = json.loads(match.group(1))
        question = payload["question"]
        options = payload["options"]
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if not isinstance(options, list) or not (2 <= len(options) <= 4):
            raise ValueError("options must be a list of 2-4 items")
        cleaned_options = []
        for opt in options:
            label = opt["label"]
            if not isinstance(label, str) or not label.strip():
                raise ValueError("each option needs a non-empty label")
            cleaned_options.append({
                "label": label,
                "description": opt.get("description"),
            })
    except Exception as exc:
        print(f"[team] malformed needs_clarification block, ignoring: {exc}")
        return stripped, None

    return stripped, {"question": question, "options": cleaned_options}


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


def _mcp_error_text(result) -> str | None:
    """The error text when an MCP call came back isError=True, else None.

    An MCP tool call can FAIL WITHOUT RAISING. The protocol reports a tool-level
    failure as a perfectly normal response carrying isError=True and the message as
    ordinary text content -- so every caller that only reads .content treats the error
    STRING as if it were the tool's answer.

    Confirmed by direct probe 2026-08-20 against the LightRAG MCP: calling list_skills
    there returns `isError=True, content=[TextContent(text='Unknown tool:
    list_skills')]`, no exception. That is LightRAG behaving CORRECTLY -- it never
    claimed to have skills. Every consequence was on this side:
      * _fetch_skill_catalog then ran json.loads('Unknown tool: list_skills'), and the
        resulting JSONDecodeError -- raised inside the streamablehttp_client task group
        -- surfaced as the opaque "unhandled errors in a TaskGroup (1 sub-exception)"
        seen 17 times in 30 days.
      * _verify_claims is worse: it returns `"could NOT be found" in report` as its
        `bad` flag, so an error string simply does not match and the call reports
        bad=False, unavailable=False -- INDISTINGUISHABLE FROM A CLEAN VERIFICATION.
        The answer ships with no disclaimer, as though its claims had been checked.
        That is the same fail-open trap the 2026-08-19 timeout fix closed for
        exceptions, reachable by a second route that fix never covered.
    """
    if result is not None and getattr(result, "isError", False):
        return _extract_mcp_text(result) or "tool reported an error with no message"
    return None


# Fail-open vs fail-safe (2026-08-19, T12 re-test, task 738813cb-...): before this
# fix, a verify_claims failure (exception or the _BESPOKE_MCP_SESSION_TIMEOUT cutoff
# above) returned ("", False) -- bad=False, IDENTICAL to "ran cleanly, zero problems
# found." Live-confirmed harm: that same re-test's verify_claims call genuinely hung
# for the full ~90s window against a degraded hive-mcp, timed out (the timeout fix
# itself worked correctly), and the run shipped a FALSE claim ("no frontend RTK
# Query hooks were found" -- 11 real hooks exist in businessApi.ts, found by this
# same run's own earlier, correctly-scoped pass) with no signal anywhere that
# verification never actually ran. The safety net built specifically to catch this
# kind of self-contradiction went silent exactly when it was needed.
#
# The fix distinguishes a third state -- `unavailable` -- from `bad`, and BOTH
# _verified_answer call sites below append _UNVERIFIED_DISCLAIMER instead of
# shipping silently clean when unavailable=True. Deliberately NOT wired into the
# existing missing_symbols/bad_citations/lint_violations retry machinery: that
# path calls _stream_team_run for a full extra pipeline turn, which would (a) most
# likely hit the SAME degraded hive-mcp session that just failed, wasting another
# ~90s-plus for no benefit, (b) re-expose the run to the repeat-loop/stub-escalation
# territory those retries walk through, and (c) burn the aggregate one-retry
# budget (`len(all_results) > 1`) on a check that was never actually performed --
# using it up before a REAL fabrication elsewhere in the same answer gets a chance
# to trigger its own retry. A disclaimer is a pure, local, deterministic string
# append: it cannot hang, cannot loop, and cannot touch the tool surface at all --
# same category of fix as every other grep-based mechanism in this file. This
# leaves genuine verify_claims successes (bad=True) on the existing, unchanged
# retry path; only the "we could not check" state is new.
_UNVERIFIED_DISCLAIMER = (
    "\n\n---\n**⚠️ Automated citation verification was unavailable this run "
    "(hive-mcp did not respond in time) — the claims above have NOT been "
    "checked against the repository and may be inaccurate.**"
)


async def _verify_claims(content: str, hive_mcp_url: str | None,
                         hive_mcp_tools=None) -> tuple[str, bool, bool]:
    """Run hive-mcp's verify_claims over a draft answer.

    `hive_mcp_tools` is the live agno MCPTools instance for hive-mcp, when the caller
    has one. Optional and defaulting to None so every existing caller and test keeps
    working unchanged; when present it is TRIED FIRST (see the attempt list below).

    Returns (report, has_problems, unavailable). `unavailable=True` means the check
    was ATTEMPTED and failed (exception or timeout) -- distinct from "not attempted"
    (no content, or hive_mcp_url unset -- a deliberate configuration choice, not a
    degradation, so it stays unavailable=False and silent, same as always).

    Deterministic grep, no model involved. Never raises: a verifier that breaks the run
    would be worse than the fabrication it is meant to catch, so any failure here is
    reported as "no problems, but unavailable" (see _UNVERIFIED_DISCLAIMER above for why
    callers must still surface this rather than treat it as a clean pass) and logged.
    """
    if not content or not (hive_mcp_url or hive_mcp_tools):
        return "", False, False

    async def _call_on_live_session() -> str:
        """Reuse the connection the run is already holding.

        get_session_for_run() is agno's supported accessor: with no header_provider
        configured (this codebase never sets one) it returns the MCPTools instance's
        own live ClientSession, already initialized. _verified_answer runs INSIDE the
        AsyncExitStack that entered these MCPTools, so the session is guaranteed still
        open at this point -- verified by call-site nesting, not assumed.
        """
        session = await hive_mcp_tools.get_session_for_run()
        return await session.call_tool("verify_claims", {"answer": content})

    async def _call_on_fresh_connection():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(hive_mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("verify_claims", {"answer": content})

    # One retry on failure, added 2026-08-20. This check is a deterministic grep with no
    # model involved, so a failure here is essentially always transient -- a busy or
    # briefly-unreachable hive-mcp, not a wrong answer -- and retrying is cheap.
    #
    # It matters because the check was disabling itself exactly when it was most needed.
    # Measured that day across three live groundedness probes: the two LONG runs (128s and
    # 138s, both heavy enough to be backgrounded by the caller) both came back carrying
    # "Automated citation verification was unavailable this run", and both shipped a real
    # fabrication uncaught -- a scrambled function/line-range attribution in one, and
    # "the items table does not exist" (it exists, as inventory.items) in the other. The
    # one SHORT run (79s) verified cleanly and was also the only clean answer. A verifier
    # whose availability is inversely correlated with the complexity of the run is close
    # to no verifier at all on the cases that matter.
    #
    # Second attempt gets a SHORTER budget so the worst case (135s) stays comfortably
    # under config.liveness_silence_threshold_s -- the same do-not-race-the-watchdog
    # invariant that drove _MCP_TIMEOUT down to 180. Deliberately not a longer first
    # timeout: the observed failures were hive-mcp not answering at all, which a longer
    # wait does not fix, and a fresh connection might.
    # Failures are logged with the exception TYPE and elapsed seconds, not just str(exc).
    # The old log line was `unavailable (url): {exc}` -- and an asyncio.TimeoutError
    # stringifies to the EMPTY STRING, so every real timeout was recorded as
    # "unavailable (http://...:9003/mcp): " with nothing after the colon. Two such lines
    # on 2026-08-20 were the only server-side trace of two probes that shipped
    # fabrications uncaught, and they could not be told apart from a connection refusal.
    # Elapsed time is what separates the two causes: ~90s means the call hung waiting,
    # ~0s means it never connected.
    #
    # That distinction already paid for itself. hive-mcp's own tool log for that window
    # shows only 2 verify_claims calls, taking 18.6s and 22.4s -- while agno-api logged 2
    # SEPARATE unavailable events. So the failing calls never reached hive-mcp at all:
    # a client-side hang before any bytes hit the wire, NOT a slow grep. Exactly the
    # signature already documented for _MCP_TIMEOUT ("hive-mcp's own docker logs showed
    # the request never arriving"). Raising the budget would not have helped; a fresh
    # connection might, which is what the retry above is.
    # Attempt order: the run's OWN live session first, a brand-new connection only as
    # the fallback. Added 2026-08-20 after the evidence showed the failures were never
    # slow greps but connections that never reached hive-mcp at all -- opening a fresh
    # streamablehttp_client, while the run's existing MCPTools connections to the SAME
    # server are still open, is the step that was hanging. Reusing the already-
    # established session skips that step entirely rather than retrying through it.
    attempts: list[tuple[str, object, int]] = []
    if hive_mcp_tools is not None:
        attempts.append(("live-session", _call_on_live_session,
                         _BESPOKE_MCP_SESSION_TIMEOUT))
    if hive_mcp_url:
        attempts.append(("fresh-connection", _call_on_fresh_connection,
                         _BESPOKE_MCP_SESSION_TIMEOUT if not attempts
                         else _BESPOKE_MCP_SESSION_TIMEOUT // 2))
        if hive_mcp_tools is None:
            # No live session to try first, so the two fresh-connection attempts added
            # earlier on 2026-08-20 are the only retry this caller gets -- keep both.
            # Dropping the second here would silently regress every url-only caller
            # back to the original single-shot give-up, which is what a test caught.
            attempts.append(("fresh-connection-retry", _call_on_fresh_connection,
                             _BESPOKE_MCP_SESSION_TIMEOUT // 2))

    last_exc = None
    for n, (label, call, budget) in enumerate(attempts, start=1):
        started = time.monotonic()
        try:
            res = await asyncio.wait_for(call(), timeout=budget)
            # isError checked OUTSIDE the connection context manager on purpose: an
            # exception raised inside it gets rewritten by anyio into "unhandled errors
            # in a TaskGroup", discarding the message that says what actually went
            # wrong. The inner helpers therefore return the raw result and the decision
            # is made here, where the text survives.
            err = _mcp_error_text(res)
            if err:
                raise RuntimeError(f"verify_claims tool returned an error: {err}")
            report = _extract_mcp_text(res)
            # Logged on EVERY success, including the first attempt. Previously only
            # attempt 2+ logged, so a successful run produced no line at all and the
            # path actually taken was unobservable -- "no failure lines" was equally
            # consistent with live-session reuse and with a fresh connection, which is
            # exactly the ambiguity that made verifying the 2026-08-20 reuse fix harder
            # than it should have been (settled only by reading hive-mcp's raw HTTP log
            # for a missing POST/GET handshake).
            print(f"[team] verify_claims ok via {label} (attempt {n}) in "
                  f"{time.monotonic() - started:.1f}s")
            return report, "could NOT be found" in report, False
        except Exception as exc:
            last_exc = exc
            print(f"[team] verify_claims attempt {n}/{len(attempts)} ({label}) failed "
                  f"after {time.monotonic() - started:.1f}s (budget {budget}s) "
                  f"({hive_mcp_url}): {type(exc).__name__}: {exc or '<no message>'}")
            if n < len(attempts):
                await asyncio.sleep(_VERIFY_RETRY_PAUSE_S)

    print(f"[team] verify_claims unavailable after {len(attempts)} attempt(s) "
          f"({hive_mcp_url}): {type(last_exc).__name__}: {last_exc or '<no message>'}")
    return "", False, True


def _pick_hive_mcp_url(all_mcp_urls: list[str] | None,
                       project_mcp_url: str | None = None) -> str | None:
    """hive-mcp's URL out of the connected set, or None when it isn't there.

    Every hive-mcp-specific guard used to take `all_mcp_urls[0]` and ASSUME it was
    hive-mcp. Usually true by caller convention -- _resolve_mcp_urls APPENDS the
    LightRAG url, and callers put hive-mcp first -- but nothing enforced it, and when
    a caller passed no mcp_urls at all the list collapsed to [lightrag, project-mcp]
    and position 0 silently became LightRAG.

    That is not hypothetical: all 17 "skill catalog unavailable" failures in the 30
    days to 2026-08-20 were against http://localhost:9002/mcp -- LightRAG, which has
    no list_skills tool -- reported as an opaque "unhandled errors in a TaskGroup".
    16 of them fell in one afternoon, so that whole session ran with no skill catalog
    and nothing said so. The same positional assumption also feeds verify_claims and
    the count-marker guard, where the consequence would be worse: a groundedness check
    silently aimed at a server that cannot answer it, degrading to "unavailable"
    without ever naming the real cause.

    Exclusion-based because this runs BEFORE any MCPTools are connected (the skill
    catalog is needed to build the team's instructions), so no tool list exists to
    interrogate yet. Both excluded urls are known independently: LightRAG's from
    config, the project MCP's from the caller. Post-connection callers should prefer
    _pick_hive_mcp below, which identifies hive-mcp by capability instead of by
    elimination.
    """
    for u in all_mcp_urls or []:
        if u == config.lightrag_mcp_url:
            continue
        if project_mcp_url and u == project_mcp_url:
            continue
        return u
    return None


def _pick_hive_mcp(mcp_by_url: dict | None, required_tool: str = "verify_claims"):
    """(url, MCPTools) of the connected server that actually exposes `required_tool`.

    Definitive rather than positional: agno populates MCPTools.functions as a dict
    keyed by tool name, so "which server can run verify_claims" is answerable directly
    instead of inferred from ordering. Returns (None, None) when no connected server
    has it -- a real condition (hive-mcp down or never passed) that the caller should
    report, not paper over.
    """
    for url, mcp in (mcp_by_url or {}).items():
        funcs = getattr(mcp, "functions", None) or {}
        try:
            if required_tool in funcs:
                return url, mcp
        except TypeError:      # an unexpected functions shape must never break a run
            continue
    return None, None


# verify_claims' symbol-anchored MISMATCH ends with the symbol's REAL location, which
# _symbol_line_numbers computed by reading the file: "`sku_prefix` is not within 5 lines
# of 142; it actually appears at line(s) 129". Captures (symbol, lines) so the citation
# retry can be handed the answer instead of being sent to rediscover it.
_CORRECT_LINE_RE = re.compile(
    r"`([^`\n]{1,120})` is not within \d+ lines of \d+; "
    r"it actually appears at line\(s\) ([\d, ]+)"
)

def _resolve_tool_call_limit(team_name: str | None, role_name: str) -> int:
    """This (team, role)'s real tool-call budget, DB override first.

    Added 2026-08-21. `swarm/team.py`'s Team(...) construction passed
    `config.tool_call_limit` unconditionally, so the per-role overrides in
    `team_role_models` — engineering Coordinator 60, Researcher 50 — reached member
    Agents (`agents.py`) but never the Team itself. Raising the Coordinator's budget
    through /admin/model-routes silently did nothing: a knob that looked like it
    worked and didn't.

    TWO independent None cases, both real, both falling back to config:
      * no row for this (team, role) at all — get_role_policy returns None;
      * a row that exists with the column NULL — RolePolicy.tool_call_limit is
        itself `int | None`, and NULL means "no override, use the global default"
        (see RolePolicy's own docstring).
    `team_name` is also `str | None`: the request.agents path builds a team with no
    name at all, so that is checked before the lookup rather than passed through.
    """
    if not team_name:
        return config.tool_call_limit
    policy = model_routing.get_role_policy(team_name, role_name)
    if policy is None or policy.tool_call_limit is None:
        return config.tool_call_limit
    return policy.tool_call_limit


_MAX_HANDED_OVER_LOCATIONS = 4


def _citation_retry_hint(corrected_lines: list[tuple[str, str]]) -> str:
    """The HOW half of the citation-correction retry: what kind of tool call to make.

    Named rather than inlined so a test can exercise the real string instead of a
    parallel reimplementation that can drift from it.

    Demands a NARROW read. The previous wording said "call get_file_content on the
    exact file(s) involved"; the logs show the model complied exactly and reproduced
    the same unbounded read that produced the wrong number. Not a compliance failure
    -- the instruction prescribed the wrong remedy.

    `corrected_lines` are (symbol, "129" | "116, 208") pairs harvested from
    verify_claims' own symbol-anchored MISMATCH, which computed them by reading the
    real file. Handing them over converts the retry from a rediscovery into a
    correction. Capped: past a handful this stops being a hint and becomes another
    wall of text for a model already struggling to locate one line.
    """
    hint = (
        "Do NOT re-read the whole file — that is exactly what produced the wrong "
        "number. Either call search_files for the symbol and use the file:line it "
        "reports, or call get_file_content with offset/limit for a SMALL window "
        "(about 10-20 lines) around the candidate line, and read the number off "
        "the tool's own numbered output. "
    )
    if corrected_lines:
        found = "; ".join(
            f"{sym} is at line(s) {lines}"
            for sym, lines in corrected_lines[:_MAX_HANDED_OVER_LOCATIONS]
        )
        hint += (
            f"A repository grep has ALREADY located them for you: {found}. Verify "
            f"with a small windowed read around those lines and cite what you see "
            f"there — do not cite a different number than the grep found unless the "
            f"tool output you just read plainly contradicts it. "
        )
    return hint


_MODEL_DIRECTED_VERDICT_RE = re.compile(
    r"\s*Fix the answer before returning it[^\n]*", re.IGNORECASE
)


def _reader_facing_report(report: str) -> str:
    """Strip the model-directed imperative from a verify_claims report before it is
    shown to a human.

    The report has two audiences. During the correction retry the model reads it and
    "Fix the answer before returning it — a NOT FOUND symbol or a BAD citation is
    fabrication, not a near miss" is exactly the right thing to say. When the retry
    budget is gone the SAME text is appended to the final answer (see the two call
    sites below, deliberately: "surface rather than hide" — the reader does need to
    know which claims are unsupported). There it reads as an instruction the pipeline
    was given and visibly ignored, which is both confusing and worse than the truth:
    the check ran, it found something, and there was no retry left to spend.

    Observed twice in one T1-T13 re-run, 2026-08-21. Only that one sentence is
    removed — every finding line, and the factual half of the VERDICT, stays exactly
    as it was, because those are what the reader actually needs.
    """
    return _MODEL_DIRECTED_VERDICT_RE.sub("", report).rstrip()


async def _sync_tool_registry(mcp_list: list, skill_catalog: list[dict] | None) -> None:
    """Keep tool_registry/skill_registry current from this run's own live MCP
    enumeration — see team_config.sync_registry_from_live() for why a swarm run
    is the right place for this and a server's own bootstrap is not.

    `mcp.functions` is the connected surface as agno enumerated it, before any
    read_only stripping (that happens later, on the scoped per-agent lists, not
    on this dict), so a read-only run reports the same surface as any other.

    Never raises and never blocks the run: the registry is write-time grant
    validation, not something a task depends on. A failure here means the next
    attempt to grant a brand-new tool gets a 400 — annoying and self-explanatory
    — which does not justify failing a task that was otherwise fine.
    """
    try:
        tool_names = sorted({name for mcp in mcp_list for name in mcp.functions})
        skill_names = sorted({s["name"] for s in (skill_catalog or []) if s.get("name")})
        added = await team_config.sync_registry_from_live(tool_names, skill_names)
        if added:
            print(f"[registry] synced from live MCP — added "
                  f"{len(added['tools_added'])} tool(s) {added['tools_added']}, "
                  f"{len(added['skills_added'])} skill(s) {added['skills_added']}")
    except Exception as exc:
        print(f"[registry] sync skipped ({type(exc).__name__}: {exc or '<no message>'})")


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

    async def _call():
        async with streamablehttp_client(hive_mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("list_skills", {})

    try:
        res = await asyncio.wait_for(_call(), timeout=_BESPOKE_MCP_SESSION_TIMEOUT)
        # isError decided out here, not inside the context manager -- see the same
        # note in _verify_claims. Previously the raw error TEXT was fed straight to
        # json.loads(), and the JSONDecodeError it raised inside the task group was
        # rewritten as "unhandled errors in a TaskGroup (1 sub-exception)", hiding
        # both the real message and which server produced it.
        err = _mcp_error_text(res)
        if err:
            print(f"[team] skill catalog unavailable — {hive_mcp_url} rejected "
                  f"list_skills: {err}. Agents run without skill instructions this "
                  f"run (is this hive-mcp?)")
            return []
        return json.loads(_extract_mcp_text(res))
    except Exception as exc:
        print(f"[team] skill catalog unavailable ({hive_mcp_url}): "
              f"{type(exc).__name__}: {exc or '<no message>'}")
        return []


# Tools that gather EVIDENCE. An answer asserting facts about the codebase without one
# of these has no basis beyond the model's priors and the loaded context.
_READ_TOOLS = {
    "get_file_content", "search_files", "find_files", "count_matches",
    "list_directory", "list_directory_tree", "get_project_context",
    "get_context_section", "list_recent_files", "search_knowledge_graph",
    "lightrag_query", "db_query", "db_schema",
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

# Tools that answer a "live database" question. See the DB-evidence guard below.
_DB_TOOLS = {"db_query", "db_schema"}
# Phrases that mean the task itself demands a live-DB check, not a file grep. Deliberately
# narrow -- a false positive here just means "checked for db_query evidence when the task
# didn't strictly need it" (cheap); a false negative means a fabricated schema claim ships
# unretried, which is the exact failure this guard exists to catch (see 2026-08-20 note below).
_DB_TASK_RE = re.compile(
    r"\blive database\b|\bdb_query\b|\bdb_schema\b|\brow count\b|\bhow many rows\b",
    re.IGNORECASE,
)


# Tools that actually ENUMERATE a directory, as opposed to reading one thing out of it.
# find_files counts: a glob genuinely lists what matches, which is a real enumeration.
_ENUM_TOOLS = {"list_directory", "list_directory_tree", "find_files"}
# Task shapes that demand a real listing rather than recall. Same narrowness rule as
# _DB_TASK_RE above, and the same asymmetry: a false positive costs one extra evidence
# check on a task that didn't need it; a false negative ships a confidently wrong
# inventory. Measured 2026-08-21 -- across four probes needing enumeration,
# list_directory was called ZERO times, including one whose prompt named the tool
# outright. Produced "the directory holds one file" (six), "3 router files" (24), and
# "the entire Parties frontend is missing" (it exists) -- all stated with no hedging.
_ENUM_TASK_RE = re.compile(
    r"\blist (?:every|all|each)\b|\bhow many\b.{0,40}\b(?:files?|modules?|routers?|services?)\b"
    r"|\bevery (?:file|module|router|service)\b|\bwhat(?:'s| is) in (?:the )?(?:dir|directory|folder)\b"
    r"|\blist_directory\b|\bdirectory listing\b|\benumerate\b"
    # "name what router files DO exist in that directory" — T11's real wording, and the
    # shape an absence question takes when it asks for the alternatives. Requires the
    # interrogative, so a bare single-file "does X exist?" (T11's own first half) stays
    # out: that one needs no listing to answer.
    r"|\bwhat\b[^.]{0,40}\b(?:files?|modules?|routers?|services?)\b[^.]{0,25}\bexist\b",
    re.IGNORECASE,
)
# A concrete FILE the task names -- needs a name part before the dot, so a bare ".py
# files" mention is not mistaken for a path.
_FILE_TARGET_RE = re.compile(r"\b[\w./\\-]+\.(?:py|ts|tsx|js|jsx|md|ya?ml|json|scss|sql|toml)\b")
# Words that mean the question really is about a DIRECTORY, even when a filename also
# appears somewhere in the prompt.
_DIRECTORY_WORD_RE = re.compile(r"\b(?:director(?:y|ies)|folder|list_directory)\b|/\s*(?:$|and\b)", re.IGNORECASE)


def _is_enumeration_task(task: str | None) -> bool:
    """Does this task require a DIRECTORY listing as evidence?

    Narrowed 2026-08-21 after a live false positive. "How many @router endpoints are
    defined in API/inventory-service/router/uom_api.py?" matched _ENUM_TASK_RE on "how
    many ... " and the guard then demanded list_directory/find_files evidence -- but
    that is a question about a FILE's CONTENTS, where get_file_content is the correct
    and sufficient tool and a directory listing proves nothing. A correct answer (3
    endpoints, exact methods and paths, read straight from the file) shipped carrying
    "NOT VERIFIED BY A DIRECTORY LISTING".

    That matters beyond the noise: this guard also forces a RETRY, so a false positive
    spends a pipeline turn and risks the documented failure where an un-grounded re-run
    overwrites a correct answer.

    Rule: an enumeration-shaped task that names a concrete FILE and never mentions a
    directory is a file-contents question, not a listing question. A task that mentions
    a directory stays in scope even if a filename also appears -- T11 ("does gst_api.py
    exist? ... name what router files DO exist in that directory") is exactly that shape
    and must still be checked.
    """
    if not task or not _ENUM_TASK_RE.search(task):
        return False
    if _FILE_TARGET_RE.search(task) and not _DIRECTORY_WORD_RE.search(task):
        return False
    return True


# The coordinator narrating that it is about to call a tool it does not have. This is a
# reliable, greppable signal and it appears VERBATIM before the loop starts -- measured
# 2026-08-21, where the coordinator emitted "the environment shows that list_directory is
# available. I will now call list_directory directly on the requested path" four times in
# a row and then leaked a raw <tool_call> tag, never once delegating.
_NARRATED_TOOL_INTENT_RE = re.compile(
    r"\b(?:I(?:'ll| will| am going to)?\s+(?:now\s+)?(?:call|use|invoke|run)|"
    r"let me\s+(?:now\s+)?(?:call|use|invoke|run)|"
    r"next(?:,)?\s+I(?:'ll| will)\s+(?:call|use|invoke|run))\b[^.\n]{0,40}?"
    r"\b(find_files|search_files|list_directory_tree|list_directory|"
    r"search_knowledge_graph|lightrag_query|get_context_section|get_graph_report|"
    r"web_search|web_fetch)\b",
    re.IGNORECASE,
)


# Any mention of a blocked tool by name, whatever the surrounding wording.
_BLOCKED_TOOL_MENTION_RE = re.compile(
    r"\b(find_files|search_files_batch|search_files|list_directory_tree|list_directory|"
    r"search_knowledge_graph|lightrag_query|get_context_section|get_graph_report|"
    r"web_search|web_fetch)\b"
)


def _narrated_unreachable_tool(content: str | None, delegations: int = -1) -> str | None:
    """A blocked tool the coordinator is stuck on, or None.

    Widened 2026-08-21, immediately after the intent-phrasing version proved too
    narrow live. It matched "I will now call list_directory directly" -- the phrasing
    actually observed -- and then two consecutive re-runs produced neither that nor
    anything like it:

        run 1: answered "exactly one .py file: vouchers_api.py" with no narration at all
        run 2: "The `list_directory` tool cannot be used without `git` or a valid file
                system state. No further action can be taken."

    Three runs, three different framings of the same underlying event. Matching intent
    phrasing is the denylist treadmill in another costume, so the signal moved to
    something the model cannot phrase its way around:

        the answer NAMES a tool the coordinator cannot call
        AND the run made ZERO delegations

    A run that legitimately delegated is untouched no matter what it names, and a run
    that never mentions a blocked tool is untouched too. `delegations == -1`
    (undeterminable) is treated as "do not fire" -- the same rule _count_read_calls and
    _count_delegations already follow, so a missing signal never becomes evidence.
    """
    if not content or delegations != 0:
        return None
    m = _BLOCKED_TOOL_MENTION_RE.search(content)
    if not m:
        return None
    tool = m.group(1)
    return tool if tool in _COORDINATOR_DISCOVERY_TOOLS else None


def _member_holding(team, tool_name: str) -> tuple[str, str] | None:
    """(member_id, display name) of a member that actually holds `tool_name`.

    Read off the live team rather than any static map, so it cannot drift from what
    the members were really built with. Returns None when nothing holds it, in which
    case the caller must NOT invent a delegation target -- naming a member that does
    not have the tool would send the coordinator into the exact "member resolution
    failure" spiral documented for _member_id (2026-08-15).
    """
    for m in getattr(team, "members", None) or []:
        for t in getattr(m, "tools", None) or []:
            if getattr(t, "name", None) == tool_name:
                name = getattr(m, "name", "") or ""
                return _member_id(name), name
    return None


def _run_read_count(team, tool_names: set[str] = _READ_TOOLS) -> int:
    """Real reads recorded anywhere in this run, INCLUDING inside delegated members.

    Deliberately separate from _count_read_calls rather than folded into it, because
    the two answer different questions and only one of them is run-scoped:

      * _count_read_calls(result) is PER-ATTEMPT -- _more_grounded compares an original
        draft against its retry, and a run-scoped total would be identical for both
        (it includes each other's reads), silently breaking that comparison.
      * this is PER-RUN, which is exactly right for "did this answer's team read
        anything at all before asserting code facts".

    Returns -1 when undeterminable (no team, or a team built by some path that never
    attached the state), matching _count_read_calls' own convention so a caller can
    tell "nothing was read" from "cannot say" -- the distinction that keeps a missing
    signal from being read as evidence of fabrication.
    """
    state = getattr(team, "_read_state", None)
    if not isinstance(state, dict) or "reads" not in state:
        return -1
    return sum(1 for r in state["reads"] if r.get("tool") in tool_names)


def _count_read_calls(result, tool_names: set[str] = _READ_TOOLS) -> int:
    """Count evidence-gathering tool calls in a run. Returns -1 when undeterminable.

    tool_names defaults to _READ_TOOLS (the generic "did it read anything" check).
    Pass a narrower set (e.g. {"db_query", "db_schema"}) to check for a SPECIFIC
    tool having been called, not just any evidence-gathering tool -- added 2026-08-20
    after a task explicitly requiring db_query/db_schema was answered from a plain
    file grep instead; the generic check (reads>0) didn't catch it because a file
    read did happen, just not the one the task demanded.

    -1 (not 0) when the message shape is unrecognised: "we could not tell" must never be
    treated as "it did not read". Reading absence as evidence of absence produced several
    wrong diagnoses on 2026-07-31, and a guard that made the same mistake would force
    pointless retries on correct answers.

    2026-08-18 live incident: `result.messages` only ever holds the COORDINATOR's own
    direct tool calls -- a delegated member agent's reads happen inside a SEPARATE,
    nested run, visible to the coordinator's own message list only as one opaque
    `delegate_task_to_member` call (not in `_READ_TOOLS`) plus the delegate's final
    text answer. For a task the coordinator handles entirely through delegation (a
    correct, encouraged pattern -- see _COORDINATOR_DISCOVERY_TOOLS' whole rationale
    for forcing exactly this), `.messages`-only counting always returns 0 real reads
    even when the delegated agent(s) read extensively and correctly -- a false
    positive that triggered a retry on an ALREADY-CORRECT, doubly-Researcher-and-
    Reviewer-verified gap-analysis answer, and that retry's fresh, un-grounded
    re-run then produced a WRONG answer, overwriting the right one.

    `session_state["read_log"]` (`_record_read`, written by `_make_read_cache_tool_hook`
    on EVERY team member, coordinator or delegated, for exactly this reason -- see
    that hook's own docstring on why per-agent registration was necessary) already
    tracks every real fresh read across the WHOLE run regardless of delegation depth.
    Combined with the `.messages` count below (still checked, still correct for the
    coordinator's own direct reads) rather than replacing it -- either source finding
    real evidence is sufficient; this only ever WIDENS what counts as "did read",
    never narrows it, so it cannot introduce a new false negative.
    """
    msgs = getattr(result, "messages", None)
    n, recognised = 0, False
    if msgs:
        for m in msgs:
            for tc in (getattr(m, "tool_calls", None) or []):
                recognised = True
                fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                name = (fn or {}).get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                if name in tool_names:
                    n += 1
            if getattr(m, "role", None) == "tool":
                recognised = True
                name = getattr(m, "tool_name", None) or getattr(m, "name", None)
                if name in tool_names:
                    n += 1

    session_state = getattr(result, "session_state", None) or {}
    read_log = session_state.get("read_log") if isinstance(session_state, dict) else None
    if read_log:
        recognised = True
        n += sum(1 for entry in read_log if isinstance(entry, dict) and entry.get("tool") in tool_names)

    return n if recognised else -1


# agno's own wording when it refuses a call past tool_call_limit -- see
# create_tool_call_limit_error_result in the installed agno package
# (agno/models/base.py): "Tool call limit reached. Tool call {name} not executed.
# Don't try to execute it again."
_TOOL_LIMIT_MARKER = "tool call limit reached"


def _tools_refused_for_limit(result) -> list[str]:
    """Tool names agno REFUSED to run this turn because tool_call_limit was hit.

    This is the one failure in this file that no tool hook can ever see. Reading
    agno/models/base.py directly: once current_function_call_count exceeds the limit it
    appends create_tool_call_limit_error_result(fc) and `continue`s, so the call never
    enters function_calls_to_run and NO tool event is yielded for it. Every reinforcement
    this codebase has -- the duplicate-read stub, the forced-answer nudge, the
    tool_choice="none" escalation, stub collapsing -- hangs off tool hooks, which only
    fire for calls that actually run. A refused call is invisible to all of them, which
    is exactly why the gap was recorded as "bypasses every one of this file's
    reinforcement hooks entirely" and left open.

    What IS reachable is the refusal message itself: agno adds it to the run's messages
    as a normal tool-role message with tool_call_error=True, so it can be read after the
    fact even though it was never announced as an event.

    Production evidence for why this matters (30-day log review, 2026-08-20): a run whose
    Researcher exhausted its budget spent its remaining turns narrating "I'm encountering
    a persistent tool call limit that's preventing me from retrieving the
    utility_ai_client file. Let me try to get the file content directly from the project
    structure instead" over and over -- agno had already told it "Don't try to execute it
    again" and it kept trying -- until the repetition detector killed the run. Another
    ended with RunCompleted content "No context retrieved. Tool call limits exceeded."
    In both cases the answer rested on evidence the run was never able to gather, and
    nothing said so.
    """
    refused: list[str] = []
    for m in (getattr(result, "messages", None) or []):
        if getattr(m, "role", None) != "tool":
            continue
        if not getattr(m, "tool_call_error", False):
            continue
        if _TOOL_LIMIT_MARKER in str(getattr(m, "content", "") or "").lower():
            name = getattr(m, "tool_name", None) or "<unknown tool>"
            if name not in refused:
                refused.append(name)
    return refused


def _count_delegations(team) -> int:
    """How many real delegations the coordinator made this run. -1 = undeterminable.

    Reads the closure-local counter _make_delegation_log_hook attaches to the team in
    _build_team. -1 (not 0) when the attribute is absent -- a team built by another path
    or a test double must never be read as "delegated nothing", the same rule
    _count_read_calls follows for an unrecognised message shape.
    """
    state = getattr(team, "_delegation_state", None)
    if not isinstance(state, dict) or "count" not in state:
        return -1
    return state["count"]


def _more_grounded(original_result, retry_result) -> bool:
    """True when a retry gathered at least as much evidence as the draft it would replace.

    Every guard in _verified_answer below re-runs the pipeline and then adopts whatever
    comes back UNCONDITIONALLY (`content, result = retried, retry`). That assumes a retry
    is always an improvement. It is not: the retry re-sends the whole original task and
    re-runs the full agent pipeline from scratch, so it can land anywhere -- including on
    a LESS grounded answer than the draft that triggered it.

    Confirmed twice. (1) The 2026-08-15 write-up of the still-open coordinator
    zero-read-calls finding: a retry "repeated the exact wrong-service mistake ...
    producing a confidently WRONG answer that overwrote Researcher's correct one as the
    final result", with verify_claims flagging it too late to block, the one retry budget
    already spent. (2) A 2026-08-20 live groundedness probe on parties_api.py: the draft
    tripped the no-evidence guard, and the retry ALSO made zero read calls -- adopted
    anyway, shipping an answer that missed _check_party_limit, the per-query tenant-
    ownership filtering, and the primary/default uniqueness enforcement entirely, while
    citing line numbers it never opened a file to obtain.

    Read count is the scoring signal because it is already computed, deterministic, and
    free -- no extra model round, no second verify_claims call. It deliberately does NOT
    judge answer quality: a retry that reads MORE and is still wrong is still adopted,
    exactly as before. This only ever rejects a retry that is demonstrably less grounded
    than what it replaces, which is the one case where the old unconditional adopt was
    strictly destructive.

    -1 from either side means "could not tell" -- never treated as "did not read" (see
    _count_read_calls' own docstring on why that distinction matters), so an
    undeterminable comparison keeps the original always-adopt behaviour.
    """
    before = _count_read_calls(original_result)
    after = _count_read_calls(retry_result)
    if before < 0 or after < 0:
        return True
    return after >= before


def _adopt_retry(label: str, content: str, result, retried: str, retry):
    """Adopt a guard's retry only when it is at least as grounded as the current draft.

    Returns the (content, result) pair to carry forward. Falsy `retried` (an empty
    completion) keeps the original, matching every call site's pre-existing `if retried:`
    check -- this helper absorbs that check so the call sites stay one line each.
    """
    if not retried:
        return content, result
    if _more_grounded(result, retry):
        return retried, retry
    print(
        f"[team] {label}: retry gathered LESS evidence than the draft it would replace "
        f"-- keeping the original draft"
    )
    return content, result


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


# How far back into the answer to look for a stated-but-never-taken next action.
# Generous enough to span a real closing clause ("Let me search for relevant
# files and code patterns related to parties and inventory implementation.")
# plus a little slack, narrow enough that an unrelated "let me check X" used
# somewhere in the middle of a long, otherwise-finished answer can't reach in.
_UNFINISHED_INTENT_WINDOW = 260
# The trigger phrases themselves -- deliberately narrow to first-person stated
# INTENT verbs, not e.g. "let me know" (a benign closing offer on an already-
# finished answer, confirmed live as a real near-miss worth excluding
# explicitly). The trailing `[^.!?\n]{0,140}` allows a short object clause
# ("...for relevant files and code patterns...implementation.") without
# reaching so far that it could cross into unrelated later text.
_UNFINISHED_INTENT_RE = re.compile(
    r"\b(let me (?:try|check|search|look at|verify|see)\b|"
    r"i(?:'ll| will) (?:now )?(?:check|search|verify|look|try)\b|"
    r"i need to (?:check|search|verify|look)\b)"
    r"[^.!?\n]{0,140}[.:]?\s*$",
    re.IGNORECASE,
)
# If the matched tail itself already says the step was done ("...which I have
# already done above"), this is a genuinely finished answer that happens to
# restate its own reasoning near the end, not an unfinished one -- confirmed as
# a real near-miss the plain trigger-phrase check alone would false-positive on.
_UNFINISHED_INTENT_COMPLETION_CUE_RE = re.compile(
    r"\b(already|have (?:done|completed|verified|confirmed)|has (?:been )?(?:done|completed))\b",
    re.IGNORECASE,
)


def _ends_with_unfinished_intent(content: str) -> bool:
    """True if `content`'s own final words describe a NEXT action rather than a
    completed one -- the model narrated what it was about to do and then simply
    stopped, instead of actually doing it and reporting a result.

    Confirmed live 2026-08-14: on a genuinely fresh, un-chained session, a run
    returned a clean 200 OK in 33s having only gathered partial Notion data
    (three successful notion_get_page calls, each with an escalating max_lines
    the model apparently assumed meant the content was truncated) -- ending on
    "Let me search for relevant files and code patterns related to parties and
    inventory implementation." with that search never actually happening. Not a
    hang, not a crash, not a token-cap truncation (the returned content was only
    ~1,400 chars, nowhere near coordinator_max_tokens' ~16,000-char ceiling) --
    the coordinator appears to have decided its turn was complete despite the
    task being nowhere near done.

    Deliberately narrow to avoid two real near-misses found while designing
    this: a benign closing offer ("Let me know if you'd like me to look into
    X next") on an ALREADY-finished answer never matches the trigger phrases at
    all (none of them are "let me know"); and a genuinely finished answer that
    happens to restate its own completed reasoning near the end ("I need to
    verify this... which I have already done above") is excluded via
    _UNFINISHED_INTENT_COMPLETION_CUE_RE even though it matches the trigger
    phrase, since the SAME clause also says the step already happened.
    """
    tail = (content or "").rstrip()[-_UNFINISHED_INTENT_WINDOW:]
    m = _UNFINISHED_INTENT_RE.search(tail)
    if not m:
        return False
    return not _UNFINISHED_INTENT_COMPLETION_CUE_RE.search(m.group(0))


async def _verified_answer(content: str, task: str, team, hive_mcp_url: str | None,
                           result=None, liveness_path: str | None = None,
                           hive_mcp_tools=None) -> str:
    """Check the draft's claims and, if any are unverifiable, give the team ONE chance
    to correct itself against the evidence.

    Why a correction round rather than appending the report to the answer: appending
    leaves the fabricated sentence in place as the primary text, and — because the report
    contains phrases like "does not exist in the project" — it would also make a wrong
    answer look right to any downstream check that greps for hedging. That moves the
    metric without fixing the answer. Re-running costs one model round, but only on
    drafts that actually failed, which measured as a minority of runs.

    Bounded at ONE retry TOTAL across this whole call, not one-per-check -- there are
    four separate guards below (claimed-write, claimed-search, no-evidence, verify_claims
    citations/lint), each individually written as "if the retry also fails, surface it,
    don't loop again", but until 2026-08-10 that boundedness was only ever enforced PER
    GUARD, not in aggregate: a draft that tripped multiple guards in sequence could
    trigger up to 4 separate retries in one _verified_answer() call. Confirmed live that
    day: a caching-implementation answer took 20+ minutes AFTER its apply_diff() calls
    had already succeeded, because each retry re-sends the ENTIRE original task text
    (see retry_prompt below), re-triggering the full agent pipeline (ContextRouter
    through Reviewer) from scratch, not a small scoped fix. `len(all_results) == 1`
    (nothing appended yet) is the shared budget check every guard below now uses --
    reusing `all_results` itself as the counter rather than adding a parallel variable,
    since it already exists for `_summarize_actual_writes`. Once ANY guard has spent
    the one retry, every later guard that finds its own problem surfaces a disclaimer
    immediately instead of attempting another full pipeline re-run.
    """
    # Every result object seen this call, original attempt first -- fed to
    # _summarize_actual_writes(*all_results) at every return point so a write that
    # happened in an EARLIER attempt is never lost just because a LATER retry's own
    # trace made no further successful write call. See _summarize_actual_writes'
    # docstring for the live incident this closes.
    all_results = [result]

    # Unfinished-intent check, before every other guard — an answer whose own final
    # words describe a NEXT action rather than a completed one means the rest of its
    # content is incomplete by definition, more fundamental than whether any specific
    # claim within it happens to be right. Confirmed live 2026-08-14: a fresh,
    # un-chained session returned a clean 200 OK in 33s having only gathered partial
    # Notion data, ending on "Let me search for relevant files and code patterns
    # related to parties and inventory implementation." with that search never
    # actually happening — no hang, no crash, just a task that stopped short.
    if _ends_with_unfinished_intent(content or ""):
        if len(all_results) > 1:
            return (
                f"{content}\n\n---\n**This answer ends mid-task — its own final words "
                f"describe a next action that was never taken. Treat this as INCOMPLETE, "
                f"not a finished answer. (Not retried: this run's one correction retry "
                f"was already used by an earlier check.)**"
                + _summarize_actual_writes(*all_results)
            )
        print("[team] answer ends on a stated next action that was never taken — retrying to actually finish the task")
        try:
            retried, retry = await _stream_team_run(
                team,
                f"{task}\n\n"
                f"IMPORTANT: a previous attempt stopped mid-task — it described a next "
                f"step (e.g. 'let me search the codebase for X') but never actually took "
                f"it, and the response ended there without a real answer. Do not repeat "
                f"that mistake: actually call the tool(s) you say you are about to use, "
                f"follow through on every stated step, and only stop once you have a "
                f"complete, finished answer to the original task.",
                liveness_path=liveness_path,
            )
            all_results.append(retry)
            if retried:
                if _ends_with_unfinished_intent(retried):
                    return (
                        f"{retried}\n\n---\n**This answer still ends mid-task after one "
                        f"retry — its own final words describe a next action that was "
                        f"never taken. Treat this as INCOMPLETE.**"
                        + _summarize_actual_writes(*all_results)
                    )
                content, result = _adopt_retry(
                    "unfinished-intent", content, result, retried, retry
                )
        except Exception as exc:
            print(f"[team] unfinished-intent retry failed: {exc}")

    # Claimed-write check — a fabricated "file changed" claim means the rest of
    # the answer's factual content is moot, so this runs before the fact-groundedness
    # checks below rather than after (the unfinished-intent check above runs earlier
    # still, since a task that never finished is more fundamental than either).
    # Only fires when writes are DETERMINABLE and zero
    # succeeded; -1 (undeterminable) is deliberately excluded, matching the read-evidence
    # check's own reasoning: "we could not tell" must never be treated as "nothing
    # succeeded". See _count_successful_write_calls for the live incident this fixes.
    writes = _count_successful_write_calls(result)
    if writes == 0 and _CLAIMED_WRITE_RE.search(content or ""):
        if len(all_results) > 1:
            # Aggregate retry budget already spent by an earlier guard this call --
            # surface rather than attempt a second full pipeline re-run. See this
            # function's docstring for why the budget is shared across all four guards.
            return (
                f"{content}\n\n---\n**This answer claims a file was changed, but no "
                f"apply_diff()/write_file() call actually succeeded — treat this as "
                f"NOT applied. (Not retried: this run's one correction retry was "
                f"already used by an earlier check.)**"
                + _summarize_actual_writes(*all_results)
            )
        print("[team] answer claims a file was written but no write tool call succeeded — retrying with a mandatory write")
        try:
            retried, retry = await _stream_team_run(
                team,
                f"{task}\n\n"
                f"IMPORTANT: a previous attempt claimed a file was created, modified, "
                f"applied, or staged for review, but no apply_diff() or write_file() call "
                f"in that attempt actually succeeded (a successful call's response starts "
                f"with 'review_pending:', 'written:', or 'applied:' — a failed one does "
                f"not, even though it is still a real tool call). Do not repeat that "
                f"mistake: call apply_diff() (for an existing file) or write_file() (for a "
                f"brand-new file), confirm its response actually indicates success, and "
                f"only THEN report the change as made. If the write tool keeps failing, "
                f"report the exact failure message instead of claiming success.",
                liveness_path=liveness_path,
            )
            all_results.append(retry)
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
                content, result = _adopt_retry(
                    "write-claim", content, result, retried, retry
                )
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
    if (unverified_searches or bare_absence) and len(all_results) > 1:
        # Aggregate retry budget already spent -- surface rather than re-run again.
        named = ', '.join(unverified_searches[:6]) if unverified_searches else "the claimed absence"
        return (
            f"{content}\n\n---\n**This answer claims searches that were never "
            f"actually run ({named}) — treat any 'NOT FOUND' conclusion resting on "
            f"them as UNVERIFIED, not confirmed absent. (Not retried: this run's "
            f"one correction retry was already used by an earlier check.)**"
            + _summarize_actual_writes(*all_results)
        )
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
            retried, retry = await _stream_team_run(
                team, f"{task}\n\n{claim_note}", liveness_path=liveness_path,
            )
            all_results.append(retry)
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
                content, result = _adopt_retry(
                    "search-claim", content, result, retried, retry
                )
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
    # Both sources, because neither alone is sufficient (2026-08-21): result.messages
    # holds only the COORDINATOR's own calls, and the run-scoped closure log is the only
    # thing that sees a delegated member's. Once the coordinator is disarmed and
    # everything is delegated, the first is always 0 while real reads are happening --
    # measured live, with Researcher reading models.py (call 10/50) and this guard still
    # reporting "ZERO read calls", then retrying a correct answer into a wrong one.
    # max(), not sum(): a coordinator read appears in BOTH, and inflating the count would
    # distort nothing here (this only tests == 0) but would mislead any future caller.
    reads = max(_count_read_calls(result), _run_read_count(team))
    if reads == 0 and _CLAIMY_RE.search(content or "") and len(all_results) > 1:
        # Aggregate retry budget already spent -- surface rather than re-run again.
        return (
            f"{content}\n\n---\n**This answer states code facts without reading any "
            f"file this run — treat it as UNVERIFIED. (Not retried: this run's one "
            f"correction retry was already used by an earlier check.)**"
            + _summarize_actual_writes(*all_results)
        )
    if reads == 0 and _CLAIMY_RE.search(content or ""):
        print("[team] answer asserts code facts with ZERO read calls — retrying with evidence required")
        try:
            retried, retry = await _stream_team_run(
                team,
                f"{task}\n\n"
                f"IMPORTANT: answer this by READING the relevant file(s) first — use "
                f"get_file_content or search_files. A previous attempt answered without "
                f"opening anything and named a symbol that exists elsewhere in the "
                f"codebase but does not apply here. Base every statement on text you have "
                f"actually read this run, and if the thing asked about does not exist, say "
                f"so plainly.",
                liveness_path=liveness_path,
            )
            all_results.append(retry)
            content, result = _adopt_retry("no-evidence", content, result, retried, retry)
        except Exception as exc:
            print(f"[team] evidence retry failed: {exc}")

        # Retry-COMPLIANCE check. Every guard in this function re-runs the pipeline with a
        # corrective instruction and then carries the result forward -- but none of them
        # ever check that the retry actually DID the thing it was retried for. Measured
        # live 2026-08-20 on a parties_api.py groundedness probe: the draft tripped this
        # guard (zero reads), the corrective retry above ran, the retry ALSO made zero read
        # calls, and its answer was carried forward as final anyway. hive-mcp's own tool log
        # for that window confirms it: zero get_file_content/search_files calls across the
        # whole run, yet the answer cited specific line numbers. Two happened to be right,
        # which is precisely why silent adoption is dangerous -- a confident, correctly-
        # formatted, entirely ungrounded answer is indistinguishable from a real one
        # downstream. verify_claims caught only a fabricated symbol name (a symptom), never
        # the root fact that nothing was read.
        #
        # Deliberately a hard surface, not a second retry: the one-retry aggregate budget
        # exists because each re-run re-triggers the whole pipeline (see this function's
        # docstring), and a model that ignored an explicit "READ the file first" instruction
        # once has already demonstrated the instruction is not landing. -1 (undeterminable)
        # is not 0 and never trips this -- same rule as everywhere else in this file.
        if _count_read_calls(result) == 0 and _CLAIMY_RE.search(content or ""):
            return (
                f"{content}\n\n---\n**UNGROUNDED — this answer states code facts, and "
                f"NEITHER the original attempt NOR the corrective retry opened a single "
                f"file this run. Any file, line number, or symbol named above came from "
                f"the model's own priors, not from this codebase. Treat every specific "
                f"claim as unverified until checked by hand — including ones that look "
                f"plausible.**"
                + _summarize_actual_writes(*all_results)
            )

    # DB-evidence guard: a task explicitly demanding a live-database check must show at
    # least one db_query/db_schema call, not just any read. The generic no-evidence guard
    # above only checks "did it read ANYTHING" -- a task that greps model files instead of
    # querying the live DB still counts as reads>0 and slips through untouched. Measured
    # 2026-08-20: a task saying "you must use the live database (db_query/db_schema), not
    # a file grep or a guess" got answered entirely from a models.py grep, which also
    # fabricated "the items table does not exist" (it does, as inventory.items) -- the
    # live DB was never touched and the generic guard had nothing to catch.
    # Both sources -- a delegated member's db_query is invisible to result.messages.
    db_reads = max(_count_read_calls(result, tool_names=_DB_TOOLS),
                   _run_read_count(team, tool_names=_DB_TOOLS))
    if db_reads == 0 and _DB_TASK_RE.search(task or ""):
        print("[team] task explicitly required a live-DB check but made zero db_query/db_schema calls — retrying")
        try:
            retried, retry = await _stream_team_run(
                team,
                f"{task}\n\n"
                f"IMPORTANT: a previous attempt answered this without calling db_query or "
                f"db_schema, even though the task explicitly requires a live-database check. "
                f"Call db_query/db_schema now and base your answer on their actual output — "
                f"do not answer from a file grep or a guess about what the schema contains.",
                liveness_path=liveness_path,
            )
            all_results.append(retry)
            content, result = _adopt_retry("db-evidence", content, result, retried, retry)
        except Exception as exc:
            print(f"[team] db-evidence retry failed: {exc}")

        # Same retry-compliance rule as the no-evidence guard above: surface loudly rather
        # than silently accept an answer whose corrective retry ignored the correction.
        # A DB question answered without ever touching the DB is the failure that motivated
        # this whole guard (2026-08-20: "the items table does not exist in the codebase" --
        # it exists as inventory.items, models.py:141-142 -- answered off a models.py grep).
        if max(_count_read_calls(result, tool_names=_DB_TOOLS),
               _run_read_count(team, tool_names=_DB_TOOLS)) == 0:
            return (
                f"{content}\n\n---\n**NOT VERIFIED AGAINST THE LIVE DATABASE — this task "
                f"asked for a live-DB check, and neither the original attempt nor the "
                f"corrective retry called db_query/db_schema. Any row count, column, or "
                f"table-existence claim above is inferred from source files, not read from "
                f"the running database, and may not reflect its actual current state.**"
                + _summarize_actual_writes(*all_results)
            )

    # Narrated-unreachable-tool check (2026-08-21). Direction 2 of the "removing a tool
    # does not induce delegation" finding: the coordinator reliably ANNOUNCES the tool it
    # wants before it starts looping, so that announcement is an intercept point nothing
    # else uses.
    #
    # Measured: "the environment shows that list_directory is available. I will now call
    # list_directory directly on the requested path" -- four times verbatim, then a raw
    # <tool_call> tag, zero delegations. The model knew the tool and the path; what it
    # never did was route through a member. It is not ignorance either: the roster
    # preamble names list_directory against ContextRouter/Researcher/Coder explicitly.
    #
    # So the retry does the one thing the surface restriction could not: it names the
    # member id to call and the exact delegate_task_to_member shape to use. Removing the
    # tool told the model what it CANNOT do; this tells it what to do instead.
    narrated = _narrated_unreachable_tool(content, _count_delegations(team))
    if narrated and len(all_results) == 1:
        holder = _member_holding(team, narrated)
        if holder is not None:
            member_id, member_name = holder
            print(f"[team] coordinator narrated an unreachable tool ({narrated}) — "
                  f"retrying with an explicit delegation to {member_id}")
            try:
                retried, retry = await _stream_team_run(
                    team,
                    f"{task}\n\n"
                    f"IMPORTANT: a previous attempt said it would call `{narrated}` "
                    f"directly and then never did — you do NOT have that tool, which is "
                    f"why the attempt stalled and repeated itself. `{member_name}` DOES "
                    f"have it. Call exactly this, once, as your FIRST action:\n"
                    f"    delegate_task_to_member(member_id='{member_id}', "
                    f"task='call {narrated} on the exact path in the question and return "
                    f"its raw output verbatim')\n"
                    f"Then answer using ONLY what that member returns. Do not attempt "
                    f"`{narrated}` yourself again, and do not substitute reading "
                    f"documentation for it — that is what produced the wrong answer.",
                    liveness_path=liveness_path,
                )
                all_results.append(retry)
                content, result = _adopt_retry("narrated-tool", content, result, retried, retry)
            except Exception as exc:
                print(f"[team] narrated-tool retry failed: {exc}")

    # Enumeration-evidence check (2026-08-21). Exact same shape as the db-evidence guard
    # above, for the same reason on a different axis: a tool hook cannot catch this,
    # because the failure is the model ANSWERING without calling anything -- and
    # answering is not a tool call. Only an answer-time check sees it.
    #
    # Why this outranks the stalls it sits beside: a stall fails loudly and the watchdog
    # catches it. This ships a confident, well-formatted, WRONG inventory. In the worst
    # 2026-08-21 case verify_claims even printed the contradicting evidence
    # (FOUND Parties -> inventoryApi.ts:244) in its own report and the answer shipped
    # anyway, asserting the whole feature was missing.
    # Both sources -- see the generic reads check above for why messages alone is 0
    # once the coordinator delegates instead of reading.
    enum_reads = max(_count_read_calls(result, tool_names=_ENUM_TOOLS),
                     _run_read_count(team, tool_names=_ENUM_TOOLS))
    if enum_reads == 0 and _is_enumeration_task(task):
        print("[team] task asked for an enumeration but made zero list_directory/"
              "find_files calls — retrying with evidence required")
        try:
            retried, retry = await _stream_team_run(
                team,
                f"{task}\n\n"
                f"IMPORTANT: a previous attempt answered this WITHOUT ever listing the "
                f"directory — it answered from memory, and produced a list that was "
                f"missing most of the real files. Call list_directory (or find_files "
                f"with a glob) on the exact path NOW and enumerate from that tool's own "
                f"output. Do not name files you remember; name the ones the tool "
                f"returned. If the path does not exist, say so plainly rather than "
                f"listing what you expect to be there.",
                liveness_path=liveness_path,
            )
            all_results.append(retry)
            content, result = _adopt_retry("enumeration", content, result, retried, retry)
        except Exception as exc:
            print(f"[team] enumeration-evidence retry failed: {exc}")

        # Same retry-compliance rule as the two guards above: surface loudly rather than
        # silently accept an answer whose corrective retry ignored the correction.
        if max(_count_read_calls(result, tool_names=_ENUM_TOOLS),
               _run_read_count(team, tool_names=_ENUM_TOOLS)) == 0:
            return (
                f"{content}\n\n---\n**NOT VERIFIED BY A DIRECTORY LISTING — this task "
                f"asked what a directory contains, and neither the original attempt nor "
                f"the corrective retry called list_directory or find_files. Any file "
                f"list, count, or \"no such file\" claim above is recalled rather than "
                f"enumerated, and has been wrong by a factor of 6-8x on this exact "
                f"failure before.**"
                + _summarize_actual_writes(*all_results)
            )

    # Tool-budget-exhausted check. Unlike every other guard here this one cannot force a
    # retry: a re-run would hit the same ceiling, and agno has already told the model
    # "Don't try to execute it again". Disclosure is the whole remedy -- the answer may
    # rest on evidence the run was refused, and previously nothing said so. Checked
    # across ALL attempts, not just the last: an earlier attempt exhausting its budget
    # still shaped the answer that survived.
    refused_tools: list[str] = []
    for r in all_results:
        for name in _tools_refused_for_limit(r):
            if name not in refused_tools:
                refused_tools.append(name)
    if refused_tools:
        named = ", ".join(f"`{t}`" for t in refused_tools[:6])
        print(f"[team] tool_call_limit refused {len(refused_tools)} tool(s): {named} — "
              f"answer may rest on evidence the run could not gather")
        content = (
            f"{content}\n\n---\n**Tool budget exhausted — this run hit its "
            f"`tool_call_limit` and agno REFUSED to execute: {named}. Those calls never "
            f"ran, so anything above that depended on them is unsupported rather than "
            f"verified. Treat this answer as partial: re-run the narrower question on "
            f"its own, or raise that role's budget via `/admin/model-routes`.**"
        )

    # Coordinator-authored-alone check. The mechanical gates this codebase relies on
    # (decompose-first, search-before-browse) are wired onto the RESEARCHER's tool calls,
    # so an answer the coordinator writes itself, without ever delegating, is subject to
    # none of them. Documented as a still-open finding on the "Groundedness & Reliability
    # Hardening" page (2026-08-15): a coordinator-authored retry "repeated the exact
    # wrong-service mistake Phase 6 fixed ... producing a confidently WRONG answer that
    # overwrote Researcher's correct one as the final result."
    #
    # Both external references for this architecture say the same thing: LangGraph's
    # supervisor pattern is defined by the supervisor NOT executing tasks itself, and
    # Cognition's 2026 write-up concludes multi-agent works only when the extra agents
    # "contribute intelligence rather than actions". A coordinator that both orchestrates
    # and authors is the case neither design allows for.
    #
    # Scoped to multi-part tasks ONLY, matching the decompose-first gate's own scope: a
    # single bounded question ("is X present in Y") answered directly by the coordinator
    # is legitimate and explicitly encouraged ("stop once answered"), so flagging it would
    # be pure noise. Surfaces rather than retries -- the retry would be coordinator-
    # authored too, which is the very thing being flagged. -1 (undeterminable) never trips
    # it, same rule as everywhere else in this file.
    delegations = _count_delegations(team)
    if delegations == 0 and _is_multi_part_task(task) and _CLAIMY_RE.search(content or ""):
        print("[team] multi-part task answered with ZERO delegations — coordinator "
              "authored it alone, bypassing the researcher-scoped gates")
        content = (
            f"{content}\n\n---\n**Answered by the coordinator alone — this multi-part "
            f"task was never delegated to a specialist agent, so the decompose-first and "
            f"search-before-browse gates that normally constrain this kind of answer "
            f"never applied to it. Treat its coverage as unaudited: the known failure "
            f"mode here is a confident answer that silently examines the wrong "
            f"service/module.**"
        )

    report, bad, unavailable = await _verify_claims(content, hive_mcp_url, hive_mcp_tools)
    if unavailable:
        return content + _UNVERIFIED_DISCLAIMER + _summarize_actual_writes(*all_results)
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

    # "DOC ONLY" deliberately excluded (2026-08-15, T1e live incident, engineering
    # team) -- hive-mcp/tools/verify.py's DOC ONLY branch no longer counts toward
    # `problems`/`bad` (a real documentation citation is not fabrication just because
    # a literal code-grep can't independently confirm it), so a report reaching this
    # retry with ONLY DOC ONLY items no longer gets here at all. But if a retry DOES
    # fire for a genuinely separate reason (a real NOT FOUND/BAD citation elsewhere in
    # the same report), any DOC ONLY line present must still never be swept into this
    # "does not exist... do not mention it again" instruction -- that is factually
    # wrong for a DOC ONLY item and was the exact live incident that caused a
    # correctly-cited, real pattern (patterns/ekam-frontend.md:1109) to be discarded
    # and reported as not existing.
    missing_symbols = [t for ln in report.splitlines()
                        if (t := _claim_token(ln, ("NOT FOUND",)))]
    bad_citations = [t for ln in report.splitlines()
                      if (t := _claim_token(ln, ("BAD", "AMBIGUOUS", "MISMATCH")))]
    # verify_claims' symbol-anchored MISMATCH already KNOWS the right answer -- its own
    # message ends "...it actually appears at line(s) 116, 208", computed by
    # _symbol_line_numbers reading the real file. Until 2026-08-21 that was printed and
    # thrown away: the retry was told only that the citation was wrong, never where the
    # symbol actually is, so it had to rediscover a fact the checker had in hand.
    # Harvesting it turns the retry from a guess into a correction.
    corrected_lines = _CORRECT_LINE_RE.findall(report)
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
    if len(all_results) > 1:
        # Aggregate retry budget already spent by an earlier guard this call --
        # surface the verify_claims report rather than attempt a second full
        # pipeline re-run.
        return (f"{content}\n\n---\n**Unverified claims flagged automatically "
                f"(these could not be found in the repository, and this run's one "
                f"correction retry was already used by an earlier check):**\n"
                f"```\n{_reader_facing_report(report)}\n```"
                + _summarize_actual_writes(*all_results))
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
        # A BOUNDED re-read, not a whole-file one (2026-08-21). Root-caused live: the
        # previous wording said "call get_file_content on the exact file(s) involved",
        # the model complied exactly, and reproduced the SAME unbounded read that
        # produced the wrong number in the first place. Not a compliance failure --
        # the instruction prescribed the wrong remedy.
        #
        # Measured on API/inventory-service/models.py (774 lines, 32KB, well under the
        # skeleton threshold so the real numbered content IS returned):
        #   get_file_content(offset=124, limit=12)  -> "129\tsku_prefix = Column(
        #       String(8), nullable=True)" reproduced EXACTLY
        #   get_file_content(whole file)            -> "line 142 ... String(10)" one
        #       run, "line 142 ... String(20)" the next; the truth is line 129
        # Same file, same model, same run config -- only the read shape differed. In a
        # 774-line numbered dump the model interpolates a plausible line number rather
        # than copying one, which is why the wrong value changes every run. Numbered
        # output made citations copyable; it cannot make a model copy instead of
        # estimate, and a narrow window is what removes the opportunity to estimate.
        window_hint = _citation_retry_hint(corrected_lines)
        # Imperative FIRST/THEN, mirroring the reads==0 branch above (proven wording,
        # not a new pattern). A prior version phrased this as one soft "re-read the
        # file and copy the exact line number" sentence — measured live 2026-08-04,
        # the model did NOT reliably comply: a citation retry came back citing a
        # DIFFERENT wrong line than before rather than the verified-correct one,
        # meaning it answered from memory/estimation again instead of actually
        # reissuing a read. Making the read step syntactically first and separate
        # from the answering step is a cheaper lever than trusting prose compliance.
        instructions.append(
            f"these file:line citations were wrong or unresolvable: {named}. FIRST make a "
            f"fresh, NARROW tool call to locate them — do not rely on a read from earlier "
            f"in this conversation, your memory of its line numbers may be wrong. "
            f"{window_hint}"
            f"THEN answer using the line number exactly as printed in that tool's own "
            f"numbered output — never a recalled, estimated, or rounded number. If a "
            f"filename is shared by more than one file in the project, cite the full "
            f"repo-relative path instead of the bare filename."
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
    # Snapshotted so the retry's OWN reads can be measured as a delta (2026-08-21).
    # The run-scoped log is cumulative, so comparing its total would always look like
    # "the retry read something" -- the original attempt's reads are in there too.
    _reads_before_retry = _run_read_count(team)
    try:
        corrected, retry = await _stream_team_run(
            team, retry_prompt, liveness_path=liveness_path,
        )
        all_results.append(retry)
    except Exception as exc:
        print(f"[team] verify retry failed: {exc}")
        return content + _summarize_actual_writes(*all_results)
    if not corrected:
        return content + _summarize_actual_writes(*all_results)

    # Still not a second retry -- that would break the "bounded at ONE retry" design
    # documented above. But as of 2026-08-20 this is no longer ONLY a log line.
    #
    # Confirmed live 2026-08-04 that a citation-correction retry can silently skip
    # re-reading, and this printed it so the fact stopped requiring hours of log
    # archaeology to notice. What it did NOT do was tell the person reading the ANSWER.
    # A 2026-08-20 probe showed why that gap matters: a run corrected its citations
    # without opening a single file, and the corrected line numbers happened to be right,
    # so verify_claims' grep passed them cleanly and the answer shipped looking fully
    # verified. Grep-checking proves a citation RESOLVES; it cannot prove the citation was
    # DERIVED from the file rather than recalled. Only the read count separates those two,
    # and the reader is the one who needs to know which they are holding.
    # Delta, not total: did THIS retry read, at any delegation depth. result.messages
    # alone sees only the coordinator's own calls, so once everything is delegated it
    # reports zero on a retry that genuinely re-read -- the same blindness that made the
    # generic groundedness guard retry correct answers into wrong ones.
    _retry_reads = _run_read_count(team)
    _retry_delta = (_retry_reads - _reads_before_retry
                    if _retry_reads >= 0 and _reads_before_retry >= 0 else -1)
    citations_unread = bool(bad_citations) and max(_count_read_calls(retry), _retry_delta) == 0
    if citations_unread:
        print("[team] citation-correction retry made ZERO read calls — "
              "it answered from memory/estimation again instead of re-reading")
    unread_note = (
        "\n\n---\n**Citations were corrected WITHOUT re-opening any file — the retry that "
        "produced them made zero read calls, so every line number above is recalled, not "
        "re-derived. Ones that resolve correctly may do so by coincidence; verify any you "
        "intend to act on.**"
        if citations_unread else ""
    )

    report2, still_bad, still_unavailable = await _verify_claims(
        corrected, hive_mcp_url, hive_mcp_tools)
    if still_unavailable:
        return (corrected + _UNVERIFIED_DISCLAIMER + unread_note
                + _summarize_actual_writes(*all_results))
    if still_bad:
        # Surface rather than hide: the reader needs to know which claims are unsupported.
        return (f"{corrected}\n\n---\n**Unverified claims flagged automatically "
                f"(these could not be found in the repository):**\n```\n{_reader_facing_report(report2)}\n```"
                + unread_note
                + _summarize_actual_writes(*all_results))
    return corrected + unread_note + _summarize_actual_writes(*all_results)


async def _fill_count_markers(content: str, hive_mcp_url: str | None,
                              hive_mcp_tools=None) -> str:
    """Replace [[COUNT pattern=`..` glob=`..`]] markers with the exact count from
    hive-mcp's count_matches tool. The number is ALWAYS tool-derived. Malformed or
    unresolvable markers become '[count unavailable]'. No-op when there are no markers.

    Like _verify_claims, prefers the run's own live MCP session (`hive_mcp_tools`) and
    only opens a fresh connection as a fallback -- this runs post-run, in the same place
    and for the same reason: opening a new streamablehttp_client while the run's existing
    connections to the same server are open is the step confirmed to hang on 2026-08-20.
    Optional and defaulting to None, so any caller without a live handle is unaffected.
    """
    if not content or "[[COUNT" not in content:
        return content
    if not (hive_mcp_url or hive_mcp_tools):
        return _COUNT_MARKER_ANY.sub("[count unavailable]", content)

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    matches = list(_COUNT_MARKER_RE.finditer(content))
    if not matches:
        return _COUNT_MARKER_ANY.sub("[count unavailable]", content)

    cache: dict = {}
    resolved_any = {"ok": False}

    async def _resolve_with(session) -> None:
        for mt in matches:
            key = (mt.group(1), mt.group(2))
            # Skip only keys already resolved to a REAL count, so a fallback attempt
            # re-tries the ones a dead session turned into '[count unavailable]'.
            if cache.get(key, "[count unavailable]") != "[count unavailable]":
                continue
            try:
                res = await session.call_tool(
                    "count_matches", {"pattern": key[0], "glob_filter": key[1]}
                )
                err = _mcp_error_text(res)
                if err:
                    # Deliberately does NOT set resolved_any: a tool-level rejection
                    # (wrong server, tool missing) must still count as "this session
                    # produced nothing", so the fresh-connection fallback fires.
                    print(f"[team] count_matches rejected ({key!r}): {err}")
                    cache[key] = "[count unavailable]"
                    continue
                m = re.search(r"TOTAL:\s*(\d+)", _extract_mcp_text(res))
                cache[key] = m.group(1) if m else "[count unavailable]"
                resolved_any["ok"] = True
            except Exception as exc:
                print(f"[team] count verify failed ({key!r}): {exc}")
                cache[key] = "[count unavailable]"

    async def _resolve_on_live_session() -> None:
        await _resolve_with(await hive_mcp_tools.get_session_for_run())

    async def _resolve_on_fresh_connection() -> None:
        async with streamablehttp_client(hive_mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _resolve_with(session)

    # The per-marker try/except above swallows a dead-session error into
    # '[count unavailable]' rather than raising, so a session-level failure would never
    # reach the outer handler. resolved_any is what distinguishes "the session is gone"
    # (nothing resolved) from "these specific patterns genuinely have no matches", and
    # is the trigger for falling back to a fresh connection.
    if hive_mcp_tools is not None:
        try:
            await asyncio.wait_for(_resolve_on_live_session(),
                                   timeout=_BESPOKE_MCP_SESSION_TIMEOUT)
        except Exception as exc:
            print(f"[team] count-marker guard: live session failed "
                  f"({type(exc).__name__}: {exc or '<no message>'})")

    if not resolved_any["ok"] and hive_mcp_url:
        try:
            await asyncio.wait_for(_resolve_on_fresh_connection(),
                                   timeout=_BESPOKE_MCP_SESSION_TIMEOUT)
        except Exception as exc:
            print(f"[team] count-marker guard: hive-mcp unreachable ({hive_mcp_url}): "
                  f"{type(exc).__name__}: {exc or '<no message>'}")
            return _COUNT_MARKER_ANY.sub("[count unavailable]", content)

    out = content
    for mt in matches:
        out = out.replace(mt.group(0), cache.get((mt.group(1), mt.group(2)), "[count unavailable]"))
    return _COUNT_MARKER_ANY.sub("[count unavailable]", out)  # strip any malformed leftovers


_CACHEABLE_READ_TOOLS = {
    "get_file_content", "get_files_batch", "search_files", "search_files_batch",
    "find_files", "list_directory", "list_directory_tree", "count_matches",
    # lightrag_query added 2026-08-15 -- live-confirmed the exact same self-
    # reinforcing loop this whole cache exists for (see the block comment right
    # below), just on a different tool: a parallel-review run called
    # lightrag_query with the IDENTICAL (query, project_id, mode) args 9+ times
    # in a row, each time getting the SAME successful real result back but
    # writing "failed, falling back to find_files" into shared state anyway and
    # then immediately repeating the same call instead of actually falling back.
    # No new mechanism needed -- lightrag_query is read-only and deterministic
    # within a run exactly like the tools already here; adding it reuses the
    # proven duplicate-serve escalation (2 full serves, then an escalating stub,
    # then the aggregate forced-answer nudge) instead of a bespoke fix.
    "lightrag_query",
    # search_knowledge_graph added 2026-08-15, same day, same pattern: a planning-
    # team run cycled the SAME set of ~6 search_knowledge_graph queries
    # ({'query': 'parties form', 'limit': 10}, 'parties page', 'parties
    # component', ... ) TWICE through in a row, each call genuinely succeeding,
    # then stopped calling any tool at all and streamed nothing but empty content
    # for 2+ minutes until the 300s liveness auto-kill fired. Same fix as
    # lightrag_query above -- this tool is read-only/deterministic within a run,
    # widening the cache set is a proven pattern, not a new mechanism.
    "search_knowledge_graph",
    # web_search/web_fetch added 2026-08-18, same pattern a third time, found
    # during a live T1-T13 groundedness re-run: an engineering run researching an
    # external tool (Stalwart mail server docs) cycled the IDENTICAL 5-call
    # sequence (2 web_search queries + 3 web_fetch URLs) roughly every 13-15s for
    # over a minute -- no "FORCED STOP" ever fired, because neither tool was in
    # this set -- then stopped calling any tool at all and streamed nothing but
    # empty content until the 300s liveness auto-kill fired. Same fix as
    # lightrag_query/search_knowledge_graph above: both are read-only within a
    # run (a web page's content or a search result set does not change
    # mid-task), so the same duplicate-serve escalation applies cleanly.
    "web_search", "web_fetch",
    # The zero/low-argument catalog + context tools, added 2026-08-21 -- the FOURTH
    # and FIFTH instances of this identical pattern, both caught in one T1-T13 re-run:
    #
    #   T7 ("does API/loyalty-service exist?"): the coordinator called list_skills({})
    #   about TWENTY times, ~1/second, byte-identical empty args, exhausting its
    #   tool_call_limit in ~22s. Past that agno's own run_function_calls silently
    #   rejects further calls with zero stream events, so the model generated
    #   contentless turns for 300s until the liveness auto-kill fired. It never once
    #   looked for the directory it was asked about.
    #
    #   T9: an engineering run cycled get_project_context({}) -> get_file_content(
    #   docs/frontend.md, offset 232, limit 10) -> get_project_context({}) three times
    #   identically. get_file_content was already cached and correctly stubbed;
    #   get_project_context was not, so the loop kept its footing on the uncached half.
    #
    # A no-argument tool is the WORST case for this failure, not an edge case: every
    # call is trivially byte-identical, so a model that loses track of what it already
    # has can re-issue it indefinitely at zero prompt cost. All five here are read-only
    # and fixed for the life of a run -- a skill's text, the project context blob, a
    # DOCS.md section and the recent-git-activity list do not change mid-task.
    #
    # Deliberately NOT added: list_processes/check_port/get_env_info (genuinely change
    # between calls, so a repeat is legitimate), the git_* family (git_status changes
    # the moment any write lands), and db_query/db_schema (a migration mid-run would
    # make a cached answer wrong). Caching those would trade a repetition loop for a
    # stale answer, which is the worse failure.
    "list_skills", "load_skill", "get_project_context",
    "get_context_section", "list_recent_files",
}

# The network-only cache below (skip the hive-mcp round-trip, still hand back the
# full result every time) was not enough on its own -- confirmed live 2026-08-11: a
# Researcher cycled the SAME 4 files (774+965-line reads) for 4+ minutes, each
# re-serve appending another full copy into its own context. That's a self-reinforcing
# spiral, not just wasted network calls: more context -> the model loses track of
# what it already has -> it "re-verifies" by reading again -> more context. Serve 2+
# of an identical (agent, DELEGATION GENERATION, tool, args) key gets a short stub
# instead of the real content: this removes the payoff for repeating the call (the
# model cannot get the content again by asking again) and stops the bloat at its
# source, rather than hoping an instruction talks the model out of it -- the same
# tool-surface-over-instruction lesson this module already has on record elsewhere
# (_strip_mutating's docstring, 2026-07-31; _COORDINATOR_DISCOVERY_TOOLS, 2026-08-11).
#
# Lowered 2 -> 1 (2026-08-19, T6 follow-up): the ORIGINAL reason for tolerating a 2nd
# full serve was "a second delegate_task_to_member call to the same role may start
# with fresh context and legitimately need it again" -- but a plain (agent, tool,
# args) key couldn't tell that case apart from a repeat WITHIN the same delegation,
# so BOTH got the same 2-serve tolerance. Confirmed live: Reviewer repeated its own
# entire 8-call first read pass verbatim, inside one single delegation, entirely
# within this tolerance -- both the original and the repeat got full content with
# zero pushback, silently burning half its tool_call_limit budget on work already
# done. The serve_key above now also carries the delegation GENERATION (bumped once
# per delegate_task_to_member(s) call, see delegation_generation below) specifically
# so the legitimate case this constant used to protect is still protected: a genuinely
# NEW, separate delegation to the same role starts a fresh generation bucket and so
# still gets its own full serve at 1, unaffected by an earlier generation's history.
# Only a repeat WITHIN the same generation (the same delegation instance) is now
# caught immediately, on its 2nd ask, instead of being silently tolerated once first.
_MAX_FULL_SERVES_PER_AGENT = 1
# From serve 5 onward the stub escalates to a stronger, more directive wording -- a
# model that already ignored two stubs for the identical call needs a harder nudge,
# not the same sentence a third time.
_STUB_ESCALATION_SERVE = 5

# 2026-08-15: a THIRD escalation tier, keyed on the AGGREGATE total_stub_serve_count
# (already computed for the Tier-3 liveness signal, config.liveness_aggregate_stub_threshold
# = 15) rather than any single (agent, tool, args) key's own count. Confirmed live: a
# run that correctly grounded itself in the right files (unlike every earlier incident
# this file's other guards target) still never converged -- it cycled through the SAME
# ~5 already-fully-read files, re-requesting each one in turn, so no INDIVIDUAL file's
# serve_count crossed _STUB_ESCALATION_SERVE by much before the run was killed by the
# Tier-3 aggregate liveness check at total_stub_serve_count > 15, having produced zero
# answer. The per-key escalated stub message ("do not call this again") only ever
# addresses the ONE file just re-requested -- it says nothing about the other 4 in the
# rotation, so the model can "route around" it by cycling to a different already-read
# file instead of actually stopping. This tier fires once the AGGREGATE crosses a
# threshold well before the Tier-3 kill (15), addressing the rotation as a whole
# instead of one file at a time -- a genuine chance to converge instead of only ever
# hearing about it after the run has already been killed with no answer produced.
_FORCED_ANSWER_AGGREGATE_THRESHOLD = 6


def _duplicate_read_stub(function_name: str, args: dict, agent_key: str, serve_count: int, result_len: int) -> str:
    who = agent_key or "the coordinator"
    if serve_count < _STUB_ESCALATION_SERVE:
        return (
            f"Already returned this exact {function_name}({args}) result to {who} "
            f"{serve_count - 1} time(s) already this run ({result_len} chars, unchanged). "
            f"It is already in your context — use it, do not call this again. For a "
            f"different part of the same file, pass a different offset/limit."
        )
    return (
        f"STOP calling {function_name}({args}) — this is repeat #{serve_count} of the "
        f"IDENTICAL call for {who} this run, and the result ({result_len} chars) has not "
        f"changed and will not change. Repeating it again will not give you new "
        f"information. You already have everything this call can return. If you are "
        f"stuck, answer with what you have now, or say what you could not determine — "
        f"do not call this again."
    )


def _not_found_retry_stub(relative_path: str, count: int) -> str:
    """2026-08-18 live incident: get_file_content on a path that does not exist
    ANYWHERE in the project (hive-mcp's own zero-candidate branch, plain
    "File not found: <path>", no correction/candidate list to follow) got
    retried with a steadily incrementing offset (0 -> 2500+ across 25 calls)
    as if paginating a real file -- a plausible-looking but wrong inference,
    since offset/limit ARE real, valid parameters for a genuine truncated
    read, and nothing in the tool's own response for this specific branch
    said otherwise (unlike the multi-candidate branch, which already tells
    the model exactly what to do next). Each retry's offset differs, so
    _MAX_FULL_SERVES_PER_AGENT's identical-args dedup never catches this --
    a SEPARATE counter, keyed on relative_path alone (ignoring offset/limit,
    and not per-agent: a file's existence is an objective fact, not
    context-dependent), closes it. Live-caught burning 25+ calls with no
    sign of stopping; a py-spy dump later confirmed the run this preceded
    eventually stalled for an unrelated reason (see
    config.model_request_timeout_s), but this loop is a real, distinct waste
    on its own regardless of how the run that contained it ended."""
    return (
        f"STOP calling get_file_content('{relative_path}', ...) with a different "
        f"offset/limit — this is attempt #{count} for a path that has been confirmed "
        f"NOT TO EXIST anywhere in the project. A nonexistent file has no content at "
        f"any offset; changing offset/limit will never produce a different result. "
        f"Call find_files() or search_files() to locate the correct path instead."
    )


def _forced_answer_nudge(agent_key: str, total_stub_count: int) -> str:
    """The aggregate-tier message (see _FORCED_ANSWER_AGGREGATE_THRESHOLD) — replaces
    _duplicate_read_stub's normal per-key wording once the run-wide total crosses the
    threshold, on EVERY subsequent duplicate call (not a one-shot), since a model that
    ignores it once may still respond to seeing it again rather than reverting to a
    softer message that already failed to land."""
    who = agent_key or "the coordinator"
    return (
        f"FORCED STOP — this is not about the one file {who} just re-requested: across "
        f"this whole run, {who} has now re-requested already-fetched content "
        f"{total_stub_count} times total, counting duplicates of ANY file, not just this "
        f"one. That means {who} is cycling through content already fully in context "
        f"instead of making progress. Do NOT call get_file_content, find_files, "
        f"search_files, list_directory_tree, or get_files_batch again this turn — of ANY "
        f"file. Write the final answer NOW using what is already in context. If something "
        f"specific is still genuinely missing, say exactly what could not be determined "
        f"instead of attempting another read."
    )


# ── Runtime harness safeguards for a model that ignores the stub warnings ──────
#
# T12 live incident (2026-08-19, task kni9lmkzx): Reviewer called
# get_file_content('API/inventory-service/router/parties_api.py', offset=0,
# limit=0) 29 TIMES IN A ROW, roughly every 2 seconds, each time receiving the
# escalated FORCED STOP warning above and simply calling again immediately --
# text alone did not redirect it. Confirmed via the same run's own free-text
# output: right before this, the model's own written answer had ALSO degenerated
# into repeating the same sentence dozens of times ("The conclusion is that
# there are no RTK Query hooks...") -- the same underlying LLM failure mode
# (a model whose own recent context is dominated by a repeated pattern becomes
# statistically more likely to keep extending that pattern) manifesting as a
# repeated TOOL CALL instead of repeated prose. The liveness watchdog's
# max_stub_serve_count Tier-2 signal did eventually kill the run with a clear,
# accurate reason ("repeated an identical call 18 times despite being told to
# stop") -- a real improvement over T6/T12's original silent 300s hang -- but
# 29 wasted calls (~58s) is still 29 more than necessary once the model has
# unambiguously stopped listening to the text warning.
#
# Two complementary, MECHANICAL fixes (matching this file's own established
# doctrine -- _strip_mutating's docstring, 2026-07-31: "Instructions shape what
# a model says; only the tool surface constrains what it does"), both wired
# into _make_read_cache_tool_hook at the point a stub is about to be served:
#
# 1. Force text-only after N consecutive ignored stubs (this function): once an
#    agent has been served _FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS stubs IN A
#    ROW (reset to 0 by any real, non-stubbed fetch), we force it into text-only
#    mode. tool_choice="none" is the real OpenAI-API-compatible instruction that
#    structurally PREVENTS the model from calling any tool at all on its next
#    turn -- it must respond with text -- which is exactly what the existing
#    _forced_answer_nudge message already asks for in words. This turns "please
#    stop and answer" from a suggestion into the only option.
#
#    CORRECTED 2026-08-19 (live T6 re-test, task kcu56j2i3) -- the original
#    version of this fix mutated `agent.tool_choice` and was WRONG about where
#    agno reads it from. `agent.tool_choice` is captured ONCE, as a plain
#    function-call argument, when Agent._run calls
#    acall_model_with_fallback(..., tool_choice=agent.tool_choice, ...)
#    (agno/agent/_run.py) -- and the entire repeated tool-call loop for one
#    delegation happens INSIDE that single call, inside Model.aresponse_stream's
#    own `while True:` loop (agno/models/base.py), which reuses that same
#    captured value every iteration via `tool_choice=tool_choice or
#    self._tool_choice`. A tool_hook mutating `agent.tool_choice` mid-loop was
#    therefore mutating something that had already been read and passed by
#    value -- it could only ever affect a FUTURE, separate arun() call (i.e. the
#    next delegate_task_to_member), never the CURRENT in-flight one where the
#    repeat was actually happening. And since a fresh delegation resets this
#    right back to None (see _read_cache_tool_hook's _DELEGATION_TOOL_NAMES
#    branch, by original design so a new delegation gets a clean slate), the two
#    halves of the mechanism cancelled each other out and it never functionally
#    fired. Live-confirmed: Researcher's consecutive-stub streak crossed the
#    threshold at serve #4, yet calls #5 and #6 still went through identically;
#    what actually stopped the loop was the pre-existing, unrelated aggregate-
#    threshold `_forced_answer_nudge` (a soft, in-content nudge, not a hard
#    API-level constraint).
#
#    The real per-iteration value in that `while True:` loop is `self._tool_choice`
#    -- a private attribute read LIVE off the Model instance (`self`) on every
#    pass, precisely because `tool_choice or self._tool_choice` only falls
#    through to it when the captured outer param is falsy (the normal case,
#    since agent.tool_choice defaults to None). Mutating `agent.model._tool_choice`
#    from inside a tool_hook DOES reach the current, in-flight loop on its very
#    next iteration -- `agent.model` is the exact same object bound as `self`
#    there (get_model() in swarm/agents.py builds one distinct Model instance
#    per agent, so this mutation cannot leak across roles). This function now
#    mutates `agent.model._tool_choice` (the fix); `agent.tool_choice` is kept
#    as a harmless, defensive belt-and-suspenders set in case a future agno
#    version changes the outer capture to be re-read per iteration too.
#    Reset back to None (full tool access restored) at the start of the next
#    FRESH delegation to that member (see _read_cache_tool_hook's
#    _DELEGATION_TOOL_NAMES branch) -- a new delegation deserves a clean slate,
#    same principle _MAX_FULL_SERVES_PER_AGENT's own generation-scoping uses.
#    Only covers delegated MEMBER agents (agent is not None) -- the coordinator's
#    own direct calls (agent=None) have no accessible mutable object here, same
#    scoping gap _record_read's docstring already notes for other mechanisms;
#    the coordinator mostly delegates rather than reading directly, so this is
#    the common case, not full coverage.
_FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS = 3


# How many calls of an agent's budget to leave unspent when forcing it to answer.
# 1, not 0: the guard fires AFTER the call that reaches (limit - reserve) returns, so
# with 1 the agent still holds one unused call at the moment tool_choice flips. That
# margin is what guarantees agno never has to refuse anything -- the whole point.
_TOOL_BUDGET_RESERVE = 1


def _force_text_only(agent, team=None) -> None:
    """Flip an agent OR the coordinator into text-only mode on its next model call.

    agno re-reads `tool_choice` off the live object at every model-call site -- both
    `agent.tool_choice` (agent/_run.py) and `team.tool_choice` (team/_run.py, five
    sites) -- so mutating it from inside a tool hook takes effect on the very next turn.

    The `team` fallback was missing until 2026-08-21 and the omission was total for the
    coordinator: its own tool calls arrive at a hook with `agent=None` (agno passes
    `function._agent`, which is unset for a Team-owned function), so the early return
    fired every time and NOTHING was ever forced. Measured live, with the budget guard
    correctly detecting the threshold and then failing to act on it:

        [team] Coordinator reached 59/60 tool calls - forcing text-only ...
        [budget] Coordinator: call 110/60 (list_processes)

    110 calls against a limit of 60, and the run then stalled into the 300s liveness
    kill. Detection was never the gap; the actuator was a no-op for the one caller that
    needed it most.
    """
    target = agent if agent is not None else team
    if target is None:
        return
    target.tool_choice = "none"
    model = getattr(target, "model", None)
    if model is not None:
        # Set on the MODEL too, not only the agent/team (2026-08-21). This is what
        # VLLMToolFix._sanitize_forced_text reads to know the harness took the tool
        # away deliberately -- without it, that layer would "recover" the leaked
        # <tool_call> tags back INTO a call and undo the forcing. The two changes only
        # work as a pair: tool_choice="none" stops the runaway, and the model-side flag
        # is what stops the model's fallback syntax from becoming the user's answer.
        #
        # Measured with a controlled probe (one variable changed, served model):
        #   tool_choice omitted -> finish_reason "tool_calls", content ""
        #   tool_choice "none"  -> finish_reason "stop", content "<tool_call>{...}"
        # This model does not stop WANTING the tool; it writes the Hermes tags it was
        # trained on as prose, and agno sees an ordinary text reply made of syntax.
        model._tool_choice = "none"


def _make_tool_budget_guard_hook(
    team_name: str | None, activity: dict | None = None, role: str | None = None,
):
    """Force an answer just BEFORE tool_call_limit is reached, rather than trying to
    react to the refusal after it (2026-08-21).

    This closes the gap this file has carried as "agno's own tool_call_limit rejection
    bypasses every one of this file's reinforcement hooks entirely". Both reactive
    routes are provably closed, each confirmed by reading source rather than assuming:

      * A tool hook never fires for a refused call -- agno appends
        create_tool_call_limit_error_result(fc) and `continue`s, so it never enters
        function_calls_to_run and no tool event is emitted (see
        _tools_refused_for_limit's docstring).
      * A stream-loop watcher never sees it either -- during streaming agno yields only
        lightweight Event objects with no .messages; the TeamRunOutput that carries them
        arrives once, at the end (see _stream_team_run's docstring). By then the run is
        over and there is no "next iteration" to steer.

    So this does not observe the refusal at all. It counts the calls that DO succeed --
    which hooks see perfectly -- against the same budget agno is counting toward, and
    forces text-only one call early. There is then no refusal to detect, because the
    model has no tool call left to make.

    Measured cost of not having this: two stalled runs, each making exactly 25 calls
    (config.tool_call_limit), after which the model re-emitted the same refused call at
    ~2/s for 300s until the liveness watchdog killed the run -- no answer, no diagnostic,
    a 97.9% prefix-cache hit rate confirming the prompt never advanced. The completing
    branch of the same condition already produces an honest partial answer via
    _tools_refused_for_limit; this makes the hanging branch produce one too.

    Counted per (agent, role) with its own resolved budget, since limits differ per role
    (engineering: Coordinator 60, Researcher 50, Reviewer 45, others config's 25). The
    coordinator's own calls arrive with agent=None and are counted under "Coordinator".
    """
    counts: dict[str, int] = {}
    fired: set[str] = set()

    # `team` is declared so agno SUPPLIES it -- _build_hook_args (tools/function.py)
    # only passes a parameter the hook actually names. Without it the coordinator's
    # own calls had no object to flip and the guard was a no-op for them.
    async def _tool_budget_guard_hook(function_name, function, args, agent=None,
                                      team=None, run_context=None):
        # Counted BEFORE any early return: the budget agno enforces covers EVERY tool
        # call, not just the cacheable reads the read-cache hook filters down to.
        # `role` is bound at construction, not discovered from `agent` (2026-08-21).
        # agno stores caller identity on the Function object, which is shared across
        # every agent that lists the same tool -- live-measured as None for every call,
        # so discovery attributed member calls to "Coordinator" and counted a whole team
        # into one bucket against the wrong ceiling. Each agent now gets its own hook
        # instance with its own counter, which is also what "per-role budget" should have
        # meant all along. The getattr fallback stays for callers that don't bind one.
        who = role or getattr(agent, "name", None) or "Coordinator"
        counts[who] = counts.get(who, 0) + 1
        count = counts[who]
        limit = _resolve_tool_call_limit(team_name, who)

        # Low-noise permanent diagnostic (2026-08-21): the first call per role, then
        # every 10th. Added after this guard demonstrably did NOT fire on a live run
        # where one role made 67 calls against a limit of 25 -- while firing correctly
        # in isolation against the same deployed code. Registration, hook ordering,
        # _build_hook_args supplying `agent`, construction order, and
        # _safe_hook_call_async not swallowing exceptions were all verified correct, so
        # the remaining unknown is whether this hook is reached at all and under what
        # role name. Silence in the log is itself the answer. Kept rather than removed
        # once diagnosed: "which roles are spending budget, and how fast" is worth a
        # handful of lines per run on its own.
        if count == 1 or count % 10 == 0:
            print(f"[budget] {who}: call {count}/{limit} ({function_name})")

        result = await function(**args)

        if who in fired:
            return result
        if count < max(limit - _TOOL_BUDGET_RESERVE, 1):
            return result

        fired.add(who)
        _force_text_only(agent, team)
        if activity is not None:
            activity["tool_budget_forced"] = sorted(fired)
        print(f"[team] {who} reached {count}/{limit} tool calls — forcing text-only "
              f"before agno starts refusing calls silently")
        # Appended to the REAL result, never replacing it: this call was within budget
        # and its content is legitimately needed for the answer the agent must now write.
        return (
            f"{result}\n\n---\nTOOL BUDGET REACHED: {who} has used {count} of its "
            f"{limit} tool calls for this run and cannot make any more. Do NOT attempt "
            f"another tool call — it will be refused silently and you will loop. Answer "
            f"NOW using what you already have, and say plainly which parts you could not "
            f"determine."
        )

    return _tool_budget_guard_hook


def _bump_consecutive_stub_and_maybe_force_text_only(
    norm_agent_key: str, agent, consecutive_stub_count: dict[str, int],
) -> None:
    """Track how many stub responses in a row (not merely total) an agent has
    been served, and force it into text-only mode once that streak crosses
    _FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS. Any real, non-stubbed fetch resets
    the streak to 0 (a caller does this directly; this function only ever
    increments) -- an agent that reads three different NEW files, each stubbed
    on a legitimate later re-check, is not "stuck" the way 3 stubs IN A ROW for
    the SAME pattern is."""
    consecutive_stub_count[norm_agent_key] = consecutive_stub_count.get(norm_agent_key, 0) + 1
    if consecutive_stub_count[norm_agent_key] >= _FORCE_TEXT_ONLY_AFTER_CONSECUTIVE_STUBS and agent is not None:
        agent.tool_choice = "none"
        model = getattr(agent, "model", None)
        if model is not None:
            model._tool_choice = "none"


# 2. Context pruning (_collapse_prior_stub_messages): the T12 incident's 29
#    repeats didn't just waste 58s -- each repeat left ANOTHER near-identical
#    ~250-char "Already returned this exact..."/"STOP calling..." message in the
#    agent's own conversation history, so by repeat #10 its recent context was
#    increasingly dominated by that same repeated block -- plausibly reinforcing
#    the exact degenerate-loop tendency this whole section exists to break,
#    rather than just being inert waste. `agno.run.base.RunContext.messages` is
#    documented (confirmed by reading agno/run/base.py directly) as "available
#    in tool hooks... hooks receive a shallow copy... so accidental list
#    mutations (.clear(), .append()) won't corrupt the run. Individual Message
#    objects are shared references -- do not mutate them" -- read literally,
#    that rules out removing entries (the list itself is protected), but the
#    live Message objects inside it ARE the real, shared objects the next model
#    call will actually see. Mutating an EARLIER stub message's own `.content`
#    in place -- collapsing it to a short marker -- is exactly the mutation
#    that docstring's own wording doesn't forbid, and is the only lever this
#    hook actually has for shrinking what's already in history (removing entries
#    outright is not achievable from here). Only ever touches messages this
#    file's own stub functions generated (matched by exact known prefix,
#    _STUB_MESSAGE_PREFIXES) for the SAME (tool_name, args) pair currently being
#    re-stubbed -- never a genuine tool result, and never a different call.
_STUB_MESSAGE_PREFIXES = ("Already returned this exact ", "STOP calling ", "FORCED STOP — ")
_COLLAPSED_STUB_MARKER = (
    "[collapsed: an earlier duplicate-call warning for this exact call -- "
    "superseded, see the latest response instead]"
)


def _collapse_prior_stub_messages(run_context, function_name: str, args_key: str) -> int:
    """Mutate (never remove -- see this section's own comment on why removal
    isn't available here) any PRIOR message in run_context.messages that is
    itself one of this file's own stub responses (_duplicate_read_stub /
    _not_found_retry_stub / _forced_answer_nudge, identified by exact known
    prefix) for the SAME (function_name, args_key) pair currently being
    re-stubbed, collapsing its content to a short marker. Returns the number of
    messages collapsed (0 if run_context/messages is unavailable, or nothing
    qualified) -- purely a diagnostic count, never load-bearing for the caller.

    Matches on tool_args (re-serialized the SAME way args_key is built
    elsewhere in this hook) rather than trusting message ordering/position, so
    an interleaved, unrelated tool call in between two stubs for the same
    (tool, args) pair doesn't break matching."""
    if run_context is None:
        return 0
    messages = getattr(run_context, "messages", None)
    if not messages:
        return 0
    collapsed = 0
    for msg in messages:
        if getattr(msg, "role", None) != "tool":
            continue
        if getattr(msg, "tool_name", None) != function_name:
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, str) or not content.startswith(_STUB_MESSAGE_PREFIXES):
            continue
        if content == _COLLAPSED_STUB_MARKER:
            continue  # already collapsed by an earlier repeat -- don't re-count it
        try:
            msg_args_key = json.dumps(getattr(msg, "tool_args", None) or {}, sort_keys=True)
        except TypeError:
            continue
        if msg_args_key != args_key:
            continue
        msg.content = _COLLAPSED_STUB_MARKER
        collapsed += 1
    return collapsed


# Shared session_state (AGNOHive architecture review, Recommendation on
# share_member_interactions, 2026-08-13): agno's Team AND Agent classes both
# already support session_state/enable_agentic_state (confirmed via direct source
# read of the installed agno 2.5.17 -- team/team.py, agent/agent.py,
# team/_task_tools.py) -- a real, structured, mergeable-across-delegations shared
# dict, unused anywhere in this codebase until now. _record_read and
# _make_delegation_log_hook are its first two consumers: mechanical, automatic
# bookkeeping into session_state["read_log"] / session_state["delegations_made"],
# never relying on a model proactively calling update_session_state for these two
# specific facts (agents can still call it themselves for anything else worth
# recording -- see _COORDINATOR_INSTRUCTIONS' new shared-state section). Written
# once per DISTINCT fact (first real fetch, or each delegation call) -- not once per
# serve -- so the log stays a compact "what's already been established", not a
# duplicate of the per-agent serve-count bookkeeping the cache already does.
_MAX_READ_LOG_ENTRIES = 200


def _record_read(run_context, function_name: str, args: dict, agent_key: str, result_len: int) -> None:
    """Record one real (non-stubbed, first-fetch) read into the run's shared
    session_state. Defensive against run_context/session_state being None -- an
    older agno version, a Team/Agent built without an initial session_state dict,
    or a test double -- since a bookkeeping side effect must never break a real
    tool call. Logs the tool name, args, and who read it, plus the RESULT LENGTH
    only -- never the result content itself, which would just relocate the exact
    context-bloat problem _duplicate_read_stub already exists to stop."""
    if run_context is None or run_context.session_state is None:
        return
    log = run_context.session_state.setdefault("read_log", [])
    log.append({
        "tool": function_name,
        "args": args,
        "read_by": agent_key or "coordinator",
        "result_chars": result_len,
    })
    if len(log) > _MAX_READ_LOG_ENTRIES:
        del log[: len(log) - _MAX_READ_LOG_ENTRIES]


def _make_read_cache_tool_hook(activity: dict | None = None):
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

    Beyond the network-level cache, ALSO tracks how many times each AGENT has been
    served an identical (tool, args) result -- see _MAX_FULL_SERVES_PER_AGENT's
    comment above for why this is a second, distinct problem from the network cache
    (context-bloat spiral, not just redundant hive-mcp round-trips) and
    _duplicate_read_stub for what a repeat call gets instead of the real content past
    that budget. Counted PER AGENT, not globally: the underlying `cache` dict is
    shared (the fetched data is the same regardless of who asks), but the serve-count
    is keyed by agent name too, so the Coder's first read of a file the Researcher
    already read five times still gets the real content -- only a SINGLE agent
    repeating the SAME call gets stubbed.

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

    `agent` is only populated by agno when it's literally a parameter name in this
    hook's signature (confirmed via agno.tools.function.Function._build_hook_args,
    which introspects the hook's signature before deciding what to pass) -- an Agent
    object for a delegated member's own call, or None for the coordinator's own
    direct call (no `team` param needed here to detect that: `getattr(None, "name",
    None) or ""` already resolves to "", the same "coordinator" sentinel
    _stream_event_to_chunk's own agent_name convention already uses). `run_context`
    is populated the same way (confirmed present on both Team and Agent since agno
    2.5.17, `run/base.py`'s RunContext.session_state) -- this is what lets a fresh
    fetch below also mechanically record itself into the run's shared session_state
    (see _record_read), so a SIBLING agent can see "this was already read" as
    structured fact instead of depending on the coordinator instruction ("Don't make
    downstream agents re-read", _COORDINATOR_INSTRUCTIONS) to notice and manually
    forward a citation every time. That instruction stays -- this is its mechanical
    backstop, not a replacement; per-agent serve budgeting below is UNCHANGED and
    deliberately untouched by this addition, since a second agent genuinely lacks the
    content in its own context until it is served at least once, same as before.

    `activity` (default None, same convention as _make_tool_interception_hook's own
    parameter of the same name -- both hooks read/write the ONE dict a caller passes
    to both) gets `activity["max_stub_serve_count"]` bumped to the highest serve
    count seen across the whole run whenever a stub is actually served. This is the
    Tier-2 signal for the liveness-based auto-kill (see DOCS.md "Liveness-Based
    Auto-Kill"): a model still calling the identical read after being told to stop
    3+ times (the escalated stub wording, _STUB_ESCALATION_SERVE) is the sharpest
    "not converging" signal this file can produce, sharper than any timer, and it
    was already being computed here for an unrelated reason -- this just also
    surfaces it to the one place (the heartbeat, and through it the parent process)
    that can act on it.

    `activity["total_stub_serve_count"]` (2026-08-14) is a second, aggregate
    Tier-3 signal alongside the per-key one above: incremented once per stub
    serve regardless of which (agent, tool, args) key it belongs to. Confirmed
    live: a run rotated its reads between 3 already-cached files (6-8 serves
    each, 21 stub serves total) and never crossed the per-key threshold on any
    SINGLE one of them -- max_stub_serve_count peaked at exactly 8, one shy of
    its own >8 trigger -- so the run was just as stuck as any single-file
    repeater, but invisible to that signal alone. Summing across keys catches
    "spreads the non-convergence across several files" the same way the per-key
    max already catches "hammers one file."

    Also short-circuits a DIFFERENT, non-identical-args repeat (2026-08-18):
    get_file_content on a path confirmed not to exist gets retried with a
    steadily incrementing offset, treating "File not found" as if it were a
    truncated read needing pagination. Since each retry's offset differs, the
    main cache/serve-count mechanism above never sees an identical-args repeat
    to catch. A separate counter, keyed on relative_path alone (see
    _not_found_retry_stub's own docstring), stubs starting from the 2nd
    same-path not-found result regardless of offset/limit.
    """
    cache: dict[tuple, object] = {}
    serve_counts: dict[tuple, int] = {}
    # Separate from `cache`/`serve_counts` above -- keyed on relative_path ALONE
    # (no offset/limit, no agent), since a nonexistent file's absence is an
    # objective fact for the whole run, not per-argument or per-agent context.
    # See _not_found_retry_stub's own docstring for the live incident.
    not_found_counts: dict[str, int] = {}
    # 2026-08-19 (T6 follow-up -- Reviewer repeated its own entire first read pass
    # verbatim, 8 calls, within a SINGLE delegate_task_to_member('reviewer', ...)
    # call, landing exactly on config.tool_call_limit's ceiling with no answer
    # produced). _MAX_FULL_SERVES_PER_AGENT=2's own comment already explains why
    # a 2nd full serve is tolerated: "a second delegate_task_to_member call to the
    # same role may start with fresh context and legitimately need it again" --
    # but that reasoning is about a SEPARATE, later delegation, not a repeat
    # inside the SAME one. The live incident had exactly one delegation to
    # Reviewer; every one of its 25 tool calls happened inside it, so the
    # existing agent-only key never distinguished "fresh delegation, deserves a
    # new budget" from "same delegation, asking again for no reason" -- both
    # looked identical to a plain (agent_key, tool, args) key. Fixed by keying
    # the serve-count budget on the delegation INSTANCE too, not just the agent:
    # bumped every time delegate_task_to_member(s) targets a member, read here
    # (before the cacheable-tool check) so the bump happens exactly once per
    # delegation call, before any of the delegate's own reads occur inside it.
    # Combined with lowering _MAX_FULL_SERVES_PER_AGENT to 1 (see its own
    # comment), this closes the loophole precisely: a repeat WITHIN one
    # delegation is now stubbed on the 2nd ask, while a genuinely fresh, LATER,
    # separate delegation to the same role still gets its own full budget, since
    # it falls into a new generation bucket. "__broadcast__" is a synthetic key
    # for delegate_task_to_members (plural) -- it targets the whole team with no
    # single member_id to bump individually (see _make_duplicate_delegation_gate_hook's
    # own docstring: the plural tool is compared on task text alone, "no
    # member_id -- it goes to the whole team"), so a broadcast conservatively
    # bumps every agent's effective generation via the max() below rather than
    # trying to enumerate the roster here.
    delegation_generation: dict[str, int] = {}
    # See _bump_consecutive_stub_and_maybe_force_text_only's own section-level
    # comment (above _duplicate_read_stub's sibling functions) for both of these:
    # agent_objects lets a later stub-serve reach back into the actual live Agent
    # object (captured the first time we see it, since delegate_task_to_member's
    # own args only carry the member_id STRING, never the object) to mutate its
    # tool_choice; consecutive_stub_count is the streak that decides when to.
    agent_objects: dict[str, object] = {}
    consecutive_stub_count: dict[str, int] = {}
    # Closure-local record of every REAL (fresh, non-stubbed) read this run, at any
    # delegation depth (2026-08-21). This hook instance is shared across the coordinator
    # and every member, so its closure sees all of them -- which session_state does not.
    #
    # _record_read already writes the same facts into session_state["read_log"], and
    # _count_read_calls already reads that specifically to catch delegated reads
    # (2026-08-18). It does not work once ALL reads are delegated: agno hands a member
    # `copy(run_context.session_state)` and merges it back afterwards
    # (team/_default_tools.py:561,532), and a live trace showed Researcher reading
    # models.py (call 10/50) while the guard still reported "ZERO read calls".
    #
    # Same escalation _make_delegation_log_hook already made for the same reason -- its
    # docstring: "The closure sidesteps agno's session_state threading entirely, which is
    # the same reason the duplicate-delegation gate abandoned session_state for its own
    # local log." session_state writes are LEFT IN PLACE and unchanged; this is a second,
    # independent source, so nothing that reads the old one regresses.
    read_state: dict = {"reads": []}

    async def _read_cache_tool_hook(function_name, function, args, agent=None, run_context=None):
        if function_name in _DELEGATION_TOOL_NAMES:
            if function_name == "delegate_task_to_member":
                target = _member_id(str((args or {}).get("member_id", "")).strip())
                if target:
                    delegation_generation[target] = delegation_generation.get(target, 0) + 1
                    # Fresh delegation -- clean slate, same principle the
                    # generation-scoped serve budget above already applies.
                    consecutive_stub_count[target] = 0
                    member_obj = agent_objects.get(target)
                    if member_obj is not None:
                        member_obj.tool_choice = None
                        member_model = getattr(member_obj, "model", None)
                        if member_model is not None:
                            member_model._tool_choice = None
            else:  # delegate_task_to_members -- broadcasts to the whole team, no single target
                delegation_generation["__broadcast__"] = delegation_generation.get("__broadcast__", 0) + 1
                consecutive_stub_count.clear()
                for member_obj in agent_objects.values():
                    member_obj.tool_choice = None
                    member_model = getattr(member_obj, "model", None)
                    if member_model is not None:
                        member_model._tool_choice = None
            return await function(**args)

        if function_name == "verify_claims":
            try:
                return await asyncio.wait_for(function(**args), timeout=_MODEL_VERIFY_CLAIMS_TIMEOUT)
            except Exception as exc:
                print(f"[team] model-voluntary verify_claims call did not complete in time: {exc}")
                return _model_verify_claims_unavailable_result()

        if function_name not in _CACHEABLE_READ_TOOLS:
            return await function(**args)
        try:
            args_key = json.dumps(args or {}, sort_keys=True)
        except TypeError:
            return await function(**args)  # non-JSON-serializable args -- skip caching, call through

        agent_key = getattr(agent, "name", None) or ""
        norm_agent_key = _member_id(agent_key) if agent_key else ""
        if agent is not None and norm_agent_key:
            agent_objects[norm_agent_key] = agent
        generation = max(
            delegation_generation.get(norm_agent_key, 0),
            delegation_generation.get("__broadcast__", 0),
        )
        cache_key = (function_name, args_key)
        serve_key = (agent_key, generation, function_name, args_key)

        # Checked BEFORE calling the real function (unlike the identical-args
        # cache below, which only avoids a re-call once cache_key repeats
        # exactly) -- a retry with a DIFFERENT offset/limit on an
        # already-confirmed-missing path is never a fresh cache_key, so without
        # this early check the real hive-mcp round-trip would still happen on
        # every retry even though the eventual result is thrown away. See
        # _not_found_retry_stub's own docstring for the live incident this closes.
        if (
            function_name == "get_file_content"
            and isinstance(args, dict) and args.get("relative_path")
            and not_found_counts.get(args["relative_path"], 0) >= 1
        ):
            relative_path = args["relative_path"]
            not_found_counts[relative_path] += 1
            count = not_found_counts[relative_path]
            if activity is not None:
                activity["max_stub_serve_count"] = max(activity.get("max_stub_serve_count", 0), count)
                activity["total_stub_serve_count"] = activity.get("total_stub_serve_count", 0) + 1
            _collapse_prior_stub_messages(run_context, function_name, args_key)
            _bump_consecutive_stub_and_maybe_force_text_only(norm_agent_key, agent, consecutive_stub_count)
            return _not_found_retry_stub(relative_path, count)

        is_fresh_fetch = cache_key not in cache
        if is_fresh_fetch:
            result = await function(**args)
            cache[cache_key] = result
        else:
            result = cache[cache_key]

        if is_fresh_fetch:
            _record_read(run_context, function_name, args, agent_key, len(str(result)))
            # Second, independent source -- survives delegation, unlike session_state.
            read_state["reads"].append({"tool": function_name, "read_by": agent_key or "coordinator"})

        if (
            function_name == "get_file_content"
            and isinstance(result, str) and result.startswith("File not found:")
            and isinstance(args, dict) and args.get("relative_path")
        ):
            relative_path = args["relative_path"]
            not_found_counts[relative_path] = not_found_counts.get(relative_path, 0) + 1

        serve_counts[serve_key] = serve_counts.get(serve_key, 0) + 1
        count = serve_counts[serve_key]
        if count > _MAX_FULL_SERVES_PER_AGENT:
            total = None
            if activity is not None:
                activity["max_stub_serve_count"] = max(activity.get("max_stub_serve_count", 0), count)
                # total_stub_serve_count (2026-08-14): the aggregate Tier-3 signal,
                # incremented once per stub serve regardless of WHICH key it
                # belongs to -- see this hook's own docstring section on it for
                # the live incident this closes (max_stub_serve_count alone missed
                # a model rotating its non-convergence across several different
                # already-stubbed files, each individually staying under budget).
                activity["total_stub_serve_count"] = activity.get("total_stub_serve_count", 0) + 1
                total = activity["total_stub_serve_count"]
            _collapse_prior_stub_messages(run_context, function_name, args_key)
            _bump_consecutive_stub_and_maybe_force_text_only(norm_agent_key, agent, consecutive_stub_count)
            if total is not None and total >= _FORCED_ANSWER_AGGREGATE_THRESHOLD:
                return _forced_answer_nudge(agent_key, total)
            return _duplicate_read_stub(function_name, args, agent_key, count, len(str(result)))
        consecutive_stub_count[norm_agent_key] = 0
        return result

    # Exposed as an attribute rather than a second return value, so every existing
    # caller and test keeps working untouched -- same convention _make_delegation_log_hook
    # uses for its own closure-local counter.
    _read_cache_tool_hook.state = read_state
    return _read_cache_tool_hook


_DELEGATION_TOOL_NAMES = {"delegate_task_to_member", "delegate_task_to_members"}
_MAX_LOGGED_TASK_CHARS = 300
_MAX_DELEGATION_LOG_ENTRIES = 200

def _member_id(display_name: str) -> str:
    """The real, canonical value a delegate_task_to_member(member_id=...) call must
    use for a given agent's display name -- 2026-08-15, root-caused live during
    planning validation. Thin wrapper over agno's OWN `url_safe_string` (confirmed
    by reading agno/utils/team.py's `get_member_id`, the exact function agno's
    `_find_member_by_id` compares against with a plain `==`, no normalization on
    its side): spaces become dashes, camelCase becomes kebab-case, then everything
    is lowercased. This means for any MULTI-WORD agent name, the correct
    member_id is NOT the display name lowercased -- e.g. "ContextRouter" ->
    "context-router", "BacklogResearcher" -> "backlog-researcher" -- a dash gets
    inserted, which a naive `.lower()` never produces.

    Live-confirmed as a real bug, not theoretical: a planning-team coordinator run
    tried `delegate_task_to_member(member_id='ContextRouter', ...)` (the exact
    display name, matching this file's own _COORDINATOR_INSTRUCTIONS examples and
    _team_roster_preamble's old wording) -- agno's exact-match lookup failed, and
    the coordinator's own narration concluded "a fundamental failure in the team
    member resolution system" rather than recognizing its own casing/format
    mistake, then abandoned delegation entirely for the rest of the run. Single-
    word names (Researcher, Planner, Coder, Executor, Reviewer) were never
    affected -- url_safe_string only lowercases those, with no dash insertion --
    which is why this went unnoticed until a multi-word name (ContextRouter) hit it.
    """
    return url_safe_string(display_name)


# Same trigger-phrase set as teams/engineering.yaml's own DECOMPOSE-FIRST rule text
# ("its own wording implies more than one discrete, independently-checkable claim")
# -- kept in sync deliberately, not derived mechanically from the YAML, since the
# YAML's copy is prose read by the model and this one is a hard mechanical gate; the
# two only need to agree in SPIRIT, not be the same object. See _is_multi_part_task's
# own docstring for the live incident this exists to catch.
_MULTI_PART_TASK_RE = re.compile(
    r"\bcompare\b[\s\S]{0,80}\bagainst\b|"
    r"\bwhat'?s\s+(?:already\s+)?covered\b|\bcovered\s+vs\.?\s+(?:what'?s\s+)?(?:still\s+)?missing\b|"
    r"\baudit\s+all\b|\bwhich\s+of\s+these\b|\bgap\s+analysis\b",
    re.IGNORECASE,
)


def _is_multi_part_task(task: str | None) -> bool:
    """True if `task`'s own wording implies more than one discrete, independently-
    checkable claim ("compare X against Y", "what's covered vs missing", "audit all
    N of", "which of these are done") -- the SAME classification teams/engineering.yaml's
    DECOMPOSE-FIRST rule asks Researcher to apply to itself, mirrored here as a
    mechanical pre-check on the coordinator's OWN delegation choices.

    Deliberately narrow (a handful of specific phrase patterns, not a broad heuristic
    like sentence length or word count) -- false positives here block a legitimate
    narrow delegation the coordinator was right to make; false negatives just mean
    Phase 1's prose instruction is the only thing covering that task, same as before
    this existed. Narrow-but-precise is the safer failure direction for a mechanical
    gate sitting in front of every coordinator delegation.
    """
    return bool(_MULTI_PART_TASK_RE.search(task or ""))


def _make_decompose_first_gate_hook(task: str | None, researcher_member_id: str = "Researcher"):
    """Phase 2 (2026-08-14) of the "AgnoHive - Engineering Team 2.0 Update" plan --
    a mechanical backstop for Phase 1's prose-only coordinator instruction
    ("delegate multi-part tasks to Researcher WHOLE"), confirmed live NOT to be
    reliably followed on its own: a fresh-session re-run of the exact multi-part
    prompt that motivated the whole plan still saw the coordinator open with a
    narrow delegate_task_to_member('ContextRouter', 'search_files for "party"...')
    call instead -- the same piecemeal pattern Phase 1 was meant to replace.
    ContextRouter then looped 14 identical calls before the Tier-2 liveness
    auto-kill caught it, and Researcher's own DECOMPOSE-FIRST rule never got a
    chance to run, because the coordinator never delegated to it in the first
    place. Phase 1's prose instruction was measured insufficient, not assumed --
    see the Hive Troubleshooting / Engineering Team 2.0 Notion pages for the full
    incident.

    Intercepts ONLY the coordinator's OWN delegate_task_to_member(s) calls --
    the only tool that ever carries these names in this codebase's
    mode="coordinate"/"route" usage (members do not delegate further, confirmed
    by _make_delegation_log_hook's own docstring). For a task classified
    multi-part (_is_multi_part_task), blocks ONLY the very FIRST delegation call
    of the run if it targets a member other than Researcher -- every delegation
    after that first one is left alone, whether the first was blocked or not.
    This is a one-time nudge onto the right path, not a standing restriction: a
    legitimate narrow follow-up delegation AFTER Researcher has already produced
    its checklist must never be blocked, or this would reintroduce the OVER-
    delegation-prevention problem the coordinator's own "stop delegating once
    answered" instruction already exists to avoid (see _COORDINATOR_INSTRUCTIONS).

    A blocked call returns a redirect message instead of executing --
    delegate_task_to_member(s) is not idempotent (see _make_delegation_log_hook's
    own docstring), so unlike a duplicate READ this never re-serves cached data;
    it simply never calls the real delegation function at all for that one call,
    the same intercept-and-redirect shape _duplicate_read_stub already uses for a
    different problem.

    `delegate_task_to_members` (plural, agno's broadcast-mode tool) is
    deliberately NEVER blocked -- this codebase's only reachable modes are
    coordinate/route (see DOCS.md's `mode="collaborate"` finding: an unrecognized
    mode string silently falls through to coordinate's own default, so plural
    broadcast delegation is effectively unused in practice) -- but it still
    counts as "the first delegation call" for gating purposes, so a genuinely
    multi-part task that opens with a (rare) broadcast call doesn't leave the
    gate armed for a LATER narrow call that should have been checked.

    `task=None` (the default) makes this hook a permanent no-op -- any caller
    that doesn't pass the original task string gets the exact pre-2026-08-14
    pass-through behavior, byte-for-byte.

    `researcher_member_id` (default "Researcher", matching every pre-2026-08-15
    caller byte-for-byte) is the actual DISPLAY name of this TEAM's researcher-
    shaped member -- added for the 2026-08-15 gate-scope extension to
    parallel-review and sprint-master (see tests/test_gate_team_scoping.py). Not
    every team names this role "Researcher": `teams/sprint-master.yaml`'s
    equivalent agent is `BacklogResearcher`.

    Comparison normalizes BOTH sides through `_member_id()` (agno's own
    url_safe_string transform), not a bare `.lower()` -- 2026-08-15 fix, found
    live: for a multi-word display name, agno's REAL delegate_task_to_member
    lookup key inserts a dash at each camelCase boundary before lowercasing
    ("BacklogResearcher" -> "backlog-researcher"), which plain `.lower()` never
    produces ("backlogresearcher", no dash) -- the old comparison could never
    match a CORRECTLY-formatted delegation to a multi-word member, only single-
    word ones (Researcher, Planner) were ever safe. The REDIRECTED message's own
    code example now shows the real member_id form too, not the display name --
    telling the coordinator to literally type the wrong string was the actual
    root cause of a live incident, not just a comparison bug.
    """
    state = {"decided": False}
    target_id = _member_id(researcher_member_id)

    async def _decompose_first_gate_hook(function_name, function, args, run_context=None):
        if function_name not in _DELEGATION_TOOL_NAMES:
            return await function(**args)
        if state["decided"] or not _is_multi_part_task(task):
            return await function(**args)
        state["decided"] = True

        if function_name != "delegate_task_to_member":
            return await function(**args)

        member_id = _member_id(str((args or {}).get("member_id", "")).strip())
        if member_id == target_id:
            return await function(**args)

        target = (args or {}).get("member_id", "?")
        return (
            f"REDIRECTED: this task is multi-part — its own wording implies more "
            f"than one discrete, independently-checkable claim — and must be "
            f"delegated to {researcher_member_id} WHOLE first, not piecemeal to {target!r}. "
            f"{researcher_member_id} now also decomposes tasks internally (its own "
            f"DECOMPOSE-FIRST rule): call delegate_task_to_member({target_id!r}, "
            f"<the full original task, unabridged>) instead. This delegation to "
            f"{target!r} was NOT executed."
        )

    return _decompose_first_gate_hook


_BROWSE_TOOL_NAMES = {"list_directory_tree", "find_files", "get_file_content"}
_SEARCH_TOOL_NAMES = {"search_files", "lightrag_query"}

# 2026-08-15 gate-scope extension -- resolution of the "AgnoHive - Engineering
# Team 2.0 Update" plan's 4th open question. The two mechanical gates above were
# ALREADY structurally unconditional (keyed only on _is_multi_part_task(task) and
# a hardcoded "Researcher" agent-name match, with no team-identity awareness at
# all) -- which meant they were silently already active for `planning` too (its
# own agent happens to be named "Researcher"), contradicting this exact
# decision's "planning excluded" answer, and would have MISFIRED on
# `sprint-master` (whose researcher-shaped agent is actually named
# BacklogResearcher, confirmed by reading teams/sprint-master.yaml) rather than
# simply staying inactive. `_build_team`'s `team_name` kwarg (default None) uses
# these two maps to compute, per team, whether the gates apply at all and which
# agent name plays the Researcher role -- see tests/test_gate_team_scoping.py.
_GATE_ENABLED_TEAMS = {"engineering", "parallel-review", "sprint-master"}
# search-before-browse-only extension (2026-08-15, follow-up to the gate-scope
# extension above): planning's own Researcher was found to be excluded from BOTH
# gates alongside its Planner -- but the two gates rest on genuinely different
# premises. Decompose-first's redirect text says "Researcher now also decomposes
# tasks internally (merged with the former Planner role)" -- true for engineering/
# parallel-review/sprint-master, FALSE for planning, whose Researcher and Planner
# are two distinct, separately-shaped agents (Planner has its own DISCUSSION/
# ROADMAP vs IMPLEMENTATION dual-mode instructions) -- forcing that gate on
# planning would inject actively wrong advice. Search-before-browse's premise
# ("search before you browse, to find the actually-owning file directly instead of
# guessing a service/directory from a domain-name association") has nothing to do
# with the Researcher/Planner relationship -- it holds for ANY Researcher grounding
# ANY claim. So this set is intentionally a superset of _GATE_ENABLED_TEAMS, not a
# second independent allowlist.
_SEARCH_GATE_ENABLED_TEAMS = _GATE_ENABLED_TEAMS | {"planning"}
_RESEARCHER_AGENT_NAME_BY_TEAM = {
    "sprint-master": "BacklogResearcher",
}
_DEFAULT_RESEARCHER_AGENT_NAME = "Researcher"


def _make_search_before_browse_gate_hook(task: str | None, researcher_agent_name: str = "Researcher"):
    """A mechanical backstop for teams/engineering.yaml's Researcher DECOMPOSE-FIRST
    Step 3a ("search before you browse"), confirmed live NOT to be reliably followed
    on its own -- THREE separate live tests (2026-08-14, 2026-08-15 x2, the last one
    even after Step 3a was shortened, moved earlier, and reworded "ALWAYS, NO
    EXCEPTIONS") all saw Researcher open a multi-part checklist item with
    find_files/get_file_content browsing by directory or service name, never once
    calling search_files or lightrag_query. Same escalation this codebase already
    took once for delegation (Phase 1's prose "delegate whole to Researcher" ->
    Phase 2's _make_decompose_first_gate_hook, both above): prose instruction proven
    insufficient by direct measurement -> mechanical enforcement.

    Scoped to Researcher only (via the `agent` kwarg's own `.name`) -- ContextRouter's
    entire job is fast retrieval via these same tool names, and Coder/Reviewer
    legitimately need get_file_content before editing/reviewing; blocking either
    would be a real regression, not a fix. Scoped to _is_multi_part_task(task) the
    same as the decompose-first gate above, for the same reason: a bounded single-file
    task ("is section X present in file Y") already names its target and correctly
    skips DECOMPOSE-FIRST's checklist machinery entirely (measured live: 42s, no
    decomposition needed) -- forcing a search first there would only slow down the
    one shape that already works.

    Unlike the decompose-first gate's one-time nudge, this gate stays SHUT (blocks
    every qualifying browse call, not just the first) until Researcher's OWN first
    search_files/lightrag_query call this run -- there is no analogous
    over-restriction risk here the way repeated delegation-blocking would reintroduce
    the over-delegation problem (see the decompose-first gate's own docstring):
    Researcher can unblock itself with exactly one search call, after which every
    subsequent browse call (reading the search's own top hits, as Step 3a says to)
    passes through freely for the rest of the run.

    `task=None` (the default) makes this hook a permanent no-op, same convention as
    `_make_decompose_first_gate_hook`.

    `researcher_agent_name` (default "Researcher", matching every pre-2026-08-15
    caller byte-for-byte) is the actual name of this team's researcher-shaped
    agent -- added for the 2026-08-15 gate-scope extension to parallel-review and
    sprint-master (see `_make_decompose_first_gate_hook`'s matching parameter and
    tests/test_gate_team_scoping.py). `teams/sprint-master.yaml`'s equivalent
    agent is named `BacklogResearcher`, not `Researcher`.
    """
    state = {"searched": False}

    async def _search_before_browse_gate_hook(function_name, function, args, agent=None, run_context=None):
        agent_key = getattr(agent, "name", None) or ""
        if agent_key != researcher_agent_name:
            return await function(**args)
        if function_name in _SEARCH_TOOL_NAMES:
            state["searched"] = True
            return await function(**args)
        if function_name not in _BROWSE_TOOL_NAMES:
            return await function(**args)
        if state["searched"] or not _is_multi_part_task(task):
            return await function(**args)

        return (
            f"REDIRECTED: {function_name} was blocked — for a multi-part task, "
            f"Researcher must run a content search FIRST (search_files(<the checklist "
            f"item's own key domain term>, '**/*') and/or lightrag_query(<key term>)) "
            f"before any directory/file browsing. This is what finds the actual owning "
            f"file directly instead of guessing a service/directory from a domain-name "
            f"association. Call search_files or lightrag_query now — every browse call "
            f"after your first search this run will go through normally. This "
            f"{function_name} call was NOT executed."
        )

    return _search_before_browse_gate_hook


def _normalize_delegation_task(task_text) -> str:
    """Whitespace/case-folded form of a delegation's task text, used ONLY for
    exact-duplicate comparison in _make_duplicate_delegation_gate_hook -- not a
    general-purpose normalizer. Collapses internal whitespace runs and lowercases,
    so two calls differing only in incidental spacing/case still compare equal;
    anything else (a reworded ask, a narrower/wider scope) compares unequal, which
    is the intended narrow-not-broad behavior (see that function's own docstring).

    Also strips stray ellipses (2026-08-16, T2j live re-verification of the
    closure-based rewrite): a real repeat delegation differed from its own prior
    call by nothing but an inserted "..." between two words ("backend and list"
    vs "backend... and list") -- confirmed via direct closure instrumentation
    that the underlying persistence mechanism was correct (hook_id/log_id
    identical across calls, log_len incremented 0 -> 1 exactly as designed) and
    this single stray token was the ENTIRE reason the exact-match check missed
    a genuinely trivial repeat. Unlike fuzzy similarity (SequenceMatcher/Jaccard,
    both empirically ruled out for this gate -- see the gate's own docstring),
    stripping a specific, narrow punctuation artifact is still a deterministic,
    zero-judgment normalization step, not a similarity threshold -- it does not
    reintroduce the false-positive risk that ruled out fuzzy matching."""
    stripped = re.sub(r"\.{2,}|…", " ", str(task_text or ""))
    return " ".join(stripped.split()).lower()


# T1-T13 gap #2 follow-up (2026-08-16): the exact-match check above and the
# fuzzy-similarity attempts that preceded it (both SequenceMatcher and Jaccard,
# both ruled out -- see _normalize_delegation_task's own docstring) sit at
# opposite ends of the same failed axis: comparing raw prose either misses a
# genuinely-reworded duplicate or flags an unrelated-but-similarly-phrased
# follow-up ("what fields does Party have" vs "what fields does
# PartyRegistration have" -- different TARGET, must never match). Rather than
# tuning a third string-similarity threshold, this asks the MODEL to extract a
# small structured {component, action, target} tuple up front -- semantic
# classification is what LLMs are comparatively good at, unlike approximating
# it after the fact from surface text. Target is meant to be copied verbatim
# from something the task already names (a path, a module, an entity) --
# comparing normalized Target values directly sidesteps the Party vs
# PartyRegistration trap entirely, since those are two different targets by
# construction, not two different phrasings of the same one.
_DELEGATION_AUDIT_ACTIONS = frozenset({
    "read", "search", "analyze", "implement", "verify", "plan",
})
# The audit tuple is embedded as a required literal PREFIX of the delegation's
# own `task` argument, not emitted as separate free-standing text -- every tool
# hook in this file (this one included) only ever sees a tool call's `args`,
# never the coordinator's surrounding streamed text, so there is no other
# place for a mechanical gate to actually read it from.
_DELEGATION_AUDIT_RE = re.compile(
    r"^\s*<delegation_audit>\s*component\s*=\s*(?P<component>[^;]*?)\s*;\s*"
    r"action\s*=\s*(?P<action>[^;]*?)\s*;\s*target\s*=\s*(?P<target>.*?)\s*"
    r"</delegation_audit>\s*",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_delegation_target(target) -> str:
    """Path-shaped normalization for an audit tuple's Target field -- lowercased,
    backslashes unified to forward slashes, surrounding whitespace stripped --
    so 'API/inventory-service/parties_api.py' and the same path written with
    backslashes still compare equal. Deliberately simple: Target is meant to be
    an exact path/module/entity copied from the task, not free prose, so any
    heavier normalization would just reintroduce the fuzzy-matching problem
    this whole mechanism exists to avoid."""
    return str(target or "").strip().lower().replace("\\", "/")


def _parse_delegation_audit(task_text) -> dict | None:
    """Extracts the {component, action, target} tuple a re-delegation to an
    already-delegated-to member/broadcast is required to open with (see
    _make_duplicate_delegation_gate_hook) -- returns None if the prefix is
    missing or malformed, never raises. `action` is lowercased for comparison
    but NOT restricted to _DELEGATION_AUDIT_ACTIONS here -- an out-of-vocabulary
    action is still usable for exact-tuple comparison, matching the "narrow, err
    toward allowing" philosophy the exact-text check above already follows: a
    strict vocabulary would risk redirecting a legitimate call over a wording
    technicality, the same false-positive shape that ruled out fuzzy matching."""
    if not task_text:
        return None
    m = _DELEGATION_AUDIT_RE.match(str(task_text))
    if not m:
        return None
    return {
        "component": m.group("component").strip(),
        "action": m.group("action").strip().lower(),
        "target": _normalize_delegation_target(m.group("target")),
    }


def _make_duplicate_delegation_gate_hook():
    """Mechanical backstop for _COORDINATOR_INSTRUCTIONS' own prose-only rule
    ("Before delegate_task_to_member(s): check whether an equivalent delegation is
    already listed... use that result instead of delegating the same or a near-
    identical task again") -- confirmed live NOT reliably followed, the same class
    of failure this file has already hit and fixed twice
    (_make_decompose_first_gate_hook, _make_search_before_browse_gate_hook): prose
    instruction proven insufficient by direct measurement -> mechanical enforcement.

    Live incident (2026-08-15, T2c parallel-review groundedness retest): the
    coordinator called delegate_task_to_members with the EXACT SAME task text
    twice -- once at run start, again ~2.5 minutes later -- after round 1 had
    already produced two independently-correct, fully-cited member answers.
    Round 2 introduced a conflicting WRONG answer from one member (a failed path
    guess concluded "models.py not found") alongside a second correct one, and
    the coordinator's synthesis sided with the wrong, uncited answer over three
    independently-cited correct ones (see the new "Resolving conflicting member
    reports" section of _COORDINATOR_INSTRUCTIONS, added the same day as this
    gate, for the prose-side half of this fix). Blocking the duplicate broadcast
    at the gate prevents this incident SHAPE from recurring at the source: if
    round 2 never fires, there is nothing for a flawed synthesis step to get
    confused by. Two identical incidents of this same shape were also observed
    with delegate_task_to_member (singular) during the same test session -- a
    coordinator re-delegating byte-identical read/extract tasks to the same
    member 3-5 times in a row, each a genuinely wasted ~10-30s round-trip.

    Maintains its OWN closure-local log -- a plain list captured once when this
    function is called (the same pattern _make_decompose_first_gate_hook's
    `state = {"decided": False}` and _make_search_before_browse_gate_hook's
    `state = {"searched": False}` already use) -- rather than reading
    session_state["delegations_made"] the way the original 2026-08-15 version
    did. That version was live-verified BROKEN, not just insufficient, on
    2026-08-16: a byte-identical delegate_task_to_members call, ~70s after the
    first, was never redirected. Root-caused with direct instrumentation
    (temporary diagnostic prints of id(run_context)/id(run_context.session_state)
    on every call, three live re-runs): agno constructs a genuinely NEW
    RunContext -- and therefore a new, empty session_state dict -- for EACH
    separate delegate_task_to_members call within the SAME overall run. Two
    consecutive calls in the same run showed completely different
    run_context_id AND session_state_id values, with log_len=0 both times --
    session_state is simply never threaded across delegate_task_to_members
    calls the way _build_team's own "Two things are tracked for you
    automatically" instruction (and the original version of this gate) assumed.
    This also means the COORDINATOR's own prose-visible view of
    session_state["delegations_made"] (rendered into its context via
    add_session_state_to_context) is equally unreliable for this specific tool
    -- a closure-local log sidesteps the problem entirely rather than depending
    on agno's session_state threading being fixed.
    _make_delegation_log_hook's OWN session_state write is left unchanged --
    it may still be meaningful for delegate_task_to_member (singular, not
    confirmed broken the same way) and for whatever the coordinator's own
    context rendering is worth; this gate simply no longer depends on it.
    This hook now records its own log entry directly, immediately after a
    real (non-redirected) delegation call returns -- there is no separate
    "run before/after delegation_log_hook" ordering concern anymore, since
    this gate is fully self-contained.

    delegate_task_to_member (singular): blocks ONLY an exact (post-normalization)
    repeat of BOTH member_id (compared via _member_id(), matching agno's own real
    lookup key) AND task text, to the SAME member. A follow-up delegation with
    different wording, a narrower/wider scope, or to a DIFFERENT member is never
    blocked -- same narrow-not-broad philosophy _is_multi_part_task's own
    docstring argues for: false positives here block a legitimate follow-up
    delegation the coordinator was right to make.

    delegate_task_to_members (plural, broadcast): compared against prior
    delegate_task_to_members calls only, keyed on normalized task text alone (no
    member_id -- it goes to the whole team) -- this is the exact shape of the
    live incident above.

    A blocked call returns a redirect message instead of executing -- the same
    intercept-and-redirect shape _make_decompose_first_gate_hook already uses.
    Delegation is not idempotent IN GENERAL (a second call to the same member can
    legitimately want fresh context after new information emerged -- see
    _make_delegation_log_hook's own docstring), but an EXACT repeat of a call
    already logged this run is never that case by definition: the request text
    didn't change, so there is no new information for a fresh call to act on.

    T1-T13 gap #2 follow-up (2026-08-16): the exact-text check above still
    misses a GENUINELY-reworded duplicate by design (see
    _normalize_delegation_task's own docstring on why fuzzy string similarity
    was ruled out). This adds a second, independent tier for that case: on the
    SECOND-OR-LATER delegation to a given member (singular) or the
    second-or-later broadcast (plural) — never the first, since there is
    nothing yet to compare a first call against — the `task` argument is
    required to open with a `<delegation_audit>component=...; action=...;
    target=...</delegation_audit>` prefix (see _parse_delegation_audit). A
    missing prefix on a qualifying call is redirected with the required
    format, same intercept-and-redirect shape as every other tier. A present
    prefix is compared against every prior qualifying entry's own parsed
    audit — same normalized `target` AND same `action` is treated as a
    duplicate regardless of how differently the surrounding prose reads,
    exactly the case the T2c incident above slipped past. `component` is
    informational only, never part of the match key — matching on Target+
    Action alone is what keeps 'what fields does Party have' from colliding
    with 'what fields does PartyRegistration have' (different targets), the
    exact false-positive shape that ruled out fuzzy text matching in the
    first place.
    """
    log: list[dict] = []

    async def _duplicate_delegation_gate_hook(function_name, function, args, run_context=None):
        if function_name not in _DELEGATION_TOOL_NAMES:
            return await function(**args)

        raw_task = (args or {}).get("task")
        task_text = _normalize_delegation_task(raw_task)
        if not task_text:
            return await function(**args)

        if function_name == "delegate_task_to_member":
            member_id = _member_id(str((args or {}).get("member_id", "")).strip())
            prior_entries = [
                entry for entry in log
                if entry.get("tool") == "delegate_task_to_member"
                and _member_id(str((entry.get("args") or {}).get("member_id", "")).strip()) == member_id
            ]
            for entry in prior_entries:
                if _normalize_delegation_task((entry.get("args") or {}).get("task")) == task_text:
                    return (
                        f"REDIRECTED: this exact task was already delegated to {member_id!r} "
                        f"earlier this run — use that result instead of delegating it again. "
                        f"This delegate_task_to_member call was NOT executed."
                    )
            if prior_entries:
                audit = _parse_delegation_audit(raw_task)
                if audit is None:
                    return (
                        f"REDIRECTED: {member_id!r} has already been delegated to earlier this "
                        f"run — a re-delegation must open the task with an audit tag: "
                        f"'<delegation_audit>component=<short label>; action=<one of: "
                        f"{', '.join(sorted(_DELEGATION_AUDIT_ACTIONS))}>; target=<the exact file "
                        f"path/module/entity this call is about></delegation_audit>' followed by "
                        f"the real task text. This delegate_task_to_member call was NOT executed — "
                        f"add the audit tag and retry."
                    )
                for entry in prior_entries:
                    prior_audit = entry.get("audit")
                    if prior_audit and prior_audit["target"] == audit["target"] and prior_audit["action"] == audit["action"]:
                        return (
                            f"REDIRECTED: a delegation to {member_id!r} with the same target "
                            f"({audit['target']!r}) and action ({audit['action']!r}) was already "
                            f"made earlier this run, just worded differently — use that result "
                            f"instead of delegating it again. If this is genuinely a different "
                            f"target or action, correct the audit tag to reflect that. This "
                            f"delegate_task_to_member call was NOT executed."
                        )
        else:
            prior_entries = [entry for entry in log if entry.get("tool") == "delegate_task_to_members"]
            for entry in prior_entries:
                if _normalize_delegation_task((entry.get("args") or {}).get("task")) == task_text:
                    return (
                        "REDIRECTED: this exact task was already broadcast to the whole team "
                        "earlier this run — use those results instead of broadcasting it again. "
                        "This delegate_task_to_members call was NOT executed."
                    )
            if prior_entries:
                audit = _parse_delegation_audit(raw_task)
                if audit is None:
                    return (
                        "REDIRECTED: a broadcast has already been made earlier this run — a "
                        "re-broadcast must open the task with an audit tag: "
                        f"'<delegation_audit>component=<short label>; action=<one of: "
                        f"{', '.join(sorted(_DELEGATION_AUDIT_ACTIONS))}>; target=<the exact file "
                        "path/module/entity this call is about></delegation_audit>' followed by "
                        "the real task text. This delegate_task_to_members call was NOT executed "
                        "— add the audit tag and retry."
                    )
                for entry in prior_entries:
                    prior_audit = entry.get("audit")
                    if prior_audit and prior_audit["target"] == audit["target"] and prior_audit["action"] == audit["action"]:
                        return (
                            f"REDIRECTED: a broadcast with the same target ({audit['target']!r}) "
                            f"and action ({audit['action']!r}) was already made earlier this run, "
                            f"just worded differently — use those results instead of broadcasting "
                            f"it again. If this is genuinely a different target or action, correct "
                            f"the audit tag to reflect that. This delegate_task_to_members call was "
                            f"NOT executed."
                        )

        result = await function(**args)
        log.append({
            "tool": function_name,
            "args": dict(args or {}),
            "audit": _parse_delegation_audit(raw_task),
        })
        return result

    return _duplicate_delegation_gate_hook


def _make_delegation_log_hook():
    """Mechanically logs every delegation (member id + task text, never the
    member's result) into the run's shared session_state["delegations_made"] --
    the automatic counterpart to _record_read, for the OTHER half of the
    cross-agent-duplication problem: the coordinator re-delegating a task it
    already delegated, not a member re-reading a file a sibling already fetched.
    Only the coordinator has delegate_task_to_member(s) on its own tool list in
    this codebase's mode="coordinate"/"route"/"broadcast" usage (members do not
    delegate further), so unlike _read_cache_tool_hook there is no per-agent
    dimension to track here -- one shared list is the whole picture.

    Pure observer: never caches, never stubs, never changes what the real
    delegate_task_to_member(s) call does or returns -- delegation is not
    idempotent (a second call to the same member can legitimately want fresh
    context), so unlike reads this must never short-circuit the call itself,
    only make the fact that it happened visible as structured state. Registered
    in the SAME tool_hooks list as read_cache_hook/interception_hook -- position
    in that list doesn't matter for this hook specifically, since every non-
    delegation call already passes straight through via the early return.

    The returned hook carries a `.state` dict whose `["count"]` is a CLOSURE-LOCAL
    delegation counter. Exposed as an ATTRIBUTE rather than a second return value so the
    function's signature is unchanged and every existing caller/test keeps working.
    deliberately not derived from session_state["delegations_made"] below: agno
    constructs a genuinely NEW RunContext -- and therefore a new, empty
    session_state -- for each separate delegate_task_to_members call within one
    run (root-caused with direct instrumentation 2026-08-16, see
    _make_duplicate_delegation_gate_hook's docstring), so that list is unreliable
    for answering "did this run delegate at all". The closure sidesteps agno's
    session_state threading entirely, which is the same reason the duplicate-
    delegation gate abandoned session_state for its own local log. The
    session_state write is left in place unchanged -- it may still be meaningful
    for delegate_task_to_member (singular) and for the coordinator's own rendered
    context; nothing new depends on it.
    """
    state = {"count": 0}

    async def _delegation_log_hook(function_name, function, args, run_context=None):
        if function_name not in _DELEGATION_TOOL_NAMES:
            return await function(**args)

        result = await function(**args)
        state["count"] += 1

        if run_context is not None and run_context.session_state is not None:
            logged_args = dict(args or {})
            task_text = logged_args.get("task")
            if isinstance(task_text, str) and len(task_text) > _MAX_LOGGED_TASK_CHARS:
                logged_args["task"] = task_text[:_MAX_LOGGED_TASK_CHARS] + "...(truncated)"
            log = run_context.session_state.setdefault("delegations_made", [])
            log.append({"tool": function_name, "args": logged_args})
            if len(log) > _MAX_DELEGATION_LOG_ENTRIES:
                del log[: len(log) - _MAX_DELEGATION_LOG_ENTRIES]

        return result

    _delegation_log_hook.state = state
    return _delegation_log_hook


class ToolCallAborted(Exception):
    """Raised by _make_tool_interception_hook when a tool call is skipped
    because its abort_event was set before the call ran."""


def _make_tool_interception_hook(
    abort_event: "asyncio.Event | None" = None,
    activity: dict | None = None,
):
    """Build a tool_hooks callable giving a real per-tool-call checkpoint:
    log every call (name, args, duration, success/failure), and -- if an
    abort_event is supplied and set -- skip the call entirely instead of
    running it. This is AGNOHive 2.3.1's Phase 9a ("interception: pause /
    abort / serialize between tool calls").

    Every print here uses flush=True. Confirmed live 2026-08-09/10 that
    agno-api.service's stdout is fully block-buffered under systemd --
    these lines sat unflushed for 30+ minutes of a genuinely running task
    and were still invisible in journalctl after the client disconnected.
    Without flush=True this hook's log is theoretical, not actually usable
    for live debugging.

    `activity`, if supplied, is a plain dict this hook mutates in place on
    every call (`last_call_name`, `last_call_at` -- time.monotonic()) so a
    caller running team.arun() concurrently with a heartbeat task (see
    run_task_async) can report "Ns since the last tool call" during a
    stretch where the coordinator is generating text and calling nothing.

    Also updates `activity["last_progress_at"]` (2026-08-19 fix -- see DOCS.md
    "7-Test Groundedness Battery" liveness false-positive) on every completed
    call, success or failure. Before this fix, ONLY run_task_async's own
    top-level team.arun() stream classification touched last_progress_at (a
    content chunk, or a dict-shaped tool event surfacing at the TEAM level) --
    this hook, wired onto every DELEGATED member-agent tool call ("most of
    them" per this docstring's own note above), never fed it. In
    mode="coordinate" a single delegate_task_to_member call to a Researcher
    doing 20+ real get_file_content/search_files_batch calls surfaces to the
    team-level stream as essentially one event at delegation-start and one at
    delegation-return -- so a delegation running longer than the liveness
    interval (300s) with no coordinator-level content in between saw
    last_progress_at frozen at delegation-start, `is_stagnant` going true from
    the very first heartbeat tick, and the run auto-killed at ~300-330s
    regardless of how much real, visible work (these same tool_hook log
    lines) was happening underneath. Confirmed live: 3/3 fresh read-only
    grounding tasks (RBAC, seller verification, module settings -- all
    naturally long single-delegation research) killed this way, each with a
    tool call only 19-66s old at the moment of the "stagnant for 300s" kill.
    Feeding last_progress_at from here closes the gap using exactly the
    tracking this hook already does for last_call_at -- no new signal, just
    wiring the existing one into the check that was blind to it.

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
            print(f"[team] tool_hook: {function_name}({args}) ABORTED before execution", flush=True)
            raise ToolCallAborted(function_name)
        started = time.monotonic()
        if activity is not None:
            activity["last_call_name"] = function_name
            activity["last_call_at"] = started
        try:
            result = await function(**args)
            elapsed = time.monotonic() - started
            print(f"[team] tool_hook: {function_name}({args}) -> {elapsed:.2f}s", flush=True)
            if activity is not None:
                now = time.monotonic()
                activity["last_call_at"] = now
                activity["last_progress_at"] = now
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            print(f"[team] tool_hook: {function_name}({args}) RAISED {type(exc).__name__}: {exc} after {elapsed:.2f}s", flush=True)
            if activity is not None:
                now = time.monotonic()
                activity["last_call_at"] = now
                activity["last_progress_at"] = now
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
    activity: dict | None = None,
    task: str | None = None,
    team_name: str | None = None,
    project_id: str | None = None,
) -> Team:
    """Build a coordinator Team from agent specs (or the default Coder+Reviewer), sharing the
    already-connected `mcp_list`. Factored out of run_task_async / run_task_stream so the same
    build is reusable for router sub-teams (EK-88). `coordinator_model` is the already-resolved
    model name. `description` (default None = previous behaviour) lets the router leader route to
    this team. Behaviour is identical to the previous inline Team(...) construction when omitted.
    `skill_catalog` (default None) is forwarded to each agent's spec-based construction so its
    L1 catalog can be filtered per agent role — the default Coder+Reviewer fallback path (used
    only when agent_specs is empty) does not take a catalog; that path predates team YAMLs.
    `activity` (default None = previous behaviour) is forwarded to the interception hook so a
    caller can run a heartbeat alongside team.arun() -- see _make_tool_interception_hook.
    `task` (default None = previous behaviour, permanent no-op) is the original top-level task
    string, forwarded to the decompose-first gate hook -- see _make_decompose_first_gate_hook.
    `team_name` (default None) selects the per-team gate policy (see _GATE_ENABLED_TEAMS /
    _RESEARCHER_AGENT_NAME_BY_TEAM above) -- None preserves the exact pre-2026-08-15
    unconditional-gate behaviour byte-for-byte, matching every caller that doesn't pass it.
    `project_id` (default None) is forwarded to each spec-based member agent's own
    construction (see make_agent_from_spec's own docstring) -- unlike `task`/`instructions`,
    the shared `instructions` list passed to Team(...) only reaches the coordinator, never
    member agents, so this has to be threaded separately."""
    # One cache per run, shared by the coordinator AND every member agent (not just
    # the coordinator) -- see _make_read_cache_tool_hook's docstring for why both of
    # those are load-bearing, not incidental. The interception hook (Phase 9a) is
    # built with abort_event=None here -- see its own docstring for why that keeps
    # this a no-op audit-log pass-through today rather than a live abort switch.
    read_cache_hook = _make_read_cache_tool_hook(activity=activity)
    # team_name=None (any caller that predates this kwarg) keeps both gates armed
    # exactly as before -- each gate_task is only ever suppressed for a team_name
    # that was explicitly resolved and found NOT in ITS OWN allowlist. The two
    # gates use different allowlists (_GATE_ENABLED_TEAMS vs the broader
    # _SEARCH_GATE_ENABLED_TEAMS) -- see _SEARCH_GATE_ENABLED_TEAMS' own comment
    # for why planning gets search-before-browse but not decompose-first.
    # AGNOHive 2.3.3 (2026-08-18), Open Question #1's resolution: a
    # team_gate_flags DB row for this exact (team_name, gate_name) pair
    # OVERRIDES the hardcoded set-membership default computed below -- no row
    # falls back to the exact pre-2026-08-18 behavior unchanged. team_config's
    # cache is loaded via the same ensure_cache_loaded() call already made
    # above this function's two callers (run_task_async/run_task_stream), so
    # this stays a sync, cache-only lookup on the hot _build_team() path, same
    # as model_routing.get_route()'s own contract.
    decompose_gate_default = team_name is None or team_name in _GATE_ENABLED_TEAMS
    decompose_gate_task = task if team_config.get_gate_enabled(
        team_name, "decompose_first", default=decompose_gate_default
    ) else None
    search_gate_default = team_name is None or team_name in _SEARCH_GATE_ENABLED_TEAMS
    search_gate_task = task if team_config.get_gate_enabled(
        team_name, "search_before_browse", default=search_gate_default
    ) else None
    researcher_agent_name = _RESEARCHER_AGENT_NAME_BY_TEAM.get(team_name, _DEFAULT_RESEARCHER_AGENT_NAME)
    decompose_first_gate_hook = _make_decompose_first_gate_hook(
        task=decompose_gate_task, researcher_member_id=researcher_agent_name,
    )
    search_before_browse_gate_hook = _make_search_before_browse_gate_hook(
        task=search_gate_task, researcher_agent_name=researcher_agent_name,
    )
    duplicate_delegation_gate_hook = _make_duplicate_delegation_gate_hook()
    delegation_log_hook = _make_delegation_log_hook()
    interception_hook = _make_tool_interception_hook(activity=activity)
    # Order matters: agno makes the FIRST hook in this list the OUTERMOST wrapper
    # (confirmed via agno.tools.function.Function._build_nested_execution_chain /
    # aexecute's nested-chain builder -- hooks are reversed and reduced from the
    # innermost outward, so hooks[0] ends up wrapping everything else). With
    # interception_hook listed first, it always runs and always logs -- including on
    # a read_cache_hook cache-hit or duplicate-read stub, which otherwise returned
    # before interception_hook ever ran. Confirmed live 2026-08-11: with the old
    # [read_cache_hook, interception_hook] order, a heartbeat during a run of
    # nothing-but-cache-hits reported "194s since last tool call" while reads were
    # visibly still streaming in -- interception_hook's activity["last_call_at"]
    # update was simply never reached for those calls. decompose_first_gate_hook is
    # listed BEFORE delegation_log_hook deliberately (2026-08-14): when it blocks a
    # call, it never invokes the nested function at all, which means
    # delegation_log_hook (nested inside it) never runs either -- a blocked
    # delegation is correctly absent from session_state["delegations_made"], not
    # logged as if it had actually happened. delegation_log_hook's own relative
    # position among the other hooks doesn't matter beyond that -- it's a pure
    # observer that never short-circuits a call itself.
    # search_before_browse_gate_hook sits BEFORE read_cache_hook (more outer) so a
    # blocked browse call never reaches the cache/serve-count bookkeeping at all --
    # it never really happened, the same principle decompose_first_gate_hook's own
    # positioning comment documents for delegation_log_hook. duplicate_delegation_gate_hook
    # (2026-08-15, see its own docstring for the T2c incident that motivated it; rewritten
    # 2026-08-16 to use its own closure-local log instead of session_state -- live-confirmed
    # broken for delegate_task_to_members: agno constructs a genuinely NEW RunContext, and
    # therefore a fresh empty session_state, for each separate call within the same run) is
    # now fully self-contained -- it maintains and checks its own log directly, with no
    # dependency on delegation_log_hook or session_state at all, so its position relative to
    # delegation_log_hook no longer matters for correctness (delegation_log_hook's own
    # session_state write is independent and may still be observed elsewhere in a member's
    # context, unaffected by this hook's now-separate bookkeeping).
    # tool_budget_guard_hook goes LAST: it counts every call that actually executes, so
    # it must not count one the gates above are about to block/stub (those return their
    # own message without calling `function`, so the chain stops before reaching this).
    # Shared across every agent -- these are genuinely run-wide (one cache, one delegation
    # log, one interception trace).
    tool_hooks = [
        interception_hook, search_before_browse_gate_hook, read_cache_hook,
        decompose_first_gate_hook, duplicate_delegation_gate_hook, delegation_log_hook,
    ]

    def _hooks_for(role: str) -> list:
        """Shared hooks plus a budget guard bound to THIS role (2026-08-21).

        Per-agent, not shared, because the budget it guards is per-agent: agno resets
        tool_call_limit per arun() and each Agent carries its own. One shared instance
        counted the whole team into a single bucket -- and, worse, keyed that bucket off
        `agent`, which is None here (agno stores identity on the Function object, shared
        across every agent listing the same tool). Binding the role at construction
        removes the discovery step entirely. Safe to give each agent its own hook list
        only because make_agent_from_spec now hands each agent its own Function copies;
        with shared Functions, `tool_hooks` is one shared slot and the last writer won.
        """
        return tool_hooks + [_make_tool_budget_guard_hook(team_name, activity, role=role)]

    if agent_specs:
        members = [
            make_agent_from_spec(
                spec, *mcp_list, skill_catalog=skill_catalog,
                tool_hooks=_hooks_for(spec.name), project_id=project_id,
            )
            for spec in agent_specs
        ]
    else:
        members = [
            make_coder(*mcp_list, tool_hooks=_hooks_for("Coder")),
            make_reviewer(*mcp_list, tool_hooks=_hooks_for("Reviewer")),
        ]
    # request_clarification is always available, regardless of read_only/allowlist scoping --
    # it's a local tool (not MCP-derived, so name-based allowlisting doesn't apply to it) with
    # no side effects, safe for every team including read-only ones (planning, parallel-review).
    #
    # coordinator_no_direct_writes (2026-08-10 experiment, see config.py's docstring) forces
    # the SAME "strip mutating tools" scoping _scope_coordinator_tools already does for
    # read_only=True, applied to the coordinator ONLY -- `read_only` itself (the caller's
    # request-level flag) is untouched here, so member agents are governed purely by that as
    # before; this doesn't change their behavior at all, only whether the coordinator's own
    # surface additionally excludes write tools regardless of the request's read_only value.
    _coordinator_tool_scope = read_only or config.coordinator_no_direct_writes
    coordinator_tools_list = list(_scope_coordinator_tools(
        coordinator_tools, mcp_list, _coordinator_tool_scope, team_name=team_name,
    )) + [
        request_clarification, update_session_state,
    ]
    # One line per run (2026-08-21). The coordinator's OWN tool surface is the single
    # most consequential thing _build_team decides and the hardest to confirm from
    # outside: engineering deliberately runs it disarmed (coordinator_tools: []), and a
    # live [budget] trace attributed get_file_content -- which a disarmed coordinator
    # cannot call -- to the Coordinator. Every link in the chain (YAML -> _load_team ->
    # worker payload -> _scope_coordinator_tools) reads correct statically, so log the
    # resolved truth rather than re-deriving it.
    print(f"[team] coordinator surface ({len(coordinator_tools_list)}): "
          f"{[getattr(t, 'name', type(t).__name__) for t in coordinator_tools_list]}")
    team = Team(
        name=name,
        description=description,
        mode=mode,
        model=get_model(
            coordinator_model, config.ollama_host,
            temperature=config.coordinator_temperature, max_tokens=config.coordinator_max_tokens,
            frequency_penalty=config.coordinator_frequency_penalty,
            repetition_penalty=config.coordinator_repetition_penalty,
            min_p=config.coordinator_min_p,
        ),
        members=members,
        tools=coordinator_tools_list,
        instructions=instructions,
        show_members_responses=True,
        share_member_interactions=True,
        add_member_tools_to_context=True,
        # Shared session_state (2026-08-13): a real, agno-native, structured dict --
        # copied to each delegated member at dispatch, merged back into this team-level
        # copy when that member's turn ends (agno's own team/_task_tools.py, sequential
        # and race-free in mode="coordinate"/"route" -- this codebase's only reachable
        # modes; "broadcast" would need agno's separate merge_parallel_session_states,
        # not exercised here since no team YAML uses it yet). Seeded with the two keys
        # _record_read/_make_delegation_log_hook write into mechanically; agents may
        # also call update_session_state (swarm/agents.py -- added to
        # coordinator_tools_list above, NOT via agno's own enable_agentic_state, which
        # is deliberately never set anywhere in this codebase -- see that function's
        # own docstring for the confirmed-live agno 2.5.17 bug this avoids) for
        # anything else worth recording -- see _COORDINATOR_INSTRUCTIONS' shared state
        # section for the convention (small structured facts, never full file content
        # -- that would just relocate the exact bloat problem this exists to avoid).
        # add_session_state_to_context=True renders it into the prompt automatically
        # so a smaller local model doesn't need to proactively think to go look for it
        # -- the same "mechanical over hoped-for" lesson this run's own read-cache
        # stubbing and tool-surface guards are already built on.
        session_state={"read_log": [], "delegations_made": []},
        add_session_state_to_context=True,
        markdown=True,
        # read_only-scoped, not global -- see config.read_only_max_iterations'
        # docstring for the live scope-creep incident this fixes and why a read-only
        # run specifically can't need the full pipeline's iteration budget (no
        # Coder/Executor phase is even reachable once writes are stripped).
        max_iterations=(config.read_only_max_iterations if read_only else config.max_iterations),
        # The Coordinator's OWN budget, DB override honoured (2026-08-21) -- this was
        # config.tool_call_limit unconditionally, which is why engineering's
        # Coordinator=60 row never took effect. See _resolve_tool_call_limit.
        tool_call_limit=_resolve_tool_call_limit(team_name, "Coordinator"),
        tool_hooks=_hooks_for("Coordinator"),
    )
    # Expose the delegation hook's closure-local counter on the team object so
    # _verified_answer can ask "did the coordinator delegate at all this run?" after
    # the run completes. Attribute rather than a changed return signature: every
    # existing caller of _build_team keeps working untouched, and a team built by
    # some other path (or a test double) simply has no attribute, which the reader
    # treats as "unknown" rather than "did not delegate" -- same -1-is-not-zero rule
    # _count_read_calls follows.
    team._delegation_state = delegation_log_hook.state
    # Run-scoped read log, visible to _verified_answer's groundedness guards regardless
    # of delegation depth (2026-08-21) -- see the hook's own read_state comment.
    team._read_state = read_cache_hook.state
    return team


_CONTENT_EVENT_TYPES = {"TeamRunContent", "RunContent"}
_TOOL_START_EVENT_TYPES = {"TeamToolCallStarted", "ToolCallStarted"}
_TOOL_END_EVENT_TYPES = {"TeamToolCallCompleted", "ToolCallCompleted"}
# agno's own signal that a model/provider call failed outright (confirmed live
# 2026-08-18: a litellm.ContextWindowExceededError on a long, delegation-heavy
# run arrives as one of these, .content carrying the real error text) --
# distinct from the three sets above, which are all real progress. Previously
# fell through to `return None` (the same bucket as any other unclassified
# event), so the run just kept polling as if nothing had happened. What
# actually happened next, confirmed via the SAME incident's logs: the
# coordinator's very next delegate_task_to_member call crashed inside agno's
# own code with "'NoneType' object has no attribute 'to_dict'" -- plausibly
# the SAME context-overflow condition corrupting internal state on the retry,
# though not confirmed from these logs alone -- and the run never produced
# another real tool call or content chunk, idling until the 300s liveness
# auto-kill eventually cleaned it up 5+ minutes later. See _BackendRunError's
# own comment for the fix this classification enables.
_ERROR_EVENT_TYPES = {"TeamRunError", "RunError"}


class _BackendRunError(RuntimeError):
    """Raised the moment a RunError/TeamRunError stream event is seen -- a real,
    actionable failure from the underlying model/provider (context-window
    overflow, a provider-side rejection, etc.), not a transient hiccup to wait
    out. Deliberately fails FAST instead of letting the run idle for up to
    config.liveness_silence_threshold_s (300s) before the liveness watchdog
    notices nothing is progressing and kills it anyway -- same eventual
    outcome, ~5 minutes sooner, with the real error message instead of a
    generic "no tool call or new stream content" one.

    Caught cleanly by existing machinery at every call site, no new plumbing
    needed: main.py's _run_worker() already wraps run_task_async() in a bare
    `except Exception as exc: return {"error": f"{type(exc).__name__}: {exc}"}`,
    which api/server.py's _run_worker_subprocess() already converts into an
    immediate HTTPException(500, ...) once the child process exits -- this
    exception just needs to exist and propagate; every downstream conversion
    already worked for any other exception type before this one did."""


def _stream_event_to_chunk(event) -> str | dict | None:
    """Classify one raw agno team.arun(stream=True) event into what run_task_stream
    yields downstream (and, since 2026-08-10, what run_task_async logs for content
    visibility). Duck-typed via getattr since agno event objects vary by type.

    Recognizes BOTH the coordinator's own Team-level event types (TeamRunContent,
    TeamToolCallStarted, TeamToolCallCompleted) AND a delegated member agent's
    Agent-level equivalents (RunContent, ToolCallStarted, ToolCallCompleted --
    confirmed via agno.run.agent.RunEvent, no "Team" prefix). This was a real gap
    until 2026-08-10, not a deliberate scope choice: in mode="coordinate" the
    coordinator mostly delegates to team members rather than generating content or
    calling tools itself (same fact _make_read_cache_tool_hook's and
    _make_tool_interception_hook's docstrings document for tool_hooks), so the
    original team-only filter silently dropped most of what actually happens during
    a run -- a live investigation that day found run_task_async's new content-preview
    logging producing nothing for 4+ minutes of genuine, ongoing generation, entirely
    because the coordinator had delegated and every event from the member agent doing
    the work used the unprefixed event names. This also changes run_task_stream's
    /stream endpoint: a client consuming `chunk` events now sees a member agent's
    content too, not just the coordinator's -- a fix, not a new behavior to opt into,
    since that content was always part of the real answer being generated.

    Returns:
      str  — a text delta from the coordinator OR a delegated member agent
      dict — a tool-call sentinel: {"__tool_event__": "start", "name": str, "args": dict,
             "agent_name": str} or {"__tool_event__": "end", "name": str,
             "result_preview": str | None, "agent_name": str}. agent_name is "" for
             the coordinator's own calls (BaseAgentRunEvent's own default) and the
             member's name (e.g. "Researcher") for a delegated call.
           — OR a run-error sentinel (2026-08-18): {"__run_error__": True,
             "message": str, "agent_name": str} for a RunError/TeamRunError event
             (a real backend failure, e.g. a context-window overflow) -- every
             caller of this function checks for this shape FIRST, before treating
             the dict as a tool event, and raises _BackendRunError(message)
             immediately rather than continuing to poll. See _BackendRunError's
             own docstring for why this exists.
      None — every other event type (dropped, same as the previous hard filter)
    """
    event_type = getattr(event, "event", "")
    if event_type in _CONTENT_EVENT_TYPES:
        chunk = getattr(event, "content", None)
        return chunk if isinstance(chunk, str) and chunk else None
    if event_type in _TOOL_START_EVENT_TYPES:
        tool = getattr(event, "tool", None)
        if tool is None:
            return None
        return {
            "__tool_event__": "start",
            "name": tool.tool_name,
            "args": tool.tool_args or {},
            "agent_name": getattr(event, "agent_name", "") or "",
        }
    if event_type in _TOOL_END_EVENT_TYPES:
        tool = getattr(event, "tool", None)
        if tool is None:
            return None
        result = tool.result
        return {
            "__tool_event__": "end",
            "name": tool.tool_name,
            "result_preview": result[:200] if isinstance(result, str) else None,
            "agent_name": getattr(event, "agent_name", "") or "",
        }
    if event_type in _ERROR_EVENT_TYPES:
        message = getattr(event, "content", None)
        return {
            "__run_error__": True,
            "message": message if isinstance(message, str) and message else f"backend reported {event_type} with no message",
            "agent_name": getattr(event, "agent_name", "") or "",
        }
    return None


# How much recently-generated text to search for a repeat -- wide enough to span
# several of the periodic 10s content-preview batches this is checked alongside
# (the real incident's batches ran ~700-900 chars each), narrow enough that a
# phrase legitimately reused once near the start of a long answer, then never
# again, doesn't false-positive purely because it's technically still in the
# same accumulated string somewhere.
_REPETITION_LOOKBACK_CHARS = 4000
# Below this length, a verbatim match is too likely to be a short, legitimately
# reused phrase (a file path, "the function exists") rather than real evidence
# of a degenerate loop -- confirmed live 2026-08-14, the actual repeated
# sentence in the incident this exists to catch was well over 100 chars.
_REPETITION_MIN_SEGMENT_LEN = 60
# A small set of hedging/intensifier words common in a model's own escalating
# self-correction ("more specific" -> "even more specific") -- confirmed live
# 2026-08-14 on a SECOND, distinct incident the same day: the coordinator never
# repeated anything verbatim (the check above correctly stayed silent), but
# spiraled through slightly reworded restatements of "I need to be [even] more
# specific with my citations, let me try again..." without ever landing on an
# answer. Stripped before comparing so "more specific" and "even more specific"
# normalize to the same text. Deliberately short and generic-word-only (never a
# content word) -- removing a handful of filler words from a long sentence does
# not make two genuinely DIFFERENT sentences collapse into a false match, since
# the actual content words (what the sentence is ABOUT) are untouched.
_REPETITION_FILLER_WORDS = frozenset({
    "even", "really", "very", "actually", "now", "again", "just", "simply",
})
# How much of a new segment's OPENING alone counts as evidence of a repeat, when
# the full segment isn't a substring match. Confirmed live 2026-08-14: each
# version of the self-correction spiral above shared the same opening but kept
# APPENDING new clauses ("...from the codebase:" -> "...from the codebase, using
# the exact text from the files:" -> "...and make sure to include the actual
# file content..."), so no later, longer version was ever a full substring of an
# earlier, shorter one -- only their shared beginning repeats. Same minimum
# length as _REPETITION_MIN_SEGMENT_LEN applies to the extracted prefix too, so
# a short generic opening ("I need to check") isn't enough on its own.
#
# Raised 80 -> 100 (2026-08-16, T4 live incident, engineering-team groundedness
# retest): a genuinely long-form, correctly-progressing design-document
# generation task ("Design how the Parties module should be extended... Plan
# only") was auto-killed at 482s with "no tool call or new stream content for
# over 300s" -- but journalctl showed stream events climbing continuously the
# whole time (892 -> 949 -> 1005), real new content, not a stall. A design doc
# with numbered phases / per-field entries naturally repeats STRUCTURE across
# sections (consistent headers, consistent label-before-value formatting) even
# though the actual content differs each time -- and 80 chars is well within
# reach of two merely similarly-FORMATTED (not actually repeated) sections.
#
# NOT raised further, even though a bigger gap would reduce this false-positive
# risk more: tests/test_repetition_loop_detector.py's own reproduction of the
# original escalating-self-correction incident (the real text that motivated
# this whole check) shares only ~110-120 chars of actual overlap between its
# "prior" and "new_segment" variants before they diverge -- at 150 the check
# stopped detecting that real incident at all (both existing detection tests
# started failing). 100 is the calibrated middle: a real, tested improvement
# over 80 (raises the bar above a bare structural-header echo) without
# encroaching on the ~110-char floor the original incident's own text needs to
# still be caught. This narrows but does not eliminate the false-positive
# surface for very long, heavily-templated documents whose per-section
# boilerplate happens to run close to 100 chars -- if that recurs, the next
# lever is requiring the prefix to recur MULTIPLE times in the lookback before
# flagging (a genuine loop repeats the same opening over and over; a
# templated-but-different section's similar header typically does not), not a
# further threshold increase.
_REPETITION_PREFIX_CHARS = 100


def _normalize_for_repetition_check(text: str) -> str:
    """Whitespace-collapsed, filler-word-stripped form used for repetition
    comparison -- see _REPETITION_FILLER_WORDS' own comment for why filler
    words are stripped, not just whitespace."""
    words = text.split()
    return " ".join(w for w in words if w.lower() not in _REPETITION_FILLER_WORDS)


def _looks_like_repetition_loop(new_segment: str, prior_content: str) -> bool:
    """True if `new_segment` (freshly generated text, appended to the accumulated
    answer) is essentially a repeat of something generated recently, rather than
    genuine new progress -- either a literal repeat, or a lightly reworded /
    escalating restatement of one.

    Confirmed live 2026-08-14 (first incident): a coordinator run generated real,
    continuously GROWING content (60,000+ chars) that was nonetheless a useless
    loop -- the same sentence repeated verbatim for 17+ minutes, padded with
    large, VARIABLE runs of blank newlines between each repeat. Whitespace is
    normalized (every run of whitespace collapsed to one space) before comparing
    specifically because of that padding -- a literal, unnormalized substring
    check would see each repeat as a "different" string purely due to a
    different amount of surrounding blank lines and miss the loop entirely.

    Confirmed live 2026-08-14 (second, distinct incident, same day): a DIFFERENT
    run never repeated anything verbatim, so a pure substring check stayed
    silent, yet the coordinator still never produced a real answer -- it
    spiraled through escalating self-corrections about its own citation
    precision instead ("I need to be more specific... let me try again" ->
    "I need to be even more specific... let me try again... using the exact
    text..."). Closed by two additions, both applied only after the cheap exact
    check below has already failed: (1) filler-word stripping, so an inserted
    intensifier doesn't defeat the match; (2) also checking whether just the new
    segment's OPENING recurs, since each version kept appending new trailing
    clauses, so no version was ever a full substring of an earlier one -- only
    their shared beginning repeats.

    Deliberately narrow: only substantial segments (>= _REPETITION_MIN_SEGMENT_LEN
    after normalization) are checked, and only against a bounded recent window
    (_REPETITION_LOOKBACK_CHARS) of prior content, not the whole answer -- see
    each constant's own comment for why. This function does not decide what to DO
    about a detected loop (that's the caller's job, e.g. declining to advance
    last_progress_at) -- it only answers "does this look like one segment being
    generated over and over," nothing about intent or correctness.
    """
    normalized_new = " ".join(new_segment.split())
    if len(normalized_new) < _REPETITION_MIN_SEGMENT_LEN:
        return False
    normalized_prior = " ".join(prior_content[-_REPETITION_LOOKBACK_CHARS:].split())
    if normalized_new in normalized_prior:
        return True

    filler_stripped_new = _normalize_for_repetition_check(new_segment)
    if len(filler_stripped_new) < _REPETITION_MIN_SEGMENT_LEN:
        return False
    filler_stripped_prior = _normalize_for_repetition_check(
        prior_content[-_REPETITION_LOOKBACK_CHARS:]
    )
    if filler_stripped_new in filler_stripped_prior:
        return True

    prefix = filler_stripped_new[:_REPETITION_PREFIX_CHARS]
    if len(prefix) < _REPETITION_MIN_SEGMENT_LEN:
        return False
    return prefix in filler_stripped_prior


_REPETITION_DECAY_WINDOW_CHARS = 1500
# Large enough for a statistically meaningful n-gram sample even when a single
# 10s-boundary new_segment chunk alone is short (~150-250 chars at a typical
# ~20 tok/s generation rate), small enough to stay a LOCAL measurement --
# decay is a property of what generation is doing RIGHT NOW, not something to
# average against content from many minutes earlier in a long run. Smaller
# than _REPETITION_LOOKBACK_CHARS on purpose -- that constant bounds a
# containment SEARCH window (bigger is safer, just costs a longer string
# scan); this bounds a diversity SAMPLE (too big dilutes a real local
# collapse against healthy earlier prose, understating it).
_REPETITION_DECAY_NGRAM_SIZE = 4
# Word-level 4-grams: short enough that a slowly-drifting cycle (each
# repetition varying a word or two) still produces overlapping n-grams, long
# enough that ordinary English prose's natural short-word reuse ("the", "of",
# "a") doesn't itself collapse the ratio the way unigrams/bigrams would.
_REPETITION_DECAY_MIN_NGRAMS = 40
# Below this many n-grams the ratio is too noisy to trust -- a short but
# legitimately narrow-vocabulary passage (e.g. a list of similar file paths)
# can have a naturally low ratio without being degenerate.
_REPETITION_DECAY_RATIO_THRESHOLD = 0.30
# Distinct-n-gram / total-n-gram ratio below this is treated as decay.
# Deliberately conservative (favors under-triggering, i.e. missing a real
# decay episode, over false-triggering on legitimately repetitive-but-valid
# content, e.g. several similarly-shaped findings) -- unlike every other
# threshold in this file, this one has NOT been tuned against a captured live
# incident (the T1-T13 groundedness battery observed the "syntactically
# valid, just degraded" failure shape but did not preserve a transcript of
# it -- see DOCS.md / the "AgnoHive Teams" Notion page's "Known Open Gaps"
# section). Treat this value as a starting point, not a validated one -- the
# next live occurrence should be captured and used to confirm or retune it,
# same discipline every other constant here was held to.


def _looks_like_repetition_decay(new_segment: str, prior_content: str) -> bool:
    """True if the recent window of generated text has collapsed into a small,
    cycling vocabulary -- catches "syntactically valid, just degraded" drift
    that never repeats one earlier segment closely enough for
    _looks_like_repetition_loop's containment-based checks (exact / filler-
    stripped / opening-prefix, all requiring a match against SOME specific
    earlier passage) to catch. Deliberately a separate function rather than a
    fourth tier bolted onto that one: _looks_like_repetition_loop answers "is
    this a repeat of something specific already said," this answers "has
    generation locally collapsed into a small recombined vocabulary,
    regardless of whether it matches anything earlier" -- a different
    question, checked over a different (shorter, purely local) window, not
    _REPETITION_LOOKBACK_CHARS' bounded-but-still-comparative one.

    See _REPETITION_DECAY_RATIO_THRESHOLD's own comment: this has not yet been
    live-validated against a captured real incident. Called from both stream
    loops below alongside _looks_like_repetition_loop (either True withholds
    last_progress_at credit for that window) -- watch its own distinct log
    line ("repetition DECAY detected") specifically on the next live pass for
    false positives before trusting the threshold at face value.
    """
    window = (prior_content[-_REPETITION_DECAY_WINDOW_CHARS:] + new_segment)[-_REPETITION_DECAY_WINDOW_CHARS:]
    words = window.split()
    if len(words) < _REPETITION_DECAY_MIN_NGRAMS + _REPETITION_DECAY_NGRAM_SIZE:
        return False
    ngrams = [
        tuple(w.lower() for w in words[i:i + _REPETITION_DECAY_NGRAM_SIZE])
        for i in range(len(words) - _REPETITION_DECAY_NGRAM_SIZE + 1)
    ]
    if len(ngrams) < _REPETITION_DECAY_MIN_NGRAMS:
        return False
    return (len(set(ngrams)) / len(ngrams)) < _REPETITION_DECAY_RATIO_THRESHOLD


def _log_unclassified_stream_event(log_label: str, event, unrecognized_event_counts: dict[str, int]) -> None:
    """Log a stream event _stream_event_to_chunk() couldn't turn into a text delta or
    a tool-call sentinel -- most often a content-type event whose .content came back
    empty this particular delta, ordinarily harmless in isolation (normal for a
    streaming API to emit an occasional content-less chunk), but seen dozens of times
    in a row with no intervening tool call or real content in between, it is the
    signature of a genuine stall. Confirmed live 2026-08-14: re-running a task on a
    chained session, the coordinator's last real tool call was followed by 5 minutes
    of NOTHING but empty TeamRunContent events -- zero tool-call events, zero content
    growth -- until the (separately fixed, 2026-08-14) liveness auto-kill correctly
    ended it. Ruled out directly: the ZGX thermal watchdog (zero log entries that
    window, GPU sitting at 54C against its 78-83C trigger) and a crashed/hung vLLM
    backend (vllm-coord's own access log showed a continuous, healthy burst of
    freshly established, individually-completed /v1/chat/completions requests the
    entire time -- not one long-hung stream). That rules out the backend; something
    upstream of this event classification keeps re-invoking the model turn after
    turn and getting nothing usable back each time. Extracted from two near-identical
    inline copies (run_task_stream's _stream_team_run and run_task_async's own
    streaming block) into one function so the next diagnostic addition -- like
    model_provider_data below -- lands in exactly one place instead of two that can
    drift apart, the same duplication trap _extract_handoff_summary's two call sites
    already demonstrated in this file.

    Logs the 1st, 2nd, and every 20th occurrence per event type (unchanged threshold)
    -- now also printing model_provider_data, the one field on these events most
    likely to carry a raw finish_reason or provider-side hint that .content and
    .reasoning_content alone don't, needed to get past speculation the next time this
    pattern recurs instead of re-deriving the same "ruled out X, Y, Z" investigation
    from scratch."""
    event_type = getattr(event, "event", "") or "(no .event attr)"
    unrecognized_event_counts[event_type] = unrecognized_event_counts.get(event_type, 0) + 1
    count = unrecognized_event_counts[event_type]
    if count not in (1, 2) and count % 20 != 0:
        return
    content_val = getattr(event, "content", "<no .content attr>")
    reasoning_val = getattr(event, "reasoning_content", "<no .reasoning_content attr>")
    provider_val = getattr(event, "model_provider_data", "<no .model_provider_data attr>")
    print(
        f"[{log_label}] unrecognized stream event #{count} of type {event_type!r}: "
        f"content={content_val!r}, reasoning_content={reasoning_val!r}, "
        f"model_provider_data={provider_val!r}",
        flush=True,
    )


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
    team_name: str | None = None,
):
    """Same setup as run_task_async but yields text chunks as the coordinator generates them.

    `team_name` (default None) is forwarded to _build_team -- see run_task_async's own
    docstring for the 2026-08-15 gate-scope extension this supports.

    No cancellation-checking parameter -- see run_task_async's docstring for why
    (this function is invoked inside a worker process that gets SIGKILLed outright
    by its parent on disconnect; nothing in-process needs to check anything).

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
            _fetch_skill_catalog(_pick_hive_mcp_url(all_mcp_urls, effective_mcp_url)),
        )
    )

    instructions = (
        _project_id_preamble(project_id) + _team_roster_preamble(agent_specs)
        + list(_COORDINATOR_INSTRUCTIONS)
    )
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
        # url -> live MCPTools, so a post-run check (verify_claims) can reuse the
        # connection this run already holds instead of opening a new one. Keyed by url
        # rather than trusting mcp_list order: a server that fails to connect is skipped
        # above, so mcp_list[0] is not necessarily all_mcp_urls[0].
        mcp_by_url: dict[str, object] = {}
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
                mcp_by_url[url] = mcp
                if mcp.functions:
                    print(f"[team] MCP connected: {url} ({len(mcp.functions)} tools)")
                else:
                    # A server that connects but serves nothing is a silent, total
                    # loss of that server -- and the most likely cause is an
                    # exclude_tools entry naming a tool it no longer has, which agno
                    # reports only as "Failed to initialize MCP toolkit" plus a zero
                    # count. Say so where someone will read it (2026-08-21, after
                    # exactly that went unnoticed for a day).
                    print(f"[team] MCP connected but served 0 TOOLS: {url} — every tool "
                          f"from this server is unavailable this run. Most likely an "
                          f"exclude_tools entry naming a tool it does not have "
                          f"(excluded here: {_exclude}); agno drops the whole toolkit "
                          f"when one name does not resolve.")
            except Exception as e:
                print(f"[team] MCP unavailable, skipping ({url}): {e}")
        if not mcp_list:
            raise RuntimeError("No MCP server available — check hive-mcp and project MCP are running")

        # read_only strips mutating tools from both the agents and the coordinator, so a
        # read-only run cannot write regardless of what the model decides to do.
        _specs, _ctools = (_strip_mutating(agent_specs, coordinator_tools) if read_only
                           else (agent_specs, coordinator_tools))
        # DB-backed model routing (AGNOHive 2.3.2 addendum) — get_model() (called
        # inside _build_team, below) only ever reads model_routing's in-process
        # cache, never the DB directly. This covers BOTH the FastAPI server path
        # (already loaded at startup, so this is a fast no-op) AND main.py's plain
        # CLI one-shot path, which never runs the FastAPI startup event.
        await model_routing.ensure_cache_loaded()
        await team_config.ensure_cache_loaded()
        # After the cache is loaded (it is what the no-op path compares against)
        # and before any agent is built. Writes only when hive-mcp's surface
        # actually gained a name.
        await _sync_tool_registry(mcp_list, skill_catalog)
        team = _build_team(
            _specs, effective_coordinator, _ctools, mode, mcp_list, instructions,
            read_only=read_only, skill_catalog=skill_catalog, task=task, team_name=team_name,
            project_id=project_id,
        )

        full_content: list[str] = []
        # See _stream_team_run's own docstring for the narration-leak incident this
        # tracks -- reset to len(full_content) on every tool event so the final
        # fallback (used only when final_run_output.content is empty) can prefer
        # just the text generated SINCE the last tool call, not the whole run's
        # accumulated transcript (which interleaves every agent's own pre-tool-call
        # narration with the real final answer).
        last_segment_start = 0
        final_run_output: "TeamRunOutput | None" = None

        with _tracer.start_as_current_span("agno.task.stream", attributes={
            "project_id": project_id,
            "coordinator_model": effective_coordinator,
            "agent_count": len(team.members),
            "task": task[:120],
        }):
            from observability.metrics import task_duration, task_counter
            t0 = time.perf_counter()
            try:
                # yield_run_output=True (2026-08-10, correctness fix, not optional): without
                # it agno's streaming generator never yields the actual TeamRunOutput object,
                # only lightweight Event objects with no .messages/.tools -- confirmed via
                # agno's own source (team/_run.py: `if yield_run_output: yield run_response`,
                # gated off by default). Every event before that final yield is unaffected --
                # this only ADDS one extra item at the very end of the stream, which the
                # check below captures and does not forward to the external caller as a
                # chunk (it has no .event attribute so _stream_event_to_chunk would return
                # None for it anyway; the explicit check just avoids relying on that
                # incidentally).
                async for event in team.arun(task, stream=True, yield_run_output=True):
                    if not getattr(event, "event", None):
                        # Duck-typed rather than isinstance(event, TeamRunOutput): every
                        # real agno Event class (BaseTeamRunEvent/BaseAgentRunEvent and
                        # all their subclasses) carries a non-empty `.event` type-
                        # discriminator string; TeamRunOutput never does. This also
                        # matches lightweight test fakes that only set the attributes a
                        # given test needs, without requiring every test to construct a
                        # real TeamRunOutput instance just to satisfy an isinstance check.
                        final_run_output = event
                        continue
                    out = _stream_event_to_chunk(event)
                    if isinstance(out, str):
                        full_content.append(out)
                        yield out
                    elif isinstance(out, dict) and out.get("__run_error__"):
                        raise _BackendRunError(out["message"])
                    elif isinstance(out, dict):
                        yield out
                        last_segment_start = len(full_content)
                accumulated = "".join(full_content) or "(no response)"
                final_segment = "".join(full_content[last_segment_start:]).strip()
                fallback_content = final_segment if final_segment else accumulated
                combined = final_run_output.content if final_run_output and final_run_output.content else fallback_content
                # Tier-3 guard: fill [[COUNT ...]] markers in the final content (streamed
                # chunks above are pre-substitution; the done-sentinel content is corrected).
                try:
                    _cm_url, _cm_tools = _pick_hive_mcp(mcp_by_url, "count_matches")
                    combined = await _fill_count_markers(
                        combined, _cm_url, hive_mcp_tools=_cm_tools)
                except Exception as exc:
                    print(f"[team] count-marker guard warning: {exc}")
                tokens = _extract_tokens(final_run_output)
                task_counter.add(1, {"project_id": project_id, "outcome": "success"})
                # Fire-and-forget: don't block the response on post-run experience indexing.
                record_success_bg(task, combined, project_id)
                # Save a compact chain-boundary handoff summary so the next chained call
                # gets a small structured digest instead of the full message history.
                if session_id:
                    from swarm.sessions import save_handoff_summary
                    handoff = _extract_handoff_summary(task, combined, final_run_output)
                    asyncio.ensure_future(save_handoff_summary(session_id, handoff))
                # Primary path (2026-08-10): a real request_clarification tool call. Unlike
                # the old fenced-text convention, a caller consuming stream events already
                # sees this as a structured {"__tool_event__": ...} sentinel mid-stream (see
                # _stream_event_to_chunk) rather than raw JSON scrolling by as text chunks --
                # a strictly better streaming experience, not just parity with /run.
                # final_run_output is the real TeamRunOutput (see yield_run_output note
                # above) -- the only object in this whole function that actually has .tools.
                clarification = _extract_clarification_from_tools(final_run_output)
                if clarification is None:
                    combined, clarification = _extract_clarification(combined)
                else:
                    combined = ""
                yield {"__done__": True, "content": combined, "tokens": tokens, "clarification": clarification}
            except Exception as exc:
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                try:
                    await record_failure(task, str(exc), project_id)
                except Exception:
                    pass  # LightRAG indexing is best-effort; never crash the run
                cloud_msg = _cloud_provider_error_message(exc)
                if cloud_msg:
                    raise RuntimeError(cloud_msg) from exc
                raise
            finally:
                task_duration.record(time.perf_counter() - t0, {"project_id": project_id})


async def _run_heartbeat(
    activity: dict, run_started: float, interval: float = 30.0,
    liveness_path: str | None = None,
) -> None:
    """Prints a periodic status line while team.arun() is one opaque blocking
    await, so a long stretch with zero tool calls -- the coordinator generating
    a long answer, or genuinely stalled -- is visible as a trail of log lines
    instead of indistinguishable silence. Diagnostic only: never cancels or
    times out the run itself. The caller creates this as a background task
    alongside team.arun() and cancels it once that call returns; the
    CancelledError this raises inside asyncio.sleep is expected there.

    `activity["stream_event_count"]` (2026-08-10, optional -- .get() with a
    None default so a caller that never sets it, e.g. an older/other test,
    doesn't crash) answers a question the tool-call timing alone can't: a
    live 17-minute run showed real, ongoing vLLM generation throughput the
    whole time but produced ZERO content-preview or tool-event log lines --
    with no event count, there was no way to tell "the stream is delivering
    events run_task_async's classifier just isn't recognizing" apart from
    "no events are arriving from team.arun(stream=True) at all," two very
    different problems needing different fixes. A count that keeps climbing
    each heartbeat means the former; one stuck at the same number means the
    latter.

    `liveness_path` (default None = previous behaviour) is the Recommendation-#2
    liveness-based auto-kill's write side (see DOCS.md "Liveness-Based Auto-Kill"):
    each tick, also writes a small JSON snapshot api/server.py's own poll loop
    reads to decide whether to kill this run. Tracks CONSECUTIVE ticks with
    NEITHER a new tool call NOR new stream content ("stagnant_ticks") rather than
    a raw timestamp -- this process's and the parent's own time.monotonic() clocks
    are not reliably comparable across a process boundary, but "N seconds of
    nothing happening, as judged by the process that was actually watching" is a
    plain duration either side can reason about identically. Written atomically
    (temp file + os.replace) so a concurrent read from the parent can never see a
    torn write. A write failure is logged, never raised -- this is bookkeeping for
    an optional safety net, not allowed to take down the run it's watching."""
    last_event_count = activity.get("stream_event_count")
    stagnant_ticks = 0
    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        since_last_tool = now - activity["last_call_at"]
        last_name = activity["last_call_name"] or "(none yet)"
        event_count = activity.get("stream_event_count")
        event_count_str = f", {event_count} stream events received so far" if event_count is not None else ""
        print(
            f"[team] heartbeat: {now - run_started:.0f}s since task start, "
            f"{since_last_tool:.0f}s since last tool call (last: {last_name})"
            f"{event_count_str}, coordinator still running",
            flush=True,
        )
        last_progress_at = activity.get("last_progress_at")
        if last_progress_at is not None:
            # Preferred signal (2026-08-14, see the activity-dict-setup comment in
            # run_task_async for the incident this closes): real progress (content or
            # a tool event) landing, not just any raw stream event arriving.
            is_stagnant = (now - last_progress_at) >= interval
        else:
            # Backward compat: a caller that never tracks last_progress_at (older
            # tests, _stream_team_run's own retry-loop activity dict) keeps the
            # original event-count-based judgment, unchanged.
            is_stagnant = event_count is not None and event_count == last_event_count and since_last_tool >= interval
        if is_stagnant:
            stagnant_ticks += 1
        else:
            stagnant_ticks = 0
        last_event_count = event_count
        if liveness_path:
            try:
                snapshot = {
                    "stagnant_seconds": stagnant_ticks * interval,
                    "max_stub_serve_count": activity.get("max_stub_serve_count", 0),
                    "total_stub_serve_count": activity.get("total_stub_serve_count", 0),
                }
                tmp_path = f"{liveness_path}.tmp"
                with open(tmp_path, "w") as f:
                    json.dump(snapshot, f)
                os.replace(tmp_path, liveness_path)
            except OSError as exc:
                print(f"[team] liveness write warning: {exc}", flush=True)


async def _stream_team_run(
    team, prompt: str, *, log_label: str = "verify-retry", liveness_path: str | None = None
) -> tuple[str, "TeamRunOutput | None"]:
    """Run team.arun(prompt, stream=True, yield_run_output=True) with the same
    content-preview/tool-event/heartbeat visibility run_task_async's outer call has,
    for callers that used to make a single opaque `await team.arun(prompt)` -- today,
    that means every retry inside _verified_answer. Confirmed live 2026-08-10: a
    _verified_answer retry ran 17+ minutes with ZERO visibility (real, genuine vLLM
    activity the whole time, but no way to see it), because retries predate and were
    never covered by that day's earlier streaming fix to the OUTER call.

    yield_run_output=True is required, not optional -- without it agno's streaming
    generator never yields the actual TeamRunOutput object (only lightweight Event
    objects with no .messages), a real correctness bug discovered the same day: every
    caller of the old non-streaming `retry = await team.arun(prompt)` needs `.messages`
    (via _count_successful_write_calls / _count_read_calls / _extract_searched_patterns),
    and an Event object silently has none. The `not getattr(event, "event", None)` check
    below captures the real object once agno yields it: every real Event class carries a
    non-empty `.event` type-discriminator string, TeamRunOutput never does, so this is a
    reliable duck-typed distinction (and, unlike isinstance, also matches lightweight
    test fakes that don't construct a real TeamRunOutput).

    Returns (content, run_output) -- content prefers the real TeamRunOutput's own
    .content when available. The fallback path was assumed rare (only a mid-stream
    cancellation, where the final yield never arrives) but is NOT -- live-confirmed
    2026-08-15 (T1e/T2e/T3e engineering-team groundedness retest): a normal,
    successfully-COMPLETED multi-agent coordination run routinely leaves
    final_run_output.content empty too (agno does not always populate the Team-level
    .content field once the coordinator has synthesized an answer via delegated
    members rather than generating it directly), so this fallback fires far more
    often than the "cancelled mid-stream" case it was written for.

    When it does fire, the fallback is the LAST contiguous run of text chunks SINCE
    THE LAST TOOL EVENT (start or end, coordinator's own or any delegated member's)
    -- not the full accumulated transcript of the whole run. Live-confirmed incident
    this fixes: the full accumulated transcript concatenates every agent's own
    mid-process narration ("I'll investigate the pattern...", "I apologize for the
    error, let me correct that...", "I'll check the Notion page...") emitted BEFORE
    each tool call, in front of the real final answer -- returning that whole
    transcript as "the answer" leaked internal scratch narration into every
    user-facing response that hit this fallback. The segment-since-last-tool-event
    heuristic reliably isolates just the coordinator's final synthesis text, since no
    further tool calls happen once synthesis begins. Still falls back to the FULL
    accumulated transcript if that last segment is empty (e.g. the stream's very
    last event was itself a tool call with no trailing text) -- this function must
    never return truly nothing over returning something imperfect.

    run_output is None in the mid-stream-cancellation edge case -- callers already
    null-check every helper that reads it (_count_read_calls etc. all return
    -1/"undeterminable" for a bare getattr(None, ...) miss, the same fail-safe
    posture used everywhere else in this module), so this degrades the same way a
    non-streaming call raising would have.

    Runs its own heartbeat with its OWN fresh activity dict, not the original run's --
    it has no access to the tool_hook closure the original team was built with, so
    "last tool call name" reporting is not meaningful here (always "(none yet)"); the
    stream_event_count and elapsed-time fields are still accurate and are the signal
    that actually mattered in the 17-minute incident this exists to fix.

    liveness_path (2026-08-14, closes a real production hang): forwarded to
    _run_heartbeat exactly like run_task_async's own call does, and last_progress_at
    is now tracked in this function's activity dict too -- until this fix, NEITHER
    was true, so a retry that stalled here was invisible to the process-level
    liveness auto-kill entirely (that watchdog only ever sees what a caller writes
    to liveness_path -- writing nothing means it never observes staleness, so it
    never kills anything). Confirmed live: a verify_claims correction retry emitted
    nothing but empty TeamRunContent events for 30+ minutes with real, distinct
    vLLM completions the whole time (confirmed via each event's own
    model_provider_data carrying a unique completion id -- not one hung stream),
    and NOTHING ended it -- not the 300s liveness threshold (this code path never
    fed it), not agno's own retry bounds (0 base retries, 1 guidance retry -- far
    too few to explain it). It only stopped because the CLIENT's own unrelated
    1800s httpx timeout eventually dropped the connection, which is not a fix, just
    an accidental, much-later backstop. The caller (currently only _verified_answer)
    must be given a liveness_path to actually close this -- omitting it (the
    default) reproduces the exact unprotected behavior above, so every caller of
    this function needs updating alongside this fix, not just this function itself."""
    activity = {
        "last_call_name": None, "last_call_at": time.monotonic(),
        "stream_event_count": 0, "last_progress_at": time.monotonic(),
    }
    heartbeat_task = asyncio.create_task(
        _run_heartbeat(activity, time.monotonic(), liveness_path=liveness_path)
    )
    full_content: list[str] = []
    # Index into full_content marking where the CURRENT (most recent) text segment
    # started -- reset to len(full_content) on every tool event, so the slice
    # full_content[last_segment_start:] always holds just the text generated SINCE
    # the last tool call, from any agent. See this function's own docstring for the
    # narration-leak incident this exists to fix.
    last_segment_start = 0
    final_run_output: "TeamRunOutput | None" = None
    last_logged_len = 0
    last_logged_at = time.monotonic()
    unrecognized_event_counts: dict[str, int] = {}
    try:
        async for event in team.arun(prompt, stream=True, yield_run_output=True):
            if not getattr(event, "event", None):
                final_run_output = event
                continue
            activity["stream_event_count"] += 1
            out = _stream_event_to_chunk(event)
            if isinstance(out, str):
                # Deliberately NOT updating last_progress_at here on every chunk --
                # a chunk arriving mid-loop can't yet be told apart from genuine new
                # content (only the 10s-boundary repetition check below can tell).
                # An earlier version updated it unconditionally per-chunk, which meant
                # a rollback below was immediately re-stomped forward by the very next
                # chunk of the SAME repeating block (still inside the same 10s window),
                # so stagnant_seconds could never accumulate past ~10-20s no matter how
                # long a loop ran. Confirmed live 2026-08-14: 13+ minutes of
                # continuously-detected repetition never tripped the 300s Tier-1
                # auto-kill. Now last_progress_at only ever advances in the non-repeat
                # branch below, once per 10s window.
                full_content.append(out)
                now = time.monotonic()
                if now - last_logged_at >= 10:
                    joined = "".join(full_content)
                    new_segment = joined[last_logged_len:]
                    preview = new_segment[-300:]
                    loop_detected = _looks_like_repetition_loop(new_segment, joined[:last_logged_len])
                    decay_detected = not loop_detected and _looks_like_repetition_decay(
                        new_segment, joined[:last_logged_len]
                    )
                    if loop_detected or decay_detected:
                        # Leave last_progress_at untouched -- it already reflects the
                        # last time genuinely new content was confirmed, and neither a
                        # repeat nor a local diversity collapse this window changes that.
                        if loop_detected:
                            print(
                                f"[{log_label}] repetition loop detected -- the last "
                                f"{len(new_segment)} chars look like a repeat of earlier "
                                f"content, not counted as progress: ...{preview!r}",
                                flush=True,
                            )
                        else:
                            print(
                                f"[{log_label}] repetition DECAY detected -- the last "
                                f"{len(new_segment)} chars have collapsed into a small "
                                f"recombined vocabulary (not a match against earlier "
                                f"content), not counted as progress -- unvalidated "
                                f"threshold, see _looks_like_repetition_decay's own "
                                f"docstring: ...{preview!r}",
                                flush=True,
                            )
                    else:
                        activity["last_progress_at"] = now
                        print(
                            f"[{log_label}] content: +{len(joined) - last_logged_len} chars "
                            f"({len(joined)} total) -- ...{preview!r}",
                            flush=True,
                        )
                    last_logged_at = now
                    last_logged_len = len(joined)
            elif isinstance(out, dict) and out.get("__run_error__"):
                raise _BackendRunError(out["message"])
            elif isinstance(out, dict):
                activity["last_progress_at"] = time.monotonic()
                print(f"[{log_label}] stream tool event: {out}", flush=True)
                last_segment_start = len(full_content)
            else:
                _log_unclassified_stream_event(log_label, event, unrecognized_event_counts)
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
    accumulated = "".join(full_content) or "(no response)"
    final_segment = "".join(full_content[last_segment_start:]).strip()
    fallback_content = final_segment if final_segment else accumulated
    content = final_run_output.content if final_run_output and final_run_output.content else fallback_content
    return content, final_run_output


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
    liveness_path: str | None = None,
    team_name: str | None = None,
) -> tuple[str, dict, dict | None]:
    """Run a task with the given team spec, or fall back to default Coder+Reviewer.

    `team_name` (default None) is forwarded to _build_team -- see its own docstring
    and _GATE_ENABLED_TEAMS for the 2026-08-15 gate-scope extension. None preserves
    prior behaviour unchanged for every caller that doesn't pass it.

    `liveness_path` (default None) is forwarded to _run_heartbeat -- see its own
    docstring and DOCS.md "Liveness-Based Auto-Kill". api/server.py's worker-
    subprocess poll loop supplies this (computed from the child's own pid) when
    config.enable_liveness_autokill is set; main.py's CLI one-shot path and every
    existing caller that doesn't pass it get the previous behaviour unchanged.

    Returns (content, tokens, clarification). clarification is None on a normal
    completed answer; when the coordinator emitted a needs_clarification block
    (see _extract_clarification), it's {"question": str, "options": [...]} and
    content has had that block stripped out.

    No cancellation-checking parameter -- see DOCS.md "Process-Boundary
    Cancellation" for the full history. This function (and run_task_stream) used
    to accept `is_disconnected`, threaded through to a _make_disconnect_checker
    that raced api/server.py's own outer polling loop and, separately, only ever
    tracked the coordinator's own run_id rather than every delegated member's --
    two of four rounds of cooperative-cancellation bugs this codebase went
    through before retiring the whole approach on 2026-08-12. api/server.py now
    runs this function inside a genuinely separate OS process (main.py's
    --run-worker/--stream-worker) and SIGKILLs it outright on disconnect --
    nothing in-process needs to check anything, so there is nothing left to pass
    here. Callers with no HTTP request to poll in the first place (main.py's CLI
    one-shot path) were always unaffected either way.
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
            _fetch_skill_catalog(_pick_hive_mcp_url(all_mcp_urls, effective_mcp_url)),
        )
    )

    instructions = (
        _project_id_preamble(project_id) + _team_roster_preamble(agent_specs)
        + list(_COORDINATOR_INSTRUCTIONS)
    )
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
        # url -> live MCPTools, so a post-run check (verify_claims) can reuse the
        # connection this run already holds instead of opening a new one. Keyed by url
        # rather than trusting mcp_list order: a server that fails to connect is skipped
        # above, so mcp_list[0] is not necessarily all_mcp_urls[0].
        mcp_by_url: dict[str, object] = {}
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
                mcp_by_url[url] = mcp
                if mcp.functions:
                    print(f"[team] MCP connected: {url} ({len(mcp.functions)} tools)")
                else:
                    # A server that connects but serves nothing is a silent, total
                    # loss of that server -- and the most likely cause is an
                    # exclude_tools entry naming a tool it no longer has, which agno
                    # reports only as "Failed to initialize MCP toolkit" plus a zero
                    # count. Say so where someone will read it (2026-08-21, after
                    # exactly that went unnoticed for a day).
                    print(f"[team] MCP connected but served 0 TOOLS: {url} — every tool "
                          f"from this server is unavailable this run. Most likely an "
                          f"exclude_tools entry naming a tool it does not have "
                          f"(excluded here: {_exclude}); agno drops the whole toolkit "
                          f"when one name does not resolve.")
            except Exception as e:
                print(f"[team] MCP unavailable, skipping ({url}): {e}")
        if not mcp_list:
            raise RuntimeError("No MCP server available — check hive-mcp and project MCP are running")

        # read_only strips mutating tools from both the agents and the coordinator, so a
        # read-only run cannot write regardless of what the model decides to do.
        _specs, _ctools = (_strip_mutating(agent_specs, coordinator_tools) if read_only
                           else (agent_specs, coordinator_tools))
        # DB-backed model routing (AGNOHive 2.3.2 addendum) — get_model() (called
        # inside _build_team, below) only ever reads model_routing's in-process
        # cache, never the DB directly. This covers BOTH the FastAPI server path
        # (already loaded at startup, so this is a fast no-op) AND main.py's plain
        # CLI one-shot path, which never runs the FastAPI startup event.
        await model_routing.ensure_cache_loaded()
        await team_config.ensure_cache_loaded()
        # After the cache is loaded (it is what the no-op path compares against)
        # and before any agent is built. Writes only when hive-mcp's surface
        # actually gained a name.
        await _sync_tool_registry(mcp_list, skill_catalog)
        # Fed by the interception hook on every tool call (coordinator or member);
        # the heartbeat task below reads it to report time-since-last-tool-call as a
        # backstop signal independent of the content-chunk logging below (2026-08-10) --
        # the heartbeat still fires even during a stretch with zero stream events of any
        # kind, which the content logging alone would not catch. See
        # _make_tool_interception_hook's docstring for the hook itself.
        #
        # last_progress_at (2026-08-14): separate from stream_event_count below on
        # purpose -- see DOCS.md "Liveness-Based Auto-Kill" addendum. Only advances
        # when the stream loop below classifies an event as real content or a real
        # tool event, never on an empty/unrecognized one, so _run_heartbeat can tell
        # "events keep arriving" apart from "events keep arriving AND at least one of
        # them was real progress." Live 2026-08-13/14: a Researcher's tool calls were
        # all being silently rejected by agno's own tool_call_limit (exceeded
        # mid-run) -- agno yields zero stream event for a rejected call, but the
        # model's own contentless turn still produced a RunContent event each time,
        # so stream_event_count climbed continuously for 700+s with zero real
        # progress, and the old stagnant_ticks check (keyed purely on
        # stream_event_count going unchanged) never fired.
        activity = {
            "last_call_name": None, "last_call_at": time.monotonic(),
            "stream_event_count": 0, "last_progress_at": time.monotonic(),
        }
        team = _build_team(
            _specs, effective_coordinator, _ctools, mode, mcp_list, instructions,
            read_only=read_only, skill_catalog=skill_catalog, activity=activity, task=task,
            team_name=team_name, project_id=project_id,
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
                    heartbeat_task = asyncio.create_task(
                        _run_heartbeat(activity, time.monotonic(), liveness_path=liveness_path)
                    )
                    full_content: list[str] = []
                    # 2026-08-10, corrected same day: originally tracked "last_event" (whatever
                    # happened to be the final stream item) for token/clarification extraction,
                    # then "last_metrics_event" (whichever event had non-None .metrics) after a
                    # live test came back with all-zero token counts. Both were still wrong at
                    # the root: agno's streaming generator never yields the real TeamRunOutput
                    # object at all unless yield_run_output=True is passed (confirmed via
                    # agno's own source, team/_run.py) -- every Event object in the stream
                    # (including the completion event) has no .messages, and RunCompletedEvent
                    # specifically has no .tools either, so _extract_clarification_from_tools
                    # and every _verified_answer guard reading .messages had been silently
                    # getting nothing since the streaming conversion. final_run_output below is
                    # the actual TeamRunOutput, captured once agno yields it.
                    final_run_output: "TeamRunOutput | None" = None
                    last_logged_len = 0
                    last_logged_at = time.monotonic()
                    # See _stream_team_run's own docstring for the narration-leak incident
                    # this tracks -- reset to len(full_content) on every tool event so the
                    # final fallback (used only when final_run_output.content is empty) can
                    # prefer just the text generated SINCE the last tool call, not the whole
                    # run's accumulated transcript.
                    last_segment_start = 0
                    try:
                        # Consuming the stream internally (2026-08-10) instead of one opaque
                        # blocking team.arun(task) -- external behavior (return type, downstream
                        # guards) is unchanged, this only adds visibility into what the
                        # coordinator is generating along the way. Motivated by three live
                        # investigations that day all hitting the same wall: vLLM showing real,
                        # ongoing token throughput for 10-30+ minutes with zero new tool calls,
                        # and no way to tell WHAT was being generated the whole time (py-spy only
                        # shows Python call-stack, not model output; there was no session_id to
                        # query mid-run on a stateless call). run_task_stream already had this
                        # exact mechanism for the /stream endpoint -- this brings /run onto the
                        # same one, permanently, not just for this investigation.
                        unrecognized_event_counts: dict[str, int] = {}
                        async for event in team.arun(task, stream=True, yield_run_output=True):
                            if not getattr(event, "event", None):
                                final_run_output = event
                                continue
                            activity["stream_event_count"] += 1
                            out = _stream_event_to_chunk(event)
                            if isinstance(out, str):
                                # See _stream_team_run's identical block for the full
                                # rationale -- kept in sync deliberately. Deliberately
                                # NOT updating last_progress_at per-chunk here; only the
                                # 10s-boundary check below advances it, and only on
                                # confirmed non-repeat content.
                                full_content.append(out)
                                now = time.monotonic()
                                if now - last_logged_at >= 10:
                                    joined = "".join(full_content)
                                    new_segment = joined[last_logged_len:]
                                    preview = new_segment[-300:]
                                    loop_detected = _looks_like_repetition_loop(new_segment, joined[:last_logged_len])
                                    decay_detected = not loop_detected and _looks_like_repetition_decay(
                                        new_segment, joined[:last_logged_len]
                                    )
                                    if loop_detected or decay_detected:
                                        # Leave last_progress_at untouched -- see
                                        # _stream_team_run's identical block.
                                        if loop_detected:
                                            print(
                                                f"[team] repetition loop detected -- the last "
                                                f"{len(new_segment)} chars look like a repeat of "
                                                f"earlier content, not counted as progress: "
                                                f"...{preview!r}",
                                                flush=True,
                                            )
                                        else:
                                            print(
                                                f"[team] repetition DECAY detected -- the last "
                                                f"{len(new_segment)} chars have collapsed into a "
                                                f"small recombined vocabulary (not a match against "
                                                f"earlier content), not counted as progress -- "
                                                f"unvalidated threshold, see "
                                                f"_looks_like_repetition_decay's own docstring: "
                                                f"...{preview!r}",
                                                flush=True,
                                            )
                                    else:
                                        activity["last_progress_at"] = now
                                        print(
                                            f"[team] content: +{len(joined) - last_logged_len} chars "
                                            f"({len(joined)} total) -- ...{preview!r}",
                                            flush=True,
                                        )
                                    last_logged_at = now
                                    last_logged_len = len(joined)
                            elif isinstance(out, dict) and out.get("__run_error__"):
                                raise _BackendRunError(out["message"])
                            elif isinstance(out, dict):
                                activity["last_progress_at"] = time.monotonic()
                                print(f"[team] stream tool event: {out}", flush=True)
                                last_segment_start = len(full_content)
                            else:
                                # Diagnostic (2026-08-10, revised): the first version of this
                                # logged each unique event.event TYPE once -- but a live run
                                # showed activity["stream_event_count"] climbing steadily (proving
                                # events WERE arriving) while zero content/tool lines ever printed,
                                # meaning many events of the SAME type were all failing
                                # classification and the log-once design couldn't tell "seen once,
                                # harmless" apart from "seen 100+ times, something's genuinely
                                # wrong" -- both looked identical: one line. See
                                # _log_unclassified_stream_event's own docstring for what got added
                                # 2026-08-14 and why (deduped from two copies at the same time).
                                _log_unclassified_stream_event("team", event, unrecognized_event_counts)
                    finally:
                        heartbeat_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await heartbeat_task
                accumulated = "".join(full_content) or "(no response)"
                final_segment = "".join(full_content[last_segment_start:]).strip()
                fallback_content = final_segment if final_segment else accumulated
                content = final_run_output.content if final_run_output and final_run_output.content else fallback_content
                # Clarification check runs BEFORE the claim-verification/count-marker
                # guards below, and short-circuits past both when found: those guards
                # validate a completed factual answer, and a clarification block is
                # neither — it's a pending question, not a claim to fact-check.
                # Primary path: a real request_clarification tool call (2026-08-10) --
                # see _extract_clarification_from_tools' docstring for why this replaced
                # the original text-convention approach as the default. Falls back to
                # the original fenced-block regex only if no tool call is found, for the
                # rare case the model reverts to that old habit instead of calling the tool.
                # final_run_output is the real TeamRunOutput (see the yield_run_output note
                # above) -- the only object here that actually has .tools populated.
                clarification = _extract_clarification_from_tools(final_run_output)
                if clarification is None:
                    content, clarification = _extract_clarification(content)
                else:
                    content = ""
                if clarification is not None:
                    tokens = _extract_tokens(final_run_output)
                    span.set_status(trace.StatusCode.OK)
                    task_counter.add(1, {"project_id": project_id, "outcome": "clarification"})
                    return content, tokens, clarification
                # Tier-3 guard: fill any [[COUNT ...]] markers with deterministic counts.
                try:
                    _cm_url, _cm_tools = _pick_hive_mcp(mcp_by_url, "count_matches")
                    content = await _fill_count_markers(
                        content, _cm_url, hive_mcp_tools=_cm_tools)
                except Exception as exc:
                    print(f"[team] count-marker guard warning: {exc}")
                # Tier-4 guard: grep the draft's claims; one correction round if any are
                # unsupported. Instruction-level verification was tried first and the
                # model ignored it, so this is enforced outside the model.
                try:
                    _hive_url, _hive_tools = _pick_hive_mcp(mcp_by_url, "verify_claims")
                    if _hive_url is None:
                        # Say so explicitly. _verify_claims treats "no url" as a
                        # deliberate config choice and stays silent, which is right for
                        # an operator who turned it off and wrong for hive-mcp simply
                        # not being connected -- the case that previously showed up
                        # only as an opaque failure against whichever server happened
                        # to sit at position 0.
                        print("[team] no connected MCP exposes verify_claims — "
                              "groundedness checking is DISABLED for this run "
                              "(is hive-mcp connected?)")
                    content = await _verified_answer(
                        content, task, team, _hive_url,
                        final_run_output, liveness_path=liveness_path,
                        hive_mcp_tools=_hive_tools)
                except Exception as exc:
                    print(f"[team] verify guard warning: {exc}")
                tokens = _extract_tokens(final_run_output)
                span.set_status(trace.StatusCode.OK)
                task_counter.add(1, {"project_id": project_id, "outcome": "success"})
                # Fire-and-forget: don't block the response on post-run experience indexing.
                record_success_bg(task, content, project_id)
                # Save a compact chain-boundary handoff summary so the next chained call
                # gets a small structured digest instead of the full message history.
                if session_id:
                    from swarm.sessions import save_handoff_summary
                    handoff = _extract_handoff_summary(task, content, final_run_output)
                    asyncio.ensure_future(save_handoff_summary(session_id, handoff))
                return content, tokens, None
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                task_counter.add(1, {"project_id": project_id, "outcome": "failure"})
                try:
                    await record_failure(task, str(exc), project_id)
                except Exception:
                    pass  # LightRAG indexing is best-effort; never crash the run
                cloud_msg = _cloud_provider_error_message(exc)
                if cloud_msg:
                    raise RuntimeError(cloud_msg) from exc
                raise  # callers receive (content, tokens, clarification) on success; exception on failure
            finally:
                task_duration.record(
                    time.perf_counter() - t0,
                    {"project_id": project_id},
                )
