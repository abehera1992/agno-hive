"""Phase 2A (AGNOHive Reliability Program): reliable comparison triggering and
operand selection for _computed_comparison().

Phase 2's forensic trace found two separate, deterministic defects upstream of
the (already-fixed, Phase 1) compare_enumerations tool itself:

1. _TWO_SIDED_TASK_RE only matched enumeration-STYLE phrasing ("both sides",
   "then list every ..."). T13's task -- "list its endpoints, its tables, and
   its hooks, and identify anything ... with no frontend counterpart" -- never
   said "both sides" or "then list", so _computed_comparison was skipped for
   every T13 run, unconditionally, regardless of the model's answer.

2. Operand selection picked "top-2 enumerated files by raw declaration count",
   with no notion of which SIDE (backend/frontend) a file was on. Live on R6
   T2: the ledger held business_api.py, business_admin_api.py (both backend
   .py, one with more raw declarations) and businessApi.ts (the one real
   frontend file) -- top-2-by-count paired the two backend files together,
   producing "[team] computed the comparison for ...business_api.py vs
   ...business_admin_api.py", a nonsensical backend-vs-backend diff.

This file pins the fix for both, plus the "no safe pair" refusal the smallest
fix could not always avoid: when every enumerated file is on the same side,
guessing a same-side pair reproduces the R6 defect, so the function now
declines instead (still silently, exactly like its four pre-existing skip
reasons -- Phase 2A does not change what the Coordinator or the user sees).
"""
import asyncio

import pytest

from swarm.team import (
    _computed_comparison,
    _extension_group,
    _second_side_from_answer,
    _TWO_SIDED_TASK_RE,
)


def _run(coro):
    return asyncio.run(coro)


def _enum(path: str, count: int) -> dict:
    return {"path": path, "tool": "get_file_content", "lines": [], "count": count}


class _FakeSession:
    """Records exactly which (left_path, right_path) compare_enumerations was
    called with, so operand selection can be asserted directly instead of only
    through the final footnote text."""

    def __init__(self, text_by_pair=None, default_text=None):
        self.text_by_pair = text_by_pair or {}
        self.default_text = default_text
        self.calls: list[tuple[str, str]] = []

    async def call_tool(self, name, args):
        pair = (args["left_path"], args["right_path"])
        self.calls.append(pair)
        text = self.text_by_pair.get(pair, self.default_text)
        if text is None:
            text = (f"compare_enumerations — {pair[0]}  vs  {pair[1]}\n"
                    f"TOTALS: left 1, right 1, matched 1, left-only 0, right-only 0.")
        from types import SimpleNamespace
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class _FakeMCPTools:
    def __init__(self, session):
        self.session = session

    async def get_session_for_run(self, **kwargs):
        return self.session


# ── 2A.1: trigger regex ──────────────────────────────────────────────────────

T2_TASK = (
    "List every endpoint defined in API/business-service/router/business_api.py, "
    "then list every RTK Query hook exported by the frontend's business API "
    "slice, and state which endpoints have no corresponding hook. Enumerate "
    "both sides in full before comparing."
)
T13A_TASK = (
    "Audit the vouchers module: list its endpoints, its database tables, and "
    "its frontend hooks, and identify anything present in the backend with no "
    "frontend counterpart."
)
T13B_TASK = (
    "Audit the vouchers module: list its endpoints, its database tables, and "
    "its frontend hooks, and identify anything present in the backend with no "
    "frontend counterpart. Read API/inventory-service/router/vouchers_api.py "
    "for the endpoints, API/inventory-service/models.py for the tables, and "
    "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/"
    "inventoryApi.ts for the frontend hooks."
)
ONE_SIDED_TASK = "List every endpoint defined in API/business-service/router/business_api.py."
UNRELATED_TASK = "What does the register_seller function do in business_api.py?"


def test_trigger_matches_t2_style_task():
    assert _TWO_SIDED_TASK_RE.search(T2_TASK)


def test_trigger_matches_t13a_style_task_previously_unmatched():
    """The exact defect Phase 2 found: this task text did not match any of the
    four original alternatives (no 'both sides', no 'then list every')."""
    assert _TWO_SIDED_TASK_RE.search(T13A_TASK)


def test_trigger_matches_t13b_style_task_previously_unmatched():
    assert _TWO_SIDED_TASK_RE.search(T13B_TASK)


def test_trigger_does_not_match_one_sided_enumeration_task():
    assert _TWO_SIDED_TASK_RE.search(ONE_SIDED_TASK) is None


def test_trigger_does_not_match_unrelated_task():
    assert _TWO_SIDED_TASK_RE.search(UNRELATED_TASK) is None


# ── 2A.2: operand selection ──────────────────────────────────────────────────


def test_extension_group_classifies_backend_and_frontend():
    assert _extension_group("API/business-service/router/business_api.py") == "py"
    assert _extension_group("Client/.../businessApi.ts") == "ts"
    assert _extension_group("Client/.../Component.tsx") == "ts"
    assert _extension_group("README.md") == "other"


def test_1_correct_backend_frontend_pair_selected():
    enumerations = {
        "a": _enum("API/business-service/router/business_api.py", 13),
        "b": _enum("Client/.../business/businessApi.ts", 16),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools))
    assert session.calls == [
        ("API/business-service/router/business_api.py", "Client/.../business/businessApi.ts")
    ]
    assert "THE COMPARISON, COMPUTED" in out


def test_2_multiple_backend_candidates_only_refuses_to_pair_same_side():
    """The R6 T2 shape, isolated: two .py files, no .ts file anywhere in the
    ledger and none named in the answer either. Old code would have paired the
    two .py files (top-2-by-count); the fix must refuse instead."""
    enumerations = {
        "a": _enum("API/business-service/router/business_api.py", 13),
        "b": _enum("API/business-service/router/business_admin_api.py", 24),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools,
                                     content="no other file named here"))
    assert out == ""
    assert session.calls == []


def test_3_multiple_frontend_candidates_only_refuses_to_pair_same_side():
    enumerations = {
        "a": _enum("Client/.../business/businessApi.ts", 16),
        "b": _enum("Client/.../business/types.ts", 5),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools,
                                     content="no other file named here"))
    assert out == ""
    assert session.calls == []


def test_4_wrong_but_plausible_candidate_still_produces_a_structural_pair():
    """Operand selection is deliberately NOT trying to judge semantic
    correctness -- only structural safety (one file per side). A real,
    unrelated frontend file paired against the backend file still counts as a
    safe structural pair; compare_enumerations' own Phase 1 fix (or a human
    reader) is what catches semantic wrongness, not this layer."""
    enumerations = {
        "a": _enum("API/business-service/router/business_api.py", 13),
        "b": _enum("Client/.../inventory/inventoryApi.ts", 9),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools))
    assert session.calls == [
        ("API/business-service/router/business_api.py", "Client/.../inventory/inventoryApi.ts")
    ]
    assert "THE COMPARISON, COMPUTED" in out


def test_5_no_valid_pair_when_only_one_file_and_answer_names_nothing():
    enumerations = {"a": _enum("API/business-service/router/business_api.py", 13)}
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools,
                                     content="no file paths mentioned here at all"))
    assert out == ""
    assert session.calls == []


def test_6_one_sided_empty_tool_result_from_phase1_passes_through_unchanged():
    """Operand selection resolves a normal cross-side pair; the WARNING text is
    the underlying (Phase 1-fixed) tool's business, not this layer's -- confirms
    Phase 2A does not disturb Phase 1's one-sided-empty behaviour."""
    enumerations = {
        "a": _enum("API/business-service/router/business_api.py", 13),
        "b": _enum("Client/.../business/email/page.tsx", 0),
    }
    warning_text = (
        "compare_enumerations — API/business-service/router/business_api.py  vs  "
        "Client/.../business/email/page.tsx\n"
        "WARNING: Client/.../business/email/page.tsx yielded ZERO RTK Query "
        "endpoints while API/business-service/router/business_api.py yielded 13.\n"
        "TOTALS: left 13, right 0, matched 0, left-only 13, right-only 0."
    )
    session = _FakeSession(default_text=warning_text)
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools))
    assert len(session.calls) == 1
    assert "WARNING" in out
    assert "yielded ZERO" in out


def test_7_normal_valid_comparison_reports_totals():
    enumerations = {
        "a": _enum("API/business-service/router/business_api.py", 13),
        "b": _enum("Client/.../business/businessApi.ts", 16),
    }
    pair = ("API/business-service/router/business_api.py", "Client/.../business/businessApi.ts")
    text = (f"compare_enumerations — {pair[0]}  vs  {pair[1]}\n"
            f"TOTALS: left 13, right 16, matched 7, left-only 6, right-only 9.")
    session = _FakeSession(text_by_pair={pair: text})
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools))
    assert "TOTALS: left 13, right 16, matched 7, left-only 6, right-only 9." in out


def test_8_non_source_files_excluded_from_operand_candidates():
    enumerations = {
        "a": _enum("API/business-service/router/business_api.py", 13),
        "b": _enum("README.md", 40),
        "c": _enum("package.json", 30),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools,
                                     content="no other source file named here"))
    # README.md/package.json are not source files compare_enumerations can join
    # against, so they must never be considered candidates -- only business_api.py
    # is left, which is the "only one side enumerated" no-safe-pair case.
    assert out == ""
    assert session.calls == []


def test_r6_t2_regression_backend_file_no_longer_outranks_frontend_file():
    """Pins the exact live defect: business_admin_api.py (24 raw declarations,
    backend) must not be preferred over businessApi.ts (16, frontend) just
    because it has a higher raw count. The correct pair is business_api.py
    (highest-count backend file) vs businessApi.ts (the only frontend file)."""
    enumerations = {
        "business_api": _enum("API/business-service/router/business_api.py", 13),
        "business_admin_api": _enum(
            "API/business-service/router/business_admin_api.py", 24),
        "modules_api": _enum("API/business-service/router/modules_api.py", 8),
        "business_ts": _enum("Client/.../business/businessApi.ts", 16),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    _run(_computed_comparison(T2_TASK, enumerations, "http://x/mcp", tools))
    assert session.calls == [
        ("API/business-service/router/business_api.py", "Client/.../business/businessApi.ts")
    ]


def test_t13_task_no_longer_skipped_for_not_being_two_sided():
    """End-to-end confirmation of the 2A.1 fix through the real gate in
    _computed_comparison, not just the bare regex."""
    enumerations = {
        "a": _enum("API/inventory-service/router/vouchers_api.py", 9),
        "b": _enum("Client/.../inventory/inventoryApi.ts", 5),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(T13A_TASK, enumerations, "http://x/mcp", tools))
    assert session.calls == [
        ("API/inventory-service/router/vouchers_api.py", "Client/.../inventory/inventoryApi.ts")
    ]
    assert "THE COMPARISON, COMPUTED" in out


def test_unrelated_task_still_skipped_task_not_two_sided():
    enumerations = {
        "a": _enum("API/business-service/router/business_api.py", 13),
        "b": _enum("Client/.../business/businessApi.ts", 16),
    }
    session = _FakeSession()
    tools = _FakeMCPTools(session)
    out = _run(_computed_comparison(UNRELATED_TASK, enumerations, "http://x/mcp", tools))
    assert out == ""
    assert session.calls == []
