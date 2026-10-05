"""Phase T16 -- progress-aware repetition detection.

Root cause, reproduced directly against the real production function (not a
reimplementation): `_looks_like_repetition_loop`'s tier-3/4 checks compare only
the first `_REPETITION_PREFIX_CHARS` (100) characters of a freshly generated
segment against earlier content. A FastAPI handler's Depends()-chain opening
(`current_user: TokenUser = Depends(get_current_seller), db: AsyncSession =
Depends(get_async_db),): result = await db.execute( select(`) alone exceeds 100
normalized characters -- confirmed by direct normalization, byte-identical
across two different route handlers. The differing entity (e.g. `TenantModule`
vs `TenantSubscription`) falls entirely past that cutoff, so a SECOND, genuinely
different route is flagged as "repeating" the first.

Live-reproduced: T3 post-T15 (2026-10-04, 19:50:25-19:53:07), modules_api.py --
a TenantSubscription-querying route followed by a TenantModule-querying route,
sharing an identical >100-char opening, triggered 4 consecutive tier-3 "loop
detected" events and a full repetition-threshold stop+recovery, even though
each occurrence introduced a new, real SQLAlchemy model.

Fix: tiers 1/2 (full exact / filler-stripped segment containment) are
untouched -- a full-segment match already proves no progress regardless of what
identifiers appear in it. Tiers 3/4 (prefix-only matches) are now vetoed by
`_segment_introduces_new_identifier`: if the new segment names a CamelCase or
snake_case symbol (route/model/class/function/file-like token) absent from the
matched prior window, this occurrence is treated as progress, not a repeat.
"""
import pytest

from swarm.team import (
    _looks_like_repetition_loop,
    _segment_introduces_new_identifier,
)


# ---------------------------------------------------------------------------
# Class fixtures (mission section 7)
# ---------------------------------------------------------------------------

CLASS_A_FINDINGS = """Finding A: the auth service exposes verify_token.
Finding B: the business service exposes register_seller.
Finding C: the storage service exposes upload_file.
"""

ROUTE_SUBSCRIPTION = """    current_user: TokenUser = Depends(get_current_seller),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(TenantSubscription)
        .where(TenantSubscription.tenant_id == UUID(current_user.tenant_id))
    )
"""

ROUTE_MODULE = """    current_user: TokenUser = Depends(get_current_seller),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(TenantModule)
        .where(
            TenantModule.tenant_id == UUID(current_user.tenant_id)
        )
    )
"""

ROUTE_DEACTIVATION = """    current_user: TokenUser = Depends(get_current_seller),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(ModuleDeactivationRequest)
        .where(
            ModuleDeactivationRequest.tenant_id == UUID(current_user.tenant_id)
        )
    )
"""

SAME_ROUTE_VERBATIM = """@app.get("/verify")
def verify(current_user: TokenUser = Depends(get_current_seller)):
    return do_verify(current_user)
"""

SAME_ROUTE_REWORDED_NO_NEW_EVIDENCE = """    current_user: TokenUser = Depends(get_current_seller),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(TenantSubscription)
        .where(
            TenantSubscription.tenant_id == UUID(current_user.tenant_id)
        )
    )
    # same handler, same claim, restated with no new symbol at all
"""


class TestT16_1TrueExactContentRegeneration:
    def test_identical_block_repeated_is_detected(self):
        assert _looks_like_repetition_loop(CLASS_A_FINDINGS, CLASS_A_FINDINGS) is True


class TestT16_2TrueSemanticRegenerationMinorWording:
    def test_escalating_self_correction_shape_is_detected(self):
        """The ORIGINAL incident this detector was built for (2026-08-14): a
        coordinator spiraling through self-corrections about its own citation
        precision, sharing its opening ~110-120 chars before diverging (per
        _REPETITION_PREFIX_CHARS' own comment) -- the shared span here is
        deliberately kept at/above 100 normalized chars so tier 3 is actually
        exercised, not merely tier 1/2's full-containment check."""
        prior = (
            "I need to be even more precise about the exact citation location "
            "here before reporting it, so let me try again using the precise "
            "line number from the file in question."
        )
        new_segment = (
            "I need to be even more precise about the exact citation location "
            "here before reporting it, so let me try again using the exact "
            "wording, being even more careful about it this time."
        )
        assert _looks_like_repetition_loop(new_segment, prior) is True


class TestT16_3RepeatedRouteStructureDifferentRoutes:
    def test_depends_chain_different_models_not_escalated(self):
        """The exact live T3 shape: same Depends()-chain opening, different
        SQLAlchemy model queried -- must NOT escalate."""
        assert _looks_like_repetition_loop(
            ROUTE_MODULE, ROUTE_SUBSCRIPTION + ROUTE_SUBSCRIPTION
        ) is False

    def test_third_distinct_route_sharing_opening_also_not_escalated(self):
        """Tier 4 requires 2 prior occurrences before it even considers firing --
        confirm a 3rd distinct route (different model again) is STILL not
        escalated once tier 4 would otherwise kick in. This is the case the
        original author's own comment anticipated ('require multiple
        recurrence') but which alone would still eventually false-positive on a
        4th+ route; the identifier veto does not degrade with route count."""
        prior = ROUTE_SUBSCRIPTION + ROUTE_MODULE + ROUTE_SUBSCRIPTION + ROUTE_MODULE
        assert _looks_like_repetition_loop(ROUTE_DEACTIVATION, prior) is False


class TestT16_4RepeatedDependsStructureDifferentFunctions:
    def test_depends_boilerplate_alone_is_not_sufficient_signal(self):
        assert _segment_introduces_new_identifier(ROUTE_MODULE, ROUTE_SUBSCRIPTION) is True


class TestT16_5SameFunctionRegeneratedRepeatedly:
    def test_identical_route_definition_repeated_is_detected(self):
        assert _looks_like_repetition_loop(
            SAME_ROUTE_VERBATIM, SAME_ROUTE_VERBATIM
        ) is True


class TestT16_6SameFileSymbolClaimRepeatedWithoutNewEvidence:
    def test_same_route_reworded_without_new_evidence_is_detected(self):
        """Lightly reworded restatement of the SAME route/claim (shares the
        >100-char Depends()-chain opening AND the same TenantSubscription
        entity, just a trailing comment added) -- the identifier veto must not
        swallow this: there is no NEW identifier to excuse it (TenantSubscription
        already appeared), so it still falls through to tier 3's plain prefix
        match exactly as before this phase's change."""
        assert _looks_like_repetition_loop(
            SAME_ROUTE_REWORDED_NO_NEW_EVIDENCE, ROUTE_SUBSCRIPTION + ROUTE_SUBSCRIPTION
        ) is True


class TestT16_7DifferentFilesEquivalentTerminologyNewEvidence:
    def test_new_file_same_terminology_different_symbol_not_escalated(self):
        prior = (
            "File business_api.py proves seller registration via register_seller."
        )
        new_segment = (
            "File business_admin_api.py also proves seller registration, via "
            "the AdminSellerVerificationQueue handler."
        )
        assert _looks_like_repetition_loop(new_segment, prior) is False


class TestT16_8StructuralRepetitionEventuallyBecomesTrueRegeneration:
    def test_same_entity_finally_repeated_is_detected(self):
        """Three distinct routes (progress), then the FIRST one verbatim again --
        the 4th occurrence has no new identifier relative to the lookback window
        and must escalate."""
        prior = ROUTE_SUBSCRIPTION + ROUTE_MODULE + ROUTE_DEACTIVATION
        assert _looks_like_repetition_loop(ROUTE_SUBSCRIPTION, prior) is True


class TestT16_9T15ContaminationFixtureStillDetected:
    """T15's own cross-delegation contamination shape (verbatim reuse of an
    EARLIER, UNRELATED delegation's full report, no new identifiers at all) must
    still be caught if artificially reintroduced -- this phase never touches
    T15's fix itself, only confirms the repetition detector's unrelated change
    does not weaken this orthogonal case."""

    def test_verbatim_prior_delegation_report_is_detected(self):
        prior_delegation_report = (
            "Auth Service Client File Analysis\n\n"
            "File: API/utils/service_clients/auth_service_client.py\n\n"
            "Functions Related to Seller Operations:\n"
            "1. provision_tenant_for_seller\n   - Parameters: tenant_id, plan\n"
        )
        assert _looks_like_repetition_loop(
            prior_delegation_report, prior_delegation_report
        ) is True


class TestT16_10NormalLongStreamNoUnnecessaryRecovery:
    def test_long_varied_code_generation_never_flagged(self):
        """A realistic multi-route listing where every route is genuinely
        distinct (different path, different model, different handler name) must
        never trigger a false positive, however long it runs."""
        routes = [ROUTE_SUBSCRIPTION, ROUTE_MODULE, ROUTE_DEACTIVATION]
        accumulated = ""
        for route in routes:
            assert _looks_like_repetition_loop(route, accumulated) is False
            accumulated += route


# ---------------------------------------------------------------------------
# Counterfactual matrix (mission section 11) -- before/after on the SAME
# fixtures, using the real production function as it exists NOW (the "after").
# The "before" behavior for the structural-repetition fixtures was captured
# live against the unmodified function prior to this change (see module
# docstring and the Phase T16 final report) and is pinned here as a literal
# expected value so a future revert is caught by this same test.
# ---------------------------------------------------------------------------

class TestT16CounterfactualMatrix:
    def test_structural_repetition_old_false_positive_new_no_escalation(self):
        # OLD (pre-T16, confirmed live against the unmodified function): True.
        # NEW (this fix): False.
        assert _looks_like_repetition_loop(
            ROUTE_MODULE, ROUTE_SUBSCRIPTION + ROUTE_SUBSCRIPTION
        ) is False

    def test_true_regeneration_old_detected_new_still_detected(self):
        # OLD: True. NEW: True -- unchanged, tiers 1/2 never touched.
        assert _looks_like_repetition_loop(CLASS_A_FINDINGS, CLASS_A_FINDINGS) is True

    def test_same_entity_repetition_old_detected_new_still_detected(self):
        assert _looks_like_repetition_loop(
            SAME_ROUTE_VERBATIM, SAME_ROUTE_VERBATIM
        ) is True

    def test_new_evidence_repetition_old_false_positive_new_no_escalation(self):
        prior = "File A proves X using claim_one."
        new_segment = "File B also proves X, but adds ClaimTwoEvidence."
        assert _looks_like_repetition_loop(new_segment, prior) is False
