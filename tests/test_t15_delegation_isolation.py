"""Phase T15 -- Researcher cross-delegation context contamination, root-cause fix.

Root cause (proven by direct source read of the INSTALLED agno==2.5.17 package, not
swarm/team.py): `agno.utils.team.get_team_member_interactions_str` builds a block of
text literally introduced as "See below interactions with OTHER team members" and
splices it into every subsequent delegation's task text via `format_member_agent_task`
-- completely independent of `add_history_to_context`, `session_state`, and this
codebase's own `_build_canonical_researcher_task`. The original implementation never
actually excludes the CURRENT delegation's own target: `team_run_context["member_
responses"]` is forwarded unfiltered by role, so a member delegated to twice sees its
OWN earlier, unrelated task+response folded into its new one. Live-traced in the T11
revalidation run (2026-10-04, 18:32:53-18:36:24): a delegation to Researcher
regenerated, verbatim and with zero intervening tool call, Researcher's own output
from two earlier, unrelated delegations.

Phase C.3 (2026-10-03, already deployed) bounded this to the 3 most recent
interactions -- fixing a DIFFERENT problem (unbounded token growth over many
delegations, e.g. T12's 110K-token run). It does not exclude same-role interactions,
so a role still sees its own prior output whenever that count is within the bound
(2 of 2, in the T11 case). This phase's fix is additive to C.3, not a replacement:
self-interactions are dropped BEFORE the count bound is applied.

These tests exercise the real, installed `agno.utils.team.add_interaction_to_
team_run_context` (not a fake) together with this codebase's own `_bounded_get_
team_member_interactions_str`, so a regression in either side is caught.
"""
from types import SimpleNamespace

import pytest

from agno.utils.team import add_interaction_to_team_run_context

from swarm.team import (
    _bounded_get_team_member_interactions_str,
    _install_bounded_member_interactions,
    _member_key,
    _ORIGINAL_AGNO_GET_TEAM_MEMBER_INTERACTIONS_STR,
)


def _fake_run_response(content: str):
    """Minimal stand-in for agno's RunOutput -- get_team_member_interactions_str
    only ever calls `.to_dict()` and reads `content`/`tools` off the result."""
    return SimpleNamespace(to_dict=lambda: {"content": content, "tools": []})


def _context_targeting(member_id: str) -> dict:
    """A fresh team_run_context, as if delegate_structured_task had just marked
    `member_id` as the upcoming delegation's target (the real write site)."""
    return {"_t15_current_target_member_id": _member_key(member_id)}


@pytest.fixture(autouse=True)
def _ensure_patch_installed():
    """The module-level `_install_bounded_member_interactions()` call already runs
    at import time in production; re-running it here is idempotent (it checks the
    patch marker before installing) and guards against test order/isolation making
    `_ORIGINAL_AGNO_GET_TEAM_MEMBER_INTERACTIONS_STR` None in a fresh interpreter."""
    _install_bounded_member_interactions()
    assert _ORIGINAL_AGNO_GET_TEAM_MEMBER_INTERACTIONS_STR is not None


class TestT15_1FreshDelegationIsolation:
    """Delegation B cannot see A's generated output, when B targets the SAME role A did."""

    def test_sentinel_from_earlier_same_role_delegation_is_excluded(self):
        ctx = _context_targeting("researcher")
        add_interaction_to_team_run_context(
            ctx, member_name="Researcher", task="Delegation A",
            run_response=_fake_run_response("T15_SENTINEL_A_9f73c2 and context around it"),
        )

        result = _bounded_get_team_member_interactions_str(ctx)

        assert "T15_SENTINEL_A_9f73c2" not in result

    def test_cross_role_sharing_is_unaffected(self):
        """The fix must not break legitimate cross-role visibility -- Coder seeing
        Researcher's finished result is exactly what share_member_interactions is for."""
        ctx = _context_targeting("coder")
        add_interaction_to_team_run_context(
            ctx, member_name="Researcher", task="Delegation A",
            run_response=_fake_run_response("T15_SENTINEL_A_9f73c2 and context around it"),
        )

        result = _bounded_get_team_member_interactions_str(ctx)

        assert "T15_SENTINEL_A_9f73c2" in result
        assert "Member: Researcher" in result


class TestT15_2MultipleSequentialDelegations:
    """A -> B -> C -> D remain isolated: each delegation's own-role history never
    reaches it, while every OTHER role's history remains visible (within C.3's bound)."""

    def test_four_role_chain_each_sees_others_never_self(self):
        ctx: dict = {}
        roles_and_sentinels = [
            ("researcher", "T15_SENTINEL_A_9f73c2"),
            ("planner", "T15_SENTINEL_B_4ac81e"),
            ("coder", "T15_SENTINEL_C_1d55f0"),
            ("reviewer", "T15_SENTINEL_D_7e02aa"),
        ]
        for member_name, sentinel in roles_and_sentinels:
            ctx["_t15_current_target_member_id"] = _member_key(member_name)
            # What THIS role sees right before its own delegation runs must never
            # contain a sentinel it produced in an earlier turn (impossible on the
            # first three; the assertion matters once a role recurs, exercised by
            # test_a_b_a_b_sequence_never_leaks_self below).
            seen_before_this_delegation = _bounded_get_team_member_interactions_str(ctx)
            assert sentinel not in seen_before_this_delegation
            add_interaction_to_team_run_context(
                ctx, member_name=member_name.capitalize(), task=f"Delegation for {member_name}",
                run_response=_fake_run_response(f"{sentinel} and surrounding detail"),
            )

        # After all four, a FIFTH delegation to a brand-new role sees every other
        # role's sentinel (within the bound of 3 most recent -- Phase C.3, unchanged).
        ctx["_t15_current_target_member_id"] = _member_key("executor")
        final_view = _bounded_get_team_member_interactions_str(ctx)
        assert "T15_SENTINEL_D_7e02aa" in final_view  # most recent
        assert "T15_SENTINEL_C_1d55f0" in final_view
        assert "T15_SENTINEL_B_4ac81e" in final_view
        # 4th-most-recent falls outside C.3's bound of 3 -- unrelated to this fix.
        assert "T15_SENTINEL_A_9f73c2" not in final_view

    def test_a_b_a_b_sequence_never_leaks_self(self):
        """The exact shape the mission requires: A -> B -> A -> B, repeated, and B's
        (and A's) model input must never contain its OWN earlier output."""
        ctx: dict = {}

        def delegate(member_id: str, sentinel: str) -> str:
            ctx["_t15_current_target_member_id"] = _member_key(member_id)
            seen = _bounded_get_team_member_interactions_str(ctx)
            add_interaction_to_team_run_context(
                ctx, member_name=member_id.capitalize(), task=f"task for {member_id}",
                run_response=_fake_run_response(f"{sentinel} output"),
            )
            return seen

        SENTINEL_A = "T15_SENTINEL_A_9f73c2"
        SENTINEL_B = "T15_SENTINEL_B_4ac81e"

        seen_a1 = delegate("researcher", SENTINEL_A)
        seen_b1 = delegate("coder", SENTINEL_B)
        seen_a2 = delegate("researcher", SENTINEL_A + "_v2")
        seen_b2 = delegate("coder", SENTINEL_B + "_v2")

        assert SENTINEL_A not in seen_a1  # nothing yet
        assert SENTINEL_A in seen_b1  # B's first view: A's real output, fine (cross-role)
        # A's second delegation must not see its OWN first output, even though B's
        # (cross-role) output in between is fine to see.
        assert SENTINEL_A not in seen_a2
        assert SENTINEL_B in seen_a2
        # B's second delegation must not see its OWN first output.
        assert SENTINEL_B not in seen_b2
        assert SENTINEL_A + "_v2" in seen_b2


class TestT15_3SameMemberDifferentObjectives:
    """Researcher A and Researcher B (two delegations to the SAME role, different
    objectives) cannot inherit each other's state via the interactions channel."""

    def test_different_objective_same_role_still_excluded(self):
        ctx = _context_targeting("researcher")
        add_interaction_to_team_run_context(
            ctx, member_name="Researcher",
            task="read API/utils/service_clients/auth_service_client.py",
            run_response=_fake_run_response(
                "T15_SENTINEL_A_9f73c2 -- Auth Service Client File Analysis"
            ),
        )

        result = _bounded_get_team_member_interactions_str(ctx)

        assert "T15_SENTINEL_A_9f73c2" not in result
        assert result == ""  # the only interaction on record was self -- nothing left to show


class TestT15_6CrossGenerationEvidenceReuseUnaffected:
    """The ba0964c cross-generation reuse marker lives in _make_read_cache_tool_hook,
    an entirely different function this phase never touches. Guard against accidental
    coupling by confirming the import surface and hook still work independently of
    the interactions-string patch installed in this file's fixture."""

    def test_read_cache_hook_import_and_marker_still_present(self):
        from swarm.team import _make_read_cache_tool_hook
        import inspect

        assert "EXISTING RUN EVIDENCE" in inspect.getsource(_make_read_cache_tool_hook)


class TestT15_7DuplicateDelegationGuardUnaffected:
    """The 7fedeb5 team-is-None guard on the pending-log-entry bridge lives in
    _duplicate_delegation_gate_hook, untouched by this phase. Guard against
    accidental coupling the same way as T15.6."""

    def test_duplicate_delegation_hook_import_and_guard_still_present(self):
        from swarm.team import _make_duplicate_delegation_gate_hook
        import inspect

        assert "team is not None" in inspect.getsource(_make_duplicate_delegation_gate_hook)


class TestT15BroadcastClearsMarker:
    """A broadcast delegation has no single target -- the marker must be cleared
    (None), not left stale from an earlier single-target delegation, or every
    member's own prior interaction would be silently excluded from a broadcast."""

    def test_no_target_marker_filters_nothing(self):
        ctx: dict = {"_t15_current_target_member_id": None}
        add_interaction_to_team_run_context(
            ctx, member_name="Researcher", task="Delegation A",
            run_response=_fake_run_response("T15_SENTINEL_A_9f73c2"),
        )

        result = _bounded_get_team_member_interactions_str(ctx)

        assert "T15_SENTINEL_A_9f73c2" in result
