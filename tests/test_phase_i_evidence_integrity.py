"""Phase I: evidence integrity + evidence-grounded recovery.

Root-caused against the live T13b ZGX validation of Phase H (docs/runbooks/
groundedness-battery.md): a member's report CITED
Client/.../inventoryApi.ts and invented `getVoucherById` from pattern-matching,
but the run's own telemetry showed it never actually called get_file_content on
that file (read_chars_this_delegation=0, cited_not_read=[".../inventoryApi.ts"]).
When Phase H's repair attempt correctly tried to re-delegate a REAL read of that
file, the duplicate-delegation gate blocked it — its notion of "already done" is
a target/action text match with no awareness of whether the prior delegation
actually produced grounded evidence.

Core invariant under test:

    cited_not_read -> corrective read permitted exactly once -> actual read
    establishes grounded evidence -> subsequent duplicate protection restored

and its mirror:

    cited_not_read -> no corrective read possible / corrective read fails ->
    no fabricated evidence -> safe fallback (VERIFICATION FAILED, unchanged)

Test style matches tests/test_duplicate_delegation_gate_hook.py: a fresh hook per
test, sequential calls through the SAME hook instance (its closure IS the run-scoped
state), team as a bare SimpleNamespace.
"""
import asyncio
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _RepairFinding,
    _attempt_evidence_grounded_reconstruction,
    _coverage_verdict,
    _make_duplicate_delegation_gate_hook,
    _member_actually_read_target,
)


async def _fake_delegate(**kwargs):
    return f"delegated: {kwargs}"


def _read_state(reads):
    return {"reads": reads}


# ── _member_actually_read_target: pure evidence check ────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_target_actually_read_matches_exact_path():
    team = SimpleNamespace(_read_state=_read_state([
        {"tool": "get_file_content", "read_by": "researcher",
         "path": "API/inventory-service/router/vouchers_api.py", "chars": 43457},
    ]))
    assert _member_actually_read_target(
        team, "researcher", "API/inventory-service/router/vouchers_api.py") is True


def test_target_never_read_is_false():
    team = SimpleNamespace(_read_state=_read_state([
        {"tool": "get_file_content", "read_by": "researcher",
         "path": "API/inventory-service/router/vouchers_api.py", "chars": 43457},
    ]))
    assert _member_actually_read_target(
        team, "researcher",
        "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts"
    ) is False


def test_target_read_by_a_different_member_does_not_count():
    """Evidence is per-member -- the fact that Coder read a file does not make it
    grounded evidence for Researcher's own citation of it."""
    team = SimpleNamespace(_read_state=_read_state([
        {"tool": "get_file_content", "read_by": "coder",
         "path": "API/inventory-service/models.py", "chars": 1000},
    ]))
    assert _member_actually_read_target(
        team, "researcher", "API/inventory-service/models.py") is False


def test_member_key_normalization_matches_across_spellings():
    team = SimpleNamespace(_read_state=_read_state([
        {"tool": "get_file_content", "read_by": "context-router",
         "path": "x.py", "chars": 10},
    ]))
    assert _member_actually_read_target(team, "ContextRouter", "x.py") is True


def test_case_insensitive_and_backslash_normalized_like_audit_targets():
    team = SimpleNamespace(_read_state=_read_state([
        {"tool": "get_file_content", "read_by": "researcher",
         "path": "API\\Inventory-Service\\Models.py", "chars": 10},
    ]))
    assert _member_actually_read_target(
        team, "researcher", "api/inventory-service/models.py") is True


def test_no_read_state_is_false_not_a_crash():
    assert _member_actually_read_target(SimpleNamespace(), "researcher", "x.py") is False
    assert _member_actually_read_target(None, "researcher", "x.py") is False


def test_empty_target_is_false():
    team = SimpleNamespace(_read_state=_read_state([
        {"tool": "get_file_content", "read_by": "researcher", "path": "x.py", "chars": 10},
    ]))
    assert _member_actually_read_target(team, "researcher", "") is False


def test_search_tool_calls_do_not_count_as_a_read_of_the_searched_path():
    """search_files finding a path is not the same as get_file_content opening
    it -- the path field is populated for both (glob_pattern/pattern vs
    relative_path), and this helper does not distinguish by tool name, matching
    the existing read_state schema exactly as team.py already records it. This
    test documents that behavior rather than asserting a stronger guarantee."""
    team = SimpleNamespace(_read_state=_read_state([
        {"tool": "search_files", "read_by": "researcher",
         "path": "**/vouchers_api.py", "chars": 500},
    ]))
    # Exact-path match only -- a glob pattern is not a real path, so this is False,
    # which is the conservative (never over-credit) direction.
    assert _member_actually_read_target(
        team, "researcher", "API/inventory-service/router/vouchers_api.py") is False


# ── Gate: normal duplicate behavior is UNCHANGED when evidence is grounded ──────

async def test_A_prior_delegation_genuinely_read_file_duplicate_remains_blocked():
    """Test A from the phase spec: prior delegation actually read the file ->
    duplicate protection behaves exactly as before this phase."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(
        _member_results={"researcher": "The real content of x.md"},
        _read_state=_read_state([
            {"tool": "get_file_content", "read_by": "researcher",
             "path": "x.md", "chars": 500},
        ]),
    )
    args = {"member_id": "researcher", "task": "Read x.md and summarize it"}

    first = await hook("delegate_task_to_member", _fake_delegate, args,
                        run_context=None, team=team)
    second = await hook("delegate_task_to_member", _fake_delegate, args,
                         run_context=None, team=team)

    assert first.startswith("delegated:")
    assert second.startswith("ALREADY DONE")


# ── Gate: the new exception, exact-text tier ─────────────────────────────────

async def test_B_prior_delegation_cited_but_did_not_read_one_corrective_read_allowed():
    """Test B from the phase spec, exact-text tier: the SAME task text asked
    twice, but the member's stored result never actually read the target this
    run's own read_state shows nothing for -- the second, identical ask must be
    let through as a corrective re-read, not served ALREADY DONE."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(
        # A result WAS captured (this is not the "no prior result" case) --
        # but read_state shows the target was never actually opened.
        _member_results={"researcher": "getVouchers, getVoucherById, createVoucher..."},
        _read_state=_read_state([]),
    )
    args = {"member_id": "researcher",
            "task": "Read Client/.../inventoryApi.ts and return every function."}

    first = await hook("delegate_task_to_member", _fake_delegate, args,
                        run_context=None, team=team)
    second = await hook("delegate_task_to_member", _fake_delegate, args,
                         run_context=None, team=team)

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")  # corrective read allowed, not blocked


async def test_evidence_defect_exception_fires_on_the_reworded_tier_t13b_shape():
    """The actual live T13b shape: the corrective delegation's wording differs
    from the original (a repair prompt necessarily rewords it), so this hits the
    REWORDED tier, not the exact-text one -- confirmed against the real ZGX
    journal's own STOP message format ("...for this same target...")."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(
        _member_results={"researcher": "getVouchers, getVoucherById, createVoucher..."},
        _read_state=_read_state([]),
    )
    original = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher",
         "task": "Read Client/.../inventoryApi.ts and return every function that "
                 "calls a voucher-related endpoint, verbatim."},
        run_context=None, team=team)
    corrective = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher",
         "task": "<delegation_audit>component=vouchers; action=read; "
                 "target=Client/.../inventoryApi.ts</delegation_audit>\n"
                 "Read Client/.../inventoryApi.ts and return every function "
                 "declaration that contains 'voucher' in its name."},
        run_context=None, team=team)

    assert original.startswith("delegated:")
    assert corrective.startswith("delegated:")


async def test_C_corrective_read_also_fails_no_unlimited_retries():
    """Test C from the phase spec: the corrective read is granted once, but its
    OWN result still shows no real read of the target (e.g. it also only cited
    it) -- a THIRD ask for the same target must be blocked normally, not granted
    a second exception."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(
        _member_results={"researcher": "still just citing inventoryApi.ts"},
        _read_state=_read_state([]),  # still empty -- the "corrective" call also
                                       # never actually read anything
    )
    args = {"member_id": "researcher",
            "task": "Read Client/.../inventoryApi.ts and return every function."}

    first = await hook("delegate_task_to_member", _fake_delegate, args,
                        run_context=None, team=team)
    second = await hook("delegate_task_to_member", _fake_delegate, args,
                         run_context=None, team=team)   # corrective exception spent
    third = await hook("delegate_task_to_member", _fake_delegate, args,
                        run_context=None, team=team)    # must be blocked normally

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")
    assert third.startswith("ALREADY DONE")


async def test_D_multiple_repeated_corrective_attempts_are_bounded():
    """Test D from the phase spec: hammering the same target many times never
    yields more than one corrective grant -- every ask past the second is a
    normal duplicate block (and the pre-existing 3-strike STOP ceiling still
    applies on top, since `repeats` keeps incrementing)."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(
        _member_results={"researcher": "still just citing inventoryApi.ts"},
        _read_state=_read_state([]),
    )
    args = {"member_id": "researcher",
            "task": "Read Client/.../inventoryApi.ts and return every function."}

    results = [await hook("delegate_task_to_member", _fake_delegate, args,
                           run_context=None, team=team) for _ in range(5)]

    assert results[0].startswith("delegated:")
    assert results[1].startswith("delegated:")  # the one corrective grant
    # Every subsequent ask is a normal block/escalation, never another grant.
    for r in results[2:]:
        assert not r.startswith("delegated:")


async def test_exception_is_spent_once_a_real_read_follow_up_lands():
    """The core invariant's second half: once the corrective read actually
    succeeds (read_state now shows the target as read), a LATER third ask for
    the same target is treated as fully grounded -- normal ALREADY DONE with the
    real result served, not another corrective grant and not a hard block."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(
        _member_results={"researcher": "cited only, not read yet"},
        _read_state=_read_state([]),
    )
    args = {"member_id": "researcher",
            "task": "Read Client/.../inventoryApi.ts and return every function."}

    first = await hook("delegate_task_to_member", _fake_delegate, args,
                        run_context=None, team=team)
    second = await hook("delegate_task_to_member", _fake_delegate, args,
                         run_context=None, team=team)   # corrective grant
    assert first.startswith("delegated:")
    assert second.startswith("delegated:")

    # Simulate the corrective delegation actually reading the file this time,
    # and updating the stored result to real, grounded content.
    team._read_state["reads"].append({
        "tool": "get_file_content", "read_by": "researcher",
        "path": "Client/.../inventoryApi.ts", "chars": 900})
    team._member_results["researcher"] = "getVouchers, createVoucher, postVoucher (real)"

    third = await hook("delegate_task_to_member", _fake_delegate, args,
                        run_context=None, team=team)
    assert third.startswith("ALREADY DONE")
    assert "real" in third


# ── Gate: exception scope is narrow — different target/action unaffected ────

async def test_E_different_target_normal_behavior():
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={}, _read_state=_read_state([]))

    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher", "task": "Read a.py and summarize it"},
        run_context=None, team=team)
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher", "task": "Read b.py and summarize it"},
        run_context=None, team=team)

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")  # never touched the gate's dedupe tiers


async def test_F_different_action_preserves_existing_semantics():
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(
        _member_results={"researcher": "real content"},
        _read_state=_read_state([
            {"tool": "get_file_content", "read_by": "researcher",
             "path": "x.py", "chars": 500}]),
    )
    first = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher",
         "task": "<delegation_audit>component=x; action=read; "
                 "target=x.py</delegation_audit>\nRead x.py"},
        run_context=None, team=team)
    second = await hook(
        "delegate_task_to_member", _fake_delegate,
        {"member_id": "researcher",
         "task": "<delegation_audit>component=x; action=verify; "
                 "target=x.py</delegation_audit>\nVerify x.py against the spec"},
        run_context=None, team=team)

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")  # different action -- not a duplicate at all


async def test_no_prior_result_exception_still_takes_priority_over_evidence_check():
    """The PRE-EXISTING exception (no stored result at all -- a discarded
    tool-syntax report) must still work exactly as before: this phase's new
    check only applies when a result WAS captured. Regression guard for that
    ordering."""
    hook = _make_duplicate_delegation_gate_hook()
    team = SimpleNamespace(_member_results={}, _read_state=_read_state([]))
    args = {"member_id": "researcher", "task": "Read x.md and summarize it"}

    first = await hook("delegate_task_to_member", _fake_delegate, args,
                        run_context=None, team=team)
    second = await hook("delegate_task_to_member", _fake_delegate, args,
                         run_context=None, team=team)

    assert first.startswith("delegated:")
    assert second.startswith("delegated:")


# ── _coverage_verdict: pure normalizer over existing signals ─────────────────

def test_coverage_all_covered_is_complete():
    verdict, unresolved = _coverage_verdict({"routers": (16, 16), "models": (31, 31)})
    assert verdict == "complete"
    assert unresolved == []


def test_coverage_count_mismatch_evidence_16_answer_13_is_incomplete():
    """T12's real live shape: expected 16, reported 13."""
    verdict, unresolved = _coverage_verdict({"routers": (13, 16)})
    assert verdict == "incomplete"
    assert unresolved == ["routers"]


def test_coverage_one_short_of_expected_is_incomplete():
    verdict, _ = _coverage_verdict({"models": (30, 31)})
    assert verdict == "incomplete"


def test_coverage_missing_multiple_items_is_incomplete():
    verdict, unresolved = _coverage_verdict({"routers": (4, 16)})
    assert verdict == "incomplete"
    assert "routers" in unresolved


def test_coverage_missing_an_entire_subpart_is_incomplete():
    """T12's real live shape: models 0/31, integration missing entirely."""
    verdict, unresolved = _coverage_verdict({
        "routers": (13, 16), "models": (0, 31), "integration": False,
    })
    assert verdict == "incomplete"
    assert set(unresolved) == {"routers", "models", "integration"}


def test_coverage_multi_part_one_answered_rest_omitted_is_not_complete():
    verdict, unresolved = _coverage_verdict({
        "routers": (16, 16), "models": (0, 31), "integration": False,
        "dependencies": None,
    })
    assert verdict != "complete"
    assert "routers" not in unresolved


def test_coverage_unknown_is_never_complete():
    verdict, unresolved = _coverage_verdict({"integration": None})
    assert verdict == "unknown"
    assert verdict != "complete"
    assert unresolved == ["integration"]


def test_coverage_incomplete_outranks_unknown_when_both_present():
    verdict, unresolved = _coverage_verdict({
        "routers": (13, 16), "dependencies": None,
    })
    assert verdict == "incomplete"
    assert unresolved == ["routers"]  # the confirmed gap, not the undetermined one


def test_coverage_empty_input_is_unknown_not_complete():
    verdict, unresolved = _coverage_verdict({})
    assert verdict == "unknown"
    assert unresolved == []


def test_coverage_boolean_true_component_counts_as_covered():
    verdict, _ = _coverage_verdict({"integration": True})
    assert verdict == "complete"


def test_coverage_zero_expected_is_unknown_not_complete():
    """An expected count of 0 or None means the denominator itself could not be
    established -- never silently treated as '0 needed, 0 found, complete'."""
    verdict, unresolved = _coverage_verdict({"routers": (0, 0)})
    assert verdict == "unknown"
    assert unresolved == ["routers"]


# ── _attempt_evidence_grounded_reconstruction ────────────────────────────────

class _Team:
    """Bare stand-in, matching this file's other tests' own convention."""


@pytest.mark.asyncio
async def test_t10_style_non_verification_failed_content_is_never_touched():
    """T10's own real result: bad=False, no repair, no reconstruction. This
    function must be a complete no-op on ordinary content -- it never even looks
    at read_state or calls compare_enumerations for a normal answer."""
    calls = {"n": 0}

    async def fail_if_called(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not be called for non-VERIFICATION-FAILED content")
    import swarm.team as tm
    orig = tm._computed_comparison
    tm._computed_comparison = fail_if_called
    try:
        out = await _attempt_evidence_grounded_reconstruction(
            "There is no rate-limiting middleware in the authentication service.",
            "is there a rate-limiting middleware?", _Team(),
            "http://fake-hive-mcp", None, None)
    finally:
        tm._computed_comparison = orig
    assert calls["n"] == 0
    assert out == "There is no rate-limiting middleware in the authentication service."


@pytest.mark.asyncio
async def test_t13b_style_successful_reconstruction_returns_the_correct_answer(
        monkeypatch):
    """The phase spec's own desired T13b outcome: a real computed comparison
    exists (the corrective read succeeded), the reconstruction states exactly
    that comparison's gaps, and final verification passes -> the correct answer
    is released instead of VERIFICATION FAILED."""
    failed_content = (
        "**VERIFICATION FAILED — this answer could not be confirmed against the "
        "repository and is not being returned as fact.** Verification "
        "(verify_claims) flagged: ...\n\n---\n**The flagged draft, kept below for "
        "tracing only — do not treat anything in it as confirmed:**\n"
        "0 gaps (fabricated)")
    real_gap_note = (
        "\n\n---\n**THE COMPARISON, COMPUTED**\n```\n"
        "compare_enumerations — vouchers_api.py vs inventoryApi.ts\n"
        "LEFT ONLY (4):\n  grn\n  credit-note\n  stock-adjustment\n  stock-transfer\n"
        "TOTALS: left 9, right 5, matched 5, left-only 4, right-only 0.\n```")

    async def fake_computed_comparison(task, enumerations, hive_mcp_url,
                                        hive_mcp_tools, content, team=None):
        assert enumerations == {"both": "sides"}
        return real_gap_note
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_computed_comparison)

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        assert "grn" in prompt and "credit-note" in prompt
        assert "LEFT ONLY" in prompt
        return ("4 gaps: grn, credit-note, stock-adjustment, stock-transfer",
                None)
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def clean_verify(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", clean_verify)

    team = SimpleNamespace(_read_state={"enumerations": {"both": "sides"}})
    out = await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", team,
        "http://fake-hive-mcp", None, None)

    assert out == "4 gaps: grn, credit-note, stock-adjustment, stock-transfer"
    assert "VERIFICATION FAILED" not in out


@pytest.mark.asyncio
async def test_no_computed_comparison_available_keeps_verification_failed(monkeypatch):
    """compare_enumerations' own "no safe pair" refusal (untouched by this
    phase) means there is nothing to reconstruct from -- the original
    VERIFICATION FAILED result must ship unchanged, not a second attempt."""
    failed_content = "**VERIFICATION FAILED — ...**"

    async def no_comparison(*a, **k):
        return ""  # compare_enumerations declined -- e.g. "no safe pair"
    monkeypatch.setattr(team_mod, "_computed_comparison", no_comparison)

    calls = {"n": 0}

    async def fail_if_called(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not attempt a reconstruction with no evidence")
    monkeypatch.setattr(team_mod, "_stream_team_run", fail_if_called)

    out = await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", SimpleNamespace(),
        "http://fake-hive-mcp", None, None)

    assert out == failed_content
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_reconstruction_that_fails_verification_keeps_verification_failed(
        monkeypatch):
    """Never weaken the final guard to make T13b 'pass' -- a reconstruction that
    itself is not grounded must not ship either."""
    failed_content = "**VERIFICATION FAILED — original**"

    async def fake_comparison(*a, **k):
        return "\n\n---\nLEFT ONLY (4): grn, credit-note, stock-adjustment, stock-transfer"
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_comparison)

    async def fake_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return "invented a fifth gap: getVoucherByIdMutation (not real)", None
    monkeypatch.setattr(team_mod, "_stream_team_run", fake_stream)

    async def still_bad_verify(content, hive_mcp_url, hive_mcp_tools):
        return "NOT FOUND getVoucherByIdMutation", True, False
    monkeypatch.setattr(team_mod, "_verify_claims", still_bad_verify)

    out = await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", SimpleNamespace(),
        "http://fake-hive-mcp", None, None)

    assert out == failed_content  # unchanged -- the original VERIFICATION FAILED ships


@pytest.mark.asyncio
async def test_reconstruction_that_raises_keeps_verification_failed(monkeypatch):
    failed_content = "**VERIFICATION FAILED — original**"

    async def fake_comparison(*a, **k):
        return "\n\n---\nLEFT ONLY (4): grn, credit-note, stock-adjustment, stock-transfer"
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_comparison)

    async def raising_stream(*a, **k):
        raise RuntimeError("model unreachable")
    monkeypatch.setattr(team_mod, "_stream_team_run", raising_stream)

    out = await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", SimpleNamespace(),
        "http://fake-hive-mcp", None, None)

    assert out == failed_content


@pytest.mark.asyncio
async def test_reconstruction_is_bounded_to_exactly_one_attempt(monkeypatch):
    calls = {"n": 0}

    async def fake_comparison(*a, **k):
        return "\n\n---\nLEFT ONLY (4): grn, credit-note, stock-adjustment, stock-transfer"
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_comparison)

    async def counting_stream(*a, **k):
        calls["n"] += 1
        return "still wrong", None
    monkeypatch.setattr(team_mod, "_stream_team_run", counting_stream)

    async def still_bad_verify(content, hive_mcp_url, hive_mcp_tools):
        return "NOT FOUND x", True, False
    monkeypatch.setattr(team_mod, "_verify_claims", still_bad_verify)

    failed_content = "**VERIFICATION FAILED — original**"
    team = SimpleNamespace()
    await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", team,
        "http://fake-hive-mcp", None, None)
    # Second call this run (e.g. a re-entrant guard) must not attempt a second
    # live reconstruction even if content is still VERIFICATION FAILED-shaped.
    await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", team,
        "http://fake-hive-mcp", None, None)

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_reconstruction_is_never_folded_into_phase_h_repair_budget():
    """Structural check: reconstruction has its OWN flag, entirely separate
    from Phase H's _REPAIR_ATTEMPTED_FLAG -- confirms the two stages are
    independently bounded, not one budget shared across both, per this phase's
    own explicit 'two distinct, separately-bounded stages' requirement."""
    from swarm.team import _RECONSTRUCTION_ATTEMPTED_FLAG, _REPAIR_ATTEMPTED_FLAG
    assert _RECONSTRUCTION_ATTEMPTED_FLAG != _REPAIR_ATTEMPTED_FLAG
