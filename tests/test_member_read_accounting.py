"""Tests: a delegated member's reads count as evidence.

The blocker that kept the coordinator disarm turned off (2026-08-21).

With the coordinator disarmed, EVERY read happens inside a delegated member. Neither
existing source sees those:

  * `result.messages` holds only the COORDINATOR's own tool calls -- a member's reads
    happen in a separate nested run, visible only as one opaque
    `delegate_task_to_member` entry.
  * `session_state["read_log"]` was added on 2026-08-18 specifically to cover that,
    and does not fire once ALL reads are delegated -- agno hands a member
    `copy(run_context.session_state)` and merges it back afterwards
    (team/_default_tools.py:561,532).

Measured live: Researcher read models.py (`[budget] Researcher: call 10/50`) while the
guard reported "ZERO read calls", then retried a correct answer into a wrong one --
T3 came back citing `API/parties-service/`, a service that does not exist.

Fix is the escalation `_make_delegation_log_hook` already made for the same reason: a
closure-local log on the shared read-cache hook, which every agent's calls pass
through regardless of delegation depth. session_state writes are left untouched, so
this only ever WIDENS what counts as evidence.
"""
import pytest

from swarm.team import _READ_TOOLS, _make_read_cache_tool_hook, _run_read_count


class _Team:
    """Stands in for the Team object _build_team attaches the state to."""
    def __init__(self, state=None):
        if state is not None:
            self._read_state = state


async def _fetch(**kwargs):
    return "FILE CONTENT"


# ── _run_read_count ───────────────────────────────────────────────────────────

def test_undeterminable_when_no_state_is_attached():
    """-1, not 0 — the same convention _count_read_calls uses. "Cannot say" must not
    read as "nothing was read", or a missing signal becomes evidence of fabrication."""
    assert _run_read_count(_Team()) == -1
    assert _run_read_count(None) == -1


def test_an_empty_log_counts_zero():
    assert _run_read_count(_Team({"reads": []})) == 0


def test_it_counts_only_the_requested_tools():
    state = {"reads": [
        {"tool": "get_file_content", "read_by": "Researcher"},
        {"tool": "db_query", "read_by": "Researcher"},
        {"tool": "list_directory", "read_by": "ContextRouter"},
    ]}

    assert _run_read_count(_Team(state), tool_names={"db_query"}) == 1
    assert _run_read_count(_Team(state), tool_names={"list_directory"}) == 1
    assert _run_read_count(_Team(state)) >= 1   # default _READ_TOOLS


# ── the hook records reads at any delegation depth ────────────────────────────

@pytest.mark.asyncio
async def test_a_fresh_read_is_recorded():
    hook = _make_read_cache_tool_hook()

    await hook("get_file_content", _fetch, {"relative_path": "a.py"})

    assert _run_read_count(_Team(hook.state)) == 1


@pytest.mark.asyncio
async def test_a_members_read_is_recorded_the_same_as_the_coordinators():
    """The whole point: one shared hook instance serves the coordinator and every
    member, so its closure sees reads session_state cannot carry back."""
    hook = _make_read_cache_tool_hook()

    class _Agent:
        name = "Researcher"

    await hook("get_file_content", _fetch, {"relative_path": "a.py"}, agent=_Agent())

    reads = hook.state["reads"]
    assert len(reads) == 1
    assert reads[0]["read_by"] == "Researcher"


@pytest.mark.asyncio
async def test_a_stubbed_duplicate_is_not_counted_as_new_evidence():
    """Only FRESH fetches count. A stub returns no new content, so counting it would
    let a model manufacture 'evidence' by re-requesting one file."""
    hook = _make_read_cache_tool_hook()

    for _ in range(4):
        await hook("get_file_content", _fetch, {"relative_path": "a.py"})

    assert _run_read_count(_Team(hook.state)) == 1


@pytest.mark.asyncio
async def test_distinct_files_each_count():
    hook = _make_read_cache_tool_hook()

    await hook("get_file_content", _fetch, {"relative_path": "a.py"})
    await hook("get_file_content", _fetch, {"relative_path": "b.py"})

    assert _run_read_count(_Team(hook.state)) == 2


@pytest.mark.asyncio
async def test_a_non_read_tool_is_not_counted():
    hook = _make_read_cache_tool_hook()

    await hook("get_file_content", _fetch, {"relative_path": "a.py"})

    assert _run_read_count(_Team(hook.state), tool_names={"db_query"}) == 0


# ── wiring ────────────────────────────────────────────────────────────────────

def test_build_team_attaches_the_state_to_the_team():
    import inspect

    from swarm import team

    src = inspect.getsource(team._build_team)
    assert "team._read_state = read_cache_hook.state" in src


def test_the_groundedness_guards_consult_both_sources():
    """max(), not sum() — a coordinator's own read appears in BOTH, and inflating the
    count would mislead any future caller that reads it as a magnitude."""
    import inspect

    from swarm import team

    src = inspect.getsource(team._verified_answer)
    assert "max(_count_read_calls(result), _run_read_count(team))" in src
    assert "_run_read_count(team, tool_names=_DB_TOOLS)" in src
    assert "_run_read_count(team, tool_names=_ENUM_TOOLS)" in src


def test_the_citation_retry_check_uses_a_delta_not_a_total():
    """That one asks "did THIS retry re-read", which the cumulative run log cannot
    answer directly — the original attempt's reads are already in it."""
    import inspect

    from swarm import team

    src = inspect.getsource(team._verified_answer)
    assert "_reads_before_retry = _run_read_count(team)" in src
    assert "_retry_delta" in src


def test_session_state_recording_is_untouched():
    """The old source stays: this is a second, independent signal, so anything else
    reading read_log keeps working and no new false negative is possible."""
    import inspect

    from swarm import team

    src = inspect.getsource(team._make_read_cache_tool_hook)
    assert "_record_read(run_context, function_name, args, agent_key" in src
