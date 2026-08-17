"""Tests for _normalize_delegation_task and _make_duplicate_delegation_gate_hook
(swarm/team.py) -- mechanical backstop for _COORDINATOR_INSTRUCTIONS' own prose-only
rule ("check whether an equivalent delegation is already listed... use that result
instead of delegating the same or a near-identical task again").

Live incident (2026-08-15, T2c parallel-review groundedness retest, see
_make_duplicate_delegation_gate_hook's own docstring for the full writeup): the
coordinator called delegate_task_to_members with the EXACT SAME task text twice,
~2.5 minutes apart, after round 1 had already produced two independently-correct,
fully-cited member answers. Round 2 introduced a conflicting wrong answer, and the
coordinator's synthesis sided with the wrong one over three correct ones.

Rewritten 2026-08-16: the original version compared against
run_context.session_state["delegations_made"], seeded via a fake run_context in
these tests. That was live-confirmed BROKEN, not just untested against the real
thing: direct instrumentation (id(run_context), id(run_context.session_state) on
every call) showed agno constructs a genuinely NEW RunContext -- and therefore a
fresh, empty session_state -- for each separate delegate_task_to_members call
within the SAME run, so a real duplicate was never actually caught live despite
this file's own tests passing against the fake. The gate now maintains its own
closure-local log (the same pattern _make_decompose_first_gate_hook's
`state = {"decided": False}` already uses) -- these tests now make TWO
SEQUENTIAL calls through the SAME hook instance to exercise it, rather than
pre-seeding a fake run_context's session_state.
"""
from swarm.team import (
    _normalize_delegation_task, _make_duplicate_delegation_gate_hook,
    _parse_delegation_audit, _normalize_delegation_target,
)


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


def test_stray_ellipsis_is_stripped():
    """T2j live incident (2026-08-16): a real repeat delegation differed from
    its own prior call by nothing but an inserted '...' between two words
    ('backend and list' vs 'backend... and list') -- confirmed via direct
    closure instrumentation that the persistence mechanism itself was already
    correct; this single stray token was the entire reason the exact-match
    check missed a genuinely trivial repeat."""
    a = "Read the actual model/schema file for the Parties module backend and list all tables and fields."
    b = "Read the actual model/schema file for the Parties module backend... and list all tables and fields."
    assert _normalize_delegation_task(a) == _normalize_delegation_task(b)


def test_unicode_ellipsis_character_is_also_stripped():
    a = "Read the file and summarize it."
    b = "Read the file… and summarize it."
    assert _normalize_delegation_task(a) == _normalize_delegation_task(b)


def test_genuinely_different_tasks_still_differ_after_ellipsis_stripping():
    """Ellipsis-stripping must not be so aggressive it collapses genuinely
    different requests -- this is still a narrow, deterministic normalization
    step, not a similarity threshold."""
    a = "Read x.md and summarize it..."
    b = "Read y.md and summarize it..."
    assert _normalize_delegation_task(a) != _normalize_delegation_task(b)


# ── _make_duplicate_delegation_gate_hook: delegate_task_to_member ────────────────

async def test_non_delegation_tool_calls_are_never_touched():
    hook = _make_duplicate_delegation_gate_hook()

    async def fake_get_file_content(**kwargs):
        return "file content"

    result = await hook("get_file_content", fake_get_file_content, {"relative_path": "x.py"}, run_context=None)

    assert result == "file content"


async def test_first_delegation_this_run_is_never_blocked():
    hook = _make_duplicate_delegation_gate_hook()

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md and summarize it"},
        run_context=None,
    )

    assert result.startswith("delegated:")


async def test_exact_repeat_to_same_member_is_blocked_on_the_second_call():
    """The gate's own closure records the first call's real execution, then
    blocks a second, identical one against that same closure -- no run_context
    involved at all, matching what's actually true live (a fresh RunContext per
    delegate_task_to_members call, but the SAME hook function object/closure
    persists across all calls in one run)."""
    hook = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": "Read x.md and summarize it"}

    first = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)
    second = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert "already delegated" in second


async def test_repeat_normalizes_whitespace_and_case_before_comparing():
    """The real live incident's repeated task text was byte-identical, but the
    gate should also catch incidental whitespace/case drift from the model
    re-typing the same request slightly differently."""
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher", "task": "Read x.md   and summarize it"},
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "read x.md and summarize it"},
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")


async def test_repeat_matches_via_real_member_id_form_not_display_name():
    """Confirms this gate uses _member_id() (agno's real dashed/lowercased lookup
    key), not a bare display-name comparison -- a multi-word member called as
    'context-router' must still match a later call spelled 'ContextRouter'."""
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "context-router", "task": "list_directory_tree()"},
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "ContextRouter", "task": "list_directory_tree()"},
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")


async def test_same_task_to_a_different_member_is_not_blocked():
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md and summarize it"},
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "SecurityReviewer", "task": "Read x.md and summarize it"},
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")


async def test_differently_worded_follow_up_to_same_member_without_audit_is_redirected():
    """T1-T13 gap #2 follow-up (2026-08-16) changes this test's own expected
    behavior on purpose: a 2nd+ call to an already-delegated-to member can no
    longer skip straight through just because the wording differs -- it must
    carry a <delegation_audit> tag so the tuple-based check below can tell a
    genuinely different follow-up from a reworded duplicate (which the old
    exact-text check alone could never catch -- see the gate's own docstring).
    A missing tag is redirected asking for it, same as every other tier."""
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md and summarize it"},
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Now read y.md and compare it to x.md"},
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert "delegation_audit" in second


async def test_audited_follow_up_with_a_genuinely_different_target_is_not_blocked():
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {
            "member_id": "Researcher",
            "task": "<delegation_audit>component=parties; action=read; target=x.md</delegation_audit>\nRead x.md and summarize it",
        },
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {
            "member_id": "Researcher",
            "task": "<delegation_audit>component=parties; action=read; target=y.md</delegation_audit>\nNow read y.md and compare it to x.md",
        },
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")


async def test_audited_follow_up_with_the_same_target_and_action_is_blocked_despite_different_wording():
    """The exact scenario the T2c-shaped incident this gate exists for: two
    calls about the same target with the same action, worded completely
    differently -- the exact-text check alone would never catch this, the
    tuple check does."""
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {
            "member_id": "Researcher",
            "task": (
                "<delegation_audit>component=parties; action=read; "
                "target=API/inventory-service/models.py</delegation_audit>\n"
                "Read the actual model/schema file for the Parties module backend and list "
                "all tables and fields. Do not guess field names."
            ),
        },
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {
            "member_id": "Researcher",
            "task": (
                "<delegation_audit>component=parties; action=read; "
                "target=API/inventory-service/models.py</delegation_audit>\n"
                "Read the actual model/schema file(s) for the Parties module backend. If the "
                "thing asked about does not exist, say so plainly."
            ),
        },
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert "same target" in second


async def test_audit_only_required_from_the_second_call_onward():
    """The very first delegation to a member never needs the audit tag --
    there is nothing yet to compare it against."""
    hook = _make_duplicate_delegation_gate_hook()

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md and summarize it"},
        run_context=None,
    )

    assert result.startswith("delegated:")


async def test_second_broadcast_without_audit_is_redirected():
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Review the new auth endpoints for security issues"},
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Please review the newly added auth routes for any security problems"},
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert "delegation_audit" in second


async def test_second_broadcast_with_matching_audit_tuple_is_blocked():
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_members", _fake_delegate,
        {
            "task": (
                "<delegation_audit>component=auth; action=verify; "
                "target=API/authentication-service/router/auth_api.py</delegation_audit>\n"
                "Review the new auth endpoints for security issues"
            ),
        },
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_members", _fake_delegate,
        {
            "task": (
                "<delegation_audit>component=auth; action=verify; "
                "target=API/authentication-service/router/auth_api.py</delegation_audit>\n"
                "Please review the newly added auth routes for any security problems"
            ),
        },
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert "same target" in second


async def test_blocked_call_is_never_actually_invoked():
    calls = []

    async def tracking_delegate(**kwargs):
        calls.append(kwargs)
        return "should not run"

    hook = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": "dup task"}

    await hook("delegate_task_to_member", tracking_delegate, args, run_context=None)
    calls.clear()  # only interested in whether the SECOND call reaches the function
    await hook("delegate_task_to_member", tracking_delegate, args, run_context=None)

    assert calls == []


async def test_a_third_identical_call_is_also_blocked():
    """The closure log accumulates real entries -- it must not stop protecting
    after the first block (e.g. by clearing itself)."""
    hook = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": "dup task"}

    first = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)
    second = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)
    third = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert third.startswith("REDIRECTED:")


# ── _make_duplicate_delegation_gate_hook: delegate_task_to_members (broadcast) ───

async def test_first_broadcast_this_run_is_never_blocked():
    hook = _make_duplicate_delegation_gate_hook()

    result = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Read the schema file and list every table and field."},
        run_context=None,
    )

    assert result.startswith("delegated:")


async def test_t2j_incident_shape_ellipsis_variant_is_now_blocked():
    """The exact real incident, end-to-end through the hook: round 2's task text
    differed from round 1's only by an inserted ellipsis. Before the
    ellipsis-stripping fix this was NOT blocked (a genuine gap, not a bug in
    persistence -- confirmed live the closure itself was already working
    correctly). Must be blocked now."""
    hook = _make_duplicate_delegation_gate_hook()
    round1 = {
        "task": "Read the actual model/schema file for the Parties module backend "
                "and list all tables and fields. Do not guess field names — base "
                "your response strictly on the file content.",
    }
    round2 = {
        "task": "Read the actual model/schema file for the Parties module backend... "
                "and list all tables and fields. Do not guess field names — base "
                "your response strictly on the file content.",
    }

    first = await hook("delegate_task_to_members", _fake_delegate, round1, run_context=None)
    second = await hook("delegate_task_to_members", _fake_delegate, round2, run_context=None)

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")


async def test_exact_repeat_broadcast_is_blocked_on_the_second_call():
    """The actual T2c/T2e live-incident shape: delegate_task_to_members called
    twice with byte-identical task text, each call carrying its OWN fresh
    RunContext (confirmed live 2026-08-16) -- this hook's closure-local log,
    not run_context, is what makes the second call detectable at all."""
    hook = _make_duplicate_delegation_gate_hook()
    args = {"task": "Read the schema file and list every table and field."}

    first = await hook("delegate_task_to_members", _fake_delegate, args, run_context=None)
    second = await hook("delegate_task_to_members", _fake_delegate, args, run_context=None)

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert "already broadcast" in second


async def test_broadcast_repeat_does_not_match_a_singular_delegate_log_entry():
    """delegate_task_to_member and delegate_task_to_members are compared in
    separate spaces -- a prior singular delegation with the same text must never
    block a later broadcast call, since they are semantically different actions."""
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read the schema file and list every table and field."},
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Read the schema file and list every table and field."},
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")


async def test_differently_worded_broadcast_without_audit_is_redirected():
    """T1-T13 gap #2 follow-up (2026-08-16) changes this test's own expected
    behavior on purpose -- see the singular-delegation equivalent above
    (test_differently_worded_follow_up_to_same_member_without_audit_is_redirected)
    for the full rationale; a genuinely-different 2nd+ broadcast still needs an
    audit tag before it can be told apart from a reworded duplicate."""
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Read the schema file."}, run_context=None,
    )
    second = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "Now check the API endpoints for security issues."}, run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("REDIRECTED:")
    assert "delegation_audit" in second


async def test_audited_broadcast_with_a_genuinely_different_target_is_not_blocked():
    hook = _make_duplicate_delegation_gate_hook()

    first = await hook(
        "delegate_task_to_members", _fake_delegate,
        {"task": "<delegation_audit>component=schema; action=read; target=models.py</delegation_audit>\nRead the schema file."},
        run_context=None,
    )
    second = await hook(
        "delegate_task_to_members", _fake_delegate,
        {
            "task": (
                "<delegation_audit>component=auth; action=verify; "
                "target=API/authentication-service/router/auth_api.py</delegation_audit>\n"
                "Now check the API endpoints for security issues."
            ),
        },
        run_context=None,
    )

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")


# ── Isolation between separate hook instances (separate runs) ────────────────────

async def test_two_separate_hook_instances_do_not_share_state():
    """Each _build_team() call constructs its own hook via
    _make_duplicate_delegation_gate_hook() -- one run's delegations must never
    leak into a different run's closure."""
    hook_a = _make_duplicate_delegation_gate_hook()
    hook_b = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": "same task text"}

    await hook_a("delegate_task_to_member", _fake_delegate, args, run_context=None)
    result_b = await hook_b("delegate_task_to_member", _fake_delegate, args, run_context=None)

    assert result_b.startswith("delegated:")


# ── Edge cases shared by both tool shapes ────────────────────────────────────────

async def test_missing_run_context_does_not_crash_and_never_blocks():
    hook = _make_duplicate_delegation_gate_hook()

    result = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "Researcher", "task": "Read x.md"},
        run_context=None,
    )

    assert result.startswith("delegated:")


async def test_empty_task_text_is_never_blocked():
    hook = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": ""}

    first = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)
    second = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")


# ── _parse_delegation_audit / _normalize_delegation_target: pure parsers ─────────

def test_parses_a_well_formed_audit_tag():
    task = (
        "<delegation_audit>component=parties; action=read; "
        "target=API/inventory-service/models.py</delegation_audit>\n"
        "Read the model file"
    )
    assert _parse_delegation_audit(task) == {
        "component": "parties",
        "action": "read",
        "target": "api/inventory-service/models.py",
    }


def test_action_is_lowercased():
    task = "<delegation_audit>component=x; action=READ; target=y.md</delegation_audit>\nRead it"
    assert _parse_delegation_audit(task)["action"] == "read"


def test_out_of_vocabulary_action_still_parses():
    """Deliberate: a strict vocabulary would risk redirecting a legitimate call
    over a wording technicality -- the same false-positive shape that ruled out
    fuzzy text matching (see the gate's own docstring)."""
    task = "<delegation_audit>component=x; action=summarize; target=y.md</delegation_audit>\nDo it"
    assert _parse_delegation_audit(task)["action"] == "summarize"


def test_missing_audit_tag_returns_none():
    assert _parse_delegation_audit("Just read the file, no audit tag here") is None


def test_none_task_returns_none():
    assert _parse_delegation_audit(None) is None


def test_empty_task_returns_none():
    assert _parse_delegation_audit("") is None


def test_malformed_audit_tag_returns_none():
    assert _parse_delegation_audit("<delegation_audit>not the right shape</delegation_audit>\nRead it") is None


def test_target_backslashes_normalize_to_forward_slashes():
    assert _normalize_delegation_target("API\\inventory-service\\models.py") == "api/inventory-service/models.py"


def test_target_normalization_strips_and_lowercases():
    assert _normalize_delegation_target("  Parties/Models.PY  ") == "parties/models.py"


def test_none_target_normalizes_to_empty_string():
    assert _normalize_delegation_target(None) == ""
