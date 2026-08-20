"""Regression tests: a guard's corrective retry must be ADJUDICATED, not trusted.

Every guard in `_verified_answer` (swarm/team.py) re-runs the whole pipeline with a
corrective instruction and, until 2026-08-20, adopted whatever came back
unconditionally (`content, result = retried, retry`). Two live findings show why that
is unsafe:

1. 2026-08-15 (documented as a still-open finding on the "AgnoHive Teams — Groundedness
   & Reliability Hardening" page): a retry "repeated the exact wrong-service mistake ...
   producing a confidently WRONG answer that overwrote Researcher's correct one as the
   final result." The retry was strictly worse and won anyway.

2. 2026-08-20, live groundedness probe on `API/inventory-service/router/parties_api.py`:
   the draft tripped the no-evidence guard (zero reads), the corrective retry ALSO made
   zero read calls, and its answer was carried forward as final regardless. hive-mcp's
   own tool log for that window confirms zero get_file_content/search_files calls across
   the entire run, yet the answer cited specific line numbers -- two of which happened to
   be correct, so verify_claims' grep passed them and the answer shipped looking verified.

`_more_grounded` / `_adopt_retry` reject a demonstrably less-grounded retry; the
retry-compliance checks surface a retry that ignored its own correction instead of
silently accepting it.
"""
from types import SimpleNamespace

from swarm.team import (
    _DB_TASK_RE,
    _DB_TOOLS,
    _adopt_retry,
    _count_read_calls,
    _more_grounded,
)


def _result(messages=None, session_state=None):
    return SimpleNamespace(messages=messages, session_state=session_state)


def _read_log(*tools, read_by: str = "Researcher"):
    return {"read_log": [
        {"tool": t, "args": {}, "read_by": read_by, "result_chars": 500} for t in tools
    ]}


def _reads(n: int, tool: str = "get_file_content"):
    """A result whose read_log holds exactly n real reads.

    n=0 still carries one NON-read tool message, because a genuine zero-read run is not
    a run with no messages at all: probe 1's own zero-read run still called verify_claims
    and list_skills (confirmed in hive-mcp's tool log for that window). That distinction
    is load-bearing -- `_record_read` only creates session_state["read_log"] on the first
    real read, so a zero-read run has no read_log key, and with no recognisable message
    shape either the count is genuinely undeterminable (-1), NOT zero. Building the n=0
    fixture without any messages would therefore have tested the wrong thing entirely:
    the -1 path, which by design keeps the old always-adopt behaviour. The truly
    undeterminable case has its own dedicated tests below.
    """
    if n == 0:
        return _result(
            messages=[SimpleNamespace(role="tool", tool_name="verify_claims", content="ok")],
            session_state=None,
        )
    return _result(messages=[], session_state=_read_log(*([tool] * n)))


# ── _more_grounded ────────────────────────────────────────────────────────────


def test_retry_that_read_more_is_more_grounded():
    assert _more_grounded(_reads(1), _reads(4)) is True


def test_retry_that_read_the_same_amount_is_accepted():
    """Equal evidence is not a regression -- only a DROP is rejected, so a retry that
    re-reads the same files and genuinely improves the prose is never discarded."""
    assert _more_grounded(_reads(3), _reads(3)) is True


def test_retry_that_read_less_is_not_more_grounded():
    """The 2026-08-15 shape: a well-grounded draft replaced by a thinner re-run."""
    assert _more_grounded(_reads(6), _reads(1)) is False


def test_zero_read_retry_against_a_grounded_draft_is_rejected():
    assert _more_grounded(_reads(5), _reads(0)) is False


def test_undeterminable_original_falls_back_to_accepting_the_retry():
    """-1 means "could not tell", never "did not read" -- an unrecognised message shape
    must preserve the original always-adopt behaviour rather than start discarding
    retries on a signal that was never measured."""
    unknown = _result(messages=None, session_state=None)
    assert _count_read_calls(unknown) == -1
    assert _more_grounded(unknown, _reads(0)) is True


def test_undeterminable_retry_falls_back_to_accepting_the_retry():
    unknown = _result(messages=None, session_state=None)
    assert _more_grounded(_reads(9), unknown) is True


# ── _adopt_retry ──────────────────────────────────────────────────────────────


def test_adopt_retry_keeps_original_when_retry_content_is_empty():
    """Mirrors the `if retried:` check every call site used before this helper existed:
    an empty completion never replaces a real draft."""
    draft, draft_result = "grounded draft", _reads(3)
    content, result = _adopt_retry("t", draft, draft_result, "", _reads(9))
    assert content == draft
    assert result is draft_result


def test_adopt_retry_takes_a_better_grounded_retry():
    retry_result = _reads(7)
    content, result = _adopt_retry("t", "draft", _reads(2), "retried answer", retry_result)
    assert content == "retried answer"
    assert result is retry_result


def test_adopt_retry_keeps_original_when_retry_is_less_grounded():
    """The core regression: the old unconditional adopt would have returned the retry."""
    draft, draft_result = "well-grounded draft", _reads(8)
    content, result = _adopt_retry("t", draft, draft_result, "thin re-run", _reads(1))
    assert content == draft
    assert result is draft_result


# ── _count_read_calls' tool_names filter (backs the DB-evidence guard) ────────


def test_read_count_filtered_to_db_tools_counts_only_db_calls():
    result = _result(messages=[], session_state=_read_log("db_query", "db_schema"))
    assert _count_read_calls(result, tool_names=_DB_TOOLS) == 2


def test_file_reads_do_not_satisfy_a_db_tool_filter():
    """The 2026-08-20 failure: a live-DB question answered entirely off a models.py grep.
    The generic guard saw reads>0 and let it through; the filtered count sees zero."""
    result = _result(messages=[], session_state=_read_log("get_file_content", "search_files"))
    assert _count_read_calls(result) == 2
    assert _count_read_calls(result, tool_names=_DB_TOOLS) == 0


def test_unfiltered_default_is_unchanged():
    """Existing callers pass no tool_names and must behave exactly as before."""
    result = _result(messages=[], session_state=_read_log("get_file_content"))
    assert _count_read_calls(result) == 1


# ── _DB_TASK_RE ───────────────────────────────────────────────────────────────


def test_db_task_regex_matches_the_phrasings_that_demand_a_live_check():
    for task in (
        "Check the live EkamApp database: how many rows exist in the items table?",
        "You must use the live database (db_query/db_schema), not a file grep.",
        "Give me the row count for inventory.items",
    ):
        assert _DB_TASK_RE.search(task), task


def test_db_task_regex_ignores_ordinary_code_questions():
    """A false positive costs a needless guard check; keeping it narrow matters because
    the guard can hard-surface a disclaimer on the answer."""
    for task in (
        "Describe how party validation works in parties_api.py",
        "Add pagination to the sellers endpoint",
        "What does the Item model look like?",
    ):
        assert not _DB_TASK_RE.search(task), task
