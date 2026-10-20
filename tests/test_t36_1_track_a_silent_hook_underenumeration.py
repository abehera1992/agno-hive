"""T36.1 Track A -- silent frontend-hook under-enumeration, independent of wording.

T36's Track B (`_integrity_gap_list_without_comparison` / `_integrity_completeness_
claim_without_comparison`) only ever fires when the ANSWER's own text makes a gap-
shaped claim. The second live T13a replay this phase re-ran against (session
7b93fe6b-a72e-406f-886d-a97b4eaa2d56, 2026-10-20) produced an answer that named only
2 of the module's real hooks and made NO gap claim at all:

    "The frontend hooks related to vouchers are located in `page.tsx`. However, I
    was unable to retrieve the specific hooks due to a tool limitation... it's
    challenging to identify any gaps between the backend and frontend
    implementations."

No wording-based check -- however widened -- can ever anchor on this text, because
there is no sentence asserting absence to match against. `_module_hook_coverage_gap`
closes this by deriving the module's real hook count itself, live, from this
project's own RTK Query naming convention (`export const use<Module>...Query|
Mutation`), the same "call the tool myself instead of trusting what the run
gathered" shape `_scoped_coverage_gap` already uses for backend routers -- never
hardcoded to 5 hooks or to `inventoryApi.ts` specifically.
"""
import asyncio

import pytest

import swarm.team as team_mod
from swarm.team import (
    _MODULE_HOOK_ASK_RE,
    _module_hook_coverage_gap,
    _run_repo_derived_guards,
)

_T13A_TASK_NO_FILES = (
    "Audit the vouchers module: list its endpoints, its database tables, and its "
    "frontend hooks, and identify anything present in the backend with no frontend "
    "counterpart."
)

# The real 5 voucher hooks, as exported by inventoryApi.ts (ground truth, T36
# runbook). Used only as SIMULATED search_files output below -- never hardcoded
# into the production function itself, which derives the count from whatever the
# live search returns.
_REAL_VOUCHER_HOOKS = [
    "useGetVouchersQuery", "useGetVoucherQuery", "useCreateVoucherMutation",
    "usePostVoucherMutation", "useCancelVoucherMutation",
]


def _run(coro):
    return asyncio.run(coro)


def _patch_search(monkeypatch, hook_names):
    """Stand in for _repo_search_text: returns one fake `path:line: content` line
    per hook name, mirroring hive-mcp's real search_files output shape."""
    async def _fake(pattern, glob_filter, hive_mcp_url, hive_mcp_tools=None):
        lines = [
            f"Client/.../inventoryApi.ts:{30 + i}: export const {h} = "
            f"inventoryApi.useQuery;"
            for i, h in enumerate(hook_names)
        ]
        return "\n".join(lines)
    monkeypatch.setattr(team_mod, "_repo_search_text", _fake)


def test_task_regex_matches_the_real_no_files_named_t13a_prompt():
    assert _MODULE_HOOK_ASK_RE.search(_T13A_TASK_NO_FILES)


# ── 3. THE PRIMARY CASE: no gap claim at all, canonical inventory available ────

def test_silent_underenumeration_with_no_gap_claim_is_detected(monkeypatch):
    """The exact second-live-replay shape: 2 hooks named, zero gap-claim wording."""
    _patch_search(monkeypatch, _REAL_VOUCHER_HOOKS)
    answer = (
        "The frontend hooks related to vouchers are located in `page.tsx`. "
        "I found: useGetVouchersQuery and useCreateVoucherMutation. However, I was "
        "unable to retrieve further details due to a tool limitation."
    )
    note = _run(_module_hook_coverage_gap(_T13A_TASK_NO_FILES, answer,
                                          "http://hive-mcp:9003/mcp"))
    assert "COVERS ONLY 2 OF 5" in note
    assert "usePostVoucherMutation" in note
    assert "useCancelVoucherMutation" in note


# ── 1. Explicit historical gap-claim shape is ALSO caught by this independent check ─

def test_explicit_historical_gap_claim_shape_also_caught(monkeypatch):
    _patch_search(monkeypatch, _REAL_VOUCHER_HOOKS)
    answer = (
        "### Frontend Hooks\n1. useGetVouchersQuery\n2. useCreateVoucherMutation\n\n"
        "### Backend Endpoints Without Frontend Counterpart\n"
        "The following do not have a corresponding frontend hook:\n"
        "1. Post voucher\n2. Cancel voucher\n3. Create GRN from PO\n"
        "4. Create credit note\n5. Create stock adjustment\n6. Create stock transfer\n"
    )
    note = _run(_module_hook_coverage_gap(_T13A_TASK_NO_FILES, answer,
                                          "http://hive-mcp:9003/mcp"))
    assert "COVERS ONLY 2 OF 5" in note


# ── 4. A legitimate alternate implementation is NOT falsely flagged ─────────────

def test_naming_all_real_hooks_via_different_wording_is_silent(monkeypatch):
    _patch_search(monkeypatch, _REAL_VOUCHER_HOOKS)
    answer = (
        "The vouchers module exposes useGetVouchersQuery, useGetVoucherQuery, "
        "useCreateVoucherMutation, usePostVoucherMutation and "
        "useCancelVoucherMutation on the frontend -- full coverage."
    )
    note = _run(_module_hook_coverage_gap(_T13A_TASK_NO_FILES, answer,
                                          "http://hive-mcp:9003/mcp"))
    assert note == ""


def test_naming_most_real_hooks_above_threshold_is_silent(monkeypatch):
    """4 of 5 (80%) clears the same 50% coverage-ratio bar _scoped_coverage_gap
    uses for routers -- a near-complete answer is not flagged."""
    _patch_search(monkeypatch, _REAL_VOUCHER_HOOKS)
    answer = (
        "Hooks: useGetVouchersQuery, useGetVoucherQuery, useCreateVoucherMutation, "
        "usePostVoucherMutation."
    )
    note = _run(_module_hook_coverage_gap(_T13A_TASK_NO_FILES, answer,
                                          "http://hive-mcp:9003/mcp"))
    assert note == ""


# ── Negative controls: no hardcoding, no false signal when nothing to measure ──

def test_no_hooks_found_anywhere_is_silent_not_a_false_accusation(monkeypatch):
    """A module this convention genuinely does not apply to (or wherever the live
    search cannot reach) must not be treated as 'zero coverage' -- that would be
    indistinguishable from a real module with real hooks the search missed."""
    _patch_search(monkeypatch, [])
    answer = "The frontend has no hooks for this module."
    note = _run(_module_hook_coverage_gap(_T13A_TASK_NO_FILES, answer,
                                          "http://hive-mcp:9003/mcp"))
    assert note == ""


def test_fewer_than_minimum_real_hooks_is_silent(monkeypatch):
    """A module with exactly 1 real hook is below _MODULE_HOOK_MIN_EXPORTS --
    not a listing worth measuring (mirrors _SCOPED_MIN_FILES's own floor)."""
    _patch_search(monkeypatch, ["useGetWidgetsQuery"])
    task = ("Audit the widgets module: list its endpoints and its frontend hooks, "
            "identifying anything with no frontend counterpart.")
    note = _run(_module_hook_coverage_gap(task, "No hooks found.",
                                          "http://hive-mcp:9003/mcp"))
    assert note == ""


def test_task_not_module_hook_shaped_is_silent():
    note = _run(_module_hook_coverage_gap(
        "Add a new endpoint to the vouchers module.", "Done.", None))
    assert note == ""


def test_no_hive_mcp_connection_is_silent_not_an_error(monkeypatch):
    async def _fake(pattern, glob_filter, hive_mcp_url, hive_mcp_tools=None):
        return ""  # _repo_search_text's own real behavior with no URL/tools
    monkeypatch.setattr(team_mod, "_repo_search_text", _fake)
    note = _run(_module_hook_coverage_gap(
        _T13A_TASK_NO_FILES, "Hooks: useGetVouchersQuery.", None))
    assert note == ""


# ── Derivation is NOT hardcoded: a different module measures against its OWN total ─

def test_derivation_is_not_hardcoded_to_vouchers_or_five(monkeypatch):
    """A completely different module name with a completely different real hook
    count is measured against ITS OWN total, proving the mechanism derives
    expectations from a live search rather than a fixed list or count."""
    widget_hooks = ["useGetWidgetsQuery", "useCreateWidgetMutation",
                    "useDeleteWidgetMutation"]
    _patch_search(monkeypatch, widget_hooks)
    task = ("Audit the widgets module: list its endpoints and its frontend hooks, "
            "identifying anything with no frontend counterpart.")
    answer = "Hooks: useGetWidgetsQuery."
    note = _run(_module_hook_coverage_gap(task, answer, "http://hive-mcp:9003/mcp"))
    assert "COVERS ONLY 1 OF 3" in note
    assert "useCreateWidgetMutation" in note
    assert "useDeleteWidgetMutation" in note


# ── Integration proof: the PRODUCTION call path actually invokes this guard ─────
# ── (not just "the function is callable in isolation") -- _run_repo_derived_    ─
# ── guards is the shared implementation both the parent-process rescue path and ─
# ── _verified_answer's corrected-answer path call; this proves the wiring, not  ─
# ── just the function's own standalone behavior already proven above.          ─

@pytest.mark.asyncio
async def test_production_path_run_repo_derived_guards_actually_invokes_it(
        monkeypatch):
    _patch_search(monkeypatch, _REAL_VOUCHER_HOOKS)
    answer = "Hooks: useGetVouchersQuery, useCreateVoucherMutation."
    notes = await _run_repo_derived_guards(
        _T13A_TASK_NO_FILES, answer, "http://hive-mcp:9003/mcp", None,
        label="t36.1-integration-test")
    assert any("COVERS ONLY 2 OF 5" in n for n in notes)
