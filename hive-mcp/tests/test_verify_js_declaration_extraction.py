"""Phase AH, fix 2 -- JS/TS declaration-shaped backtick claims must be tokenized
by their declared NAME, the same way W1 already fixed Python's `class`/`def`.

Live incidents (Phase AH, T13a and T13b, fresh-session reproductions): a member
report and a coordinator answer each described the vouchers module's frontend
hooks by inventing a wrapper-function shape that does not exist anywhere in this
codebase's actual RTK Query convention (`export const { useX, useY } = api;`
destructured re-export, never a hand-written wrapper):

    T13a: `export const useGetVouchersQuery = (params) => useQuery({ ... })`
    T13b: `async function getVouchers(...)`

Root cause, identical in shape to the Phase V class/def gap W1 fixed: neither
span starts with "class" or "def", so `_DECL_RE` never matches. Both fall to the
old split-at-"(" fallback, producing "export const useGetVouchersQuery =" and
"async function getVouchers" -- both contain spaces, both fail _IDENT_RE and
_DOTTED_RE, both silently dropped. verify_claims never checked either fabrication.

Fix: `_JS_FUNCTION_DECL_RE` / `_JS_CONST_FN_DECL_RE`, tried as a fallback right
after `_DECL_RE` in the same extraction loop, tokenizing by declared NAME exactly
like the Python case. Deliberately narrow -- only the two observed shapes, and the
arrow-const form requires `(` or `async (` immediately after "=" so an ordinary
value/call assignment (`export const businessApi = createApi({`, `const x = 5`)
is left completely alone, unaffected, exactly as before this phase.
"""
import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._checked_answer_counts = {}


def _fake_rg(real_hits: dict):
    def fake(pattern, fixed=True, glob_filter="", whole_word=False):
        return real_hits.get(pattern, [])
    return fake


# ── T13a shape: export const NAME = (...) => ... ----------------------------

def test_arrow_const_declaration_is_extracted_and_checked(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = ("- `export const useMadeUpHookQuery = (params) => "
              "useQuery({ ... })`")
    report = verify.verify_claims(answer)
    assert "no checkable claims found" not in report
    assert "useMadeUpHookQuery" in report
    assert "NOT FOUND" in report


def test_arrow_const_real_name_resolves_found(monkeypatch):
    hits = {"useGetVouchersQuery": ["Client/.../inventoryApi.ts:941:  useGetVouchersQuery,"]}
    monkeypatch.setattr(verify, "_rg", _fake_rg(hits))
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: hits.get(p, []) for p in patterns})
    answer = ("- `export const useGetVouchersQuery = (params) => "
              "useQuery({ ... })`")
    report = verify.verify_claims(answer)
    assert "FOUND      useGetVouchersQuery" in report
    assert "could NOT be found" not in report


def test_arrow_const_with_async_keyword_is_extracted(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `export const useMadeUpMutation = async (id) => fetch(...)`"
    report = verify.verify_claims(answer)
    assert "useMadeUpMutation" in report
    assert "NOT FOUND" in report


# ── T13b shape: (async) function NAME(...) -----------------------------------

def test_async_function_declaration_is_extracted_and_checked(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `async function madeUpFunction(...)`"
    report = verify.verify_claims(answer)
    assert "no checkable claims found" not in report
    assert "madeUpFunction" in report
    assert "NOT FOUND" in report


def test_async_function_real_name_resolves_found(monkeypatch):
    hits = {"getVouchers": ["Client/.../inventoryApi.ts:581:    getVouchers: builder.query<"]}
    monkeypatch.setattr(verify, "_rg", _fake_rg(hits))
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: hits.get(p, []) for p in patterns})
    answer = "- `async function getVouchers(...)`"
    report = verify.verify_claims(answer)
    assert "FOUND      getVouchers" in report
    assert "could NOT be found" not in report


def test_plain_function_no_async_no_export_is_extracted(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `function madeUpHelper(x)`"
    report = verify.verify_claims(answer)
    assert "madeUpHelper" in report
    assert "NOT FOUND" in report


def test_export_default_function_is_extracted(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `export default function MadeUpPage()`"
    report = verify.verify_claims(answer)
    assert "MadeUpPage" in report
    assert "NOT FOUND" in report


# ── Multiple fabrications in one answer, the exact T13b incident shape -------

def test_multiple_async_function_fabrications_all_flagged(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = (
        "- `async function getVouchers(...)`\n"
        "- `async function getVoucherById(...)`\n"
        "- `async function createVoucher(...)`\n"
        "- `async function postVoucher(...)`\n"
        "- `async function cancelVoucher(...)`\n"
    )
    report = verify.verify_claims(answer)
    not_found = [ln for ln in report.splitlines() if ln.strip().startswith("NOT FOUND")]
    assert len(not_found) == 5


# ── No false positives / no regression on unrelated shapes -------------------

def test_ordinary_const_value_assignment_is_unaffected(monkeypatch):
    """`const x = 5` has no "(" or "async" after "=" -- must not match either new
    regex, and must fall through to the OLD (unchanged) behavior: split-at-"("
    finds no "(" at all, the whole span has spaces, dropped exactly as before."""
    calls = {"n": 0}

    def counting_rg(*a, **k):
        calls["n"] += 1
        return []
    monkeypatch.setattr(verify, "_rg", counting_rg)
    answer = "- `const x = 5`"
    report = verify.verify_claims(answer)
    assert "no checkable claims found" in report
    assert calls["n"] == 0


def test_export_const_object_call_assignment_is_unaffected(monkeypatch):
    """`export const businessApi = createApi({` -- after "=" comes an
    IDENTIFIER ("createApi"), not "(" or "async" -- must NOT match
    _JS_CONST_FN_DECL_RE, preserving this exact pre-existing (unaffected)
    behavior: the old split-at-"(" fallback produces a space-containing token
    and drops it, same as before this phase."""
    calls = {"n": 0}

    def counting_rg(*a, **k):
        calls["n"] += 1
        return []
    monkeypatch.setattr(verify, "_rg", counting_rg)
    answer = "- `export const businessApi = createApi({`"
    report = verify.verify_claims(answer)
    assert "no checkable claims found" in report
    assert calls["n"] == 0


def test_bare_call_shape_citation_still_works_unchanged(monkeypatch):
    """The pre-existing, already-fixed (2026-09-01) T13b shape --
    `createGRNFromPO(poId: string)` -- a bare call with no declaration keyword,
    must remain completely unaffected by the new fallback (it is checked by the
    _DECL_RE-miss -> split-at-"(" path exactly as it always has been)."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `createGRNFromPO(poId: string)`"
    report = verify.verify_claims(answer)
    assert "createGRNFromPO" in report
    assert "NOT FOUND" in report


# ── Existing Python class/def path completely unaffected --------------------

def test_python_class_declaration_still_works_unchanged(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})
    answer = "- `class VoucherSeriesConfig(BaseModel)`"
    report = verify.verify_claims(answer)
    assert "NOT FOUND" in report
    assert "VoucherSeriesConfig" in report


def test_python_def_declaration_still_works_unchanged(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `def get_voucher(...)`"
    report = verify.verify_claims(answer)
    assert "get_voucher" in report
    assert "NOT FOUND" in report


def test_w2_base_class_check_still_reachable_for_python(monkeypatch):
    """The kind/args groupdict guard added for the JS fallback must not disturb
    W1/W2's own base-class mismatch detection for the Python path."""
    fake_hits = {
        "Voucher": ["API/inventory-service/models.py:150:class Voucher(Base):"],
        r"^\s*class\s+Voucher\b": ["API/inventory-service/models.py:150:class Voucher(Base):"],
    }
    monkeypatch.setattr(verify, "_rg", lambda pattern, **k: fake_hits.get(pattern, []))
    monkeypatch.setattr(verify, "_rg_batch",
                         lambda patterns, **k: {p: fake_hits.get(p, []) for p in patterns})
    monkeypatch.setattr("tools.symbol_index.class_bases",
                         lambda rel_path, class_name: ["Base"] if class_name == "Voucher" else None)
    answer = "- `class Voucher(BaseModel)`"
    report = verify.verify_claims(answer)
    assert "WRONG BASE" in report


def test_declaration_inside_proposed_new_code_framing_still_excluded(monkeypatch):
    """New/add/propose framing must still route a JS declaration to PROPOSED,
    not the fabrication verdict -- same discipline the Python path already has."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = ("We should add a new hook:\n"
              "`export const useNewThingQuery = (params) => useQuery({...})` "
              "to fetch new things.")
    report = verify.verify_claims(answer)
    assert "PROPOSED" in report
    proposed_section = report.split("PROPOSED", 1)[1]
    assert "useNewThingQuery" in proposed_section
    assert "NOT FOUND" not in report
