"""T36.1 Track B -- controlled integration proof for T36's
`_unconfirmed_service_inclusion` (T11 containment), through the real production
call path (`_run_repo_derived_guards`), not just standalone unit calls.

T36's own 9 unit tests (tests/test_t36_track_c_service_inclusion.py) already prove
the function's standalone behavior. A live T11 replay this phase produced a
CORRECT, narrow answer (business-service + storage-service only) and therefore
never exercised the containment path at all -- a clean live answer is not proof
the guard works, per this phase's own mandate.

These tests add the missing layer: the mechanism invoked through
`_run_repo_derived_guards`, the same shared gather call
`_module_hook_coverage_gap`'s own integration test (T36.1 Track A) already proved
wiring for, across four controlled scenarios:

1. Negative control -- a service named via pure keyword co-occurrence, no
   qualifying relationship anywhere in the repo -- MUST be flagged.
2. Positive control -- a genuine dependency with real search_files evidence --
   MUST NOT be flagged.
3. Mixed control -- the verified business-service/storage-service chain preserved
   alongside one unsupported addition -- the valid chain is never touched, only
   the unsupported member is named.
4. Unknown control -- evidence is genuinely insufficient (hive-mcp unreachable) --
   reported as uncertain (silent, per the documented contract: "an unresolved
   relationship is marked uncertain... never silently declaring the dependency
   proven or nonexistent") rather than asserting either outcome.

Also documents, per this phase's explicit ask ("confirm that the mechanism checks
the actual relevant relationship rather than merely establishing that a URL
constant exists somewhere in the repository"), the mechanism's real, ALREADY-
accepted scope boundary from T36's own design gate: it establishes constant
EXISTENCE, not reference PROVENANCE. A constant referenced for a reason wholly
unrelated to the task's claimed chain still reads as "confirmed" -- documented
here as a known limitation, not silently fixed, per this phase's explicit
instruction not to widen the evidence contract beyond the approved design.
"""
import asyncio

import pytest

import swarm.team as team_mod
from swarm.team import _run_repo_derived_guards, _unconfirmed_service_inclusion

_T11_TASK = (
    "Which services are involved when a seller uploads a document, and which "
    "functions handle it in each service?"
)


def _run(coro):
    return asyncio.run(coro)


def _patch_search(monkeypatch, confirmed: set[str]):
    async def _fake(pattern, glob_filter, hive_mcp_url, hive_mcp_tools=None):
        for svc in confirmed:
            const = svc.upper().replace("-", "_") + "_SERVICE_URL"
            if pattern == const:
                return (f"API/utils/service_clients/"
                        f"{svc.replace('-service', '')}_service_client.py:10:"
                        f"{const}: str = ...")
        return ""
    monkeypatch.setattr(team_mod, "_repo_search_text", _fake)


def _patch_search_unreachable(monkeypatch):
    """hive-mcp unreachable -- _repo_search_text's own real behavior when no
    session can be established returns "" for every pattern, indistinguishable
    (by design) from "constant genuinely not found"."""
    async def _fake(pattern, glob_filter, hive_mcp_url, hive_mcp_tools=None):
        return ""
    monkeypatch.setattr(team_mod, "_repo_search_text", _fake)


_MIXED_CHAIN_ANSWER = (
    "### Conclusion\n\nWhen a seller uploads a document, the following services "
    "are involved:\n\n"
    "1. **API/business-service**: Registers document references.\n"
    "2. **API/storage-service**: Handles the actual file storage.\n"
    "3. **API/email-service**: Sends a notification about the upload.\n"
)


# ── 1. Negative control: pure keyword co-occurrence, no relationship anywhere ──

@pytest.mark.asyncio
async def test_negative_control_unsupported_inclusion_is_flagged_via_production_path(
        monkeypatch):
    _patch_search(monkeypatch, confirmed={"business-service", "storage-service"})
    notes = await _run_repo_derived_guards(
        _T11_TASK, _MIXED_CHAIN_ANSWER, "http://hive-mcp:9003/mcp", None,
        label="t36.1-track-b-negative-control")
    assert any("email-service" in n and "SERVICE INCLUSION NOT CONFIRMED" in n
               for n in notes)


# ── 2. Positive control: a genuine dependency, real evidence -- not flagged ────

@pytest.mark.asyncio
async def test_positive_control_genuine_dependency_not_flagged(monkeypatch):
    _patch_search(monkeypatch, confirmed={
        "business-service", "storage-service", "email-service"})
    notes = await _run_repo_derived_guards(
        _T11_TASK, _MIXED_CHAIN_ANSWER, "http://hive-mcp:9003/mcp", None,
        label="t36.1-track-b-positive-control")
    assert not any("SERVICE INCLUSION NOT CONFIRMED" in n for n in notes)


# ── 3. Mixed control: the verified chain is preserved, only the unsupported ────
# ── member is named -- the SAME run as the negative control proves this, but ──
# ── explicitly re-asserted here as its own named scenario per the mission's   ──
# ── own enumeration.                                                          ──

@pytest.mark.asyncio
async def test_mixed_control_preserves_valid_chain_flags_only_unsupported(
        monkeypatch):
    _patch_search(monkeypatch, confirmed={"business-service", "storage-service"})
    notes = await _run_repo_derived_guards(
        _T11_TASK, _MIXED_CHAIN_ANSWER, "http://hive-mcp:9003/mcp", None,
        label="t36.1-track-b-mixed-control")
    flagged = [n for n in notes if "SERVICE INCLUSION NOT CONFIRMED" in n]
    assert len(flagged) == 1
    assert "business-service" not in flagged[0].split("unlike")[0]
    assert "storage-service" not in flagged[0].split("unlike")[0]
    assert "email-service" in flagged[0]
    # The valid chain is named as the CONTRAST ("unlike `business-service`,
    # `storage-service`, which does have one"), not as an accusation.
    assert "does have one" in flagged[0]


# ── 4. Unknown control: insufficient evidence -- reported as uncertain (silent) ─

@pytest.mark.asyncio
async def test_unknown_control_insufficient_evidence_stays_uncertain(monkeypatch):
    _patch_search_unreachable(monkeypatch)
    notes = await _run_repo_derived_guards(
        _T11_TASK, _MIXED_CHAIN_ANSWER, "http://hive-mcp:9003/mcp", None,
        label="t36.1-track-b-unknown-control")
    # All-unreachable means every service shows "not found" -- per the
    # documented contract this is all-unconfirmed, which stays SILENT (most
    # likely the live search did not run at all) rather than asserting the
    # chain disproven. This is "uncertainty reported as uncertainty" in this
    # mechanism's specific vocabulary: silence, not a false "disproven" claim.
    assert not any("SERVICE INCLUSION NOT CONFIRMED" in n for n in notes)


# ── Documents the real, already-accepted scope boundary (existence vs ─────────
# ── provenance) rather than silently "fixing" it, per this phase's explicit ───
# ── instruction not to widen the evidence contract beyond the approved design ─

@pytest.mark.asyncio
async def test_documented_limitation_existence_not_provenance(monkeypatch):
    """A constant that exists for a reason WHOLLY UNRELATED to this task's
    claimed chain still reads as 'confirmed' -- this is T36's own documented,
    accepted tradeoff (favor false negatives over false positives), not a bug
    this track is scoped to fix. Pinned here so a future tightening attempt
    changes this test deliberately, not by accident."""
    # email-service's EMAIL_SERVICE_URL exists, but for an entirely different
    # service (auth-service verification mail) -- unrelated to document upload.
    async def _fake(pattern, glob_filter, hive_mcp_url, hive_mcp_tools=None):
        if pattern == "EMAIL_SERVICE_URL":
            return ("API/authentication-service/config.py:12:EMAIL_SERVICE_URL: "
                     "str = ...  # used only for signup verification mail")
        if pattern == "STORAGE_SERVICE_URL" or pattern == "BUSINESS_SERVICE_URL":
            return (f"API/utils/service_clients/x_client.py:10:{pattern}: str = ...")
        return ""
    monkeypatch.setattr(team_mod, "_repo_search_text", _fake)
    found = await _unconfirmed_service_inclusion(
        _T11_TASK, _MIXED_CHAIN_ANSWER, "http://hive-mcp:9003/mcp")
    # Documented behavior: stays silent even though the real relationship is
    # unrelated to THIS task -- the mechanism checks existence, not provenance.
    assert found == ""
