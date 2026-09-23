"""Phase F: evidence-gated delegation.

Problem (T3, 2026-09-21 post-deployment battery): "Trace it from the API route to the
database model" -- both endpoints were found by the 3rd delegation (the route at #2, the
model at #3), and the run then made 7 MORE delegations, each naming a genuinely different,
task-unstated model or enum. None of them was an exact or reworded repeat of #1-3, so the
duplicate-delegation gate (C) correctly let every one of them through -- a different,
complementary mechanism was needed, not a stricter version of C.

_make_evidence_gate_hook recognises ONE task shape mechanically ("from X to Y", exactly
T3's wording) and, once that many distinct evidence targets have been covered, blocks a
delegation for any further, task-unstated target. Any task NOT matching that shape is
"coverage undetermined" and is never blocked -- deliberately conservative, no generalised
task-understanding.
"""
from types import SimpleNamespace

import pytest

from swarm.team import (
    _EVIDENCE_ENTITY_RE, _evidence_gate_audit, _make_duplicate_delegation_gate_hook,
    _make_evidence_gate_hook, _required_coverage_count,
)

TRACE_TASK = ("How does seller verification work in this codebase? Trace it from the API "
              "route to the database model.")

# The real Researcher/Coordinator delegation task strings from T3's actual live run
# (2026-09-21 battery), verbatim.
T3_REAL_DELEGATIONS = [
    "Find all files related to seller verification in the codebase and return their paths.",
    "Read the business_admin_api.py file and identify the API route for seller verification.",
    "Find and read the BusinessProfile model definition and its relationship to seller verification.",
    "Find and read the VerificationCheck model definition and its role in seller verification.",
    "Find and read the BusinessDocument model definition and its role in seller verification.",
    "Find and read the BusinessAuditEvent model definition and its role in seller verification.",
    "Find and read the AdminVerifyRequest and AdminVerifyAction enum definitions and their role in seller verification.",
    "Find and read the BusinessProfileStatus enum definition and its role in seller verification.",
    "Find and read the BusinessProfile model's relationships and their role in seller verification.",
    "Find and read the BusinessAddress, BankDetails, and ONDCConfig model definitions and their role in seller verification.",
]


async def _delegate(**kwargs):
    return f"delegated: {kwargs}"


async def _call(hook, member_id, task_text, team=None):
    return await hook("delegate_task_to_member", _delegate,
                      {"member_id": member_id, "task": task_text}, run_context=None, team=team)


def _tag(action, target):
    return f"<delegation_audit>component=svc; action={action}; target={target}</delegation_audit>"


# ── 1. insufficient evidence -> delegation executes ──────────────────────────────────────

@pytest.mark.asyncio
async def test_first_delegation_toward_a_two_endpoint_task_always_executes():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    out = await _call(hook, "researcher", "Read the business_admin_api.py file for the route.")
    assert out.startswith("delegated:")


@pytest.mark.asyncio
async def test_one_of_two_required_endpoints_covered_is_still_insufficient():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    await _call(hook, "researcher", "Read the business_admin_api.py file for the route.")
    out = await _call(hook, "researcher", "Find and read the BusinessProfile model definition.")
    assert out.startswith("delegated:")


@pytest.mark.asyncio
async def test_a_task_with_no_recognisable_shape_never_blocks_anything():
    hook = _make_evidence_gate_hook(task="Audit the vouchers module thoroughly.")
    for i in range(6):
        out = await _call(hook, "researcher", f"Find and read the Entity{i} model definition.")
        assert out.startswith("delegated:"), i


@pytest.mark.asyncio
async def test_no_task_text_at_all_never_blocks():
    hook = _make_evidence_gate_hook(task=None)
    out = await _call(hook, "researcher", "Find and read the Foo model definition.")
    assert out.startswith("delegated:")


# ── 2. sufficient evidence -> delegation blocked ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_third_unstated_target_is_blocked_once_both_endpoints_are_covered():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    await _call(hook, "researcher", "Read the business_admin_api.py file for the route.")
    await _call(hook, "researcher", "Find and read the BusinessProfile model definition.")
    out = await _call(hook, "researcher", "Find and read the VerificationCheck model definition.")
    assert out.startswith("DELEGATION_BLOCKED")
    assert "reason=EVIDENCE_SUFFICIENT" in out


@pytest.mark.asyncio
async def test_blocked_call_never_actually_invokes_the_real_delegate_function():
    calls = []

    async def _tracking_delegate(**kwargs):
        calls.append(kwargs)
        return "should not run"

    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    await hook("delegate_task_to_member", _tracking_delegate,
               {"member_id": "researcher", "task": "Read the business_admin_api.py file."},
               run_context=None, team=None)
    await hook("delegate_task_to_member", _tracking_delegate,
               {"member_id": "researcher", "task": "Find and read the BusinessProfile model definition."},
               run_context=None, team=None)
    calls.clear()
    await hook("delegate_task_to_member", _tracking_delegate,
               {"member_id": "researcher", "task": "Find and read the VerificationCheck model definition."},
               run_context=None, team=None)
    assert calls == []


# ── 3. unresolved required coverage -> delegation allowed ───────────────────────────────

def test_required_coverage_count_is_none_for_an_unrecognised_task():
    assert _required_coverage_count("Audit the vouchers module.") is None
    assert _required_coverage_count("List every Python file in the router directory.") is None
    assert _required_coverage_count(None) is None


def test_required_coverage_count_is_two_for_the_from_x_to_y_shape():
    assert _required_coverage_count(TRACE_TASK) == 2
    assert _required_coverage_count(
        "Trace the request from the frontend hook to the SQL migration.") == 2


@pytest.mark.asyncio
async def test_unresolved_coverage_allows_many_delegations_with_no_block():
    hook = _make_evidence_gate_hook(task="Write a detailed overview. Be thorough.")
    for i in range(8):
        out = await _call(hook, "researcher", f"Find and read the Entity{i} model definition.")
        assert out.startswith("delegated:"), i


# ── 4. repeated delegation after sufficient evidence -> blocked ─────────────────────────

@pytest.mark.asyncio
async def test_every_subsequent_new_target_stays_blocked_not_just_the_first_one():
    """Unlike decompose-first's one-time nudge, this must keep blocking for the rest of
    the run -- T3 made 7 MORE delegations after sufficiency, not just one."""
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    await _call(hook, "researcher", "Read the business_admin_api.py file for the route.")
    await _call(hook, "researcher", "Find and read the BusinessProfile model definition.")
    for i in range(5):
        out = await _call(hook, "researcher", f"Find and read the Entity{i} model definition.")
        assert out.startswith("DELEGATION_BLOCKED"), i


@pytest.mark.asyncio
async def test_the_real_t3_trajectory_blocks_exactly_the_five_classifiable_extra_targets():
    """The exact historical run this phase exists for, replayed verbatim. #1-3 execute
    (discovery, then the two required endpoints). #4-8 (five genuinely new, classifiable
    targets) are blocked. #9 re-names an already-covered target (BusinessProfile again) --
    not F's job, executes. #10 names a 3-item list this gate's narrow pattern does not
    parse -- unclassified, safe default, executes."""
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    outcomes = []
    for task_text in T3_REAL_DELEGATIONS:
        out = await _call(hook, "researcher", task_text)
        outcomes.append(out.startswith("delegated:"))
    assert outcomes == [True, True, True, False, False, False, False, False, True, True]


# ── 5. explicit model delegation tag does not bypass evidence sufficiency ───────────────

@pytest.mark.asyncio
async def test_an_explicit_tag_naming_a_new_target_is_still_blocked_once_sufficient():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    await _call(hook, "researcher", _tag("read", "a.py") + " Read a.py for the route.")
    await _call(hook, "researcher", _tag("read", "b.py") + " Read b.py for the model.")
    out = await _call(hook, "researcher", _tag("read", "c.py") + " Read c.py, a third file.")
    assert out.startswith("DELEGATION_BLOCKED")


@pytest.mark.asyncio
async def test_explicit_tags_are_still_used_for_target_identity_when_not_yet_sufficient():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    out1 = await _call(hook, "researcher", _tag("read", "a.py") + " Read a.py.")
    out2 = await _call(hook, "researcher", _tag("read", "b.py") + " Read b.py.")
    assert out1.startswith("delegated:") and out2.startswith("delegated:")


def test_evidence_gate_audit_prefers_an_explicit_tag_over_the_entity_pattern():
    action, target = _evidence_gate_audit(
        _tag("read", "override.py") + " Find and read the BusinessProfile model definition.")
    assert (action, target) == ("read", "override.py")


# ── 6. existing C behavior remains intact ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_delegation_gate_still_serves_already_done_for_an_exact_repeat():
    """C's own exact-repeat handling, run through UNCHANGED and unaffected by F existing
    as a separate hook."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={"researcher": "PRIOR"})
    args = {"member_id": "researcher", "task": "Read a.py"}
    first = await hook("delegate_task_to_member", _delegate, dict(args), run_context=None, team=team)
    second = await hook("delegate_task_to_member", _delegate, dict(args), run_context=None, team=team)
    assert first.startswith("delegated:") and second.startswith("ALREADY DONE")


@pytest.mark.asyncio
async def test_f_and_c_both_run_in_the_real_hook_chain_without_conflicting():
    """The two hooks are independent closures over the SAME call sequence -- both must see
    every call and neither must short-circuit the other's own bookkeeping. Simulated by
    chaining them exactly as _build_team's tool_hooks list does (F wraps C wraps the
    delegate function)."""
    f_hook = _make_evidence_gate_hook(task=TRACE_TASK)
    c_hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={})

    async def chained(function_name, function, args, run_context=None, team=None):
        async def inner(**a):
            return await c_hook(function_name, function, a, run_context=run_context, team=team)
        return await f_hook(function_name, inner, args, run_context=run_context, team=team)

    out1 = await chained("delegate_task_to_member", _delegate,
                         {"member_id": "researcher", "task": "Read the business_admin_api.py file."},
                         team=team)
    team._member_results["researcher"] = "PRIOR route result"
    out2 = await chained("delegate_task_to_member", _delegate,
                         {"member_id": "researcher", "task": "Find and read the BusinessProfile model definition."},
                         team=team)
    team._member_results["researcher"] = "PRIOR model result"
    # C still catches an EXACT repeat of the first call, through the chain
    out3 = await chained("delegate_task_to_member", _delegate,
                         {"member_id": "researcher", "task": "Read the business_admin_api.py file."},
                         team=team)
    # F still blocks a genuinely new target once sufficient, through the chain
    out4 = await chained("delegate_task_to_member", _delegate,
                         {"member_id": "researcher", "task": "Find and read the VerificationCheck model definition."},
                         team=team)
    assert out1.startswith("delegated:") and out2.startswith("delegated:")
    assert out3.startswith("ALREADY DONE")          # C's territory
    assert out4.startswith("DELEGATION_BLOCKED")     # F's territory


@pytest.mark.asyncio
async def test_broadcast_delegation_is_never_touched_by_this_gate():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    out = await hook("delegate_task_to_members", _delegate,
                     {"task": "Read the business_admin_api.py file."}, run_context=None, team=None)
    assert out.startswith("delegated:")


@pytest.mark.asyncio
async def test_implement_and_plan_actions_are_never_blocked_by_this_gate():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    await _call(hook, "researcher", "Read the business_admin_api.py file for the route.")
    await _call(hook, "researcher", "Find and read the BusinessProfile model definition.")
    out = await _call(hook, "researcher", _tag("implement", "new_feature.py") + " Implement a fix.")
    assert out.startswith("delegated:")


@pytest.mark.asyncio
async def test_verify_action_against_an_already_covered_target_is_never_blocked():
    hook = _make_evidence_gate_hook(task=TRACE_TASK)
    await _call(hook, "researcher", "Read the business_admin_api.py file for the route.")
    await _call(hook, "researcher", "Find and read the BusinessProfile model definition.")
    out = await _call(hook, "researcher", _tag("verify", "business_admin_api.py") + " Double-check the route.")
    assert out.startswith("delegated:")


# ── entity-pattern extractor itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Find and read the BusinessProfile model definition.", "BusinessProfile"),
    ("Find and read the VerificationCheck model definition.", "VerificationCheck"),
    ("Find and read the AdminVerifyRequest and AdminVerifyAction enum definitions.", "AdminVerifyRequest"),
    ("Find and read the BusinessProfileStatus enum definition.", "BusinessProfileStatus"),
])
def test_entity_pattern_matches_the_real_t3_phrasing(text, expected):
    m = _EVIDENCE_ENTITY_RE.search(text)
    assert m and m.group(1) == expected


def test_entity_pattern_does_not_match_a_three_item_list():
    assert _EVIDENCE_ENTITY_RE.search(
        "Find and read the BusinessAddress, BankDetails, and ONDCConfig model definitions.") is None


def test_entity_pattern_requires_the_model_class_enum_table_schema_suffix():
    assert _EVIDENCE_ENTITY_RE.search("The BusinessProfile handles verification.") is None


def test_evidence_gate_audit_rejects_an_entity_mention_with_no_gathering_verb():
    assert _evidence_gate_audit(
        "Plan how the BusinessProfile model should change for phase 2.") is None
