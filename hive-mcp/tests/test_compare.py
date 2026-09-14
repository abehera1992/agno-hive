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
