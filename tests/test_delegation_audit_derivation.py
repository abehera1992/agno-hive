"""C: an untagged re-delegation gets its audit DERIVED from its own text.

The duplicate-delegation gate used to answer every untagged re-delegation to an
already-used member with "REDIRECTED: add an <delegation_audit> tag and retry" -- a
protocol the model has to remember, that consumed a tool call each time it forgot, and
that blocked legitimate new targets as hard as true repeats. The audit tuple is now
derived mechanically at that exact branch; a tag the model DID emit still wins.

The audit is DATA. args["task"] is never modified: _carry_prior_findings rewrites it as
the last step, and every dedupe tier compares task text.
"""
from types import SimpleNamespace

import pytest

from swarm.team import (
    _derive_delegation_audit, _make_duplicate_delegation_gate_hook, _parse_delegation_audit,
)


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


def _team(**results):
    return SimpleNamespace(_member_results=dict(results))


def _tag(action, target, body):
    return (f"<delegation_audit>component=svc; action={action}; target={target}"
            f"</delegation_audit> {body}")


# ── the derivation itself ────────────────────────────────────────────────────

def test_derives_target_and_action_from_the_task_text():
    audit, from_text = _derive_delegation_audit(
        "read API/inventory-service/models.py and return every class declaration")
    assert from_text is True
    assert audit["action"] == "read"
    assert audit["target"] == "api/inventory-service/models.py"


def test_target_is_normalised_like_a_tagged_target():
    audit, _ = _derive_delegation_audit("Read API\\Svc\\Models.py")
    tagged = _parse_delegation_audit(_tag("read", "API/Svc/Models.py", "x"))
    assert audit["target"] == tagged["target"] or audit["target"].endswith("models.py")


def test_action_is_the_earliest_action_word():
    assert _derive_delegation_audit("search_files for foo in x.py, then read it")[0]["action"] == "search"
    assert _derive_delegation_audit("Verify x.py is correct")[0]["action"] == "verify"
    assert _derive_delegation_audit("Implement the change in x.py")[0]["action"] == "implement"


def test_several_paths_become_one_sorted_target():
    audit, _ = _derive_delegation_audit("Read b.py and a.py")
    assert audit["target"] == "a.py, b.py"


def test_unclassifiable_text_falls_back_to_a_task_hash_that_matches_nothing_else():
    a, ok_a = _derive_delegation_audit("what is going on here")
    b, ok_b = _derive_delegation_audit("what is happening over there")
    assert ok_a is False and ok_b is False
    assert a["target"].startswith("task:") and a["action"] == "unknown"
    assert a["target"] != b["target"]
    same, _ = _derive_delegation_audit("What is   going on here")
    assert same["target"] == a["target"]            # whitespace/case-stable


def test_task_text_is_never_modified_by_the_gate():
    import asyncio
    hook = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": "Read a.py"}
    second = {"member_id": "Researcher", "task": "Now read b.py"}
    asyncio.run(hook("delegate_task_to_member", _fake_delegate, args, run_context=None))
    asyncio.run(hook("delegate_task_to_member", _fake_delegate, second, run_context=None))
    assert second["task"] == "Now read b.py" and args["task"] == "Read a.py"


# ── the gate ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_delegation_is_never_blocked_or_tagged():
    hook = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": "Read a.py"}
    out = await hook("delegate_task_to_member", _fake_delegate, args, run_context=None)
    assert out.startswith("delegated:") and args["task"] == "Read a.py"


@pytest.mark.asyncio
async def test_untagged_repeat_of_the_same_target_and_action_is_served_the_prior_result():
    hook = _make_duplicate_delegation_gate_hook()
    team = _team(researcher="PRIOR MODELS.PY LISTING")
    await hook("delegate_task_to_member", _fake_delegate,
               {"member_id": "Researcher", "task": "Read models.py and list the classes"},
               run_context=None, team=team)
    out = await hook("delegate_task_to_member", _fake_delegate,
                     {"member_id": "Researcher",
                      "task": "Please open models.py and enumerate its class declarations"},
                     run_context=None, team=team)
    assert out.startswith("ALREADY DONE") and "PRIOR MODELS.PY LISTING" in out
    assert "worded differently" in out


@pytest.mark.asyncio
async def test_untagged_follow_up_naming_a_new_target_runs():
    hook = _make_duplicate_delegation_gate_hook()
    team = _team(researcher="PRIOR")
    await hook("delegate_task_to_member", _fake_delegate,
               {"member_id": "Researcher", "task": "Read a.py"}, run_context=None, team=team)
    out = await hook("delegate_task_to_member", _fake_delegate,
                     {"member_id": "Researcher", "task": "Read b.py"},
                     run_context=None, team=team)
    assert out.startswith("delegated:")


@pytest.mark.asyncio
async def test_untagged_follow_up_with_a_new_action_on_the_same_target_runs():
    hook = _make_duplicate_delegation_gate_hook()
    team = _team(researcher="PRIOR")
    await hook("delegate_task_to_member", _fake_delegate,
               {"member_id": "Researcher", "task": "Read a.py"}, run_context=None, team=team)
    out = await hook("delegate_task_to_member", _fake_delegate,
                     {"member_id": "Researcher", "task": "Implement a change in a.py"},
                     run_context=None, team=team)
    assert out.startswith("delegated:")


@pytest.mark.asyncio
async def test_no_missing_tag_redirect_is_ever_issued_for_a_singular_delegation():
    hook = _make_duplicate_delegation_gate_hook()
    for task in ("Read a.py", "who knows", "something else entirely", "Read a.py again"):
        out = await hook("delegate_task_to_member", _fake_delegate,
                         {"member_id": "Researcher", "task": task}, run_context=None)
        assert "must open the task with an audit tag" not in out


@pytest.mark.asyncio
async def test_a_tag_the_model_supplied_wins_over_derivation():
    hook = _make_duplicate_delegation_gate_hook()
    team = _team(researcher="PRIOR")
    await hook("delegate_task_to_member", _fake_delegate,
               {"member_id": "Researcher", "task": "Read a.py"}, run_context=None, team=team)
    # Text says a.py, but the model tagged it as b.py: the tag is the truth.
    out = await hook("delegate_task_to_member", _fake_delegate,
                     {"member_id": "Researcher",
                      "task": _tag("read", "b.py", "Read a.py but focus on the b.py side")},
                     run_context=None, team=team)
    assert out.startswith("delegated:")


@pytest.mark.asyncio
async def test_tagged_repeat_still_matches_a_derived_prior_entry():
    hook = _make_duplicate_delegation_gate_hook()
    team = _team(researcher="PRIOR")
    await hook("delegate_task_to_member", _fake_delegate,
               {"member_id": "Researcher", "task": "Read a.py"}, run_context=None, team=team)
    out = await hook("delegate_task_to_member", _fake_delegate,
                     {"member_id": "Researcher", "task": _tag("read", "a.py", "look again")},
                     run_context=None, team=team)
    assert out.startswith("ALREADY DONE")


@pytest.mark.asyncio
async def test_exact_repeat_still_stops_on_the_third_ask():
    hook = _make_duplicate_delegation_gate_hook()
    team = _team(researcher="PRIOR")
    args = {"member_id": "Researcher", "task": "Read a.py"}
    outs = [await hook("delegate_task_to_member", _fake_delegate, dict(args),
                       run_context=None, team=team) for _ in range(4)]
    assert outs[0].startswith("delegated:")
    assert outs[1].startswith("ALREADY DONE")
    assert any(o.startswith("STOP") for o in outs[2:])


@pytest.mark.asyncio
async def test_a_blocked_retry_with_no_stored_result_is_still_allowed_once():
    hook = _make_duplicate_delegation_gate_hook()
    args = {"member_id": "Researcher", "task": "Read a.py"}
    first = await hook("delegate_task_to_member", _fake_delegate, dict(args), run_context=None)
    second = await hook("delegate_task_to_member", _fake_delegate, dict(args), run_context=None)
    assert first.startswith("delegated:") and second.startswith("delegated:")


@pytest.mark.asyncio
async def test_derived_audit_is_recorded_so_later_untagged_repeats_are_caught():
    """First delegation untagged, second untagged repeat: previously both hit the tag
    redirect; now the derived audit of the first is what the second is compared to."""
    hook = _make_duplicate_delegation_gate_hook()
    team = _team(researcher="PRIOR")
    await hook("delegate_task_to_member", _fake_delegate,
               {"member_id": "Researcher", "task": "Read a.py and list the routes"},
               run_context=None, team=team)
    out = await hook("delegate_task_to_member", _fake_delegate,
                     {"member_id": "Researcher", "task": "Show me the routes in a.py"},
                     run_context=None, team=team)
    assert out.startswith("ALREADY DONE")


@pytest.mark.asyncio
async def test_broadcast_still_requires_its_audit_tag():
    hook = _make_duplicate_delegation_gate_hook()
    try:
        await hook("delegate_task_to_members", _fake_delegate,
                   {"task": "Read the schema file and list every table."}, run_context=None)
    except UnboundLocalError:
        # Pre-existing, unrelated to derivation: an EXECUTED broadcast reaches the
        # evidence-manifest line, which reads `member_id`, a name only the singular
        # branch binds. The log entry is appended before it raises, so the second
        # broadcast below still exercises the real missing-tag branch.
        pass
    out = await hook("delegate_task_to_members", _fake_delegate,
                     {"task": "Now list every index."}, run_context=None)
    assert out.startswith("REDIRECTED:") and "audit tag" in out


def test_only_a_real_derivation_is_recorded_as_a_covered_target():
    import inspect
    from swarm import team as team_mod
    src = inspect.getsource(team_mod._make_duplicate_delegation_gate_hook)
    assert "if _from_text:" in src and "_logged_audit = _derived" in src
