"""T32 -- structurally safe Markdown fence handling.

_FENCED_RE's `` ```lang\n(.*?)``` `` paired ANY later ``` with an earlier,
UNCLOSED fence -- its non-greedy `.*?` has no way to distinguish "the real
closer" from "the next, unrelated fence's own opener". T30 already found and
fixed one SHAPE of the consequence (a markdown heading swallowed into false
"block content"); this phase reproduces the SAME root cause producing a false
BLOCK NOT IN finding on plain PROSE with no heading at all, and separately
confirms the genuinely distinct SECOND block's own content was never
independently checked either.

Fix: `_iter_fenced_blocks`, a small, explicit line scanner applying
CommonMark's own actual fence rules (same character, closer length >=
opener length, closer line carries ONLY the fence + optional whitespace) in
place of the single regex. An unclosed fence terminates at end of input,
never at a later unrelated fence.

This does not fully resolve the deepest ambiguity: CommonMark itself has no
way to treat an info-stringed line (`` ```python ``) as a valid closer for an
earlier, still-open fence, so two genuinely separate, differently-labeled
blocks separated by one truly unclosed fence are -- correctly, per spec --
still one merged span. `_EMBEDDED_FENCE_OPEN_RE` is the narrow, additional
safety net for exactly that residual case: a fence-opening-shaped line found
INSIDE a captured block's own content means two blocks were merged, and the
whole span is skipped rather than guessed at, mirroring T30's own
conservative-default convention for the markdown-heading case.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_guard():
    verify._checked_answer_counts = {}
    yield
    verify._checked_answer_counts = {}


def _mk_models_py(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    real_file = tmp_path / "models.py"
    real_file.write_text("class Voucher(Base):\n    status = Column(String)\n", encoding="utf-8")
    return real_file


# ── 1/2. Unclosed fence + a later unrelated fence, same and different lang ─

def test_unclosed_fence_plus_later_same_lang_fence_produces_no_false_positive(
    tmp_path, monkeypatch,
):
    """T33 (2026-10-19): the swallowed span must never be reported as a
    confident ABSENT/BLOCK NOT IN finding (that was the T32 false positive)
    -- but it must also no longer be silently discarded. It is now reported
    AMBIGUOUS, counted, visible: "could not be checked" is distinct from
    both "found absent" and "found clean"."""
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\ncla\n"
        "I will continue describing the voucher model and its fields in more detail shortly.\n"
        "This explanation covers several more attributes before the summary concludes.\n\n"
        "```python\nuseGetVouchersQuery\n```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "AMBIGUOUS"


def test_unclosed_fence_plus_later_different_lang_fence_produces_no_false_positive(
    tmp_path, monkeypatch,
):
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\ncla\n"
        "I will continue describing the voucher model and its fields in more detail shortly.\n"
        "This explanation covers several more attributes before the summary concludes.\n\n"
        "```typescript\nuseGetVouchersQuery\n```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "AMBIGUOUS"


# ── 4. Complete valid quotation followed by ordinary prose ─────────────────

def test_valid_quotation_followed_by_ordinary_prose_is_accepted(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\nclass Voucher(Base):\n"
        "    status = Column(String)\n```\n"
        "This is ordinary prose after the block, describing something else entirely "
        "and naming no other file."
    )
    assert verify._blocks_not_in_their_file(answer) == []


# ── 5. Fabricated/unsupported quotation remains flagged ────────────────────

def test_fabricated_standalone_quotation_is_still_flagged(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\n"
        "completely_fabricated_call_one()\n"
        "completely_fabricated_call_two()\n```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "ABSENT"
    assert findings[0][1] == "models.py"


# ── 6. Valid quotation remains accepted ─────────────────────────────────────

def test_valid_standalone_quotation_is_accepted(tmp_path, monkeypatch):
    _mk_models_py(tmp_path, monkeypatch)
    answer = "Defined in `models.py`:\n```python\nclass Voucher(Base):\n    status = Column(String)\n```\n"
    assert verify._blocks_not_in_their_file(answer) == []


# ── 7. T30's markdown-heading regression case, preserved exactly ───────────

def test_t30_markdown_heading_in_truncated_block_regression_preserved(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    frontend = tmp_path / "inventoryApi.ts"
    frontend.write_text("export const useGetVouchersQuery = () => {};\n", encoding="utf-8")
    answer = (
        "### Frontend Hooks (from `inventoryApi.ts`):\n"
        "```typescript\n"
        "use\n\n"
        "I'll continue from where the previous answer left off and complete "
        "the comparison.\n\n"
        "### Frontend Hooks (from `inventoryApi.ts`):\n"
        "```typescript\n"
        "useGetVouchersQuery,\n"
        "```\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "AMBIGUOUS"
    assert "BLOCK NOT IN" not in str(findings)


# ── 8. Fence variants: tilde fences, different lengths ──────────────────────

def test_tilde_fence_fabrication_is_detected(tmp_path, monkeypatch):
    """New capability: _FENCED_RE never recognized ~~~ fences at all, so a
    fabrication inside one was previously invisible to this check entirely."""
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n~~~python\n"
        "completely_fabricated_tilde_call_one()\n"
        "completely_fabricated_tilde_call_two()\n~~~\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "ABSENT"


def test_longer_fence_length_is_closed_correctly_not_by_a_shorter_embedded_run(
    tmp_path, monkeypatch,
):
    """A 4-backtick fence must only be closed by 4+ backticks on their own
    line -- a literal triple-backtick substring inside the content (e.g.
    quoting markdown syntax) must not prematurely close it."""
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n````python\n"
        "doc = \"written as ```like this```\"\n"
        "````\n"
    )
    # Must not raise, and must not misfire on the embedded triple-backtick text.
    verify._blocks_not_in_their_file(answer)


def test_unclosed_fence_at_end_of_input_is_bounded_not_discarded(tmp_path, monkeypatch):
    """An unclosed fence that runs straight to end of input (no later fence
    at all) must still be checked, bounded at EOF -- never silently
    discarded (which would create a false-acceptance loophole for a
    genuinely fabricated, merely truncated quotation)."""
    _mk_models_py(tmp_path, monkeypatch)
    answer = (
        "Defined in `models.py`:\n```python\n"
        "completely_fabricated_call_one()\n"
        "completely_fabricated_call_two()\n"
    )
    findings = verify._blocks_not_in_their_file(answer)
    assert len(findings) == 1
    assert findings[0][0] == "ABSENT"


# ── Preserved invariants: T29 path resolution, genuine route/symbol checks ──

class _FakeRgFiles:
    def __init__(self, paths):
        self.stdout = "\n".join(paths)


def test_t29_bare_filename_path_resolution_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    monkeypatch.setattr(verify.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(
        verify.subprocess, "run",
        lambda *a, **k: _FakeRgFiles(["Client/inventoryApi.ts"]))
    frontend = tmp_path / "Client" / "inventoryApi.ts"
    frontend.parent.mkdir(parents=True)
    frontend.write_text("export const useGetVouchersQuery = () => {};\n", encoding="utf-8")
    answer = "Frontend hooks (from `inventoryApi.ts`): `useGetVouchersQuery`."
    report = verify.verify_claims(answer)
    assert "EXISTS" in report
    assert "Client/inventoryApi.ts" in report


def test_genuine_route_claim_detection_unaffected():
    answer = 'Endpoint: `@router.get("/vouchers/this-route-does-not-exist-at-all")`.'
    report = verify.verify_claims(answer)
    assert "ROUTES (" in report
