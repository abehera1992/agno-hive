"""T36 Track F -- preserve T13b's deterministic comparison; no code change for
the missing Phase-2/roadmap framing.

Investigation finding (no production code change follows from it -- see
below): T13b's live 2026-10-19 answer (session f4c97c78-e935-46cd-af1f-
135e7692b5bb, recovered verbatim from this conversation's own transcript)
correctly used `compare_enumerations` end to end -- LEFT (9 real backend
routes) and RIGHT (48 real frontend endpoints, from all 49 exported hooks)
exactly matched ground truth, Post/Cancel were correctly classified
PARTIAL_MATCH (same route, http_method PUT vs POST -- "these are NOT
missing", in the delivered text itself) rather than LEFT_ONLY, and the 4
genuine gaps (grn, credit-note, stock-adjustment, stock-transfer) were
reported exactly, with zero over-counting.

The one thing missing from the delivered answer is attributing those 4
gaps to the "Phase 2 -- Advanced Vouchers" roadmap item (per this
project's own Notion board and `docs/inventory.md:21`) rather than
reporting them as bare, unexplained gaps.

Traced to its source rather than assumed: the actual T13b TASK PROMPT (see
docs/runbooks/groundedness-battery.md's own "files named" variant) asks
only to "list its endpoints, its database tables, and its frontend hooks,
and identify anything present in the backend with no frontend
counterpart" -- it names the three files to read and nothing about Notion,
roadmaps, or phase status. The Phase-2 framing requirement exists ONLY in
the runbook's own grading rubric ("a full pass states the four and
attributes them to Phase 2"), written by a human evaluator documenting
what a COMPLETE answer should look like -- it was never part of the
contract handed to the swarm. This is not evidence-handoff loss, prompt
loss, or an incomplete output-validation contract (the four candidates
this track's own mission text asks to distinguish between): the swarm
answered exactly what it was asked, correctly and deterministically, and
was never told to check anything else.

Per this track's own explicit constraints ("do not add generic mandatory
sections to unrelated tasks"; "if the framing requirement is already
explicit in the task contract, verify whether it is lost... before adding
a new output guard"): no guard is added here. A generic "check Notion
before calling something a gap" requirement would fire on every two-sided
comparison task regardless of whether a roadmap even exists for its
domain -- real scope creep and real regression risk, for a rubric
requirement the task was never actually given. The smallest validation
needed before changing anything is adding the Phase-2 framing to the TASK
PROMPT itself (or the runbook's own live-probe template) the next time
this exact probe is run -- a text change to the test script, not the
production swarm.

These tests instead protect the PRESERVATION half of this track's mandate:
the deterministic comparison path, parsed through swarm/team.py's own
authoritative parsers, against the exact real T13b tool output.
"""
from swarm.team import (
    _comparison_body,
    _comparison_summary,
    _integrity_named_item_falsely_gapped,
)

# The real `compare_enumerations` footnote from the live T13b answer, verbatim
# (HTML entities decoded back to their literal source form).
_T13B_CMP_NOTE = (
    "\n\n---\n**THE COMPARISON, COMPUTED — the answer above states a "
    "relationship between two files; this is that same relationship worked "
    "out by string match over both, not by reading and comparing. Where the "
    "two disagree, trust this one.**\n```\n"
    "compare_enumerations — API/inventory-service/router/vouchers_api.py  vs  "
    "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts\n"
    "join: two-stage -- identity (path-boundary suffix match, exact string, "
    "no inference), then attribute agreement (http_method). Same identity "
    "with a differing attribute is PARTIAL MATCHES, not LEFT ONLY/RIGHT "
    "ONLY. Identity matching more than one candidate on either side is "
    "AMBIGUOUS -- no candidate is ever silently selected.\n\n"
    "LEFT — @router routes in API/inventory-service/router/vouchers_api.py (9):\n"
    "  GET /vouchers\n  GET /vouchers/{voucher_id}\n  POST /vouchers\n"
    "  PUT /vouchers/{voucher_id}/post\n  PUT /vouchers/{voucher_id}/cancel\n"
    "  POST /vouchers/grn/{po_id}\n  POST /vouchers/credit-note/{invoice_id}\n"
    "  POST /vouchers/stock-adjustment\n  POST /vouchers/stock-transfer\n\n"
    "MATCHED (3):\n"
    "  GET /vouchers   <->   /api/inventoryservice/vouchers?${qs}\n"
    "  GET /vouchers/{voucher_id}   <->   /api/inventoryservice/vouchers/${id}\n"
    "  POST /vouchers   <->   /api/inventoryservice/vouchers\n\n"
    "PARTIAL MATCHES — same identity (path), differing on a named attribute (2):\n"
    "  /vouchers/{}/post   PUT /vouchers/{voucher_id}/post  <->  "
    "POST /api/inventoryservice/vouchers/${id}/post   (http_method: PUT != POST)\n"
    "  /vouchers/{}/cancel   PUT /vouchers/{voucher_id}/cancel  <->  "
    "POST /api/inventoryservice/vouchers/${id}/cancel   (http_method: PUT != POST)\n\n"
    "AMBIGUOUS — deterministic identity produced more than one plausible "
    "pairing; none was selected (0):\n  (none)\n\n"
    "LEFT ONLY — defined on the left with no match on the right (4):\n"
    "  POST /vouchers/grn/{po_id}\n  POST /vouchers/credit-note/{invoice_id}\n"
    "  POST /vouchers/stock-adjustment\n  POST /vouchers/stock-transfer\n\n"
    "HOOKS EXPORTED BY "
    "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts (49):\n"
    "  useCancelVoucherMutation\n  useCreateVoucherMutation\n  useGetVoucherQuery\n"
    "  useGetVouchersQuery\n  usePostVoucherMutation\n\n"
    "TOTALS: left 9, right 48, matched 3, left-only 4, right-only 43.\n"
    "PARTIAL-MATCH TOTAL: 2 (same identity, differing attribute -- not "
    "counted in left-only/right-only above).\n"
    "AMBIGUOUS TOTAL: 0 (left-side item(s) whose identity matched more than "
    "one candidate, or whose only candidate was itself contested -- not "
    "counted in matched/partial/left-only/right-only above; see the "
    "AMBIGUOUS block for every retained candidate).\n```"
)


# ── Preserve: the deterministic comparison's own totals parse correctly ────

def test_real_t13b_totals_parse_exactly_right():
    summary = _comparison_summary(_T13B_CMP_NOTE)
    assert summary == {
        "left": 9, "right": 48, "matched": 3,
        "left_only": 4, "right_only": 43,
        "partial_match": 2, "ambiguous": 0,
    }


def test_comparison_body_extracts_the_raw_tool_block():
    body = _comparison_body(_T13B_CMP_NOTE)
    assert "LEFT ONLY — defined on the left with no match on the right (4):" in body
    assert "POST /vouchers/grn/{po_id}" in body


# ── Preserve: Post/Cancel classified PARTIAL_MATCH, never falsely gapped ───

def test_post_and_cancel_are_not_falsely_gapped_against_the_real_cmp_note():
    """If a future answer narrated Post/Cancel as plain gaps (reverting the
    PARTIAL_MATCH framing this run got right), the named-item guard must
    still catch it against this exact real tool output."""
    regressed_answer = (
        "The following backend endpoints are missing a frontend counterpart:\n\n"
        "- PUT /vouchers/{voucher_id}/post\n"
        "- PUT /vouchers/{voucher_id}/cancel\n"
        "- POST /vouchers/grn/{po_id}\n"
    )
    found = _integrity_named_item_falsely_gapped(regressed_answer, _T13B_CMP_NOTE)
    assert found is not None
    name, real = found
    assert name in (
        "PUT /vouchers/{voucher_id}/post", "PUT /vouchers/{voucher_id}/cancel",
        "/vouchers/{voucher_id}/post", "/vouchers/{voucher_id}/cancel",
    )


def test_the_real_delivered_answer_correctly_frames_partial_matches_as_not_missing():
    """The actual delivered T13b text explicitly says Post/Cancel 'are NOT
    missing' and lists them under PARTIAL_MATCH, not as gaps -- confirming
    no regression check is needed against the real delivered text itself."""
    real_answer_excerpt = (
        "PARTIAL_MATCH (2) -- present on BOTH sides but differing on a named "
        "attribute -- these are NOT missing:\n"
        "  /vouchers/{}/post   PUT /vouchers/{voucher_id}/post\n"
        "  /vouchers/{}/cancel   PUT /vouchers/{voucher_id}/cancel\n\n"
        "LEFT_ONLY (4) -- defined on the left with no match on the right at all:\n"
        "  POST /vouchers/grn/{po_id}\n  POST /vouchers/credit-note/{invoice_id}\n"
        "  POST /vouchers/stock-adjustment\n  POST /vouchers/stock-transfer\n"
    )
    assert _integrity_named_item_falsely_gapped(
        real_answer_excerpt, _T13B_CMP_NOTE) is None
