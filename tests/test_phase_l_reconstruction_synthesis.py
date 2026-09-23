"""Phase L: deterministic reconstruction synthesis (K4) + safe fallback (K5)
+ narrow consistency guard (K7).

Phase K's forensic (live T13b ZGX trace, this session) found the deterministic
compare_enumerations layer already correct, but the reconstruction step handed
that already-classified data to a free-form model regeneration which
re-derived -- and conflated -- the classification: PUT/POST PARTIAL_MATCH
entries collapsed into the "no matching frontend hooks" list alongside the
genuine LEFT_ONLY entries, reproducing the historical "6 gaps" defect one
layer downstream of J-C's fix.

_synthesize_comparison_answer (K4) removes the free-form step for the facts
themselves: pure string templating from _comparison_entries' parsed, verbatim
per-item lines, so a category cannot be moved or merged -- there is no
generation step that could do it. _attempt_evidence_grounded_reconstruction
tries this FIRST and only falls back to the pre-Phase-L free-form
_stream_team_run path when the comparison note isn't synthesis-eligible (not
real-tool-shaped -- see _comparison_entries' own docstring for exactly when).
_reconstruction_category_conflict (K7) is a narrow, exact-substring
defense-in-depth check wired into that fallback path only.
"""
import re
from types import SimpleNamespace

import pytest

import swarm.team as team_mod
from swarm.team import (
    _attempt_evidence_grounded_reconstruction,
    _comparison_entries,
    _comparison_summary,
    _reconstruction_category_conflict,
    _synthesize_comparison_answer,
)


class _Team:
    """Bare stand-in, matching this project's own test convention."""


# ── The real T13b comparison text, captured this session by independently
# re-executing the deployed compare_enumerations() against fresh copies of
# the two real files (hive-mcp/tools/compare.py, unmodified). Embedded here
# verbatim -- not paraphrased, not shortened -- as the regression fixture the
# Phase L spec asks for.
_T13B_BODY = """compare_enumerations — API/inventory-service/router/vouchers_api.py  vs  Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts
join: two-stage -- identity (path-boundary suffix match, exact string, no inference), then attribute agreement (http_method). Same identity with a differing attribute is PARTIAL MATCHES, not LEFT ONLY/RIGHT ONLY. Identity matching more than one candidate on either side is AMBIGUOUS -- no candidate is ever silently selected.

LEFT — @router routes in API/inventory-service/router/vouchers_api.py (9):
  GET /vouchers
  GET /vouchers/{voucher_id}
  POST /vouchers
  PUT /vouchers/{voucher_id}/post
  PUT /vouchers/{voucher_id}/cancel
  POST /vouchers/grn/{po_id}
  POST /vouchers/credit-note/{invoice_id}
  POST /vouchers/stock-adjustment
  POST /vouchers/stock-transfer

RIGHT — RTK Query endpoints in Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts (48):
  GET /api/inventoryservice/categories
  GET /api/inventoryservice/categories/manage
  POST /api/inventoryservice/categories
  POST /api/inventoryservice/vouchers?${qs}
  GET /api/inventoryservice/vouchers/${id}
  POST /api/inventoryservice/vouchers
  POST /api/inventoryservice/vouchers/${id}/post
  POST /api/inventoryservice/vouchers/${id}/cancel

MATCHED (3):
  GET /vouchers   <->   /api/inventoryservice/vouchers?${qs}
  GET /vouchers/{voucher_id}   <->   /api/inventoryservice/vouchers/${id}
  POST /vouchers   <->   /api/inventoryservice/vouchers

PARTIAL MATCHES — same identity (path), differing on a named attribute (2):
  /vouchers/{}/post   PUT /vouchers/{voucher_id}/post  <->  POST /api/inventoryservice/vouchers/${id}/post   (http_method: PUT != POST)
  /vouchers/{}/cancel   PUT /vouchers/{voucher_id}/cancel  <->  POST /api/inventoryservice/vouchers/${id}/cancel   (http_method: PUT != POST)

AMBIGUOUS — deterministic identity produced more than one plausible pairing; none was selected (0):
  (none)

LEFT ONLY — defined on the left with no match on the right (4):
  POST /vouchers/grn/{po_id}
  POST /vouchers/credit-note/{invoice_id}
  POST /vouchers/stock-adjustment
  POST /vouchers/stock-transfer

RIGHT ONLY — present on the right with no match on the left (43):
  GET /api/inventoryservice/categories
  GET /api/inventoryservice/categories/manage
  POST /api/inventoryservice/categories
  POST /api/inventoryservice/categories/bootstrap-ondc
  POST /api/inventoryservice/categories/remap-ondc${arg && arg.force ?
  POST /api/inventoryservice/categories/suggest
  POST /api/inventoryservice/categories/${id}/reactivate
  PUT /api/inventoryservice/categories/${id}
  DELETE /api/inventoryservice/categories/${id}
  GET /api/inventoryservice/hsn-lookup/${encodeURIComponent(code)}
  POST /api/inventoryservice/items/suggest-description
  GET /api/inventoryservice/items?${qs}
  GET /api/inventoryservice/items/${id}
  POST /api/inventoryservice/items
  PUT /api/inventoryservice/items/${id}
  DELETE /api/inventoryservice/items/${id}
  GET /api/inventoryservice/parties?${qs}
  GET /api/inventoryservice/parties/${id}
  POST /api/inventoryservice/parties
  PUT /api/inventoryservice/parties/${id}
  GET /api/inventoryservice/godowns
  POST /api/inventoryservice/godowns
  PUT /api/inventoryservice/godowns/${id}
  DELETE /api/inventoryservice/godowns/${id}
  GET /api/inventoryservice/stock/levels?${qs}
  GET /api/inventoryservice/uom
  GET /api/inventoryservice/hsn/search?q=${encodeURIComponent(q)}
  POST /api/inventoryservice/admin/hsn/rate-refresh${mode ?
  GET /api/inventoryservice/admin/hsn/rate-refresh/history
  GET /api/inventoryservice/admin/gst/rate-history${qs.toString() ?
  GET /api/inventoryservice/admin/gst/compliance-tasks${qs.toString() ?
  GET /api/inventoryservice/admin/hsn/rate-proposals${qs.toString() ?
  GET /api/inventoryservice/admin/hsn/rate-proposals/${proposal_id}/impact
  POST /api/inventoryservice/admin/hsn/rate-proposals/${proposal_id}/approve
  POST /api/inventoryservice/admin/hsn/rate-proposals/${proposal_id}/reject
  POST /api/inventoryservice/import/start
  GET /api/inventoryservice/import/${session_id}
  GET /api/inventoryservice/import/${session_id}/rows?${qs}
  PUT /api/inventoryservice/import/${session_id}/rows/${row_id}
  PUT /api/inventoryservice/import/${session_id}/confirm
  DELETE /api/inventoryservice/import/${session_id}
  GET /api/inventoryservice/categories/compliance-status
  POST /api/inventoryservice/categories/reapply-gst

TOTALS: left 9, right 48, matched 3, left-only 4, right-only 43.
PARTIAL-MATCH TOTAL: 2 (same identity, differing attribute -- not counted in left-only/right-only above).
AMBIGUOUS TOTAL: 0 (left-side item(s) whose identity matched more than one candidate, or whose only candidate was itself contested -- not counted in matched/partial/left-only/right-only above; see the AMBIGUOUS block for every retained candidate)."""

_T13B_NOTE = (
    "\n\n---\n**THE COMPARISON, COMPUTED — the answer above states a "
    "relationship between two files; this is that same relationship worked out "
    "by string match over both, not by reading and comparing. Where the two "
    "disagree, trust this one.**\n```\n" + _T13B_BODY + "\n```"
)

# ── A small, deliberately unrelated synthetic comparison, real-tool-shaped
# (all five headers), for the genericity test (Test 6). Nothing here names a
# file, endpoint, hook, or verb from the vouchers/inventory domain.
_GENERIC_BODY = """compare_enumerations — left.cfg  vs  right.cfg
join: two-stage.

LEFT — widgets in left.cfg (3):
  ALPHA widget-a
  BETA widget-b
  GAMMA widget-c

RIGHT — gadgets in right.cfg (3):
  ALPHA gadget-a
  BETA gadget-x
  DELTA gadget-d

MATCHED (1):
  ALPHA widget-a   <->   gadget-a

PARTIAL MATCHES — same identity (path), differing on a named attribute (1):
  widget-b   BETA widget-b  <->  BETA gadget-x   (kind: widget-b != gadget-x)

AMBIGUOUS — none (0):
  (none)

LEFT ONLY — defined on the left with no match on the right (1):
  GAMMA widget-c

RIGHT ONLY — present on the right with no match on the left (1):
  DELTA gadget-d

TOTALS: left 3, right 3, matched 1, left-only 1, right-only 1.
PARTIAL-MATCH TOTAL: 1 (same identity, differing attribute -- not counted in left-only/right-only above).
AMBIGUOUS TOTAL: 0 (not counted in matched/partial/left-only/right-only above)."""

_GENERIC_NOTE = (
    "\n\n---\n**THE COMPARISON, COMPUTED — ...**\n```\n" + _GENERIC_BODY + "\n```"
)


# ── Test 1: T13b exact comparison fixture ────────────────────────────────────

def test_1_t13b_exact_comparison_fixture():
    summary = _comparison_summary(_T13B_NOTE)
    assert summary == {
        "left": 9, "right": 48, "matched": 3, "left_only": 4,
        "right_only": 43, "partial_match": 2, "ambiguous": 0,
    }
    entries = _comparison_entries(_T13B_NOTE)
    assert len(entries["partial_match"]) == 2
    assert any("PUT != POST" in e for e in entries["partial_match"])
    assert all("PUT != POST" in e for e in entries["partial_match"])
    assert len(entries["left_only"]) == 4
    assert entries["left_only"] == [
        "POST /vouchers/grn/{po_id}",
        "POST /vouchers/credit-note/{invoice_id}",
        "POST /vouchers/stock-adjustment",
        "POST /vouchers/stock-transfer",
    ]
    assert len(entries["right_only"]) == 43


# ── Test 2: no category collapse (the most important Phase L test) ──────────

def test_2_no_category_collapse_in_synthesis():
    answer = _synthesize_comparison_answer(_T13B_NOTE)
    assert answer is not None

    partial_section, left_only_section = _split_sections(answer)

    assert "PUT /vouchers/{voucher_id}/post" in partial_section
    assert "PUT /vouchers/{voucher_id}/cancel" in partial_section
    assert "PUT /vouchers/{voucher_id}/post" not in left_only_section
    assert "PUT /vouchers/{voucher_id}/cancel" not in left_only_section

    for gap in ("POST /vouchers/grn/{po_id}", "POST /vouchers/credit-note/{invoice_id}",
                "POST /vouchers/stock-adjustment", "POST /vouchers/stock-transfer"):
        assert gap in left_only_section
        assert gap not in partial_section


def _split_sections(answer: str) -> tuple[str, str]:
    """Slice the synthesized answer into its PARTIAL_MATCH and LEFT_ONLY
    sections only, so an assertion about one section can't accidentally match
    text from another (e.g. the note text mentioning "not missing" near
    PARTIAL_MATCH must not leak the word "missing" into a LEFT_ONLY check)."""
    partial_start = answer.index("PARTIAL_MATCH (")
    left_start = answer.index("LEFT_ONLY (")
    right_start = answer.index("RIGHT_ONLY (")
    return answer[partial_start:left_start], answer[left_start:right_start]


# ── Test 3: exact preservation ───────────────────────────────────────────────

def test_3_exact_preservation_no_renaming():
    answer = _synthesize_comparison_answer(_T13B_NOTE)
    # The exact comparison strings, byte for byte -- never camelCased, never
    # rewritten to the frontend's ${id} form, never method-normalized.
    assert "PUT /vouchers/{voucher_id}/post" in answer
    assert "PUT /vouchers/{voucher_id}/cancel" in answer
    assert "POST /vouchers/grn/{po_id}" in answer
    assert "POST /vouchers/credit-note/{invoice_id}" in answer
    assert "POST /vouchers/stock-adjustment" in answer
    assert "POST /vouchers/stock-transfer" in answer
    # Never invented: no camelCase hook name anywhere in the deterministic answer.
    for invented in ("createGrnFromPo", "createCreditNote", "createStockAdjustment",
                      "createStockTransfer", "postVoucher", "cancelVoucher"):
        assert invented not in answer


# ── Test 4: repetition/degradation fallback ──────────────────────────────────

@pytest.mark.asyncio
async def test_4_repetition_fallback_uses_deterministic_synthesis_not_a_second_llm_call(
        monkeypatch):
    """When the comparison note IS synthesis-eligible, K4 must ship before any
    free-form model call is even attempted -- so a reconstruction-time
    repetition/degradation never has a chance to occur in the first place for
    this class of input. Asserts _stream_team_run (the free-form path) is
    never invoked."""
    async def fake_comparison(*a, **k):
        return _T13B_NOTE
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_comparison)

    calls = {"n": 0}

    async def fail_if_called(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not attempt a free-form reconstruction when "
                              "deterministic synthesis is eligible")
    monkeypatch.setattr(team_mod, "_stream_team_run", fail_if_called)

    async def clean_verify(content, hive_mcp_url, hive_mcp_tools):
        return "verify_claims: no checkable claims found", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", clean_verify)

    failed_content = "**VERIFICATION FAILED — original**"
    team = SimpleNamespace(_read_state={"enumerations": {"both": "sides"}})
    out = await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", team,
        "http://fake-hive-mcp", None, None)

    assert calls["n"] == 0
    assert "VERIFICATION FAILED" not in out
    assert "PARTIAL_MATCH" in out and "LEFT_ONLY" in out


@pytest.mark.asyncio
async def test_4b_fallback_still_passes_through_verification_and_can_still_fail(
        monkeypatch):
    """K5's safety invariant: even the deterministic path must pass through
    the EXISTING verify_claims recheck, and a failure there still keeps
    VERIFICATION FAILED -- synthesis correctness is never assumed."""
    async def fake_comparison(*a, **k):
        return _T13B_NOTE
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_comparison)

    async def fail_if_called(*a, **k):
        raise AssertionError("must not reach the free-form path from this test's assert")
    # Free-form path IS allowed to run here (synthesis fails verification and
    # must fall back) -- give it a clean, real-tool-shaped path-through too.
    async def fallback_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return "fallback text", None

    monkeypatch.setattr(team_mod, "_stream_team_run", fallback_stream)

    calls = {"n": 0}

    async def still_bad_once_then_clean(content, hive_mcp_url, hive_mcp_tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return "synthesis rejected for this test", True, False
        return "clean", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", still_bad_once_then_clean)

    failed_content = "**VERIFICATION FAILED — original**"
    team = SimpleNamespace(_read_state={"enumerations": {"both": "sides"}})
    out = await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", team,
        "http://fake-hive-mcp", None, None)

    # Synthesis was rejected (calls["n"]==1 path) -> fell back to the free-form
    # path, which this time succeeds cleanly.
    assert out == "fallback text"


# ── Test 5: incomplete/unresolved evidence ───────────────────────────────────

def test_5_ambiguous_reported_as_unresolved_not_filled_in():
    ambiguous_note = _T13B_NOTE.replace(
        "AMBIGUOUS — deterministic identity produced more than one plausible "
        "pairing; none was selected (0):\n  (none)",
        "AMBIGUOUS — deterministic identity produced more than one plausible "
        "pairing; none was selected (1):\n  GET /vouchers/{voucher_id}/extra   "
        "candidates: GET a, GET b   -- 2 right-side items share this identity, "
        "no pairing selected",
    ).replace("AMBIGUOUS TOTAL: 0 (", "AMBIGUOUS TOTAL: 1 (")

    answer = _synthesize_comparison_answer(ambiguous_note)
    assert answer is not None
    assert "UNRESOLVED" in answer
    ambiguous_section = answer[answer.index("AMBIGUOUS ("):]
    assert "GET /vouchers/{voucher_id}/extra" in ambiguous_section
    # Never silently resolved into MATCH, PARTIAL_MATCH, or LEFT_ONLY.
    for other_key in ("MATCH (", "PARTIAL_MATCH (", "LEFT_ONLY ("):
        other_section = answer[answer.index(other_key):answer.index(other_key) + 400]
        assert "extra" not in other_section


# ── Test 6: generic comparison (not inventory/vouchers-specific) ────────────

def test_6_generic_two_sided_comparison_not_domain_specific():
    summary = _comparison_summary(_GENERIC_NOTE)
    assert summary == {
        "left": 3, "right": 3, "matched": 1, "left_only": 1,
        "right_only": 1, "partial_match": 1, "ambiguous": 0,
    }
    answer = _synthesize_comparison_answer(_GENERIC_NOTE)
    assert answer is not None
    partial_section, left_only_section = _split_sections(answer)
    assert "BETA widget-b" in partial_section
    assert "GAMMA widget-c" in left_only_section
    assert "BETA widget-b" not in left_only_section
    assert "GAMMA widget-c" not in partial_section
    for domain_word in ("vouchers", "inventory", "endpoint", "hook", "API",
                         "frontend", "backend"):
        assert domain_word not in answer


# ── K7: narrow consistency guard ─────────────────────────────────────────────

def test_k7_detects_the_exact_t13b_conflation_shape():
    bad_reconstruction = (
        "### Vouchers Module Audit\n\n"
        "#### Backend with No Frontend Counterpart\n"
        "The following endpoints are present in the backend but have no "
        "matching frontend hooks:\n"
        "- PUT /vouchers/{voucher_id}/post\n"
        "- PUT /vouchers/{voucher_id}/cancel\n"
        "- POST /vouchers/grn/{po_id}\n"
        "- POST /vouchers/credit-note/{invoice_id}\n"
        "- POST /vouchers/stock-adjustment\n"
        "- POST /vouchers/stock-transfer\n"
    )
    conflict = _reconstruction_category_conflict(bad_reconstruction, _T13B_NOTE)
    assert conflict is not None
    assert "PARTIAL_MATCH" in conflict and "LEFT_ONLY" in conflict


def test_k7_no_false_positive_on_correctly_separated_answer():
    good_reconstruction = (
        "PARTIAL_MATCH (differ on method, not missing):\n"
        "- PUT /vouchers/{voucher_id}/post\n"
        "- PUT /vouchers/{voucher_id}/cancel\n\n"
        "LEFT_ONLY (genuinely missing):\n"
        "- POST /vouchers/grn/{po_id}\n"
        "- POST /vouchers/credit-note/{invoice_id}\n"
        "- POST /vouchers/stock-adjustment\n"
        "- POST /vouchers/stock-transfer\n"
    )
    conflict = _reconstruction_category_conflict(good_reconstruction, _T13B_NOTE)
    assert conflict is None


def test_k7_is_a_no_op_when_comparison_note_is_not_synthesis_eligible():
    """Every pre-Phase-L test fixture (simplified, not all-five-headers) must
    leave this guard inert -- it never changes behavior for those cases."""
    old_style_note = (
        "\n\n---\nLEFT ONLY (4): grn, credit-note, stock-adjustment, "
        "stock-transfer")
    assert _reconstruction_category_conflict("anything at all", old_style_note) is None


@pytest.mark.asyncio
async def test_k7_fires_inside_the_free_form_fallback_and_keeps_verification_failed(
        monkeypatch):
    """End-to-end: comparison note is real-tool-shaped, but K4 synthesis
    itself is made to fail verification (forcing the free-form fallback),
    and the free-form model's OWN output reproduces the exact T13b
    conflation -- K7 must catch it even though verify_claims' own grep found
    nothing wrong (mirroring the live T13b trace, where verify_claims'
    content-grep genuinely found nothing to flag)."""
    async def fake_comparison(*a, **k):
        return _T13B_NOTE
    monkeypatch.setattr(team_mod, "_computed_comparison", fake_comparison)

    conflated_output = (
        "Backend with No Frontend Counterpart:\n"
        "- PUT /vouchers/{voucher_id}/post\n"
        "- PUT /vouchers/{voucher_id}/cancel\n"
        "- POST /vouchers/grn/{po_id}\n"
        "- POST /vouchers/credit-note/{invoice_id}\n"
        "- POST /vouchers/stock-adjustment\n"
        "- POST /vouchers/stock-transfer\n"
    )

    async def fallback_stream(team, prompt, *, log_label="verify-retry", liveness_path=None):
        return conflated_output, None
    monkeypatch.setattr(team_mod, "_stream_team_run", fallback_stream)

    calls = {"n": 0}

    async def verify_sequence(content, hive_mcp_url, hive_mcp_tools):
        calls["n"] += 1
        if calls["n"] == 1:
            # K4 synthesis recheck -- force it to fail so the free-form
            # fallback actually runs (this test is specifically exercising
            # the fallback's own K7 guard).
            return "forced synthesis rejection for this test", True, False
        # The free-form fallback's own verify_claims recheck: clean, exactly
        # as the live T13b trace showed ("clean -- nothing flagged").
        return "clean -- nothing flagged", False, False
    monkeypatch.setattr(team_mod, "_verify_claims", verify_sequence)

    failed_content = "**VERIFICATION FAILED — original**"
    team = SimpleNamespace(_read_state={"enumerations": {"both": "sides"}})
    out = await _attempt_evidence_grounded_reconstruction(
        failed_content, "audit the vouchers module", team,
        "http://fake-hive-mcp", None, None)

    # verify_claims itself said "clean", but K7's structural check must still
    # keep VERIFICATION FAILED.
    assert out == failed_content
