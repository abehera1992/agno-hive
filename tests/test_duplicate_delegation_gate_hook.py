"""Tests for _normalize_delegation_task and _make_duplicate_delegation_gate_hook
(swarm/team.py) -- mechanical backstop for _COORDINATOR_INSTRUCTIONS' own prose-only
rule ("check whether an equivalent delegation is already listed... use that result
instead of delegating the same or a near-identical task again").

Live incident (2026-08-15, T2c parallel-review groundedness retest, see
_make_duplicate_delegation_gate_hook's own docstring for the full writeup): the
coordinator called delegate_task_to_members with the EXACT SAME task text twice,
~2.5 minutes apart, after round 1 had already produced two independently-correct,
fully-cited member answers. Round 2 introduced a conflicting wrong answer, and the
coordinator's synthesis sided with the wrong one over three correct ones. This gate
prevents the duplicate broadcast/delegation from ever firing in the first place --
the companion fix (a new "Resolving conflicting member reports" section in
_COORDINATOR_INSTRUCTIONS) covers the synthesis side for cases where two genuinely
different delegations still produce conflicting reports.
"""
from swarm.team import _normalize_delegation_task, _make_duplicate_delegation_gate_hook


class _FakeRunContext:
    def __init__(self, session_state):
        self.session_state = session_state


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


# ── _normalize_delegation_task: pure normalizer ──────────────────────────────────

def test_collapses_internal_whitespace():
    assert _normalize_delegation_task("Read   the file\n\n and extract it") == "read the file and extract it"


def test_lowercases():
    assert _normalize_delegation_task("Read File.md") == "read file.md"


def test_strips_leading_trailing_whitespace():
    assert _normalize_delegation_task("  read x  ") == "read x"


def test_none_task_normalizes_to_empty_string():
    assert _normalize_delegation_task(None) == ""


def test_differently_worded_tasks_are_not_equal():
    assert _normalize_delegation_task("Read patterns/x.md") != _normalize_delegation_task("Read patterns/y.md")


# ── _make_duplicate_delegation_gate_hook: delegate_task_to_member ────────────────

async def test_non_delegation_tool_calls_are_never_touched():
    hook = _make_duplicate_delegation_gate_hook()

    async def fake_get_file_content(**kwargs):
        return "file content"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, run_context=None)

    assert result == "file content"


async def test_first_delegation_this_run_is_never_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({"delegations_made": []})

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md and summarize it"},
        run_context=run_context,
    )

    assert result.startswith("delegated:")


async def test_exact_repeat_to_same_member_is_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {
                "member_id": "researcher", "task": "Read x.md and summarize it",
            }},
        ],
    })

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md and summarize it"},
        run_context=run_context,
    )

    assert result.startswith("REDIRECTED:")
    assert "already delegated" in result


async def test_repeat_normalizes_whitespace_and_case_before_comparing():
    """The real live incident's repeated task text was byte-identical, but the
    gate should also catch incidental whitespace/case drift from the model
    re-typing the same request slightly differently."""
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {
                "member_id": "researcher", "task": "Read x.md   and summarize it",
            }},
        ],
    })

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "read x.md and summarize it"},
        run_context=run_context,
    )

    assert result.startswith("REDIRECTED:")


async def test_repeat_matches_via_real_member_id_form_not_display_name():
    """Confirms this gate uses _member_id() (agno's real dashed/lowercased lookup
    key), not a bare display-name comparison -- a multi-word member logged as
    'context-router' must still match a later call spelled 'ContextRouter'."""
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {
                "member_id": "context-router", "task": "list_directory_tree()",
            }},
        ],
    })

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "ContextRouter", "task": "list_directory_tree()"},
        run_context=run_context,
    )

    assert result.startswith("REDIRECTED:")


async def test_same_task_to_a_different_member_is_not_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {
                "member_id": "researcher", "task": "Read x.md and summarize it",
            }},
        ],
    })

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "SecurityReviewer", "task": "Read x.md and summarize it"},
        run_context=run_context,
    )

    assert result.startswith("delegated:")


async def test_differently_worded_follow_up_to_same_member_is_not_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {
                "member_id": "researcher", "task": "Read x.md and summarize it",
            }},
        ],
    })

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Now read y.md and compare it to x.md"},
        run_context=run_context,
    )

    assert result.startswith("delegated:")


async def test_blocked_call_is_never_actually_invoked():
    calls = []

    async def tracking_delegate(**kwargs):
        calls.append(kwargs)
        return "should not run"

    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {"member_id": "researcher", "task": "dup task"}},
        ],
    })

    await hook(
        "delegate_task_to_member", tracking_delegate,
        {"member_id": "Researcher", "task": "dup task"},
        run_context=run_context,
    )

    assert calls == []


# ── _make_duplicate_delegation_gate_hook: delegate_task_to_members (broadcast) ───

async def test_first_broadcast_this_run_is_never_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({"delegations_made": []})

    result = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Read the schema file and list every table and field."},
        run_context=run_context,
    )

    assert result.startswith("delegated:")


async def test_exact_repeat_broadcast_is_blocked():
    """The actual T2c incident shape: delegate_task_to_members called twice with
    byte-identical task text."""
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_members", "args": {
                "task": "Read the schema file and list every table and field.",
            }},
        ],
    })

    result = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Read the schema file and list every table and field."},
        run_context=run_context,
    )

    assert result.startswith("REDIRECTED:")
    assert "already broadcast" in result


async def test_broadcast_repeat_does_not_match_a_singular_delegate_log_entry():
    """delegate_task_to_member and delegate_task_to_members are compared in
    separate spaces -- a prior singular delegation with the same text must never
    block a later broadcast call, since they are semantically different actions."""
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {
                "member_id": "researcher", "task": "Read the schema file and list every table and field.",
            }},
        ],
    })

    result = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Read the schema file and list every table and field."},
        run_context=run_context,
    )

    assert result.startswith("delegated:")


async def test_differently_worded_broadcast_is_not_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_members", "args": {"task": "Read the schema file."}},
        ],
    })

    result = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Now check the API endpoints for security issues."},
        run_context=run_context,
    )

    assert result.startswith("delegated:")


# ── Edge cases shared by both tool shapes ────────────────────────────────────────

async def test_missing_run_context_does_not_crash_and_never_blocks():
    hook = _make_duplicate_delegation_gate_hook()

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md"},
        run_context=None,
    )

    assert result.startswith("delegated:")


async def test_none_session_state_does_not_crash_and_never_blocks():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext(None)

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md"},
        run_context=run_context,
    )

    assert result.startswith("delegated:")


async def test_empty_task_text_is_never_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    run_context = _FakeRunContext({
        "delegations_made": [
            {"tool": "delegate_task_to_member", "args": {"member_id": "researcher", "task": ""}},
        ],
    })

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": ""},
        run_context=run_context,
    )

    assert result.startswith("delegated:")
