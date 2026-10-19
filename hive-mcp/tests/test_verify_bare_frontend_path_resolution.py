"""T29 -- verify_claims' asserted-path extraction missed bare, single-segment
backticked filenames (no directory part), so a claim about a file the answer
only ever named by its SHORT name was invisible to `_structural_verdict`'s
per-symbol file selection entirely.

Live-confirmed (T28's own verify_claims live validation run, 2026-10-19): the
answer's own earlier heading named the frontend file only as
"`inventoryApi.ts`" (no directory prefix -- it had already been read this
run and the model referred back to it by its short name), while the backend
file was always named with its full repo-relative path
(`API/inventory-service/router/vouchers_api.py`). `_ASSERTED_PATH_RE`
requires at least one "/" segment, so only the backend path ever entered
`asserted_paths` -- every frontend-hook symbol was then checked ONLY against
the one indexable file the answer "named" by that measure, the unrelated
backend router, producing a wall of false NOT IN FILE verdicts for real,
correctly-named frontend hooks.

Fix: a bare, backticked, single-segment filename with a recognized code
extension is now resolved via the EXISTING `_resolve_path` mechanism (the
same hint_paths pattern the CITATIONS section already applies to bare
file:line citations) and, when unambiguous, folded into `asserted_paths`.
An ambiguous or unresolved bare name is left out entirely -- never guessed --
so `_structural_verdict`'s existing fallback-to-None/repo-wide-grep path
remains the safety net for every case this cannot safely settle.

Mocking note: this dev machine has no `rg` on PATH reachable from a Python
subprocess (confirmed live; same fact test_verify_labeled_line_forward_path.py
and test_verify_content_location.py already document and work around) --
`_resolve_path`'s `rg --files` fallback is mocked with the SAME
`_FakeRgFiles`/`_mock_rg_files` convention those files use. `declares()`/
`field_of()` (symbol_index.py) read files directly via AST, not through rg,
but bind their OWN module-level `PROJECT_ROOT` at import time -- separate
from verify.py's -- so both must be patched (same dual-patch convention
test_verify_near_miss_basename_fallback.py already uses for `context`).
"""
import pytest

from tools import symbol_index, verify


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


_BACKEND_REL = "API/inventory-service/router/vouchers_api.py"
_FRONTEND_REL = (
    "Client/EcommClient-Web/ekamweb/src/lib/api/services/inventory/inventoryApi.ts"
)


def _make_vouchers_inventory_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(symbol_index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    monkeypatch.setattr(verify, "_rg_batch", _rg_batch_noop)

    backend = tmp_path / _BACKEND_REL
    backend.parent.mkdir(parents=True)
    backend.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n\n"
        "@router.get('/vouchers')\n"
        "def list_vouchers():\n"
        "    pass\n",
        encoding="utf-8",
    )
    frontend = tmp_path / _FRONTEND_REL
    frontend.parent.mkdir(parents=True)
    frontend.write_text(
        "export const useGetVouchersQuery = () => {};\n"
        "export const useCreateVoucherMutation = () => {};\n",
        encoding="utf-8",
    )
    return backend, frontend


# ── 1. Frontend/backend mismatch -- the real T28 incident shape ────────────

def test_frontend_hook_is_checked_against_the_frontend_file_not_the_backend_one(
    tmp_path, monkeypatch,
):
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)
    _mock_rg_files(monkeypatch, [_BACKEND_REL, _FRONTEND_REL])

    answer = (
        f"Backend endpoints are defined in `{_BACKEND_REL}`.\n\n"
        "### Frontend Hooks (from `inventoryApi.ts`)\n"
        "- `useGetVouchersQuery`\n"
    )
    report = verify.verify_claims(answer)

    assert "DECLARED   useGetVouchersQuery" in report
    tail = report.split("DECLARED   useGetVouchersQuery")[1][:160]
    assert "inventoryApi.ts" in tail
    assert "NOT IN FILE useGetVouchersQuery" not in report


# ── 2. Backend claim remains correct -- no regression on the existing path ─

def test_backend_endpoint_claim_still_verified_against_the_backend_file(
    tmp_path, monkeypatch,
):
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)

    answer = f"Backend endpoints are defined in `{_BACKEND_REL}`.\n- `list_vouchers`\n"
    report = verify.verify_claims(answer)

    assert "DECLARED   list_vouchers" in report
    assert _BACKEND_REL.rsplit("/", 1)[-1] in \
        report.split("DECLARED   list_vouchers")[1][:160]


# ── 3. Cross-layer claim -- both files named in full, both symbols checked ─

def test_cross_layer_claim_checks_each_symbol_against_its_own_real_file(
    tmp_path, monkeypatch,
):
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)

    answer = (
        f"Backend: `{_BACKEND_REL}` defines `list_vouchers`. "
        f"Frontend: `{_FRONTEND_REL}` defines `useGetVouchersQuery`."
    )
    report = verify.verify_claims(answer)

    assert "DECLARED   list_vouchers" in report
    assert "DECLARED   useGetVouchersQuery" in report
    assert "NOT IN FILE" not in report


# ── 4. Missing provenance -- ambiguous/unresolved bare name is never guessed ─

def test_ambiguous_bare_basename_is_not_silently_resolved(tmp_path, monkeypatch):
    """Two different files share the basename `inventoryApi.ts` -- the fix
    must not guess which one. The pre-existing behavior (checked only against
    whatever full path WAS named) is preserved rather than a fabricated
    resolution."""
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)
    other_frontend_rel = "Client/EcommClient-Mobile/src/api/inventoryApi.ts"
    _mock_rg_files(monkeypatch, [_BACKEND_REL, _FRONTEND_REL, other_frontend_rel])

    answer = (
        f"Backend: `{_BACKEND_REL}`.\n"
        "Frontend hooks (from `inventoryApi.ts`): `useGetVouchersQuery`.\n"
    )
    report = verify.verify_claims(answer)

    paths_section = report.split("PATHS (")[1] if "PATHS (" in report else ""
    assert "EcommClient-Web" not in paths_section
    assert "EcommClient-Mobile" not in paths_section


def test_bare_name_with_no_real_file_anywhere_is_not_resolved(tmp_path, monkeypatch):
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)
    _mock_rg_files(monkeypatch, [_BACKEND_REL])  # no match for the bare name below

    answer = (
        f"Backend: `{_BACKEND_REL}`.\n"
        "See also `doesNotExistAnywhere.ts` for frontend details.\n"
    )
    report = verify.verify_claims(answer)

    paths_section = report.split("PATHS (")[1] if "PATHS (" in report else ""
    assert "doesNotExistAnywhere" not in paths_section


# ── 5. Valid explicit provenance -- a full path is never disturbed ─────────

def test_explicit_full_frontend_path_is_unaffected_by_the_bare_name_fallback(
    tmp_path, monkeypatch,
):
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)

    answer = f"Frontend: `{_FRONTEND_REL}` defines `useGetVouchersQuery`."
    report = verify.verify_claims(answer)

    assert "DECLARED   useGetVouchersQuery" in report
    paths_block = report.split("PATHS (")[1].split("\n\n")[0]
    # No duplicate PATHS entry for the same already-fully-named file.
    assert paths_block.count("inventoryApi.ts") == 1


# ── 6. Path normalization -- a backslash-written hint still resolves ───────

def test_backslash_hint_path_resolves_the_same_bare_name(tmp_path, monkeypatch):
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)
    backend_backslash = _BACKEND_REL.replace("/", "\\")
    _mock_rg_files(monkeypatch, [_BACKEND_REL, _FRONTEND_REL])

    answer = (
        f"Backend: `{backend_backslash}`.\n"
        "Frontend hooks (from `inventoryApi.ts`): `useGetVouchersQuery`.\n"
    )
    report = verify.verify_claims(answer)

    assert "DECLARED   useGetVouchersQuery" in report


# ── 8. No regression in existing verification semantics ────────────────────

def test_fabricated_frontend_symbol_is_still_flagged_not_found(tmp_path, monkeypatch):
    """The fix must not weaken verification -- a genuinely fabricated hook
    name, even once the frontend file is correctly resolved, still fails.

    Two indexable files are now asserted (backend + resolved frontend), so
    _structural_verdict's own strict "exactly one named file" NOT IN FILE
    branch correctly declines to fire (pre-existing, unchanged behavior --
    the symbol might legitimately live in a third, unindexed location) and
    defers to the plain repo-wide grep, which reports the generic NOT FOUND
    shape. Either shape is a real fabrication flag; this test's point is
    that *some* correct flag still fires, not which exact wording."""
    _make_vouchers_inventory_repo(tmp_path, monkeypatch)
    _mock_rg_files(monkeypatch, [_BACKEND_REL, _FRONTEND_REL])

    answer = (
        f"Backend: `{_BACKEND_REL}`.\n"
        "Frontend hooks (from `inventoryApi.ts`): `useDeleteEverythingMutation`.\n"
    )
    report = verify.verify_claims(answer)

    assert "NOT FOUND  useDeleteEverythingMutation" in report
    assert "DECLARED   useDeleteEverythingMutation" not in report
    assert "could NOT be found" in report
