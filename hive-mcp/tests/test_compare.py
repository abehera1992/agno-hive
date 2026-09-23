"""compare_enumerations: reproduction + regression tests for Experiment 6 Phase 1.

No existing tests covered this tool before this file -- it shipped in one commit
(f0dcf44, 2026-09-02) with zero test coverage. These tests first REPRODUCE the current
behavior (including the R5 Groundedness-Battery wrong-file defect) against the
unmodified tool, then pin the minimal validation added to close it.

Fixtures are synthetic and framework-shaped (FastAPI @router.* / RTK Query `endpoint:`),
never EkamApp-specific -- compare.py's own docstring says the extractor shapes are
framework conventions, not project ones, and the tests follow that same rule.
"""
from tools import compare


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "PROJECT_ROOT", tmp_path)
    return tmp_path


# ── baseline: valid pairs behave as designed (establishes the tool works before
#    touching it) ──────────────────────────────────────────────────────────────────

def test_valid_pair_with_a_genuine_gap(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.get("/status")\n@router.post("/register")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/api/svc/status",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "LEFT ONLY — defined on the left with no match on the right (1):" in out
    assert "  POST /register" in out
    assert "TOTALS: left 2, right 1, matched 1, left-only 1, right-only 0." in out


def test_valid_pair_with_full_coverage(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/status")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/api/svc/status",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "TOTALS: left 1, right 1, matched 1, left-only 0, right-only 0." in out


def test_nonexistent_left_file_is_already_caught(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "right.ts").write_text('endpoint: "/api/svc/status",\n', encoding="utf-8")
    out = compare.compare_enumerations("missing.py", "right.ts")
    assert out.startswith("compare_enumerations failed:")
    assert "not a file: missing.py" in out


def test_nonexistent_right_file_is_already_caught(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/status")\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "missing.ts")
    assert out.startswith("compare_enumerations failed:")
    assert "not a file: missing.ts" in out


def test_path_escaping_project_root_is_already_caught(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    out = compare.compare_enumerations("../../etc/passwd", "../../etc/hosts")
    assert out.startswith("compare_enumerations failed:")
    assert "escapes the project root" in out


def test_both_sides_empty_is_already_caught(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text("# no routes here\n", encoding="utf-8")
    (root / "right.ts").write_text("// no endpoints here\n", encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert out.startswith("No endpoints found in either file.")


# ── reproduction: the R5 Groundedness-Battery defect ────────────────────────────────
# R5 T2 (2026-09-11): the Coordinator invented a wrong frontend target
# ("frontend/src/store/slices/businessSlice.js"), the Researcher's search recovered an
# UNRELATED real file (a page component) instead, and compare_enumerations was called
# with that unrelated file as right_path. LEFT (business_api.py) had 13 real routes;
# RIGHT (the page component) had 0 RTK Query endpoints, because it is not an API slice
# file at all. The tool reported "LEFT ONLY (13)" with no indication that RIGHT was
# empty, which the model's own header text ("where the two disagree, trust this one")
# then treated as authoritative -- an empty, wrong-target comparison presented with the
# same confidence as a real one.

def test_reproduction_one_sided_empty_target_now_carries_a_warning(tmp_path, monkeypatch):
    """R5 T2 (2026-09-11): the Coordinator invented a wrong frontend target, the
    Researcher's search recovered an unrelated real file (a page component) instead,
    and compare_enumerations was called with that unrelated file as right_path. LEFT
    (business_api.py) had 13 real routes; RIGHT (the page component) had 0 RTK Query
    endpoints -- not an API slice file at all. Reproduced here with a synthetic,
    non-EkamApp-specific fixture (an ordinary .tsx page component with no RTK Query
    `endpoint:` field at all). Pins the FIXED behavior: the lopsided extraction now
    carries an explicit, unmissable warning ahead of the raw data, which itself
    remains fully visible below it -- unchanged transparency, added signal."""
    root = _setup(tmp_path, monkeypatch)
    (root / "business_api.py").write_text(
        '@router.get("/status")\n@router.post("/register")\n'
        '@router.get("/my-businesses")\n', encoding="utf-8")
    (root / "email_page.tsx").write_text(
        'export default function EmailPage() {\n'
        '  const { data } = useGetInboxQuery();\n'
        '  return <div>{data}</div>;\n}\n', encoding="utf-8")
    out = compare.compare_enumerations("business_api.py", "email_page.tsx")
    assert "WARNING: email_page.tsx yielded ZERO RTK Query endpoints" in out
    assert "business_api.py yielded 3" in out
    assert "wrong file for this comparison" in out
    # the warning is a PREPEND, not a replacement -- the full data is still there
    assert "RIGHT — RTK Query endpoints in email_page.tsx (0):" in out
    assert "LEFT ONLY — defined on the left with no match on the right (3):" in out
    assert "TOTALS: left 3, right 0, matched 0, left-only 3, right-only 0." in out
    # the warning appears before the data blocks, not buried after them
    assert out.index("WARNING") < out.index("LEFT —")


def test_warning_names_whichever_side_is_empty_not_a_fixed_side(tmp_path, monkeypatch):
    """The check is symmetric and content-blind -- it must fire identically when LEFT
    is the empty side, naming LEFT by path, never assuming 'left is always correct'."""
    root = _setup(tmp_path, monkeypatch)
    (root / "unrelated.py").write_text("# a python file with no routes\n", encoding="utf-8")
    (root / "real_api.ts").write_text(
        'endpoint: "/api/svc/status",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("unrelated.py", "real_api.ts")
    assert "WARNING: unrelated.py yielded ZERO @router routes" in out
    assert "real_api.ts yielded 1" in out


def test_no_warning_when_both_sides_have_matches(tmp_path, monkeypatch):
    """No false positive on a normal, healthy comparison with real content on both
    sides and a genuine partial gap -- the exact shape of test_valid_pair_with_a_
    genuine_gap above, re-asserted here specifically for warning ABSENCE."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.get("/status")\n@router.post("/register")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/api/svc/status",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "WARNING" not in out


def test_no_warning_when_both_sides_have_full_coverage(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/status")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/api/svc/status",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "WARNING" not in out


def test_no_warning_when_both_sides_are_empty(tmp_path, monkeypatch):
    """The pre-existing both-empty short-circuit still returns before the new check
    is even reached -- the new check must not change that message or double-fire."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text("# no routes here\n", encoding="utf-8")
    (root / "right.ts").write_text("// no endpoints here\n", encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert out.startswith("No endpoints found in either file.")
    assert "WARNING" not in out


def test_wrong_but_plausible_target_is_not_caught_by_this_check(tmp_path, monkeypatch):
    """Documents a REAL, ACKNOWLEDGED LIMIT of this mechanical check, not a defect in
    it: a real, unrelated file with its OWN genuine endpoints (e.g. a sibling API
    slice for a different domain) produces two non-empty sides and triggers no
    warning, even though the comparison is still semantically wrong. Catching this
    would require knowing which domain each file belongs to -- exactly the
    project-specific / LLM-reasoning territory this phase was told not to enter.
    Left as a known gap for a later phase, not silently declared solved here."""
    root = _setup(tmp_path, monkeypatch)
    (root / "business_api.py").write_text(
        '@router.get("/status")\n@router.post("/register")\n', encoding="utf-8")
    (root / "unrelated_but_real_api.ts").write_text(
        'endpoint: "/api/svc/totally-different-domain",\nmethod: "get",\n',
        encoding="utf-8")
    out = compare.compare_enumerations("business_api.py", "unrelated_but_real_api.ts")
    assert "WARNING" not in out
    assert "TOTALS: left 2, right 1, matched 0, left-only 2, right-only 1." in out


def test_unrecognised_file_type_on_one_side_also_warns(tmp_path, monkeypatch):
    """An unsupported extension (e.g. a doc file) is an even clearer wrong-target
    signal than same-extension-zero-matches, and the check fires on the same
    empty-vs-nonempty extraction count regardless of why the side is empty."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/status")\n', encoding="utf-8")
    (root / "README.md").write_text("# Not a route file at all\n", encoding="utf-8")
    out = compare.compare_enumerations("left.py", "README.md")
    assert "WARNING: README.md yielded ZERO unrecognised file type" in out


# ── Phase J-A: identity vs attribute, PARTIAL_MATCH ──────────────────────────────────
#
# Root-caused against the live Phase I T13b ZGX validation: the backend declares
# `PUT /vouchers/{voucher_id}/post`, the frontend's RTK Query definition for the same
# operation declares `method: "post"`. Before this phase, method was fused into route
# identity (_joins checked it first and gated everything else), so a method
# disagreement made the pair invisible to each other -- reported as LEFT ONLY,
# indistinguishable from a genuinely unrelated route. These tests pin the fix:
# identity is the path alone; method is a reported ATTRIBUTE difference, never a
# PUT<->POST equivalence rule (this phase explicitly does not special-case any pair
# of values -- the fix is structural, not a lookup table).

def test_A1_exact_match(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (1):" in out
    assert "PARTIAL MATCHES — same identity (path), differing on a named attribute (0):" in out
    assert "TOTALS: left 1, right 1, matched 1, left-only 0, right-only 0." in out
    assert "PARTIAL-MATCH TOTAL: 0" in out


def test_A2_parameter_name_normalization_is_still_a_full_match(tmp_path, monkeypatch):
    """Case B from the Phase J investigation: parameter-NAME differences are
    already inside identity normalization (_PARAM_RE), not a new attribute
    category -- this must remain MATCHED, never PARTIAL_MATCH."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/x/{id}")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/x/${thing_id}",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "TOTALS: left 1, right 1, matched 1, left-only 0, right-only 0." in out
    assert "PARTIAL-MATCH TOTAL: 0" in out


def test_A3_method_mismatch_is_partial_match_not_left_only(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.put("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "post",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "LEFT ONLY — defined on the left with no match on the right (0):" in out
    assert "PARTIAL-MATCH TOTAL: 1" in out
    assert "http_method: PUT != POST" in out
    assert "TOTALS: left 1, right 1, matched 0, left-only 0, right-only 0." in out


def test_A3b_partial_match_preserves_both_real_values_never_pretends_equal(
        tmp_path, monkeypatch):
    """Safety requirement: the mismatch must remain visible, never silently
    resolved into a MATCH or an invented equivalence."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.put("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "post",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (0):" in out
    assert "PUT" in out and "POST" in out
    assert "PUT != POST" in out  # the disagreement itself is the reported fact


def test_A5_genuine_absence_is_still_left_only(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.post("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/y",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "LEFT ONLY — defined on the left with no match on the right (1):" in out
    assert "  POST /x" in out
    assert "PARTIAL-MATCH TOTAL: 0" in out


def test_A6_right_only_is_unaffected(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/x",\nmethod: "get",\nendpoint: "/y",\nmethod: "post",\n',
        encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "RIGHT ONLY — present on the right with no match on the left (1):" in out
    assert "  POST /y" in out
    assert "PARTIAL-MATCH TOTAL: 0" in out


def test_A7_real_ekam_shape_post_and_cancel_become_partial_match(tmp_path, monkeypatch):
    """The exact live-validated T13b shape, reproduced with a synthetic,
    non-EkamApp-specific fixture (this file's own established convention):
    backend declares PUT for two operations the frontend declares POST for
    (same path both times), plus two backend-only operations with no
    frontend counterpart at all. Expected: PARTIAL_MATCH=2, LEFT_ONLY=2 --
    the genuinely unmatched operations, not conflated with the attribute
    mismatches. Never described as "4 gaps" or "2 gaps" without qualifying
    which category each number belongs to."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.put("/vouchers/{voucher_id}/post")\n'
        '@router.put("/vouchers/{voucher_id}/cancel")\n'
        '@router.post("/vouchers/grn/{po_id}")\n'
        '@router.post("/vouchers/credit-note/{invoice_id}")\n',
        encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: `/api/svc/vouchers/${id}/post`,\nmethod: "post",\n'
        'endpoint: `/api/svc/vouchers/${id}/cancel`,\nmethod: "post",\n',
        encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "PARTIAL-MATCH TOTAL: 2" in out
    assert "LEFT ONLY — defined on the left with no match on the right (2):" in out
    assert "  POST /vouchers/grn/{po_id}" in out
    assert "  POST /vouchers/credit-note/{invoice_id}" in out
    assert "PUT /vouchers/{voucher_id}/post" in out
    assert "PUT /vouchers/{voucher_id}/cancel" in out
    assert "TOTALS: left 4, right 2, matched 0, left-only 2, right-only 0." in out


def test_a_left_item_with_a_unique_full_match_is_not_made_ambiguous_by_a_sibling(
        tmp_path, monkeypatch):
    """Refines the original J-A regression test once J-B's real data run
    (validated directly against the live EkamApp files) proved the naive "any
    shared path is ambiguous" rule wrong: GET /vouchers and POST /vouchers
    share one path-only identity with two real, distinct right-side operations,
    and treating that as AMBIGUOUS would have destroyed two genuine, certain
    matches -- method already discriminates them perfectly. A left item whose
    method gives it a UNIQUE full match must resolve to MATCH regardless of
    what its path-sharing sibling's own fate is; the sibling with no matching
    method correctly becomes LEFT_ONLY, not ambiguous, because there is
    nothing left for it to compete over once the true match is taken."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.get("/x")\n@router.put("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (1):" in out
    assert "GET /x   <->   /x" in out
    assert "AMBIGUOUS TOTAL: 0" in out
    assert "LEFT ONLY — defined on the left with no match on the right (1):" in out
    assert "  PUT /x" in out


def test_many_to_one_is_ambiguous_only_when_neither_side_has_a_full_match(
        tmp_path, monkeypatch):
    """The genuine many-to-one shape: two left items whose OWN methods do not
    match the single right-side candidate's method at all, so neither gets a
    stage-1 full match and both fall through to stage 2's identity-only
    fallback, where they truly, deterministically compete for the same sole
    candidate. Neither may be silently resolved; both become AMBIGUOUS."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.put("/x")\n@router.delete("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (0):" in out
    assert "PARTIAL-MATCH TOTAL: 0" in out
    assert "LEFT ONLY — defined on the left with no match on the right (0):" in out
    assert "RIGHT ONLY — present on the right with no match on the left (0):" in out
    assert "AMBIGUOUS TOTAL: 2" in out
    assert "PUT /x   candidates: GET /x" in out
    assert "DELETE /x   candidates: GET /x" in out
    assert "also claimed by: DELETE /x" in out
    assert "also claimed by: PUT /x" in out


def test_no_warning_regression_with_partial_matches_present(tmp_path, monkeypatch):
    """The lopsided-extraction WARNING must still fire/not-fire on the same
    empty-vs-nonempty rule, unaffected by partial-match classification."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.put("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "post",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "WARNING" not in out


# ── Phase J-B: deterministic AMBIGUOUS / multi-candidate matching ───────────────────
#
# The Phase J investigation found a real, confirmed defect in the J-A-era matching
# loop: `next((r for r in right if ...), None)` -- first identity candidate wins,
# silently, in both directions. This section makes multi-candidate identity explicit
# rather than resolved by file order. No fuzzy matching, no similarity score, no
# assignment/optimisation solver -- purely deterministic candidate accounting.

def test_B1_single_candidate_is_match(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (1):" in out
    assert "AMBIGUOUS TOTAL: 0" in out


def test_B2_single_candidate_with_attribute_mismatch_is_partial_match(
        tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.put("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "post",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "PARTIAL-MATCH TOTAL: 1" in out
    assert "AMBIGUOUS TOTAL: 0" in out


def test_B3_zero_candidates_is_left_only(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/y",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "LEFT ONLY — defined on the left with no match on the right (1):" in out
    assert "AMBIGUOUS TOTAL: 0" in out


def test_B4_multiple_candidates_is_ambiguous(tmp_path, monkeypatch):
    """One-to-many, genuinely: LEFT DELETE /x has no full-match discriminator
    against either right-side method, so it reaches stage 2 and finds two
    real identity candidates there."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.delete("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/x",\nmethod: "get",\nendpoint: "/x",\nmethod: "post",\n',
        encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (0):" in out
    assert "PARTIAL-MATCH TOTAL: 0" in out
    assert "AMBIGUOUS TOTAL: 1" in out
    assert "candidates: GET /x, POST /x" in out


def test_B5_every_candidate_is_preserved_in_the_output(tmp_path, monkeypatch):
    """Three right-side candidates for one left-side identity -- all three must
    be individually visible, not just a count. LEFT's method (DELETE) matches
    none of them, so all three genuinely reach stage 2."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.delete("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/x",\nmethod: "get",\n'
        'endpoint: "/x",\nmethod: "post",\n'
        'endpoint: "/x",\nmethod: "put",\n',
        encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "AMBIGUOUS TOTAL: 1" in out
    assert "GET /x" in out and "POST /x" in out and "PUT /x" in out
    assert "candidates: GET /x, POST /x, PUT /x" in out


def test_B6_one_to_many_is_ambiguous_not_match_not_partial_match(tmp_path, monkeypatch):
    """The exact shape from the phase spec (LEFT one item, RIGHT two
    candidates), constructed so LEFT's own method matches NEITHER right-side
    candidate -- see test_a_left_item_with_a_unique_full_match_is_not_made_
    ambiguous_by_a_sibling for the discovered, documented refinement: a
    shared path is only genuinely AMBIGUOUS when no attribute discriminates
    it, never merely because more than one right-side item shares the path."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.delete("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/x",\nmethod: "get",\nendpoint: "/x",\nmethod: "post",\n',
        encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (0):" in out
    assert "PARTIAL-MATCH TOTAL: 0" in out
    assert "AMBIGUOUS TOTAL: 1" in out


def test_B7_many_to_one_is_ambiguous_documented_deterministic_behavior(
        tmp_path, monkeypatch):
    """The reverse shape: LEFT has two items (PUT /x, DELETE /x), RIGHT has
    only one /x (GET). Neither left item's method matches, so both genuinely
    reach stage 2 and compete for the same sole candidate. Decision made and
    documented here (see the module's own Phase J-B comment in compare.py): a
    right-side record claimed by more than one left-side item at that stage is
    NOT silently assigned to either -- both left items become AMBIGUOUS, and
    the shared candidate is named as contested. No Hungarian-algorithm-style
    optimal assignment was introduced; this is deterministic candidate
    accounting only."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.put("/x")\n@router.delete("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (0):" in out
    assert "PARTIAL-MATCH TOTAL: 0" in out
    assert "LEFT ONLY — defined on the left with no match on the right (0):" in out
    assert "RIGHT ONLY — present on the right with no match on the left (0):" in out
    assert "AMBIGUOUS TOTAL: 2" in out
    assert "PUT /x   candidates: GET /x" in out
    assert "DELETE /x   candidates: GET /x" in out


def test_B8_mixed_comparison_preserves_all_five_classifications(tmp_path, monkeypatch):
    """One fixture exercising MATCH, PARTIAL_MATCH, LEFT_ONLY, RIGHT_ONLY, and
    AMBIGUOUS simultaneously -- each category must be independently correct,
    not just individually testable in isolation. Also includes a "shared path,
    unique full match" pair (the real Ekam /vouchers shape) alongside the
    genuinely ambiguous one, so the fixture proves the two are told apart
    correctly in the same comparison, not just in separate tests."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.get("/match")\n'          # -> MATCH
        '@router.put("/partial")\n'        # -> PARTIAL_MATCH (method differs)
        '@router.get("/gone")\n'           # -> LEFT_ONLY (no right candidate)
        '@router.delete("/ambig")\n'       # -> AMBIGUOUS (no method discriminates)
        '@router.get("/shared")\n',        # -> MATCH (method discriminates a shared path)
        encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/match",\nmethod: "get",\n'
        'endpoint: "/partial",\nmethod: "post",\n'
        'endpoint: "/ambig",\nmethod: "get",\n'
        'endpoint: "/ambig",\nmethod: "post",\n'
        'endpoint: "/shared",\nmethod: "get",\n'
        'endpoint: "/shared",\nmethod: "post",\n'
        'endpoint: "/orphan",\nmethod: "get",\n',   # -> RIGHT_ONLY
        encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "MATCHED (2):" in out
    assert "GET /match   <->   /match" in out
    assert "GET /shared   <->   /shared" in out
    assert "PARTIAL-MATCH TOTAL: 1" in out and "http_method: PUT != POST" in out
    assert "LEFT ONLY — defined on the left with no match on the right (1):" in out
    assert "  GET /gone" in out
    assert "RIGHT ONLY — present on the right with no match on the left (2):" in out
    assert "  POST /shared" in out
    assert "  GET /orphan" in out
    assert "AMBIGUOUS TOTAL: 1" in out
    assert "DELETE /ambig   candidates: GET /ambig, POST /ambig" in out
    assert "TOTALS: left 5, right 7, matched 2, left-only 1, right-only 2." in out


def test_B9_JA_preserved_exact_match(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "TOTALS: left 1, right 1, matched 1, left-only 0, right-only 0." in out


def test_B9_JA_preserved_parameter_name_match(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.get("/x/{id}")\n', encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: "/x/${thing}",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "TOTALS: left 1, right 1, matched 1, left-only 0, right-only 0." in out


def test_B9_JA_preserved_method_mismatch_partial_match(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.put("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/x",\nmethod: "post",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "PARTIAL-MATCH TOTAL: 1" in out
    assert "http_method: PUT != POST" in out


def test_B9_JA_preserved_genuine_absence(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text('@router.post("/x")\n', encoding="utf-8")
    (root / "right.ts").write_text('endpoint: "/y",\nmethod: "get",\n', encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "LEFT ONLY — defined on the left with no match on the right (1):" in out


def test_B9_JA_preserved_real_ekam_shape(tmp_path, monkeypatch):
    """The real, live-validated T13b shape must still produce exactly
    PARTIAL_MATCH=2, LEFT_ONLY=2 (the synthetic 4-route fixture from A7) after
    J-B -- none of these routes has more than one identity candidate on either
    side, so J-B's new ambiguity handling must not change this result."""
    root = _setup(tmp_path, monkeypatch)
    (root / "left.py").write_text(
        '@router.put("/vouchers/{voucher_id}/post")\n'
        '@router.put("/vouchers/{voucher_id}/cancel")\n'
        '@router.post("/vouchers/grn/{po_id}")\n'
        '@router.post("/vouchers/credit-note/{invoice_id}")\n',
        encoding="utf-8")
    (root / "right.ts").write_text(
        'endpoint: `/api/svc/vouchers/${id}/post`,\nmethod: "post",\n'
        'endpoint: `/api/svc/vouchers/${id}/cancel`,\nmethod: "post",\n',
        encoding="utf-8")
    out = compare.compare_enumerations("left.py", "right.ts")
    assert "PARTIAL-MATCH TOTAL: 2" in out
    assert "LEFT ONLY — defined on the left with no match on the right (2):" in out
    assert "AMBIGUOUS TOTAL: 0" in out
    assert "TOTALS: left 4, right 2, matched 0, left-only 2, right-only 0." in out
