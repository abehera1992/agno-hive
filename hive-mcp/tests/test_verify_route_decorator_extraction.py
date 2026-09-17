"""Phase AF, defect 2 -- REST decorator claim extraction.

Phase AE found a real, live incident (T13b, three separate `verify_claims`
calls, all returned a clean bad=False verdict): an answer listed 8 backend
endpoints as backticked `@router.<verb>('/path')` decorator citations, 6 of
which do not exist anywhere in the file it claimed to be reading. Traced to
two independent, compounding gaps:

1. The SYMBOLS extraction loop tokenizes a non-declaration backtick span by
   splitting at the first "(" -- `@router.get('/vouchers/{voucher_id}/versions/')`
   becomes the bare token "@router.get", which starts with "@" and so fails
   BOTH `_IDENT_RE` and `_DOTTED_RE` (neither anchor permits a leading "@"),
   silently dropping the entire claim before any grep runs.
2. The pre-existing ROUTES mechanism (`_ROUTE_RE`) cannot catch it either --
   it only matches paths carrying one of config.ROUTE_PREFIXES (default
   "/api"), and a decorator's own literal path is written relative to
   wherever the router gets mounted ("/vouchers/...", never "/api/...").

The fix adds `_DECORATOR_ROUTE_RE`, recognizing only the documented common
verb set (get/post/put/patch/delete/options/head) and extracting nothing but
the quoted path, then feeding that path into the SAME, already-tested ROUTES
suffix-walk verification `_ROUTE_RE`-extracted paths already use -- not a new
verifier, not a general decorator parser.

Mocking note: this sandbox has no `rg` binary (see
test_verify_declaration_structure.py's own note), so `_rg` is monkeypatched
for every test that needs a genuine hit -- a fabricated route is correctly
reported absent even with no mock at all, since a real grep search or an
empty-fallback search both return nothing for text that does not exist.
"""
import re

import pytest

from tools import verify


@pytest.fixture(autouse=True)
def _reset_repeat_tracking():
    verify._checked_answer_counts = {}


def _fake_rg_over_corpus(corpus_lines):
    """A grep double that actually applies the given regex PATTERN against a
    small fake corpus, rather than pre-computing the exact escaped/substituted
    pattern string by hand -- this exercises the real _PARAM_RE-substitution +
    suffix-walk logic in verify.py unmodified, only faking the filesystem."""
    def fake_rg(pattern, fixed=False, glob_filter="", whole_word=False):
        if fixed:
            return [ln for ln in corpus_lines if pattern in ln]
        rx = re.compile(pattern)
        return [ln for ln in corpus_lines if rx.search(ln)]
    return fake_rg


# ── 1/4. Positive @router.get/post/put/patch/delete/options/head extraction --

@pytest.mark.parametrize("verb", ["get", "post", "put", "patch", "delete", "options", "head"])
def test_each_supported_verb_is_extracted_as_a_route_claim(monkeypatch, verb):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = f"- `@router.{verb}('/vouchers/{{voucher_id}}/post')`"

    report = verify.verify_claims(answer)

    assert "no checkable claims found" not in report
    assert "ROUTES (" in report
    assert "/vouchers/{voucher_id}/post" in report


def test_different_router_object_name_still_recognized(monkeypatch):
    """The router variable name varies by file (`app`, `api_router`, ...);
    only the verb set is fixed."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `@api_router.post('/vouchers/{voucher_id}/grn')`"

    report = verify.verify_claims(answer)

    assert "ROUTES (" in report
    assert "/vouchers/{voucher_id}/grn" in report


# ── 5. Fabricated decorator route -> NOT FOUND -------------------------------

def test_fabricated_decorator_route_is_not_found(monkeypatch):
    """The exact Phase AE T13b shape: a plausible-looking but entirely
    invented endpoint that does not exist anywhere in the real router file."""
    corpus = [
        'API/inventory-service/router/vouchers_api.py:212:@router.get("/vouchers")',
        'API/inventory-service/router/vouchers_api.py:220:@router.get("/vouchers/{voucher_id}")',
        'API/inventory-service/router/vouchers_api.py:589:@router.post("/vouchers/grn/{po_id}")',
    ]
    monkeypatch.setattr(verify, "_rg", _fake_rg_over_corpus(corpus))
    answer = "- `@router.get('/vouchers/{voucher_id}/versions/')`"

    report = verify.verify_claims(answer)

    assert "ROUTES (" in report
    assert "NOT FOUND" in report
    assert "/vouchers/{voucher_id}/versions/" in report
    assert "could NOT be found" in report  # counts toward the verdict


def test_the_exact_t13b_incident_six_of_eight_fabricated(monkeypatch):
    """Reproduces the real run's shape: a mix of near-miss and wholly
    fabricated routes, none of which exist in the real 9-endpoint file."""
    real_corpus = [
        'API/inventory-service/router/vouchers_api.py:212:@router.get("/vouchers")',
        'API/inventory-service/router/vouchers_api.py:220:@router.get("/vouchers/{voucher_id}")',
        'API/inventory-service/router/vouchers_api.py:240:@router.post("/vouchers")',
        'API/inventory-service/router/vouchers_api.py:260:@router.put("/vouchers/{voucher_id}/post")',
        'API/inventory-service/router/vouchers_api.py:280:@router.put("/vouchers/{voucher_id}/cancel")',
    ]
    monkeypatch.setattr(verify, "_rg", _fake_rg_over_corpus(real_corpus))
    answer = (
        "- `@router.get('/vouchers/')`\n"
        "- `@router.get('/vouchers/{voucher_id}')`\n"
        "- `@router.post('/vouchers/')`\n"
        "- `@router.post('/vouchers/{voucher_id}/post')`\n"          # wrong method
        "- `@router.delete('/vouchers/{voucher_id}')`\n"             # fabricated
        "- `@router.get('/vouchers/{voucher_id}/versions/')`\n"      # fabricated
        "- `@router.get('/vouchers/{voucher_id}/versions/{version_id}')`\n"  # fabricated
        "- `@router.post('/vouchers/{voucher_id}/versions/')`\n"     # fabricated
    )

    report = verify.verify_claims(answer)

    assert "could NOT be found" in report
    not_found = [ln for ln in report.splitlines() if "NOT FOUND" in ln]
    assert len(not_found) >= 3  # at minimum the three unambiguously invented ones


# ── 6. Real decorator route -> accepted (PLAUSIBLE) --------------------------

def test_real_decorator_route_is_accepted(monkeypatch):
    corpus = [
        'API/inventory-service/router/vouchers_api.py:589:'
        '@router.post("/vouchers/grn/{po_id}", status_code=201)',
    ]
    monkeypatch.setattr(verify, "_rg", _fake_rg_over_corpus(corpus))
    answer = "- `@router.post('/vouchers/grn/{po_id}')`"

    report = verify.verify_claims(answer)

    assert "PLAUSIBLE" in report
    assert "NOT FOUND" not in report
    assert "could NOT be found" not in report
    assert "VERDICT: every checked claim exists" in report


def test_real_route_with_different_param_syntax_still_matches(monkeypatch):
    """Path params are written differently across the codebase (`{id}` vs
    `:id` vs `${id}`) -- the existing param-wildcard substitution (unchanged
    by this phase) must still bridge them; this is regression coverage that
    the new extraction did not disturb that behavior."""
    corpus = [
        'API/inventory-service/router/vouchers_api.py:260:'
        '@router.put("/vouchers/:voucher_id/post")',
    ]
    monkeypatch.setattr(verify, "_rg", _fake_rg_over_corpus(corpus))
    answer = "- `@router.put('/vouchers/{voucher_id}/post')`"

    report = verify.verify_claims(answer)

    assert "PLAUSIBLE" in report


# ── 7. Existing symbol/declaration verification remains unchanged -----------

def test_class_declaration_claim_alongside_a_route_claim_both_checked(monkeypatch):
    corpus = [
        'API/inventory-service/router/vouchers_api.py:220:'
        '@router.get("/vouchers/{voucher_id}")',
    ]
    fake_rg = _fake_rg_over_corpus(corpus)
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", lambda patterns, **k: {p: [] for p in patterns})

    answer = (
        "- `class VoucherSeriesConfig(BaseModel)`\n"
        "- `@router.get('/vouchers/{voucher_id}')`\n"
    )
    report = verify.verify_claims(answer)

    assert "SYMBOLS (" in report
    assert "VoucherSeriesConfig" in report
    assert "ROUTES (" in report
    assert "/vouchers/{voucher_id}" in report


def test_existing_symbol_only_answers_are_completely_unaffected(monkeypatch):
    """No route-shaped text at all -- ROUTES section must not appear, and
    behavior must be byte-for-byte the pre-Phase-AF path."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The class `Voucher` represents a single voucher record."

    report = verify.verify_claims(answer)

    assert "ROUTES (" not in report
    assert "Voucher" in report


def test_declared_bases_mismatch_check_w2_still_reachable(monkeypatch):
    """A route claim in the SAME answer must not interfere with W1/W2's own
    class-declaration/base-class checking path."""
    fake_rg, fake_rg_batch = (
        lambda pattern, fixed=True, glob_filter="", whole_word=False: (
            ["API/inventory-service/models.py:150:class Voucher(Base):"]
            if pattern in ("Voucher", r"^\s*class\s+Voucher\b") else []
        ),
        lambda patterns, glob_filter="", whole_word=False, per_pattern_cap=8: {
            p: (["API/inventory-service/models.py:150:class Voucher(Base):"]
                if p == "Voucher" else []) for p in patterns
        },
    )
    monkeypatch.setattr(verify, "_rg", fake_rg)
    monkeypatch.setattr(verify, "_rg_batch", fake_rg_batch)
    monkeypatch.setattr(
        "tools.symbol_index.class_bases",
        lambda rel_path, class_name: ["Base"] if class_name == "Voucher" else None,
    )

    answer = "- `class Voucher(BaseModel)`\n- `@router.get('/vouchers')`\n"
    report = verify.verify_claims(answer)

    assert "WRONG BASE" in report


# ── 8. No false positives from ordinary @ text or unrelated syntax ----------

def test_staticmethod_decorator_is_not_a_route_claim(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "The method is decorated with `@staticmethod`."
    report = verify.verify_claims(answer)
    assert "ROUTES (" not in report


def test_pytest_marker_is_not_a_route_claim(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Tests use `@pytest.mark.asyncio` throughout."
    report = verify.verify_claims(answer)
    assert "ROUTES (" not in report


def test_email_address_is_not_a_route_claim(monkeypatch):
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "Contact support at user@example.com for help."
    report = verify.verify_claims(answer)
    assert "ROUTES (" not in report


def test_unsupported_verb_is_not_extracted(monkeypatch):
    """Flask-style `@app.route(...)` is a real, common decorator in other
    frameworks but is deliberately NOT one of the documented supported verbs
    -- this is not a general parser."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `@app.route('/vouchers')`"
    report = verify.verify_claims(answer)
    assert "ROUTES (" not in report


def test_single_segment_decorator_route_is_not_falsely_flagged(monkeypatch):
    """A one-segment path ("/vouchers") gets zero candidates in the existing
    suffix-walk (which requires >= 2 segments, unchanged by this phase) --
    silently excluded from ROUTES rather than reported as a false NOT FOUND.
    Absence of shape data is not evidence of bad shape, the same principle
    this file already applies elsewhere."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [
        'API/inventory-service/router/vouchers_api.py:212:@router.get("/vouchers")'])
    answer = "- `@router.get('/vouchers')`"
    report = verify.verify_claims(answer)
    # Either no ROUTES section at all, or it exists but never flags this
    # single-segment path as NOT FOUND.
    assert "NOT FOUND  /vouchers " not in report
    assert "could NOT be found" not in report


def test_decorator_call_without_a_literal_string_argument_is_ignored(monkeypatch):
    """@router.get(path_variable) -- no quoted literal, nothing to check."""
    monkeypatch.setattr(verify, "_rg", lambda *a, **k: [])
    answer = "- `@router.get(path_variable)`"
    report = verify.verify_claims(answer)
    assert "ROUTES (" not in report
