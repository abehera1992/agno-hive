"""T30 -- verify_claims false-positive classification and extraction integrity.

Three independent false-positive symptoms, all surfaced on the SAME T29 live
validation run (that phase's own final report, section 8), traced to three
distinct root causes in this module:

1. A bare, backticked filename with a single dot (`inventoryApi.ts`,
   `vouchers_api.py`) matched `_DOTTED_RE` and was classified as an
   `owner.attribute` symbol claim, producing SPLIT-FOUND noise once the
   downstream attribute-only grep fallback matched an unrelated file.
   Fixed by `_looks_like_bare_filename` (a narrow, structural check: exactly
   one dot, and the segment after it is a recognized file extension), applied
   before a dotted token is added to `idents`.

2. A bare `owner.verb` decorator-method reference (`router.post`) used
   descriptively in prose -- not as a specific route claim -- matched
   `_DOTTED_RE` too, and the plain repo-wide grep for the literal substring
   found it inside some OTHER file's own `@router.post(...)` usage, citing a
   file the answer never mentioned. Fixed by `_is_generic_decorator_reference`
   (narrow and contextual: only excluded when the SAME answer separately uses
   the token as a real decorator call, `@{tok}(`, which the pre-existing
   ROUTES mechanism already verifies specifically).

3. `_FENCED_RE`'s non-greedy fence pairing paired an UNCLOSED fence (left
   open by a mid-stream cutoff, e.g. Z16's repetition guard) with the NEXT,
   unrelated fence-open later in the answer, swallowing the prose and
   markdown heading in between as "block content" -- producing a false
   BLOCK NOT IN finding against whatever file was named nearby. Fixed by
   `_MD_HEADING_IN_BLOCK_RE`: a captured block containing a markdown ATX
   sub-heading (##+) is almost certainly a mis-paired span, not real source
   content, and is skipped rather than checked.

Each fix is independently scoped and verified NOT to weaken detection of a
genuine claim of the same general shape (a real dotted symbol, a real route
claim including the historical T13b fabricated-route shape, and a real
fabricated source quotation).
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_guard():
    verify._checked_answer_counts = {}
    yield
    verify._checked_answer_counts = {}


class _FakeRgFiles:
    def __init__(self, paths: list[str]):
        self.stdout = "\n".join(paths)


def _mock_rg_files(monkeypatch, paths: list[str]):
    monkeypatch.setattr(verify.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: _FakeRgFiles(paths))


def _rg_noop(*a, **k):
    return []


def _rg_batch_noop(patterns, **k):
    return {p: [] for p in patterns}


# ── Group 1: filenames and dotted symbols ───────────────────────────────────

def test_bare_ts_filename_is_not_treated_as_a_dotted_symbol():
    assert verify._looks_like_bare_filename("inventoryApi.ts") is True


def test_bare_py_filename_is_not_treated_as_a_dotted_symbol():
    assert verify._looks_like_bare_filename("vouchers_api.py") is True


def test_genuine_dotted_symbol_is_not_treated_as_a_filename():
    assert verify._looks_like_bare_filename("ItemCategory.sku_prefix") is False
    assert verify._looks_like_bare_filename("Voucher.save") is False


def test_filename_mentioned_in_prose_does_not_become_a_declaration_claim(monkeypatch):
    """End-to-end: a bare filename backticked in ordinary prose must not appear
    in the SYMBOLS section as a checked dotted claim at all."""
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    monkeypatch.setattr(verify, "_rg_batch", _rg_batch_noop)
    answer = "Also see `inventoryApi.ts` for the frontend hooks."
    report = verify.verify_claims(answer)
    assert "inventoryApi.ts" not in report or "no checkable claims" in report


def test_t29_bare_filename_path_resolution_still_resolves_a_unique_candidate(
    tmp_path, monkeypatch,
):
    """T29 regression guard: the asserted-PATH resolution (a separate
    extraction pass from the SYMBOLS/idents one this phase touches) must
    still resolve a unique bare filename into asserted_paths."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    frontend = tmp_path / "Client" / "inventoryApi.ts"
    frontend.parent.mkdir(parents=True)
    frontend.write_text("export const useGetVouchersQuery = () => {};\n", encoding="utf-8")
    _mock_rg_files(monkeypatch, ["Client/inventoryApi.ts"])
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    monkeypatch.setattr(verify, "_rg_batch", _rg_batch_noop)

    answer = "Frontend hooks (from `inventoryApi.ts`): `useGetVouchersQuery`."
    report = verify.verify_claims(answer)

    assert "PATHS (" in report
    assert "Client/inventoryApi.ts" in report
    assert "EXISTS" in report


def test_ambiguous_filename_remains_unresolved_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    monkeypatch.setattr(verify, "_rg_batch", _rg_batch_noop)
    _mock_rg_files(monkeypatch, ["Client/web/inventoryApi.ts", "Client/mobile/inventoryApi.ts"])

    answer = "Frontend hooks (from `inventoryApi.ts`): `useGetVouchersQuery`."
    report = verify.verify_claims(answer)

    paths_section = report.split("PATHS (")[1] if "PATHS (" in report else ""
    assert "Client/web" not in paths_section
    assert "Client/mobile" not in paths_section


# ── Group 2: generic decorator and route fragments ──────────────────────────

def test_bare_decorator_method_reference_correlated_with_a_real_call_is_excluded():
    answer = (
        "The backend uses `router.post` for creation endpoints, e.g. "
        '`@router.post("/vouchers/grn/{po_id}")`.'
    )
    assert verify._is_generic_decorator_reference(answer, "router.post") is True


def test_bare_decorator_method_reference_with_no_matching_call_is_not_excluded():
    """No real `@router.post(` anywhere in this answer -- the generic shape
    alone is not enough; the token still reaches normal dotted-claim checking."""
    answer = "The backend uses `router.post` for creation endpoints."
    assert verify._is_generic_decorator_reference(answer, "router.post") is False


def test_generic_fragment_does_not_cause_an_unrelated_file_citation(monkeypatch):
    """End-to-end: `router.post`, correlated with a real call in the SAME
    answer, must not appear as a checked SYMBOLS entry citing some other file."""
    def fake_rg(tok, **k):
        if tok == "router.post":
            return ["API/storage-service/router/storage_api.py:32:@router.post(\"/upload\")"]
        return []
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: fake_rg(p) for p in patterns})

    answer = (
        "The backend uses `router.post` for creation endpoints, e.g. "
        '`@router.post("/vouchers/grn/{po_id}")`.'
    )
    report = verify.verify_claims(answer)

    assert "storage_api.py" not in report


def test_specific_route_claim_remains_eligible_for_verification():
    """A complete `@router.post(...)` decorator call is untouched by the T30
    exclusions -- it never reached the dotted-symbol path in the first place
    (the "@" prefix blocks _DOTTED_RE/_IDENT_RE), and is handled exclusively
    by the pre-existing ROUTES mechanism."""
    answer = 'Endpoint: `@router.post("/vouchers/grn/{po_id}", status_code=201)`.'
    report = verify.verify_claims(answer)
    assert "ROUTES (" in report
    assert "/vouchers/grn/{po_id}" in report


def test_genuine_missing_route_remains_detectable():
    answer = 'Endpoint: `@router.get("/vouchers/this-route-does-not-exist-at-all")`.'
    report = verify.verify_claims(answer)
    assert "ROUTES (" in report


def test_historical_t13b_fabricated_route_call_remains_detectable(monkeypatch):
    """T13b (2026-09-01): a backticked CALL claim with a signature
    (`createGRNFromPO(poId: string)`) that appears zero times in the repo must
    still be flagged -- this regression guard is untouched by any T30 change
    (the call-split path at `tok = span.split('(', 1)[0]...` is unmodified)."""
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    monkeypatch.setattr(verify, "_rg_batch", _rg_batch_noop)
    answer = "The frontend hook is `createGRNFromPO(poId: string)`."
    report = verify.verify_claims(answer)
    assert "NOT FOUND" in report
    assert "createGRNFromPO" in report


def test_genuine_dotted_symbol_not_shaped_like_a_decorator_is_unaffected():
    answer = "Field: `ItemCategory.sku_prefix` is on the model."
    assert verify._is_generic_decorator_reference(answer, "ItemCategory.sku_prefix") is False


# ── Group 3: markdown prose and citation/block matching ─────────────────────

def test_fence_mispairing_across_an_unclosed_fence_does_not_flag_markdown_heading(
    tmp_path, monkeypatch,
):
    """Reconstructed directly from the real T29 live-validation answer text
    (same phase's final report, section 8): Z16's repetition guard cut a
    ```typescript fence short mid-line, and the repetition
    -completion's own later fence (same language, same purpose) was
    non-greedily paired with it as if it were the closer -- swallowing a
    prose sentence and a markdown heading in between. PROJECT_ROOT must
    point at a real file for `_resolve_path` to reach the point where this
    check matters -- without it, the function bails out earlier for an
    unrelated reason and the test would pass vacuously either way."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    frontend = tmp_path / "inventoryApi.ts"
    frontend.write_text("export const useGetVouchersQuery = () => {};\n", encoding="utf-8")

    answer = (
        "### Frontend Hooks (from `inventoryApi.ts`):\n"
        "```typescript\n"
        "use\n"
        "\n"
        "I'll continue from where the previous answer left off and complete "
        "the comparison.\n"
        "\n"
        "### Frontend Hooks (from `inventoryApi.ts`):\n"
        "```typescript\n"
        "useGetVouchersQuery,\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert findings == []


def test_ordinary_prose_about_a_file_is_not_a_source_block_claim():
    answer = (
        "The frontend file `models.py` defines several API clients; this is "
        "explained in the module docstring, not shown inline here."
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert findings == []


def test_valid_fenced_quotation_with_no_markdown_heading_is_still_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    real_file = tmp_path / "models.py"
    real_file.write_text("class Voucher(Base):\n    status = Column(String)\n", encoding="utf-8")

    answer = (
        "Defined in `models.py`:\n"
        "```python\n"
        "class Voucher(Base):\n"
        "    status = Column(String)\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert findings == []  # lines genuinely present -- correctly not flagged


def test_fabricated_source_quotation_with_no_markdown_heading_is_still_flagged(
    tmp_path, monkeypatch,
):
    """Per _blocks_not_in_their_file's own documented contract, ANY single
    matching distinctive line is enough to stay silent (a partially
    reformatted quotation is not treated as fabrication) -- so this fixture
    makes BOTH distinctive lines genuinely absent from the real file, the
    shape that contract actually flags."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    real_file = tmp_path / "models.py"
    real_file.write_text("class Voucher(Base):\n    status = Column(String)\n", encoding="utf-8")

    answer = (
        "Defined in `models.py`:\n"
        "```python\n"
        "completely_fabricated_top_level_call_one()\n"
        "completely_fabricated_top_level_call_two()\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "models.py"


def test_a_block_with_a_single_hash_python_comment_is_still_checked(tmp_path, monkeypatch):
    """Regression guard: a single '#' is an ordinary Python/shell comment, not
    a markdown heading -- must NOT trigger the T30 exclusion and lose real
    detection coverage."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    real_file = tmp_path / "models.py"
    real_file.write_text("class Voucher(Base):\n    status = Column(String)\n", encoding="utf-8")

    answer = (
        "Defined in `models.py`:\n"
        "```python\n"
        "# a totally fabricated comment line about this class\n"
        "completely_fabricated_top_level_call()\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
